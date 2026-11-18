"""Help the robot! Hero jobs that open by themselves when a robot vacuum needs a kid.

  * Rescue: the robot is stuck (tangled brush, trapped, jammed wheels, stranded...). The job: free it, press the Home
    button on top so it can go home, and (when it got stuck on something) tidy at least part of that room: the rescue
    pays its own stars plus partial stars for the part tidied, which come off the room's own job. Stuck on something
    in a room (a brush or wheel jam, trapped, bumper stuck) also marks that room untidy at once.
  * Water: the dock's clean water tank is empty or its dirty water tank is full. The job: change the water.

Nothing else opens a job: worn parts, filters, the dust bag and other dock upkeep stay grown-up maintenance.

Each job is a built-in on-demand chore per robot ("help_rescue_<id>", "help_water_<id>"). It opens with a one-time
price, so it arrives as BIG NEWS (flashing, with a fanfare) on the kids' screens, and it goes through inspection like
any job. Jobs never open during bedtime
(the room is still marked untidy at once). If the robot gets going again without a kid (a grown-up fixed it) and no
kid has started or reported the job within CLOSE_AFTER_S, the job closes again; the room stays untidy.

house.json:
  "vacuum_help": {
    "points": {"rescue": 3, "water": 2},          # rescue: plus whatever the inspector gives for the part tidied
    "robots": [{"id": "down", "name": "Downstairs Robot", "floor": "Downstairs",
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
# Stuck ON something in the room: the room is untidy.
TIDY = {"main_brush_jammed", "side_brush_jammed", "wheels_jammed", "robot_trapped", "bumper_stuck",
        "vertical_bumper_pressed", "wheels_suspended", "robot_tilted", "vibrarise_jammed"}
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
                             "home. It comes with tidying: tidy at least part of the room where it got stuck, for "
                             "extra stars for that part."))
        out.append(dict(common, id=water_id(r), name="Fresh water for " + r["name"], icon="1f4a7", help="water",
                        desc=r["name"] + " is thirsty! Change its water at the dock: empty the dirty tank, fill the "
                             "clean one."))
    return [{k: v for k, v in c.items() if v is not None or k == "points"} for c in out]


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
    tidy = bool(spaces) and n["error"] in TIDY
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
            n = need(robot, self.read)
            for kind, cid in (("rescue", rescue_id(robot)), ("water", water_id(robot))):
                K = R.setdefault(kind, {})
                cur = n if n and n["kind"] == kind else None
                if cur:
                    K["clear_since"] = 0
                    ep = round(cur["since"])
                    if now - cur["since"] < HOLD_S:
                        continue
                    if K.get("episode") != ep:        # a new time it needs help
                        K.update(episode=ep, untidy=False, opened=False)
                        room = robot_room(robot, self.read) if kind == "rescue" else None
                        sp = spaces_of(robot, room, spaces_all) if room else []
                        K.update(room=room, spaces=[s["id"] for s in sp])
                        g["ilog"]({"src": "vac_help", "robot": robot["id"], "kind": kind, "need": cur, "room": room})
                        changed = True
                    sp = [s for s in spaces_all if s["id"] in (K.get("spaces") or [])]
                    if kind == "rescue" and not K.get("untidy"):
                        if cur["error"] in TIDY:
                            for ev in untidy_events(sp, st["board"], st.get("tidy_spaces") or {}, robot, K.get("room")):
                                g["append"](ev)
                        K["untidy"] = True
                        changed = True
                    if not K.get("opened") and not (st.get("bedtime") or {}).get("on"):
                        if busy(cid, st) or (st["board"].get(cid) or {}).get("opened"):
                            K["opened"] = True        # already open (e.g. from before a restart)
                        else:
                            note = rescue_note(robot, cur, sp, K.get("room"), int(pts.get("rescue", 3))) if kind == "rescue" \
                                else water_note(robot, cur)
                            price = int(pts.get("rescue", 3)) if kind == "rescue" else int(pts.get("water", 2))
                            g["append"]({"type": "reopen", "chore": cid, "by": "vacuum", "note": note,
                                         "help": kind, "episode": ep,
                                         "spaces": (K.get("spaces") or []) if cur.get("error") in TIDY else []})
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
        if changed:
            self.save()
            g["after_change"]()


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
