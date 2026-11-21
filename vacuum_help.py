"""Help the robot! Hero jobs that open by themselves when a robot vacuum needs a kid.

  * Rescue: the robot is stuck (tangled brush, trapped, jammed wheels, stranded...). The job is to free it and send it
    home. A main-brush jam also asks the kid to tidy part of that room: the rescue pays its own stars plus partial stars
    for that tidying, which come off the room's own job, and the room is marked untidy at once. Other rescues do not
    claim or offer room-cleaning credit.
  * Water: the dock's clean water tank is empty or its dirty water tank is full. The job: change the water.

Dirty sensors are a quick kid job. Worn parts, filters, the dust bag and other dock upkeep stay grown-up maintenance.

Each job is a built-in on-demand chore per robot ("help_rescue_<id>", "help_water_<id>"). It opens with a one-time
price, so it arrives as BIG NEWS (flashing, with a fanfare) on the kids' screens, and it goes through inspection like
any job. A claimed rescue completes and pays automatically when that robot is subsequently seen docked; a claimed
water job does the same when every configured tank signal is healthy. Jobs never open during bedtime
(the room is still marked untidy at once). If the robot gets going again without a kid (a grown-up fixed it) and no
kid has started or reported the job within CLOSE_AFTER_S, the job closes again; the room stays untidy.

house.json:
  "vacuum_help": {
    "points": {"rescue": 3, "water": 2},          # main-brush rescue: plus any inspected room-tidying credit
    "robots": [{"id": "down", "name": "Downstairs Robot", "floor": "Downstairs",
                "vacuum": "vacuum.downstairs_robot",                                  # proves recovery/docking
                "error": "sensor.downstairs_robot_vacuum_error",
                "dock_error": "sensor.downstairs_robot_dock_error",                      # optional
                "water": ["binary_sensor.downstairs_robot_dock_clean_water_box"],        # optional, on = needs water
                "room": ["sensor.downstairs_robot_room_live", "sensor.downstairs_robot_current_room"],  # first known wins
                "rooms": {"Kitchen": ["kitchen", "breakfast"], "Hall": ["south_hall"]}}]  # robot room -> tidy_spaces ids
  }
"""
import json
import time
from pathlib import Path

# The robot needs a hand where it stands.
STUCK = {
    "main_brush_jammed": "its main brush is tangled",
    "side_brush_jammed": "its side brush is tangled",
    "wheels_jammed": "its wheels are jammed",
    "robot_trapped": "it is trapped",
    "bumper_stuck": "its bumper is stuck",
    "vertical_bumper_pressed": "its top bumper got pushed (often it has already backed out)",
    "wheels_suspended": "its wheels are off the floor",
    "robot_tilted": "it is tipped up",
    "vibrarise_jammed": "its mop is caught",
    "lidar_blocked": "something is blocking the tower on top",
    "nogo_zone_detected": "it wandered into a no-go zone",
    "invisible_wall_detected": "it wandered past an invisible wall",
    "robot_on_carpet": "it is stuck on a rug",
    "cannot_cross_carpet": "it can't cross a rug",
    "low_battery": "its battery ran out",
    "return_to_dock_fail": "it can't find its way home",
}
# Room-tidying is part of the mission only when the main brush caught room clutter. A bumper press, water job, wheel
# problem, or other rescue earns only its rescue points and must not imply that the kid cleaned the room.
TIDY_CREDIT = {"main_brush_jammed"}
# How a kid gets it going again, when it's more than "pull it free, press Home".
FIX = {
    "vertical_bumper_pressed": ["If it is still under something, pull it out (usually it isn't anymore).",
                                "Gently jiggle the laser tower on top of the robot.",
                                "Press the center button on top so it goes again."],
}
# It can't drive home by itself: carry it.
CARRY = {"low_battery", "return_to_dock_fail"}
WATER_ERRORS = {"clear_water_box_hoare": "clean", "clear_water_box_exception": "clean", "dirty_water_box_hoare": "dirty"}
WATER_DOCK = {"water_empty": "clean", "waste_water_tank_full": "dirty"}

HOLD_S = 30           # a need must last this long (blips are ignored)
CLOSE_AFTER_S = 900   # the robot is fine again and nobody took the job: close it
TICK_S = 10
OFF = (None, "", "none", "ok", "unknown", "unavailable")


