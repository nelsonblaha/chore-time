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
from datetime import datetime, timedelta

# Filled from house.json "vacuum" by configure(); see example-config/house.json.
VAC = ROOM_SENSOR = STATUS = BATTERY = ERROR = IN_CLEAN = MODE_SEL = ENABLE = ACTIVE = UPSTAIRS = CLEAN_SCRIPT = None
MAP_FLAG = 0
SEGMENTS = []
SEG_ROOMS = {}   # segment -> floor-plan room ids it covers (house.json segments.<seg>.rooms)
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
    global SEGMENTS
    SEGMENTS = list(v.get("segments") or {})   # every robot room gets the every_h goal
    global SEG_ROOMS
    SEG_ROOMS = {seg: list(x["rooms"]) for seg, x in (v.get("segments") or {}).items() if x.get("rooms")}
    MOP = {seg for seg, x in (v.get("segments") or {}).items() if x.get("mop")}
    SENSORS = dict(v.get("sensors") or {})
    global VOICE, NIGHT
    VOICE = v.get("voice_volume")
    global NIGHT_CARPET_OFF, SEG_LIGHTS
    SEG_LIGHTS = dict(v.get("segment_lights") or {})
    NIGHT_CARPET_OFF = bool(v.get("night_carpet_boost_off"))
    global ENTRY_SENSORS, ENTRY_DOORS
    ENTRY_SENSORS, ENTRY_DOORS = set(v.get("entry_sensors") or []), set(v.get("entry_doors") or [])
    NIGHT = (int(v.get("night_from_h", 20)), int(v.get("night_until_h", 7)))
    global RESUME_QUIET_S, SCHEDULE, EVERY_H, DINNER
    RESUME_QUIET_S = int(v.get("resume_quiet_s", 20))
    SCHEDULE, EVERY_H, DINNER = dict(v.get("schedule") or {}), float(v.get("every_h", 72)), dict(v.get("dinner") or {})


# A PERSON coming onto this floor vs the robot tripping sensors: the robot sets off random sensors one at a time;
# a person enters through an entry point (stairs, entry hall, a door) or walks through rooms. Only that pauses it.
ENTRY_SENSORS = set()   # motion at the ways onto this floor (house.json vacuum.entry_sensors)
ENTRY_DOORS = set()     # door contacts: opened = a person, always (vacuum.entry_doors)
PATH_WINDOW_S = 90      # 2+ different non-robot rooms tripping within this = someone walking through
START_QUIET_S = 20 * 60      # Q6
SCHEDULE = {}   # segment -> {"nightly_after": "19:00", "needs_dinner": bool, "every_h": N}
EVERY_H = 72    # every other segment: at least this often
DINNER = {}     # {"sensor": dining motion, "from": "16:30", "until": "20:30", "min_on_min": 15}
RESUME_QUIET_S = 20         # motion sensors only go "off" after ~4 min without movement, so "all off" already IS quiet;
                            # this is just a debounce (house.json vacuum.resume_quiet_s)
PARK_DOCK_S = 10 * 60        # Q5
DWELL_S = 120                # a room counts as robot-cleaned after this long in it while cleaning
PREV_GRACE_S = 90            # sensors of the room it just left still belong to the robot
RETRY_S = 30 * 60
SEG_LIGHTS = {}   # segment -> room light-mode selects, lit white while the robot finds its way home
NIGHT_CARPET_OFF = False   # turn the robot's Carpet Boost off at night (vacuum.night_carpet_boost_off)
VOICE = None          # optional robot voice-volume number: 0 at night, daytime level restored in the morning
NIGHT = (20, 7)       # night hours: quietest suction, robot voice off


def epoch(s):
    try:
        return datetime.fromisoformat(s["last_changed"]).timestamp()
    except Exception:  # noqa: BLE001
        return 0


