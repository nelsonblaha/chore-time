"""Vacuum autopilot: the robot cleans the floors the kids just tidied, when nobody's around.

The robot cleans the rooms the chores system recently declared clean (a passed tidy = floor clear),
once per declaration, when downstairs has been empty 20 min. Vacuum-only rooms first, then the mop rooms.
Suction: 20:00-07:00 quiet; nobody home max_plus; otherwise (people upstairs) turbo.
A person downstairs mid-run => pause in place; 3 min quiet => resume; still there after 10 min => dock.
No phone notices. Everything is logged (src "vac") and published as sensor.vacuum_autopilot.
"""
import json
import time
import urllib.parse
from datetime import datetime

# Filled from house.json "vacuum" by configure(); see example-config/house.json.
VAC = ROOM_SENSOR = STATUS = BATTERY = ERROR = IN_CLEAN = MODE_SEL = ENABLE = ACTIVE = UPSTAIRS = CLEAN_SCRIPT = None
MAP_FLAG = 0
SEG_CHORE = {}   # robot segment name -> chore whose passed tidy makes it eligible (others: never automatic)
MOP = set()      # segments that get vacuum + mop; the rest vacuum only
SENSORS = {}     # person-evidence sensor -> the segment it sits in (the robot trips its own room's sensors)


def configure(v):
    """v = house.json "vacuum" section."""
    global VAC, ROOM_SENSOR, STATUS, BATTERY, ERROR, IN_CLEAN, MODE_SEL, ENABLE, ACTIVE, UPSTAIRS, CLEAN_SCRIPT, MAP_FLAG
    global SEG_CHORE, MOP, SENSORS
    VAC, ROOM_SENSOR, STATUS = v["vacuum"], v["room_sensor"], v.get("status")
    BATTERY, ERROR, IN_CLEAN, MODE_SEL = v.get("battery"), v.get("error"), v["cleaning"], v["mode_select"]
    ENABLE, ACTIVE = v["enable"], v["active"]
    UPSTAIRS = v.get("nobody_upstairs")      # "people on the other floor" sensor (off = nobody there 30 min)
    CLEAN_SCRIPT, MAP_FLAG = v.get("clean_script", "script.vacuum_clean_rooms"), v.get("map_flag", 0)
    # a segment may span several jobs ("chores": [...]): it goes only when ALL are presumed clean
    SEG_CHORE = {seg: (x.get("chores") or [x["chore"]]) for seg, x in (v.get("segments") or {}).items() if x.get("chore") or x.get("chores")}
    MOP = {seg for seg, x in (v.get("segments") or {}).items() if x.get("mop")}
    SENSORS = dict(v.get("sensors") or {})
    global VOICE, NIGHT
    VOICE = v.get("voice_volume")
    global ENTRY_SENSORS, ENTRY_DOORS
    ENTRY_SENSORS, ENTRY_DOORS = set(v.get("entry_sensors") or []), set(v.get("entry_doors") or [])
    NIGHT = (int(v.get("night_from_h", 20)), int(v.get("night_until_h", 7)))


# A PERSON coming onto this floor vs the robot tripping sensors: the robot sets off random sensors one at a time;
# a person enters through an entry point (stairs, entry hall, a door) or walks through rooms. Only that pauses it.
ENTRY_SENSORS = set()   # motion at the ways onto this floor (house.json vacuum.entry_sensors)
ENTRY_DOORS = set()     # door contacts: opened = a person, always (vacuum.entry_doors)
PATH_WINDOW_S = 90      # 2+ different non-robot rooms tripping within this = someone walking through
START_QUIET_S = 20 * 60      # Q6
RESUME_QUIET_S = 3 * 60      # Q6
PARK_DOCK_S = 10 * 60        # Q5
DWELL_S = 120                # a room counts as robot-cleaned after this long in it while cleaning
PREV_GRACE_S = 90            # sensors of the room it just left still belong to the robot
RETRY_S = 30 * 60
VOICE = None          # optional robot voice-volume number: 0 at night, daytime level restored in the morning
NIGHT = (20, 7)       # night hours: quietest suction, robot voice off


def epoch(s):
    try:
        return datetime.fromisoformat(s["last_changed"]).timestamp()
    except Exception:  # noqa: BLE001
        return 0