def rescue_id(robot):
    return "help_rescue_" + robot["id"]


def water_id(robot):
    return "help_water_" + robot["id"]


def chores(cfg):
    """The built-in chores, two per robot. Hidden until a robot needs help (on_demand)."""
    out = []
    for r in (cfg or {}).get("robots") or []:
        common = {"points": None, "cooldown_h": 0, "on_demand": True, "household": r.get("household"),
                  "floor": r.get("floor", "Other"), "help_robot": r["id"], "order": -50}
        out.append(dict(common, id=rescue_id(r), name="Rescue " + r["name"] + "!", icon="1f9b8", help="rescue",
                        desc="A robot hero mission: free " + r["name"] + " and press the Home button on top so it can go "
                             "home. If its main brush is tangled, also tidy at least part of that room for extra stars "
                             "for the part you cleaned."))
        if r.get("sensors_left"):   # trusted: no inspection; the kid's "yes, I did it" resets the robot's reminder
            out.append(dict(common, id=sensors_id(r), name="Clean " + r["name"] + "'s sensors", icon="1f9fd", help="sensors",
                            quick=True, trust=True, photo=r.get("sensors_photo"),
                            confirm="Did you wipe every spot shown in blue?",
                            desc="Wipe every spot shown in blue in the picture with a soft dry cloth: the ones underneath "
                                 "(tip " + r["name"] + " up gently) and the ones on its front. Then put it back on its dock."))
        out.append(dict(common, id=water_id(r), name="Fresh water for " + r["name"], icon="1f4a7", help="water",
                        desc=r["name"] + " is thirsty! Change its water at the dock: empty the dirty tank, fill the "
                             "clean one."))
    return [{k: v for k, v in c.items() if v is not None or k == "points"} for c in out]


def sensors_id(r):
    return "help_sensors_" + r["id"]


def need(robot, read):
    """read(entity) -> HA state dict. What this robot needs now:
    {"kind": "rescue", "error", "since"} or {"kind": "water", "tanks": [...], "since"} or None."""
    err = read(robot["error"]) if robot.get("error") else {}
    code = err.get("state")
    if code in STUCK:
        return {"kind": "rescue", "error": code, "since": epoch(err.get("last_changed"))}
    tanks, since = set(), []
    if code in WATER_ERRORS:
        tanks.add(WATER_ERRORS[code])
        since.append(epoch(err.get("last_changed")))
    if robot.get("dock_error"):
        d = read(robot["dock_error"])
        if d.get("state") in WATER_DOCK:
            tanks.add(WATER_DOCK[d["state"]])
            since.append(epoch(d.get("last_changed")))
    for ent in robot.get("water") or []:
        w = read(ent)
        if w.get("state") == "on":
            tanks.add("dirty" if "dirty" in ent else "clean")
            since.append(epoch(w.get("last_changed")))
    if tanks:
        return {"kind": "water", "tanks": sorted(tanks), "since": min(since)}
    return None


def robot_room(robot, read):
    """The robot's room name: the first sensor that really knows it now. A room only held over a blank reading
    (the locator's held_since) is a guess, and a stuck robot is often somewhere else: never blame a room on a guess."""
    for ent in robot.get("room") or []:
        st = read(ent)
        s, a = st.get("state"), st.get("attributes") or {}
        if s in OFF or a.get("held_since"):
            continue
        return s
    return None


def spaces_of(robot, room, tidy_spaces):
    ids = (robot.get("rooms") or {}).get(room) or []
    return [s for s in tidy_spaces if s["id"] in ids]


def words(spaces, room):
    names = list(dict.fromkeys(s["name"] for s in spaces))
    return " and ".join(names) if names else (room or "")


def rescue_note(robot, n, spaces, room, base=3):
    where = words(spaces, room)
    step2 = "Carry it gently back onto its dock." if n["error"] in CARRY else \
        "Press the Home button on top so it can go home."
    steps = list(FIX[n["error"]]) if n["error"] in FIX else \
        ([] if n["error"] in CARRY else ["Free it: pull out whatever it is caught on."]) + [step2]
    tidy = bool(spaces) and n["error"] in TIDY_CREDIT
    if tidy:
        steps.append("Then tidy at least part of " + where + " so it never gets stuck there again!")
    pay = ("%d⭐ for the rescue, plus stars for the part of %s you tidy. The rescue comes with tidying: do at least "
           "some of it!" % (base, where)) if tidy else "%d⭐ for the rescue." % base
    return (robot["name"] + " needs a hero! " + ("It is stuck in " + where + ": " if where else "It is stuck: ") +
            STUCK[n["error"]] + ".\n" + "\n".join(steps) + "\n" + pay)