def dinner_from_rows(rows, q0, q1, w0, w1, gap, need):
    """rows = HA history of a motion sensor. True if one merged 'on' stretch spends >= need seconds inside [w0, w1]."""
    iv, since = [], None
    for r in rows:
        t = max(epoch(r), q0)
        if r.get("state") == "on" and since is None:
            since = t
        elif r.get("state") != "on" and since is not None:
            iv.append([since, t])
            since = None
    if since is not None:
        iv.append([since, q1])
    merged = []
    for a, b in iv:
        if merged and a - merged[-1][1] < gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return any(min(b, w1) - max(a, w0) >= need for a, b in merged)   # the part of one stretch inside the window


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
        """Per robot segment: the autopilot's real decision and why (for dashboards).
        Goals (house.json vacuum.schedule): some rooms nightly (after a time; optionally only if dinner was eaten
        there, per a motion sensor), everything else every `every_h` hours (default 72). A freshly passed tidy also
        counts (the floor is clear). ANY robot visit resets the clock, including a manual run (e.g. a room's clean
        button). Never while a kid's job there is in progress or waiting for a check."""
        st = self.g["derive"]()
        b = st["board"]
        chs = {c["id"]: c for c in st.get("config", {}).get("chores", [])}
        busy = {a.get("chore") for a in st["active"].values()} | {z.get("chore") for z in st["paused"].values()}
        now = time.time()
        out = {}
        for seg in list(dict.fromkeys(SEGMENTS + list(SEG_CHORE) + list(SCHEDULE))):
            cids = list(SEG_CHORE.get(seg) or [])
            # the map rooms this robot area really covers: explicit in house.json (a kids' job can span several robot
            # areas, e.g. "front hall + dining"), else derived from its jobs
            rooms = SEG_ROOMS.get(seg) or sorted({r for c in cids for r in (chs.get(c, {}).get("map_rooms") or [])})
            last = self.D["visits"].get(seg, 0)
            sch = SCHEDULE.get(seg) or {}
            d = {"chores": cids, "rooms": rooms, "last_robot": last, "mop": seg in MOP, "eligible": False}
            xs = [b.get(c) or {} for c in cids]
            tidied = max([x.get("last_clean") or 0 for x in xs] or [0])
            d["tidied"] = tidied
            every = float(sch.get("every_h", EVERY_H)) * 3600
            d["next_due"] = last + every if last else now
            if any(c in busy for c in cids) or any(x.get("pending_by") for x in xs):
                d["why"] = "a kid's job here is in progress or waiting for a check"
            elif sch.get("nightly_after") and self.nightly_due(sch, last):
                d["eligible"], d["why"] = True, "nightly" + (" (dinner was eaten here)" if sch.get("needs_dinner") else "")
            elif not last or now - last >= every:
                d["eligible"], d["why"] = True, "due: every %g days" % (every / 86400)
            elif cids and all(x.get("clean_until") for x in xs) and tidied > last:
                d["eligible"], d["why"] = True, "a tidy just passed (floor clear)"
            else:
                left = every - (now - last)
                d["why"] = "next due in %s" % (("%dh" % (left // 3600)) if left >= 3600 else ("%dm" % (left // 60)))
                if sch.get("nightly_after"):
                    d["why"] += "; nightly after " + sch["nightly_after"] + (" if dinner was eaten here" if sch.get("needs_dinner") else "")
            if d["eligible"]:
                d["why"] += " · goes next time the floor is empty 20 min"
            out[seg] = d
        return out

    def nightly_ref(self, after):
        """The evening this night belongs to: today's `after` time, or yesterday's before the morning end."""
        n = datetime.fromtimestamp(time.time())
        hh, mm = (int(x) for x in after.split(":"))
        ref = n.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if n < ref:
            if n.hour >= NIGHT[1]:
                return None            # daytime: not night yet
            ref -= timedelta(days=1)   # after midnight: still last night
        return ref

    def nightly_due(self, sch, last):
        ref = self.nightly_ref(sch["nightly_after"])
        if not ref or last >= ref.timestamp():
            return False
        return not sch.get("needs_dinner") or self.dinner_eaten(ref)

    def dinner_eaten(self, ref):
        """Dinner happened in this room tonight: its motion sensor shows one continuous stretch (gaps under
        merge_gap_s joined) with at least min_block_min of it inside the dinner window. Sitting at a table looks like
        one long stretch; people walking through look like many short ones (calibrated on real evenings)."""
        if not DINNER.get("sensor"):
            return False
        key = ref.strftime("%Y-%m-%d")
        cache = self.D.setdefault("dinner", {})
        if key in cache and (cache[key] or time.time() - self.D.get("dinner_ts", 0) < 600):
            return cache[key]
        hm = lambda t: ref.replace(hour=int(t[:2]), minute=int(t[3:]), second=0, microsecond=0)
        w0, w1 = hm(DINNER.get("from", "17:30")), hm(DINNER.get("until", "19:30"))
        q0, q1 = w0 - timedelta(hours=2), w1 + timedelta(hours=1)   # look around the window to see whole stretches
        gap, need = float(DINNER.get("merge_gap_s", 180)), float(DINNER.get("min_block_min", 20)) * 60
        try:
            import urllib.request
            url = (f"{self.g['HA_URL']}/api/history/period/{urllib.parse.quote(q0.astimezone().isoformat())}"
                   f"?end_time={urllib.parse.quote(q1.astimezone().isoformat())}&filter_entity_id={DINNER['sensor']}"
                   "&minimal_response&no_attributes")
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.tok}"})
            rows = (json.loads(urllib.request.urlopen(req, timeout=20).read()) or [[]])[0]
        except Exception as ex:  # noqa: BLE001
            self.g["ilog"]({"src": "vac", "action": "dinner_check_failed", "error": repr(ex)})
            return False
        ate = dinner_from_rows(rows, q0.timestamp(), min(time.time(), q1.timestamp()), w0.timestamp(), w1.timestamp(), gap, need)
        cache[key] = ate
        self.D["dinner_ts"] = time.time()
        for k in sorted(cache)[:-10]:
            cache.pop(k)
        return ate

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

    def return_lights(self, vs, now):
        """Robot heading home for a while (its room reading goes stale off-cleaning): light every room on this
        floor that nobody was in when the return began, so its camera can find the way; back to auto once docked."""
        if not SEG_LIGHTS:
            return
        D = self.D
        if vs == "returning":
            if not D.get("ret_since"):
                D["ret_since"] = now
                busy = set()
                for e, seg in SENSORS.items():   # rooms with a person in the last 10 min stay as they are
                    st_ = self.get(e)
                    if st_.get("state") == "on" or now - epoch(st_) < 600:
                        busy.add(seg)
                D["ret_busy"] = sorted(busy)
            if now - D["ret_since"] < 30:
                return
            lit = D.setdefault("ret_lit", [])
            for seg, modes in SEG_LIGHTS.items():
                if seg in D.get("ret_busy", []):
                    continue
                for m in modes:
                    if m not in lit and self.get(m).get("state") == "auto":
                        self.call("input_select.select_option", {"entity_id": m, "option": "white"})
                        lit.append(m)
                        self.log("return_light", mode=m)
            return
        if D.get("ret_since") and vs in ("docked", "idle", "charging", "cleaning", "paused", "error"):
            for m in D.get("ret_lit", []):
                if self.get(m).get("state") == "white":
                    self.call("input_select.select_option", {"entity_id": m, "option": "auto"})
            if D.get("ret_lit"):
                self.log("return_lights_off", modes=D["ret_lit"])
            D["ret_since"], D["ret_lit"], D["ret_busy"] = 0, [], []

    def dinner_reopens(self):
        """A detected dinner (see dinner_eaten) makes the chosen jobs dirty that evening, e.g. the dining table,
        so a kid can earn stars clearing up after it (vacuum.dinner.reopens). Once per evening."""
        jobs = DINNER.get("reopens") or []
        if not jobs:
            return
        n = datetime.fromtimestamp(time.time())
        f_ = DINNER.get("from", "17:00")
        start = n.replace(hour=int(f_[:2]), minute=int(f_[3:]), second=0, microsecond=0) + \
            timedelta(minutes=float(DINNER.get("min_block_min", 20)))
        key = n.strftime("%Y-%m-%d")
        done = self.D.setdefault("dinner_reopened", {})
        if n < start or done.get(key) or not self.dinner_eaten(n):
            return
        board = self.g["derive"]()["board"]
        for cid in jobs:
            if (board.get(cid) or {}).get("clean_until"):
                self.g["append"]({"type": "reopen", "chore": cid, "by": "dinner", "note": "Dinner was eaten here tonight"})
        done[key] = True
        for k in sorted(done)[:-10]:
            done.pop(k)
        self.log("dinner_reopen", jobs=jobs)

    def night_carpet(self):
        """Carpet Boost (full suction on rugs) off at night, back on by day. Not an official integration entity:
        sent as the robot's own set_carpet_mode command, once per change (vacuum.night_carpet_boost_off)."""
        if not NIGHT_CARPET_OFF:
            return
        h = datetime.fromtimestamp(time.time()).hour
        want = "off" if (h >= NIGHT[0] or h < NIGHT[1]) else "on"
        if self.D.get("carpet") == want:
            return
        self.call("vacuum.send_command", {"entity_id": VAC, "command": "set_carpet_mode",
                                          "params": [{"enable": 0 if want == "off" else 1, "current_high": 500,
                                                      "current_integral": 450, "current_low": 400, "stall_time": 10}]})
        self.D["carpet"] = want
        self.log("carpet_boost", to=want)

    def night_voice(self):
        self.night_carpet()
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
        self.return_lights(vs, now)
        self.dinner_reopens()
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