class Pilot:
    def __init__(self, g):
        self.g = g
        configure(g["HOUSE"]["vacuum"])
        self.path = g["DATA"] / "vacuum_auto.json"
        try:
            self.D = json.loads(self.path.read_text())
        except Exception:  # noqa: BLE001
            self.D = {}
        self.D.setdefault("visits", {})
        self.D.setdefault("phase", "idle")
        self.D.setdefault("runs", [])
        self.tok = g["ha_token"]()
        self.last_pub = None
        if not self.D["visits"]:
            self.backfill()

    def backfill(self, days=4):
        """seed robot room visits from HA history so rooms it already cleaned aren't redone"""
        import urllib.request
        t0 = datetime.fromtimestamp(time.time() - days * 86400).astimezone().isoformat()
        url = (f"{self.g['HA_URL']}/api/history/period/{urllib.parse.quote(t0)}?filter_entity_id={VAC},{ROOM_SENSOR}"
               "&end_time=" + urllib.parse.quote(datetime.now().astimezone().isoformat()) + "&minimal_response&no_attributes")
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.tok}"})
            hist = json.loads(urllib.request.urlopen(req, timeout=30).read())
        except Exception as ex:  # noqa: BLE001
            print("vacuum backfill failed", ex, flush=True)
            return
        evs = []
        for series in hist:
            ent = series[0].get("entity_id") if series else None
            for s in series:
                ts = epoch(s)
                evs.append((ts, ent, s.get("state")))
        evs.sort()
        vs, room = None, None
        for ts, ent, st in evs:
            if ent == VAC:
                vs = st
            else:
                room = st
            self.track(vs, room, ts)
        self.g["ilog"]({"src": "vac", "action": "backfill", "visits": self.D["visits"]})

    # --- helpers -------------------------------------------------------------------------------
    def get(self, e):
        return self.g["ha_get"](e, self.tok)

    def call(self, svc, data):
        self.g["ha_call"](svc, data, self.tok)

    def log(self, action, **kw):
        self.D["last_event"] = {"ts": time.time(), "action": action, **kw}
        self.g["ilog"]({"src": "vac", "action": action, **kw})

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.D, indent=1))
        tmp.replace(self.path)

    def tier(self):
        h = datetime.fromtimestamp(time.time()).hour
        if h >= NIGHT[0] or h < NIGHT[1]:
            return "quiet"
        if not any(self.g["adult_home"](a, self.tok) for a in self.g["ADULTS"]) and \
                (not UPSTAIRS or self.get(UPSTAIRS).get("state") == "off"):
            return "max_plus"
        return "turbo"

    def quiet_for(self, exclude=()):
        """seconds since the last person evidence downstairs (0 = someone there now), and its source"""
        now, q, who, seen = time.time(), 10 ** 9, None, 0
        for e, seg in SENSORS.items():
            if seg in exclude:
                continue
            s = self.get(e)
            st = s.get("state")
            if st == "on":
                return 0, e
            if st == "off":
                seen += 1
                if now - epoch(s) < q:
                    q, who = now - epoch(s), e
        if seen < 3:                      # sensors down: never call that "empty"
            return 0, "sensors unavailable"
        for name, t in self.g["TVS"].items():
            try:
                what, _ = self.g["tv_status"](t, self.tok)
            except Exception:  # noqa: BLE001
                continue
            if what in ("stream", "hdmi"):
                return 0, "tv:" + name
        return q, who

    def person_entered(self, now):
        """Who/what shows a PERSON came onto this floor since the last check, or None. Robot-tripped sensors don't count:
        a door opening, an entry point (stairs, entry hall) outside the robot's rooms, or 2+ rooms in a row."""
        D = self.D
        seen = D.setdefault("seen_on", {})
        trig = [t for t in D.setdefault("trig", []) if now - t[0] < 300]
        robot = self.robot_rooms(now)
        for e in list(SENSORS) + list(ENTRY_DOORS):
            st_ = self.get(e)
            if st_.get("state") != "on":
                continue
            t0 = epoch(st_)
            if t0 <= seen.get(e, 0) or t0 < D.get("run_started", now) - 5:
                seen[e] = max(seen.get(e, 0), t0)
                continue
            seen[e] = t0
            trig.append([t0, e, SENSORS.get(e, "door")])
        D["trig"] = trig
        for t0, e, seg in trig:
            if now - t0 > 60:
                continue
            if e in ENTRY_DOORS:
                return e
            # an entry sensor (stairs) alone is NOT enough: it also sees people moving around the floor above.
            # It counts once another sensor on THIS floor, away from the robot, follows within a minute.
            if e in ENTRY_SENSORS and seg not in robot and any(
                    e2 != e and e2 not in ENTRY_DOORS and s2 not in robot and 0 <= t2 - t0 <= 60 for t2, e2, s2 in trig):
                return e + " then " + next(e2 for t2, e2, s2 in trig if e2 != e and e2 not in ENTRY_DOORS and s2 not in robot and 0 <= t2 - t0 <= 60)
        segs = {seg for t0, e, seg in trig if now - t0 <= PATH_WINDOW_S and seg not in robot and seg != "door"}
        if len(segs) >= 2:
            return "walked through " + ", ".join(sorted(segs))
        return None

    def eligible(self):
        return [seg for seg, d in self.segment_status().items() if d["eligible"]]

    def segment_status(self):
        """Per robot segment: the autopilot's real decision and why (for dashboards)."""
        st = self.g["derive"]()
        b = st["board"]
        chs = {c["id"]: c for c in st.get("config", {}).get("chores", [])}
        busy = {a.get("chore") for a in st["active"].values()} | {z.get("chore") for z in st["paused"].values()}
        out = {}
        for seg in list(SEG_CHORE) + [x for x in set(SENSORS.values()) if x not in SEG_CHORE]:
            cids = SEG_CHORE.get(seg)
            cids = [] if cids is None else [cids] if isinstance(cids, str) else list(cids)
            rooms = sorted({r for c in cids for r in (chs.get(c, {}).get("map_rooms") or [])})
            last = self.D["visits"].get(seg, 0)
            d = {"chores": cids, "rooms": rooms, "last_robot": last, "mop": seg in MOP, "eligible": False}
            xs = [b.get(c) or {} for c in cids]
            names = [chs.get(c, {}).get("name", c) for c in cids]
            if not cids:
                d["why"] = "never automatic (no kids' job here)"
            elif any(c in busy for c in cids) or any(x.get("pending_by") for x in xs):
                d["why"] = "a job here is in progress or waiting for a check"
            elif any(not x.get("clean_until") for x in xs):
                d["why"] = "waiting for a passed tidy: " + ", ".join(n for n, x in zip(names, xs) if not x.get("clean_until"))
            elif max((x.get("last_clean") or 0) for x in xs) <= last:
                d["why"] = "already vacuumed since the last tidy"
            else:
                d["eligible"], d["why"] = True, "goes next time downstairs is empty 20 min"
            d["tidied"] = max([x.get("last_clean") or 0 for x in xs] or [0])
            out[seg] = d
        return out

    # --- robot room tracking (always on, manual runs count too) -------------------------------
    def track(self, vs, room, now):
        """credit a room once the robot has spent DWELL_S actually CLEANING in it (pauses/returns don't count)"""
        D = self.D
        if D.get("t_vs") == "cleaning" and D.get("cur"):
            D["cur_clean"] = D.get("cur_clean", 0) + min(now - D.get("t_last", now), 3600)
        D["t_last"], D["t_vs"] = now, vs
        cur = D.get("cur")
        if (vs == "cleaning" or (vs == "paused" and cur)) and room and room != cur:
            if cur and D.get("cur_clean", 0) >= DWELL_S:
                D["visits"][cur] = now
            D["prev"], D["prev_left"] = cur, now
            D["cur"], D["cur_clean"] = room, 0
        elif vs in ("docked", "returning", "idle", "error") and cur:
            if D.get("cur_clean", 0) >= DWELL_S:
                D["visits"][cur] = now
            D["prev"], D["prev_left"], D["cur"], D["cur_clean"] = cur, now, None, 0

    def robot_rooms(self, now):
        ex = {self.D.get("cur")}
        if self.D.get("prev") and now - self.D.get("prev_left", 0) < PREV_GRACE_S:
            ex.add(self.D["prev"])
        return ex - {None}

    # --- runs ------------------------------------------------------------------------------------
    def launch(self, now):
        gr = self.D["groups"][self.D["gi"]]
        fan = self.tier()
        self.call("select.select_option", {"entity_id": MODE_SEL, "option": gr["mode"]})
        self.call("vacuum.set_fan_speed", {"entity_id": VAC, "fan_speed": fan})
        self.call(CLEAN_SCRIPT, {"vacuum": VAC, "map_flag": MAP_FLAG, "rooms": gr["rooms"]})
        self.D.update(group_started=now, saw_clean=False, fan=fan, docked_since=0)
        self.log("launch", rooms=gr["rooms"], mode=gr["mode"], fan=fan)

    def start(self, rooms, now):
        a = self.get(VAC).get("attributes") or {}
        self.D["saved"] = {"fan": a.get("fan_speed"), "mode": self.get(MODE_SEL).get("state")}
        vac = [r for r in rooms if r not in MOP]
        mop = [r for r in rooms if r in MOP]
        groups = ([{"mode": "vacuum", "rooms": vac}] if vac else []) + ([{"mode": "vac_and_mop", "rooms": mop}] if mop else [])
        self.D.update(phase="running", groups=groups, gi=0, run_started=now, paused_at=0, pauses=[])
        self.call("input_boolean.turn_on", {"entity_id": ACTIVE})
        self.log("start", rooms=rooms, saved=self.D["saved"])
        self.launch(now)

    def finish(self, why, now):
        sv = self.D.get("saved") or {}
        if sv.get("mode") in ("vacuum", "vac_and_mop", "mop", "custom", "smart_mode"):
            self.call("select.select_option", {"entity_id": MODE_SEL, "option": sv["mode"]})
        if sv.get("fan"):
            self.call("vacuum.set_fan_speed", {"entity_id": VAC, "fan_speed": sv["fan"]})
        self.call("input_boolean.turn_off", {"entity_id": ACTIVE})
        self.D["runs"] = (self.D["runs"] + [{"start": self.D.get("run_started"), "end": now, "why": why,
                                             "groups": self.D.get("groups"), "pauses": self.D.get("pauses")}])[-30:]
        self.D.update(phase="idle", groups=[], gi=0, paused_at=0)
        self.log("finish", why=why)

    def night_voice(self):
        if not VOICE:
            return
        h = datetime.fromtimestamp(time.time()).hour
        night = h >= NIGHT[0] or h < NIGHT[1]
        st = self.get(VOICE).get("state")
        try:
            cur = float(st)
        except (TypeError, ValueError):
            return
        if night and cur != 0:
            self.D["voice_day"] = cur
            self.call("number.set_value", {"entity_id": VOICE, "value": 0})
            self.log("voice", to=0, was=cur)
        elif not night and cur == 0 and self.D.get("voice_day"):
            self.call("number.set_value", {"entity_id": VOICE, "value": self.D["voice_day"]})
            self.log("voice", to=self.D["voice_day"])

    def tick(self):
        now = time.time()
        D = self.D
        self.night_voice()
        v = self.get(VAC)
        vs = v.get("state")
        room = self.get(ROOM_SENSOR).get("state")
        incl = self.get(IN_CLEAN).get("state")
        self.track(vs, room, now)
        ph = D["phase"]
        info = {}
        if ph == "idle":
            if self.get(ENABLE).get("state") != "on":
                info["why"] = "switched off"
            else:
                rooms = self.eligible()
                info["eligible"] = rooms
                q, who = self.quiet_for() if rooms else (None, None)
                info["quiet_min"] = None if q is None else round(min(q, 99 * 3600) / 60)
                ok_robot = vs == "docked" and incl != "on" and \
                    (not BATTERY or float(self.get(BATTERY).get("state") or 0) >= 50) and (not ERROR or self.get(ERROR).get("state") in ("none", None))
                if rooms and not ok_robot:
                    info["why"] = "robot busy/not ready"
                elif rooms and q is not None and q >= START_QUIET_S and now - D.get("last_attempt", 0) > RETRY_S:
                    D["last_attempt"] = now
                    self.start(rooms, now)
        elif ph == "running":
            if vs == "cleaning":
                D["saw_clean"] = True
                who = self.person_entered(now)
                if who:
                    self.call("vacuum.pause", {"entity_id": VAC})
                    D.update(phase="paused", paused_at=now)
                    D["pauses"].append({"ts": now, "who": who, "robot_in": D.get("cur")})
                    self.log("pause", who=who, robot_in=D.get("cur"))
                else:
                    t = self.tier()
                    if t != D.get("fan") and (v.get("attributes") or {}).get("fan_speed") != t:
                        self.call("vacuum.set_fan_speed", {"entity_id": VAC, "fan_speed": t})
                        D["fan"] = t
                        self.log("fan", fan=t)
            done = D.get("saw_clean") and vs == "docked" and incl != "on"
            if done:
                D["docked_since"] = D.get("docked_since") or now
                if now - D["docked_since"] >= 60:
                    if D["gi"] + 1 < len(D["groups"]):
                        D.update(phase="between", between_since=now)
                    else:
                        self.finish("done", now)
            else:
                D["docked_since"] = 0
            if D["phase"] == "running" and not D.get("saw_clean") and now - D.get("group_started", now) > 300:
                self.finish("never started cleaning (state %s)" % vs, now)
        elif ph == "between":       # next group only if downstairs is still empty
            q, who = self.quiet_for()
            if q >= RESUME_QUIET_S:
                D["gi"] += 1
                D["phase"] = "running"
                self.launch(now)
            elif now - D.get("between_since", now) > PARK_DOCK_S:
                self.finish("people back between rooms", now)
        elif ph == "paused":
            q, who = self.quiet_for(exclude={D.get("cur")} - {None})
            if vs == "cleaning":                      # someone resumed it by hand
                D["phase"] = "running"
                self.log("resumed_elsewhere")
            elif q >= RESUME_QUIET_S:
                self.call("vacuum.send_command", {"entity_id": VAC, "command": "resume_segment_clean"})
                D.update(phase="resuming", resume_at=now)
                self.log("resume")
            elif now - D["paused_at"] >= PARK_DOCK_S:
                self.call("vacuum.return_to_base", {"entity_id": VAC})
                D["phase"] = "docking"
                self.log("dock", who=who)
        elif ph == "resuming":
            if vs == "cleaning":
                D["phase"] = "running"
            elif now - D.get("resume_at", now) > 30:
                if not D.get("resume_fallback"):
                    self.call("vacuum.start", {"entity_id": VAC})
                    D.update(resume_fallback=True, resume_at=now)
                    self.log("resume_fallback")
                else:
                    D["resume_fallback"] = False
                    self.call("vacuum.return_to_base", {"entity_id": VAC})
                    D["phase"] = "docking"
                    self.log("resume_failed")
        elif ph == "docking":
            if vs == "docked" and incl != "on":
                self.finish("docked: person stayed downstairs", now)
            elif vs == "docked":
                D["docking_since"] = D.get("docking_since") or now
                if now - D["docking_since"] > 120:
                    D["docking_since"] = 0
                    self.finish("docked", now)
        self.publish(info, vs, room)
        self.save()

    def publish(self, info, vs, room):
        D = self.D
        attrs = {"friendly_name": "Vacuum autopilot", "icon": "mdi:robot-vacuum",
                 "robot": vs, "robot_room": room, **info,
                 "groups": D.get("groups"), "gi": D.get("gi"),
                 "visits": {k: datetime.fromtimestamp(t).strftime("%a %H:%M") for k, t in D["visits"].items()},
                 "segments": self.segment_status(), "suction_now": self.tier(), "phase": D["phase"],
                 "last_event": D.get("last_event"), "runs": D["runs"][-5:]}
        key = json.dumps([D["phase"], attrs], sort_keys=True, default=str)
        if key != self.last_pub or time.time() - D.get("pub_ts", 0) > 300:
            try:
                self.g["ha_post_state"]("sensor.vacuum_autopilot", D["phase"], attrs, self.tok)
                self.last_pub, D["pub_ts"] = key, time.time()
            except Exception as ex:  # noqa: BLE001
                print("vacuum_autopilot publish failed", ex, flush=True)


def run(g):
    p = Pilot(g)
    while True:
        try:
            p.tick()
        except Exception as ex:  # noqa: BLE001
            print("vacuum autopilot error", repr(ex), flush=True)
            g["ilog"]({"src": "vac", "action": "error", "error": repr(ex)})
        time.sleep(10 if p.D["phase"] != "idle" else 30)