def water_note(robot, n):
    what = {"clean": "fill its clean water tank", "dirty": "empty its dirty water tank"}
    return robot["name"] + " is thirsty! At the dock: " + " and ".join(what[t] for t in reversed(n["tanks"])) + \
        ", just like you were shown."


def water_resolved_at(robot, read):
    """Timestamp when every configured water signal became positively healthy; unknown is never proof."""
    rows = []
    if robot.get("dock_error"):
        dock = read(robot["dock_error"])
        if dock.get("state") in (None, "unknown", "unavailable") or dock.get("state") in WATER_DOCK:
            return 0
        rows.append(dock)
    for ent in robot.get("water") or []:
        tank = read(ent)
        if tank.get("state") != "off":
            return 0
        rows.append(tank)
    if not rows:
        return 0
    changed = [epoch_or_zero(row.get("last_changed")) for row in rows]
    return max(changed) if all(changed) else 0


def report_verification(cfg, cid, started, read):
    """Return physical proof for a kid's Done tap, or a useful rejection.

    Rescue and water jobs are not matters of judgment: a rescue is complete only
    after that robot has reached its dock since the job began, and a water job is
    complete only after every configured tank signal has become healthy since it
    began.  Unknown telemetry fails closed so it can never create an inspection
    item for a grown-up to sort out.
    """
    robot = next((r for r in cfg.get("robots") or [] if cid in (rescue_id(r), water_id(r))), None)
    if not robot:
        return None
    if cid == rescue_id(robot):
        vac = read(robot.get("vacuum")) if robot.get("vacuum") else {}
        at = epoch_or_zero(vac.get("last_changed"))
        if vac.get("state") == "docked" and at >= float(started or 0):
            return {"at": at, "reason": robot["name"] + " reached its dock"}
        return {"error": robot["name"] + " is not back on its dock yet. Press Home and wait until it reaches the dock; this job is still yours."}
    at = water_resolved_at(robot, read)
    if at and at >= float(started or 0):
        return {"at": at, "reason": robot["name"] + " water tanks are ready"}
    return {"error": robot["name"] + " still says its water needs attention. Check both tanks; this job is still yours."}


def untidy_events(spaces, board, space_rows, robot, room_name):
    """Mark the rooms where the robot got stuck untidy: reopen their jobs that are presumed clean; a room with no
    jobs gets a space_tidy false."""
    evs, seen = [], set()
    note = robot["name"] + " got stuck here"
    for s in spaces:
        jobs = s.get("chores") or []
        for j in jobs:
            if j not in seen and (board.get(j) or {}).get("clean_until"):
                evs.append({"type": "reopen", "chore": j, "by": "vacuum", "note": note})
            seen.add(j)
        if not jobs and (space_rows.get(s["id"]) or {}).get("state") == "tidy":
            evs.append({"type": "space_tidy", "space": s["id"], "tidy": False, "by": "vacuum"})
    return evs


def partial_of(spaces):
    """The stuck room's own jobs: stars a grown-up pays for tidying part of the room on a rescue come off their price
    (like any partly-done job), so nobody is paid twice for the same part."""
    return list(dict.fromkeys(j for s in spaces for j in s.get("chores") or []))


def busy(cid, st):
    bd = st["board"].get(cid) or {}
    return bool(bd.get("pending_by")) or any(a.get("chore") == cid for a in list(st["active"].values()) + list(st["paused"].values()))


def epoch(s):
    if not s:
        return time.time()
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def epoch_or_zero(s):
    """Strict HA timestamp parser for evidence: missing/bad data is never proof."""
    if not s:
        return 0
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0


class Helper:
    def __init__(self, g, cfg):
        self.g, self.cfg = g, cfg
        self.path = Path(g["DATA"]) / "vacuum_help.json"
        try:
            self.D = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.D = {}
        self.tok = None

    def read(self, ent):
        return self.g["ha_get"](ent, self.tok) or {}

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.D))
        tmp.replace(self.path)

    def tick(self, now=None):
        now = now or time.time()
        self.tok = self.g["ha_token"]()
        g, changed = self.g, False
        st = g["derive"]()
        spaces_all = g["HOUSE"].get("tidy_spaces") or []
        pts = self.cfg.get("points") or {}
        for robot in self.cfg.get("robots") or []:
            R = self.D.setdefault(robot["id"], {})
            if robot.get("vacuum"):
                vac = self.read(robot["vacuum"])
                if vac.get("state") == "docked":
                    docked_at = epoch_or_zero(vac.get("last_changed"))
                    refs = g["auto_verify_chore"](rescue_id(robot), docked_at, "vacuum",
                                                   robot["name"] + " reached its dock") if docked_at else []
                    if refs:
                        g["ilog"]({"src": "vac_help", "robot": robot["id"], "kind": "rescue",
                                   "action": "auto_verify", "refs": refs})
                        changed, st = True, g["derive"]()
            water_at = water_resolved_at(robot, self.read)
            if water_at:
                refs = g["auto_verify_chore"](water_id(robot), water_at, "vacuum",
                                               robot["name"] + " water tanks are ready")
                if refs:
                    g["ilog"]({"src": "vac_help", "robot": robot["id"], "kind": "water",
                               "action": "auto_verify", "refs": refs})
                    changed, st = True, g["derive"]()
            n = need(robot, self.read)
            for kind, cid in (("rescue", rescue_id(robot)), ("water", water_id(robot))):
                K = R.setdefault(kind, {})
                cur = n if n and n["kind"] == kind else None
                # A robot that reports an error and then loses power/network has not demonstrated recovery. Keep the
                # incident alive through bedtime and a service restart until its error clears or it is seen docked.
                # Do not reuse the last room: the locator commonly goes stale just before a stranded robot disappears.
                if kind == "rescue" and not cur and K.get("episode") and K.get("error") in STUCK:
                    err_state = self.read(robot.get("error")).get("state") if robot.get("error") else None
                    vac_state = self.read(robot.get("vacuum")).get("state") if robot.get("vacuum") else None
                    if err_state in ("unknown", "unavailable") and vac_state in (None, "unknown", "unavailable"):
                        cur = {"kind": "rescue", "error": K["error"], "since": K["episode"],
                               "telemetry_lost": True}
                if cur:
                    K["clear_since"] = 0
                    ep = round(cur["since"])
                    if now - cur["since"] < HOLD_S:
                        continue
                    if K.get("episode") != ep:        # a new time it needs help
                        K.update(episode=ep, error=cur.get("error"), untidy=False, opened=False)
                        room = robot_room(robot, self.read) if kind == "rescue" else None
                        sp = spaces_of(robot, room, spaces_all) if room else []
                        K.update(room=room, spaces=[s["id"] for s in sp])
                        g["ilog"]({"src": "vac_help", "robot": robot["id"], "kind": kind, "need": cur, "room": room})
                        changed = True
                    sp = [s for s in spaces_all if s["id"] in (K.get("spaces") or [])]
                    if kind == "rescue" and not K.get("untidy"):
                        if cur["error"] in TIDY_CREDIT:
                            for ev in untidy_events(sp, st["board"], st.get("tidy_spaces") or {}, robot, K.get("room")):
                                g["append"](ev)
                        K["untidy"] = True
                        changed = True
                    if not K.get("opened") and not (st.get("bedtime") or {}).get("on"):
                        if busy(cid, st) or (st["board"].get(cid) or {}).get("opened"):
                            K["opened"] = True        # already open (e.g. from before a restart)
                        else:
                            note_sp = [] if cur.get("telemetry_lost") else sp
                            note_room = None if cur.get("telemetry_lost") else K.get("room")
                            note = rescue_note(robot, cur, note_sp, note_room, int(pts.get("rescue", 3))) if kind == "rescue" \
                                else water_note(robot, cur)
                            price = int(pts.get("rescue", 3)) if kind == "rescue" else int(pts.get("water", 2))
                            g["append"]({"type": "reopen", "chore": cid, "by": "vacuum", "note": note,
                                         "help": kind, "episode": ep,
                                         "tidy_credit": kind == "rescue" and cur.get("error") in TIDY_CREDIT,
                                         "spaces": (K.get("spaces") or []) if cur.get("error") in TIDY_CREDIT else []})
                            g["append"]({"type": "price", "chore": cid, "points": price, "by": "vacuum"})
                            g["ilog"]({"src": "vac_help", "robot": robot["id"], "kind": kind, "action": "open", "price": price})
                            K["opened"] = True
                        changed = True
                else:
                    if K.get("episode") and not K.get("clear_since"):
                        K["clear_since"] = now
                        changed = True
                    bd = st["board"].get(cid) or {}
                    if bd.get("opened") and K.get("clear_since") and now - K["clear_since"] >= CLOSE_AFTER_S \
                            and not busy(cid, st):
                        g["append"]({"type": "close_od", "chore": cid, "by": "vacuum"})
                        g["ilog"]({"src": "vac_help", "robot": robot["id"], "kind": kind, "action": "close",
                                   "why": "the robot is fine again and nobody took the job"})
                        K.update(episode=None, clear_since=0)
                        changed = True
                    elif not bd.get("opened") and K.get("episode") and K.get("clear_since"):
                        K.update(episode=None, clear_since=0)   # done (or never opened): forget the episode
                        changed = True
            if robot.get("sensors_left"):
                changed = self.sensors(robot, R, st, int(pts.get("sensors", 2)), now) or changed
        if changed:
            self.save()
            g["after_change"]()

    def sensors(self, robot, R, st, price, now):
        """The robot's sensors are due a wipe (its reminder reads 0 or less): a trusted job opens. When a kid says it's
        done, the robot's reminder is reset (its reset button); once the reminder is back up, the job closes."""
        g, cid, K = self.g, sensors_id(robot), R.setdefault("sensors", {})
        changed = False
        for r in st.get("history") or []:
            if r.get("chore") == cid and r.get("type") == "report" and r.get("status") == "inspected" \
                    and r["id"] not in (K.get("reset") or []):
                K["reset"] = (K.get("reset") or [])[-20:] + [r["id"]]
                if robot.get("sensors_reset"):
                    g["ha_call"]("button.press", {"entity_id": robot["sensors_reset"]}, self.tok)
                g["ilog"]({"src": "vac_help", "robot": robot["id"], "kind": "sensors", "action": "reset", "ref": r["id"]})
                changed = True
        try:
            left = float(self.read(robot["sensors_left"]).get("state"))
        except (TypeError, ValueError):
            return changed
        bd = st["board"].get(cid) or {}
        if left <= 0:
            if not bd.get("opened") and not busy(cid, st) and not (st.get("bedtime") or {}).get("on"):
                g["append"]({"type": "reopen", "chore": cid, "by": "vacuum", "help": "sensors",
                             "note": robot["name"] + "'s sensors are dusty: it can't see well. Look at the picture!"})
                g["append"]({"type": "price", "chore": cid, "points": price, "by": "vacuum"})
                g["ilog"]({"src": "vac_help", "robot": robot["id"], "kind": "sensors", "action": "open", "price": price})
                changed = True
        elif bd.get("opened"):
            # A grown-up can reset this reminder from the floorplan (or the robot itself). End any kid's
            # in-progress copy of the job without creating a report: the work is resolved, but no stars were earned.
            ended = set()
            for paused, jobs in ((False, st.get("active") or {}), (True, st.get("paused") or {})):
                for kid, job in jobs.items():
                    if job.get("chore") != cid:
                        continue
                    team = tuple(job.get("team") or [kid])
                    key = (paused, team)
                    if key in ended:
                        continue
                    ended.add(key)
                    g["append"]({"type": "abandon", "kid": kid, "team": list(team), "chore": cid,
                                 "paused": paused, "by": "vacuum", "why": "sensor reminder reset elsewhere"})
            g["append"]({"type": "close_od", "chore": cid, "by": "vacuum"})
            g["ilog"]({"src": "vac_help", "robot": robot["id"], "kind": "sensors", "action": "close",
                       "why": "reminder reset", "abandoned": len(ended)})
            changed = True
        return changed


def run(g):
    cfg = g["HOUSE"].get("vacuum_help") or {}
    h = Helper(g, cfg)
    while True:
        try:
            h.tick()
        except Exception as ex:  # noqa: BLE001
            print("vacuum help error", repr(ex), flush=True)
            g["ilog"]({"src": "vac_help", "action": "error", "error": repr(ex)})
        time.sleep(TICK_S)
