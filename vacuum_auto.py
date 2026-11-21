"""Vacuum autopilot: the robot cleans the floors the kids just tidied, when nobody's around.

The robot cleans the rooms the chores system recently declared clean (a passed tidy = floor clear),
once per declaration, when downstairs has been empty 20 min. Vacuum-only rooms first, then the mop rooms.
Suction: 20:00-07:00 quiet; nobody home max_plus; otherwise (people upstairs) turbo.
A person downstairs mid-run => pause in place; 3 min quiet => resume; still there after 10 min => dock.
The next group of rooms needs 5 quiet minutes. A robot that got stuck, or that a grown-up stopped or sent home,
ends the run once it's docked; so does a run more than 3 hours old.
No phone notices. Everything is logged (src "vac") and published as sensor.vacuum_autopilot.

More robots: each gets its own copy (server loads this module once per house.json section listed in
"vacuum_more"), with its own state file, sensor ("publish") and log source ("log_src"). Optional per robot:
  occupancy       {"sensor", "floor", "seal": {room: {"doors": [...], "segments": [...]}}}: floor vacancy from the house's
                  people tracker instead of motion sensors; an occupied room shut off by closed doors doesn't count, its
                  robot rooms are skipped
                  Optional passage_graph points to HA's private validated room/passages export for room-level clearance
                  of explicit Clean-when-vacant requests. Invalid graph data falls back to whole-floor vacancy.
  guest_switches  {segment: switch}: skip a room while a guest stays there
  sleep_protection {segment: entity}: hold a room while the sleep guard is on or unavailable
  untidy_blocks   true: a room marked untidy waits until it's tidy
  segments.<name>.access_through [segment, ...]: rooms that every route from the dock to this segment crosses;
                  if one is blocked, the segment behind it is blocked too. Do not list the dock/hub segment.
  smart_mode      "always" | "day": the robot's own automatic mode (it picks vacuum or mop per room)
  tvs             which TVs count as someone here ([] = none on this floor)
"""
import json
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from entry_edges import entry_edges
import schedule

# Filled from house.json "vacuum" by configure(); see example-config/house.json.
VAC = ROOM_SENSOR = STATUS = BATTERY = ERROR = IN_CLEAN = MODE_SEL = ENABLE = ACTIVE = UPSTAIRS = CLEAN_SCRIPT = None
REQUESTS = None
MAP_FLAG = 0
MAP_SELECT = MAP_NAME = None
FLOOR = "downstairs"
BACKOFF = True
PIN_MAP = False
PROGRESS = CLEAN_TIME = None
EXPLORE = {}
SEGMENTS = []
SEG_ROOMS = {}   # segment -> floor-plan room ids it covers (house.json segments.<seg>.rooms)
SEG_CHORE = {}   # robot segment name -> chore whose passed tidy makes it eligible (others: never automatic)
TRANSIT = {}     # segment -> robot rooms every route from the dock to it must cross
MOP = set()      # segments that get vacuum + mop; the rest vacuum only
SENSORS = {}     # person-evidence sensor -> the segment it sits in (the robot trips its own room's sensors)
HELD = set()     # of those, sensors whose On holds for many minutes (a thermostat's motion): only the turn-on counts
PUBLISH, LOG_SRC, NAME = "sensor.vacuum_autopilot", "vac", "Vacuum autopilot"   # one per robot: own sensor, log, name
PRESENCE_PAUSE_PUBLISH = "binary_sensor.downstairs_vacuum_presence_pause"
OCC = {}         # presence-tracker floor vacancy instead of motion sensors (vacuum.occupancy), see occ_presence()
TV_IDS = None    # TVs that count as "someone here" (None = every TV; [] = none, e.g. a floor without TVs)
SLEEP_PROTECTION = {}
GUESTS = {}      # segment -> a "guest staying here" switch: skip that room while it's on
UNTIDY_BLOCKS = False   # a room marked untidy (tidy_spaces) is never vacuumed until it's tidy again
SMART = None     # "always" | "day": the robot's own automatic mode (it picks vacuum/mop per room); night keeps quiet tiers


def configure(v):
    """v = house.json "vacuum" section."""
    global VAC, ROOM_SENSOR, STATUS, BATTERY, ERROR, IN_CLEAN, MODE_SEL, ENABLE, ACTIVE, UPSTAIRS, CLEAN_SCRIPT, MAP_FLAG
    global SEG_CHORE, TRANSIT, MOP, SENSORS
    VAC, ROOM_SENSOR, STATUS = v["vacuum"], v["room_sensor"], v.get("status")
    BATTERY, ERROR, IN_CLEAN, MODE_SEL = v.get("battery"), v.get("error"), v["cleaning"], v["mode_select"]
    ENABLE, ACTIVE = v["enable"], v["active"]
    UPSTAIRS = v.get("nobody_upstairs")      # "people on the other floor" sensor (off = nobody there 30 min)
    CLEAN_SCRIPT, MAP_FLAG = v.get("clean_script", "script.vacuum_clean_rooms"), v.get("map_flag", 0)
    global MAP_SELECT, MAP_NAME
    MAP_SELECT, MAP_NAME = v.get("map_select"), v.get("map_name")   # the robot's map picker, and the map our rooms are on
    global REQUESTS
    REQUESTS = v.get("room_requests")
    # a segment may span several jobs ("chores": [...]): it goes only when ALL are presumed clean
    SEG_CHORE = {seg: (x.get("chores") or [x["chore"]]) for seg, x in (v.get("segments") or {}).items() if x.get("chore") or x.get("chores")}
    global SEGMENTS
    SEGMENTS = list(v.get("segments") or {})   # every robot room gets the every_h goal
    global SEG_ROOMS
    SEG_ROOMS = {seg: list(x["rooms"]) for seg, x in (v.get("segments") or {}).items() if x.get("rooms")}
    TRANSIT = {seg: list(x.get("access_through") or []) for seg, x in (v.get("segments") or {}).items()
               if x.get("access_through")}
    MOP = {seg for seg, x in (v.get("segments") or {}).items() if x.get("mop")}
    SENSORS = dict(v.get("sensors") or {})
    global HELD
    HELD = set(v.get("held_sensors") or []) & set(SENSORS)
    global VOICE, NIGHT
    VOICE = v.get("voice_volume")
    global NIGHT_CARPET_OFF, SEG_LIGHTS
    SEG_LIGHTS = dict(v.get("segment_lights") or {})
    NIGHT_CARPET_OFF = bool(v.get("night_carpet_boost_off"))
    global ENTRY_SENSORS, ENTRY_DOORS, APPROACH_SENSORS, APPROACH_ROOMS, PRESENCE_PAUSE_PUBLISH
    ENTRY_SENSORS, ENTRY_DOORS = set(v.get("entry_sensors") or []), set(v.get("entry_doors") or [])
    APPROACH_SENSORS = set(v.get("approach_sensors") or ENTRY_SENSORS)
    APPROACH_ROOMS = set(v.get("approach_rooms") or [])
    floor_name = v.get("floor") or (v.get("occupancy") or {}).get("floor") or "downstairs"
    safe_floor = "".join(c if c.isalnum() else "_" for c in floor_name.lower()).strip("_")
    PRESENCE_PAUSE_PUBLISH = v.get("presence_pause_publish") or \
        "binary_sensor.%s_vacuum_presence_pause" % safe_floor
    NIGHT = (int(v.get("night_from_h", 20)), int(v.get("night_until_h", 7)))
    global RESUME_QUIET_S, SCHEDULE, EVERY_H, DINNER
    RESUME_QUIET_S = int(v.get("resume_quiet_s", 20))
    SCHEDULE, EVERY_H, DINNER = dict(v.get("schedule") or {}), float(v.get("every_h", 72)), dict(v.get("dinner") or {})
    global PUBLISH, LOG_SRC, OCC, TV_IDS, GUESTS, SLEEP_PROTECTION, UNTIDY_BLOCKS, SMART, NAME
    PUBLISH, LOG_SRC = v.get("publish", "sensor.vacuum_autopilot"), v.get("log_src", "vac")
    NAME = v.get("name", "Vacuum autopilot")
    SLEEP_PROTECTION = dict(v.get("sleep_protection") or {})
    OCC, TV_IDS, GUESTS = dict(v.get("occupancy") or {}), v.get("tvs"), dict(v.get("guest_switches") or {})
    global FLOOR, BACKOFF, PIN_MAP, EXPLORE, PROGRESS, CLEAN_TIME
    PROGRESS, CLEAN_TIME = v.get("progress"), v.get("clean_time")   # the robot's own % done and seconds this clean
    # tidiness check: while the kids are here (and tomorrow too), now and then the robot explores a room marked untidy
    # to find out how untidy it really is; if it gets stuck, that's the proof, and the kids rescue it (a rescue job)
    EXPLORE = v.get("explore") or {}
    PIN_MAP = bool(v.get("pin_map"))   # put the robot back on map_name whenever it sits docked on another map
    BACKOFF = v.get("failed_start_backoff", True)   # false while someone is fixing the robot: retry every window
    FLOOR = v.get("floor") or OCC.get("floor") or "downstairs"   # for the status sentence
    UNTIDY_BLOCKS, SMART = bool(v.get("untidy_blocks")), v.get("smart_mode")


# A PERSON coming onto this floor vs the robot tripping sensors: the robot sets off random sensors one at a time;
# a person enters through an entry point (stairs, entry hall, a door) or walks through rooms. Only that pauses it.
ENTRY_SENSORS = set()   # motion at the ways onto this floor (house.json vacuum.entry_sensors)
ENTRY_DOORS = set()     # door contacts: opened = a person, always (vacuum.entry_doors)
APPROACH_SENSORS = set()  # first hints someone could be approaching this floor; fail closed for camera privacy
APPROACH_ROOMS = set()    # presence-tracker rooms on stairs/landing, even when not assigned to this floor
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
FAILED_START_S = 60          # a run whose robot cleaned for less than this gave up: a failed start, not "done"
BACKOFF_S = (2 * 3600, 4 * 3600, 8 * 3600, 24 * 3600)   # after 1, 2, 3, 4+ failed starts in a row
BETWEEN_QUIET_S = 5 * 60     # the next group of rooms needs 5 quiet minutes, not the 20 s a paused room needs to resume
MAX_RUN_S = 3 * 3600         # a run older than this never launches another group (a stuck robot freed hours later goes home)
HANDS_OFF = ("mapping",)     # robot statuses during which the autopilot sends nothing at all
LEFTOVER_S = 10 * 60         # docked this long, charged, yet still "cleaning": the robot left its task unfinished
LEFTOVER_BATTERY = 90        # (it resumes a recharge stop well before this; a mop wash takes a few minutes)
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
    def __init__(self, g, key="vacuum"):
        self.g = g
        configure(g["HOUSE"][key])
        self.floor = FLOOR
        self.room_cleaning_sensor = g["HOUSE"][key].get("room_cleaning_evidence")
        self.lock = threading.RLock()
        self.path = g["DATA"] / ("vacuum_auto.json" if key == "vacuum" else "vacuum_auto_%s.json" % key)
        try:
            self.D = json.loads(self.path.read_text())
        except Exception:  # noqa: BLE001
            self.D = {}
        self.D.setdefault("visits", {})
        self.D.setdefault("phase", "idle")
        self.D.setdefault("runs", [])
        self.D.setdefault("incomplete", [])
        self.D.setdefault("manual_queue", [])
        self.D.setdefault("manual_seen", {})
        self.tok = g["ha_token"]()
        self.last_pub = None
        self.last_pause_pub = None
        g.setdefault("VACUUM_PLANNERS", {})[self.floor] = self
        if not self.D["visits"] and not self.room_cleaning_sensor:
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
        self.g["ilog"]({"src": LOG_SRC, "action": "backfill", "visits": self.D["visits"]})

    # --- helpers -------------------------------------------------------------------------------
    def get(self, e):
        return self.g["ha_get"](e, self.tok)

    def call(self, svc, data):
        self.g["ha_call"](svc, data, self.tok)

    def log(self, action, **kw):
        self.D["last_event"] = {"ts": time.time(), "action": action, **kw}
        self.g["ilog"]({"src": LOG_SRC, "action": action, **kw})

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

    # --- presence from the house's people tracker (vacuum.occupancy) -----------------------------
    def occ_presence(self, starting=True):
        """(who, sealed rooms): who = why someone counts as on this floor now, or None.
        Fails closed: a stale tracker or an incomplete roster counts as someone there. An occupied room whose doors
        (contacts, on = open) all read closed is sealed off: it doesn't count, but its robot rooms are skipped.
        "uncertain" holds back a START; during a run only real people pause it."""
        s = self.get(OCC["sensor"])
        a = s.get("attributes") or {}
        try:
            fresh = s.get("state") == "ok" and float(a.get("valid_until") or 0) > time.time() and a.get("roster_complete") is True
        except (TypeError, ValueError):
            fresh = False
        if not fresh:
            return "presence tracker unavailable", set()
        fl = (a.get("floors") or {}).get(OCC["floor"]) or {}
        if fl.get("state") == "vacant":             # the tracker's own floor verdict
            return None, set()
        if fl.get("state") == "unknown" and OCC.get("house_vacancy"):
            whole = self.get(OCC["house_vacancy"])
            expiry = (whole.get("attributes") or {}).get("expires_at")
            if (whole.get("state") == "vacant" and type(expiry) in (int, float)
                    and time.time() < expiry <= time.time() + 30
                    and all(type(a.get(k)) is int and a[k] == 0 for k in
                            ("expected", "people_min", "people_max", "unexplained"))):
                return None, set()
        if fl.get("state") != "occupied" or not fl.get("rooms"):            # unknown / missing: someone may be there
            return "floor %s" % (fl.get("state") or "unknown"), set()
        rooms = fl.get("rooms") or []
        sealed = set()
        for r in rooms:
            info = (a.get("rooms") or {}).get(r) or {}
            st = info.get("state")
            if st == "clear" or (st == "uncertain" and not starting):
                continue
            seal = (OCC.get("seal") or {}).get(r) or {}
            if seal.get("doors") and all(self.get(d).get("state") == "off" for d in seal["doors"]):
                sealed.add(r)
                continue
            return "%s %s" % (r, st or "unknown"), sealed
        return None, sealed

    def sealed_segments(self):
        _, sealed = self.occ_presence() if OCC else (None, set())
        return {seg for r in sealed for seg in ((OCC.get("seal") or {}).get(r) or {}).get("segments") or []}

    def request_clearance_specs(self):
        """Return validated per-segment specs, deriving them from HA's private passage graph when configured."""
        direct = OCC.get("request_clearance") or {}
        if direct:
            return direct.get("segments") if isinstance(direct.get("segments"), dict) else direct
        compiled = OCC.get("request_clearance_file")
        if compiled:
            try:
                path = Path(compiled).expanduser()
                stamp = path.stat().st_mtime_ns
                cache_key = ("compiled", stamp, OCC.get("request_clearance_key"))
                cached = getattr(self, "_passage_cache", None)
                if cached and cached[0] == cache_key:
                    self._passage_error = None
                    return cached[1]
                payload = json.loads(path.read_text())
                section = payload.get(OCC.get("request_clearance_key") or "") or {}
                specs = section.get("segments")
                if not isinstance(specs, dict) or set(specs) != set(SEGMENTS):
                    raise ValueError("robot segments do not exactly match the compiled clearance graph")
                aliases = {}
                if OCC.get("passage_graph"):
                    aliases = json.loads(Path(OCC["passage_graph"]).expanduser().read_text()).get("aliases") or {}
                normal = lambda room: aliases.get(room, room)
                for seg, spec in specs.items():
                    if not isinstance(spec, dict) or spec.get("complete") is not True:
                        raise ValueError("unvalidated segment " + seg)
                    rooms = spec.get("rooms") or []
                    if {normal(room) for room in SEG_ROOMS.get(seg, [])} != set(rooms):
                        raise ValueError("tracker rooms differ for " + seg)
                    if any(route not in specs for route in spec.get("route_segments") or []):
                        raise ValueError("unknown route segment for " + seg)
                    for passage in spec.get("passages") or []:
                        if not isinstance(passage, dict) or not isinstance(passage.get("room"), str):
                            raise ValueError("ambiguous passage for " + seg)
                        openings = passage.get("openings")
                        if not isinstance(openings, list) or not openings:
                            raise ValueError("passage has no physical openings for " + seg)
                        if any(not isinstance(opening, dict) or opening.get("from_room") not in rooms
                               for opening in openings):
                            raise ValueError("passage origin does not match " + seg)
                self._passage_cache = (cache_key, specs)
                self._passage_error = None
                return specs
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as ex:
                self._passage_error = "compiled clearance graph invalid: " + str(ex)
                return {}
        source = OCC.get("passage_graph")
        if not source:
            self._passage_error = "room passage topology is unavailable"
            return {}
        try:
            path = Path(source).expanduser()
            stamp = path.stat().st_mtime_ns
            cached = getattr(self, "_passage_cache", None)
            if cached and cached[0] == ("graph", stamp):
                self._passage_error = None
                return cached[1]
            data = json.loads(path.read_text())
            floor = OCC.get("floor")
            aliases = data.get("aliases") or {}
            segment_rooms = (data.get("segments") or {}).get(floor)
            if not isinstance(segment_rooms, dict) or set(segment_rooms) != set(SEGMENTS):
                raise ValueError("robot segments do not exactly match the validated graph")
            normal = lambda room: aliases.get(room, room)
            for seg in SEGMENTS:
                configured = {normal(room) for room in SEG_ROOMS.get(seg, [])}
                exported = set(segment_rooms.get(seg) or [])
                if not configured or configured != exported:
                    raise ValueError("tracker rooms differ for " + seg)
            floors = data.get("floors") or {}
            passages = data.get("passages")
            if not isinstance(passages, list) or not isinstance(floors, dict):
                raise ValueError("passage list or room floors missing")
            pairs, adjacent = set(), {}
            valid_s = float(data.get("contact_valid_seconds") or 0)
            for edge in passages:
                if not isinstance(edge, dict) or not isinstance(edge.get("a"), str) or not isinstance(edge.get("b"), str):
                    raise ValueError("ambiguous passage")
                a, b = normal(edge["a"]), normal(edge["b"])
                pair = frozenset((a, b))
                if len(pair) != 2 or pair in pairs:
                    raise ValueError("duplicate or self passage")
                pairs.add(pair)
                if floors.get(a) != floor or floors.get(b) != floor:
                    continue                 # robots do not cross floors, exterior doors or basement stairs
                contact = edge.get("contact")
                if contact is None:
                    opening = {"contact": None}
                elif isinstance(contact, dict) and contact.get("entity_id") and contact.get("node_status") and \
                        contact.get("last_seen") and valid_s > 0:
                    opening = {"contact": contact["entity_id"], "node_status": contact["node_status"],
                               "last_seen": contact["last_seen"], "valid_seconds": valid_s}
                else:
                    raise ValueError("incomplete contact evidence")
                adjacent.setdefault(a, []).append((b, opening))
                adjacent.setdefault(b, []).append((a, opening))
            specs = {}
            for seg, rooms in segment_rooms.items():
                target = set(rooms)
                grouped = {}
                for room in target:
                    if floors.get(room) != floor:
                        raise ValueError("segment crosses floors")
                    for neighbour, opening in adjacent.get(room, []):
                        if neighbour not in target:
                            grouped.setdefault(neighbour, []).append(opening)
                specs[seg] = {"complete": True, "rooms": sorted(target),
                              "route_segments": list(TRANSIT.get(seg) or []),
                              "passages": [{"room": room, "openings": openings}
                                           for room, openings in sorted(grouped.items())]}
            self._passage_cache = (("graph", stamp), specs)
            self._passage_error = None
            return specs
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as ex:
            self._passage_error = "passage graph invalid: " + str(ex)
            return {}

    def refresh_clear_observations(self, room_info, attrs, now):
        """Invalidate local quiet clocks before any room-level decision can return.

        HA publishes a fresh observed_at with each complete retained-occupancy snapshot.  A local clock may only span
        consecutive publications; repeated reads of one publication do not manufacture quiet time.
        """
        cache = getattr(self, "_request_clear_observed", None)
        if cache is None:
            cache = self._request_clear_observed = {}
        observed = attrs.get("observed_at")
        if type(observed) not in (int, float) or not 0 < observed <= now:
            # Direct/legacy trackers predate observed_at; their fresh lease and evaluation instant are the snapshot.
            observed = now
        observed = float(observed)
        previous = getattr(self, "_request_clear_snapshot", None)
        if previous is not None and (observed < previous or observed - previous > 45):
            cache.clear()
        for room in list(cache):
            info = room_info.get(room)
            if not isinstance(info, dict) or info.get("state") != "clear" or info.get("people"):
                cache.pop(room, None)
        self._request_clear_snapshot = observed
        return observed

    def observed_clear_since(self, room, info, now, observed):
        """Source clear time, or a restart-local consecutive-publication timer when HA has no historical `since`."""
        cache = self._request_clear_observed
        if info.get("state") != "clear" or info.get("people"):
            cache.pop(room, None)
            return None
        since = info.get("since")
        if type(since) in (int, float) and 0 < since <= now:
            cache[room] = {"first": float(since), "last": now}
            return float(since)
        previous = cache.get(room)
        first = previous["first"] if previous and 0 <= observed - previous["last"] <= 45 else observed
        cache[room] = {"first": first, "last": observed}
        verified_s = max(0, observed - first)
        return now - verified_s

    def request_clearance(self, segments, now=None, route_only=()):
        """Room-level vacancy for an explicit Clean-when-vacant request.

        This is deliberately narrower than ordinary autopilot admission. HA supplies and validates the private
        segment/room/passage graph; missing or ambiguous evidence falls back to whole-floor vacancy. An occupied
        adjacent room is isolated only when every physical opening to it has a positively closed healthy contact.
        """
        now = now or time.time()
        specs = self.request_clearance_specs()
        if not isinstance(specs, dict):
            return {"allowed": False, "quiet_s": 0, "why": "room passage topology is unavailable"}
        if not specs:
            return {"allowed": False, "quiet_s": 0,
                    "why": getattr(self, "_passage_error", None) or "room passage topology is unavailable"}
        tracker = self.get(OCC.get("sensor")) if OCC.get("sensor") else {}
        attrs = tracker.get("attributes") or {}
        try:
            fresh = tracker.get("state") == "ok" and float(attrs.get("valid_until") or 0) > now \
                and attrs.get("roster_complete") is True
        except (TypeError, ValueError):
            fresh = False
        if not fresh:
            self._request_clear_observed = {}
            self._request_clear_snapshot = None
            return {"allowed": False, "quiet_s": 0, "why": "presence tracker is stale or incomplete"}
        floor = (attrs.get("floors") or {}).get(OCC.get("floor")) or {}
        if floor.get("state") not in ("vacant", "occupied"):
            self._request_clear_observed = {}
            self._request_clear_snapshot = None
            return {"allowed": False, "quiet_s": 0, "why": "floor placement is ambiguous"}
        room_info = attrs.get("rooms") or {}
        observed = self.refresh_clear_observations(room_info, attrs, now)
        if observed is None:
            return {"allowed": False, "quiet_s": 0, "why": "presence tracker observation time is ambiguous"}
        full = set(segments)
        routes = set(route_only) - full
        needed = []
        for seg in dict.fromkeys(segments):
            spec = specs.get(seg)
            if not isinstance(spec, dict) or spec.get("complete") is not True:
                return {"allowed": False, "quiet_s": 0,
                        "why": "%s passage topology is missing or unvalidated" % seg}
            needed.append(seg)
            for route in spec.get("route_segments") or []:
                if route not in needed:
                    needed.append(route)
                    routes.add(route)
        for route in route_only:
            if route not in needed:
                needed.append(route)

        safe_since = []
        checked_rooms = set()
        for seg in needed:
            spec = specs.get(seg)
            if not isinstance(spec, dict) or spec.get("complete") is not True:
                return {"allowed": False, "quiet_s": 0,
                        "why": "%s route topology is missing or unvalidated" % seg}
            targets = spec.get("rooms") or []
            if not targets or any(not isinstance(room, str) for room in targets):
                return {"allowed": False, "quiet_s": 0, "why": "%s has no unambiguous tracker room" % seg}
            for room in targets:
                info = room_info.get(room)
                if not isinstance(info, dict) or info.get("state") != "clear" or info.get("people"):
                    return {"allowed": False, "quiet_s": 0, "why": "%s is not verifiably clear" % room}
                since = self.observed_clear_since(room, info, now, observed)
                if since is None:
                    return {"allowed": False, "quiet_s": 0, "why": "%s is not verifiably clear" % room}
                safe_since.append(since); checked_rooms.add(room)
            if seg in routes and seg not in full:
                continue                         # route rooms must be clear; their neighbours do not widen the target zone
            for passage in spec.get("passages") or []:
                if not isinstance(passage, dict) or not isinstance(passage.get("room"), str):
                    return {"allowed": False, "quiet_s": 0, "why": "%s has ambiguous adjacency" % seg}
                room = passage["room"]
                info = room_info.get(room)
                if not isinstance(info, dict):
                    return {"allowed": False, "quiet_s": 0, "why": "%s adjacency is unknown" % room}
                state, people = info.get("state"), info.get("people") or []
                if state == "clear" and not people:
                    since = self.observed_clear_since(room, info, now, observed)
                    if since is None:
                        return {"allowed": False, "quiet_s": 0, "why": "%s is not verifiably clear" % room}
                    safe_since.append(since); checked_rooms.add(room)
                    continue
                if state not in ("occupied", "still"):
                    return {"allowed": False, "quiet_s": 0, "why": "%s adjacency is %s" % (room, state or "unknown")}
                openings = passage.get("openings")
                if not isinstance(openings, list) or not openings:
                    return {"allowed": False, "quiet_s": 0, "why": "%s has an open passage to %s" % (seg, room)}
                for opening in openings:
                    if isinstance(opening, str):
                        opening = {"contact": opening}
                    contact = opening.get("contact") if isinstance(opening, dict) else None
                    healthy = opening.get("healthy") if isinstance(opening, dict) else None
                    if not contact:
                        return {"allowed": False, "quiet_s": 0,
                                "why": "%s has an unmonitored open passage to %s" % (seg, room)}
                    contact_state = self.get(contact)
                    if contact_state.get("state") != "off":
                        return {"allowed": False, "quiet_s": 0,
                                "why": "%s passage to %s is open or unavailable" % (seg, room)}
                    if opening.get("from_room"):
                        evidence = (attrs.get("door_contacts") or {}).get(contact)
                        expected_rooms = {opening["from_room"], room}
                        if (not isinstance(evidence, dict) or evidence.get("trusted_closed") is not True or
                                evidence.get("state") != "off" or set(evidence.get("rooms") or []) != expected_rooms):
                            return {"allowed": False, "quiet_s": 0,
                                    "why": "%s passage contact lacks trusted closure" % seg}
                        changed = epoch(contact_state) or now
                        operation = evidence.get("last_operation")
                        if type(operation) in (int, float) and 0 < operation <= now:
                            changed = max(changed, float(operation))
                        safe_since.append(changed)
                        continue
                    if healthy:
                        health_state = self.get(healthy)
                        if health_state.get("state") != "on":
                            return {"allowed": False, "quiet_s": 0,
                                    "why": "%s passage contact is not healthy" % seg}
                        safe_since.append(epoch(health_state) or now)
                    elif opening.get("node_status") or opening.get("last_seen"):
                        status = self.get(opening.get("node_status")) if opening.get("node_status") else {}
                        if status.get("state") not in ("alive", "awake", "asleep"):
                            return {"allowed": False, "quiet_s": 0,
                                    "why": "%s passage contact node is unavailable" % seg}
                        seen_state = self.get(opening.get("last_seen")) if opening.get("last_seen") else {}
                        seen_value = seen_state.get("state")
                        try:
                            seen_at = float(seen_value) if type(seen_value) in (int, float) else \
                                datetime.fromisoformat(str(seen_value).replace("Z", "+00:00")).timestamp()
                        except (TypeError, ValueError):
                            seen_at = 0
                        valid_s = float(opening.get("valid_seconds") or 0)
                        if not valid_s or not 0 <= now - seen_at < valid_s:
                            return {"allowed": False, "quiet_s": 0,
                                    "why": "%s passage contact report is stale" % seg}
                        safe_since.extend([epoch(status) or now, epoch(seen_state) or now])
                    safe_since.append(epoch(contact_state) or now)
                checked_rooms.add(room)
        if not safe_since:
            return {"allowed": False, "quiet_s": 0, "why": "no relevant room evidence"}
        since = max(safe_since)
        quiet = max(0, now - since)
        return {"allowed": True, "quiet_s": quiet, "since": since, "segments": needed,
                "rooms": sorted(checked_rooms),
                "why": "requested rooms and direct passages clear for %d of %d minutes" %
                       (quiet // 60, START_QUIET_S // 60)}

    def request_entry_risk(self, now):
        """Keep the existing floor-entry/approach guard without treating a remote resident as an intrusion."""
        risk = self.immediate_presence_risk(now, include_path=False)
        return risk if risk and risk.get("reason_code") in {
            "approach_motion", "approach_sensor_unavailable", "entry_door", "approach_room"} else None

    def requested_run_risk(self, now):
        """Current target/route intrusion for a room-cleared explicit request run."""
        entry = self.request_entry_risk(now)
        if entry:
            return entry
        groups = self.D.get("groups") or []
        i = min(int(self.D.get("gi") or 0), max(0, len(groups) - 1))
        rooms = (groups[i] if groups else {}).get("rooms") or []
        current = self.get(ROOM_SENSOR).get("state")
        if current in SEGMENTS and current not in rooms:
            route_only = [current]               # live route must be clear; its own neighbours do not widen the target
        elif self.get(VAC).get("state") in ("cleaning", "paused") and current not in SEGMENTS:
            return {"reason_code": "request_route_unknown", "reason": "robot route room is unknown",
                    "sensor": ROOM_SENSOR, "observed_at": now}
        else:
            route_only = []
        clearance = self.request_clearance(rooms, now, route_only=route_only)
        if clearance.get("allowed"):
            return None
        return {"reason_code": "request_zone_intrusion", "reason": clearance["why"],
                "sensor": OCC.get("sensor"), "observed_at": now}

    def requested_presence_pause(self, now):
        risk = self.requested_run_risk(now)
        previous = self.D.get("presence_pause") or {}
        if risk:
            risk.update(since=previous.get("since") or now, last_seen=now, clearing=False)
        else:
            risk = dict(previous, clearing=True) if previous.get("last_seen") and \
                now - previous["last_seen"] < RESUME_QUIET_S else {}
        self.D["presence_pause"] = risk
        return risk

    def closed_segments(self):
        """Robot rooms behind entrances that are all positively closed, occupied or not."""
        return {seg for seal in (OCC.get("seal") or {}).values()
                if seal.get("doors") and all(self.get(d).get("state") == "off" for d in seal["doors"])
                for seg in (seal.get("segments") or [])}

    def untidy_segments(self):
        st = self.g["derive"]()
        tidy = st.get("tidy_spaces") or {}
        out = set()
        for seg, rooms in SEG_ROOMS.items():
            for space in self.g["HOUSE"].get("tidy_spaces") or []:
                mapped = set(space.get("map_rooms") or [space["id"]])
                if mapped & set(rooms) and (tidy.get(space["id"]) or {}).get("state") == "untidy":
                    out.add(seg)
        return out

    def long_selection(self, supplied=None):
        """Current long-press admission: native whole floor, or safe one-segment-at-a-time fallback."""
        status = self.segment_status()
        closed = self.closed_segments()
        untidy = self.untidy_segments()
        accessible_untidy = untidy - closed
        blocked_other = set()
        for seg in SEGMENTS:
            if (status.get(seg) or {}).get("blocked") and (status.get(seg) or {}).get("block_kind") != "untidy":
                blocked_other.add(seg)
        accessible_exclusions = blocked_other - closed
        skipped = []
        if not accessible_untidy and not accessible_exclusions:
            rooms = []
            for seg in SEGMENTS:
                if seg not in closed and seg not in blocked_other:
                    rooms.append(seg)
            return rooms, True, skipped

        rooms = []
        for seg in supplied or list(SEGMENTS):
            why = (status.get(seg) or {}).get("why", "")
            if seg in closed:
                skipped.append(seg + " (closed off)")
            elif seg in blocked_other:
                skipped.append(seg + " (" + why + ")")
            elif seg in untidy:
                skipped.append(seg + " (untidy)")
            else:
                rooms.append(seg)
        return rooms, False, skipped

    def _remember_button_result(self, request_id, result, now=None):
        """Persist the complete reply so delivery retries cannot change a press into another action."""
        seen = self.D.setdefault("manual_seen", {})
        seen[request_id] = {"rooms": list(result.get("rooms") or []), "ts": now or time.time(),
                            "cancel": result.get("action") == "cancelled_and_docking",
                            "response": result}
        for old, _ in sorted(seen.items(), key=lambda x: x[1].get("ts", 0))[:-200]:
            seen.pop(old, None)

    def _button_work(self, vs, incl, now):
        """Committed/preparing work beats every Clean-button selection.

        A room merely requested for a future vacancy is deliberately absent here. A durable immediate FIFO item,
        one of our active phases, or device evidence of a real/manual/preparing clean is committed work.
        """
        D = self.D
        if D.get("phase", "idle") != "idle":
            return "planner phase " + D["phase"]
        if D.get("manual_queue"):
            return "an immediate clean is queued"
        if vs in ("cleaning", "paused", "returning"):
            return "robot is " + vs
        if incl == "on" and not self.leftover(vs, incl, now):
            return "robot has accepted or is preparing a task"
        return None

    def _stored_room_requests(self):
        if not REQUESTS:
            return {}
        try:
            values = json.loads(self.get(REQUESTS).get("state") or "{}")
        except (TypeError, ValueError):
            values = {}
        if not isinstance(values, dict):
            values = {}
        values = {seg: ts for seg, ts in values.items() if seg in SEGMENTS
                  and type(ts) in (int, float) and 0 < ts <= time.time() + 5}
        # An HA state read can briefly lag a successful input_text write.  Tombstones make an explicit map Off
        # immediately authoritative without suppressing a genuinely newer On from another mirrored display.
        removed = self.D.get("button_vacancy_removed") or {}
        values = {seg: ts for seg, ts in values.items() if ts > removed.get(seg, 0)}
        # A just-written HA input_text may not have propagated to the state reader yet. Keep the local mirror so a
        # second physical event a few milliseconds later is still atomic and sees the first press.
        for seg, ts in (self.D.get("button_vacancy_requests") or {}).items():
            if seg in SEGMENTS and type(ts) in (int, float) and 0 < ts <= time.time() + 5:
                values[seg] = max(ts, values.get(seg, 0))
        return values

    def _write_room_requests(self, rooms, add, now):
        """Update the same HA input_text consumed by map Clean-when-vacant, preserving other live requests."""
        if not REQUESTS:
            return False
        values = {seg: ts for seg, ts in self._stored_room_requests().items()
                  if not self.request_serviced(seg, ts)}
        for seg in rooms:
            if add:
                values[seg] = int(now)
            else:
                values.pop(seg, None)
        encoded = json.dumps(values, separators=(",", ":"), sort_keys=True)
        self.call("input_text.set_value", {"entity_id": REQUESTS, "value": encoded})
        self.D["button_vacancy_requests"] = values
        removed = self.D.setdefault("button_vacancy_removed", {})
        for seg in rooms:
            if add:
                removed.pop(seg, None)
            else:
                removed[seg] = now
        for seg, stamp in list(removed.items()):
            if now - stamp > 86400:
                removed.pop(seg, None)
        return True

    def _publish_button_result(self, result):
        """Reflect a map set-intent immediately instead of waiting for the next planner tick."""
        self.last_pub = None
        self.publish({"why": result["why"]}, self.get(VAC).get("state"), self.get(ROOM_SENSOR).get("state"))

    def _map_vacancy_set(self, gesture, rooms, request_id, now, body):
        want = gesture == "map_vacant_on"
        requested = self.room_requests()
        changed = [room for room in rooms if (room not in requested) == want]
        already = [room for room in rooms if room not in changed]
        if changed and not self._write_room_requests(changed, want, now):
            return {"ok": False, "action": "rejected", "rooms": [],
                    "why": "this floor has no Clean when vacant request store"}
        active, future_withdrawn = [], []
        if self.D.get("run_kind") == "request" and self.D.get("phase") != "idle":
            groups = self.D.get("groups") or []
            i = min(int(self.D.get("gi") or 0), max(0, len(groups) - 1))
            if self.D.get("phase") in ("running", "paused", "docking") and groups:
                active = [room for room in (groups[i].get("rooms") or []) if room in rooms]
            if not want and groups:
                cut = min(i + 1, len(groups))
                future = []
                for group in groups[cut:]:
                    removed = [room for room in (group.get("rooms") or []) if room in rooms]
                    future_withdrawn.extend(removed)
                    group["rooms"] = [room for room in (group.get("rooms") or []) if room not in rooms]
                    if group["rooms"]:
                        future.append(group)
                self.D["groups"] = groups[:cut] + future
                if self.D.get("phase") == "between" and i + 1 >= len(self.D["groups"]):
                    self.finish("done", now)
        word = "selected" if want else "deselected"
        why = "Clean when vacant %s for %s" % (word, ", ".join(changed)) if changed else \
            "Clean when vacant already %s for %s" % ("selected" if want else "off", ", ".join(rooms))
        if active and not want:
            why += "; already-active rooms finish their current run but will not remain requested"
        if future_withdrawn:
            why += "; unlaunched rooms were removed from this request run"
        result = {"ok": True, "action": gesture, "rooms": rooms, "changed": changed, "already": already,
                  "active_finishing": active, "future_withdrawn": sorted(set(future_withdrawn)), "why": why}
        self._remember_button_result(request_id, result, now)
        if want:
            self.D["manual_seen"][request_id]["map_rooms"] = sorted(self.confirmed_map_rooms(body, rooms))
        self.log("map_clean_when_vacant_set", request_id=request_id, on=want, rooms=rooms, changed=changed,
                 active_finishing=active, future_withdrawn=sorted(set(future_withdrawn)))
        self.save()
        self._publish_button_result(result)
        return result

    def _map_now_on(self, rooms, request_id, now):
        queue = self.D.setdefault("manual_queue", [])
        busy = self._button_work(self.get(VAC).get("state"), self.get(IN_CLEAN).get("state"), now)
        pending = {room for item in queue
                   for room in ((SEGMENTS if item.get("whole_floor") else item.get("rooms")) or [])}
        changed = [room for room in rooms if room not in pending]
        already = [room for room in rooms if room in pending]
        if changed:
            queue.append({"id": request_id, "gesture": "map_now_on", "source": "map", "rooms": changed,
                          "whole_floor": False, "native_requested": False, "one_at_time": False,
                          "created": now, "attempts": 0, "next_attempt": 0})
        why = ("Clean now queued for " + ", ".join(changed)) if changed else \
            ("Clean now already selected for " + ", ".join(rooms))
        if changed and busy:
            why += "; it will run behind current robot work"
        if already:
            why += "; already selected " + ", ".join(already)
        result = {"ok": True, "action": "map_now_on", "rooms": rooms, "changed": changed, "already": already,
                  "why": why}
        self._remember_button_result(request_id, result, now)
        self.log("map_clean_now_set", request_id=request_id, on=True, rooms=rooms, changed=changed)
        self.save()
        self._publish_button_result(result)
        return result

    def _map_now_off(self, rooms, request_id, now):
        D = self.D
        queue = D.setdefault("manual_queue", [])
        selected = set(rooms)
        active_id = D.get("manual_active") if D.get("run_kind") == "manual" and D.get("phase") != "idle" else None
        current = set()
        if active_id and D.get("groups"):
            i = min(int(D.get("gi") or 0), len(D["groups"]) - 1)
            current = set(D["groups"][i].get("rooms") or [])
        active_finishing = sorted(selected & current)
        changed, whole_floor = set(), set()
        kept = []
        for item in queue:
            item_rooms = set(item.get("rooms") or [])
            overlap = selected & (item_rooms or (set(SEGMENTS) if item.get("whole_floor") else set()))
            if item.get("whole_floor") and overlap:
                whole_floor.update(overlap)
                kept.append(item)       # indivisible native whole-floor work is the sole non-editable queue shape
                continue
            removed = item_rooms & selected
            changed.update(removed)
            if removed:
                item["rooms"] = [room for room in (item.get("rooms") or []) if room not in selected]
                if item.get("native_requested") and not item.get("whole_floor"):
                    # Once a native whole-floor request has become an editable segmented fallback, the first named
                    # withdrawal freezes that exact remainder.  A later topology/tidiness refresh must never widen it.
                    item["requested_rooms"] = list(item["rooms"])
                    item["native_requested"] = False
                elif isinstance(item.get("requested_rooms"), list):
                    item["requested_rooms"] = [room for room in item["requested_rooms"] if room not in selected]
            if item.get("rooms"):
                kept.append(item)
        D["manual_queue"] = kept

        # A launched command cannot be surgically edited.  Let its current group finish, but remove deselected rooms
        # from every not-yet-launched group so they cannot run later or retry.
        if active_id and D.get("groups"):
            cut = min(int(D.get("gi") or 0) + 1, len(D["groups"]))
            future = []
            for group in D["groups"][cut:]:
                group["rooms"] = [room for room in (group.get("rooms") or []) if room not in selected]
                if group["rooms"] or group.get("mode") == "whole_floor":
                    future.append(group)
            D["groups"] = D["groups"][:cut] + future
            if D.get("phase") == "between" and int(D.get("gi") or 0) + 1 >= len(D["groups"]):
                self.finish("done", now)

        unchanged = [room for room in rooms if room not in changed and room not in whole_floor]
        why = ("Clean now deselected for " + ", ".join(sorted(changed))) if changed else \
            ("Clean now was not selected for " + ", ".join(rooms))
        if active_finishing:
            why += "; already-active rooms finish their current pass, with future groups and retries removed"
        if whole_floor:
            why += "; native whole-floor work is unchanged because it cannot be edited room-by-room"
        result = {"ok": True, "action": "map_now_off", "rooms": rooms, "changed": sorted(changed),
                  "already": unchanged, "active_finishing": active_finishing,
                  "whole_floor_unchanged": sorted(whole_floor), "why": why}
        self._remember_button_result(request_id, result, now)
        self.log("map_clean_now_set", request_id=request_id, on=False, rooms=rooms, changed=sorted(changed),
                 active_finishing=active_finishing, whole_floor_unchanged=sorted(whole_floor))
        self.save()
        self._publish_button_result(result)
        return result

    def _cancel_button_work(self, request_id, now, supplied):
        """Forget committed planner work and make a best-effort dock command, without touching the other floor."""
        D = self.D
        active = [r for group in (D.get("groups") or []) for r in (group.get("rooms") or [])]
        queued = [r for job in (D.get("manual_queue") or []) for r in (job.get("rooms") or [])]
        rooms = list(dict.fromkeys(active + queued + list(supplied or [])))
        D["manual_queue"] = []
        if D.get("phase", "idle") != "idle":
            D.update(phase="cancelled", cancel_forget=True,
                     cancel_reason="physical Clean button stopped this floor")
        if rooms and REQUESTS:
            self._write_room_requests(rooms, False, now)
        self.call("vacuum.return_to_base", {"entity_id": VAC})
        result = {"ok": True, "action": "cancelled_and_docking", "rooms": rooms,
                  "why": "%s robot work cancelled; sending it home" % FLOOR.title()}
        self._remember_button_result(request_id, result, now)
        self.log("physical_clean_cancelled", request_id=request_id, rooms=rooms)
        self.save()
        return result

    def segment_rooms(self, seg, chores):
        return SEG_ROOMS.get(seg) or sorted({room for ch in chores if ch["id"] in SEG_CHORE.get(seg, ())
                                             for room in (ch.get("map_rooms") or ())})

    def confirmed_map_rooms(self, body, accepted):
        if body.get("map_room") is not None:
            return {body["map_room"]}
        selected = [seg for seg in accepted if not body.get("rooms") or seg in body["rooms"]]
        chores = (self.g["derive"]().get("config", {}).get("chores", [])
                  if any(seg not in SEG_ROOMS for seg in selected) else [])
        footprints = [set(self.segment_rooms(seg, chores)) for seg in selected]
        if body.get("gesture") == "map_vacant_on":
            # Older floorplans supplied only robot segments. Several can share
            # a unique tile, but one broad segment cannot identify the tapped tile.
            common = set.intersection(*footprints) if footprints else set()
            return common if len(common) == 1 else set()
        return set().union(*footprints)

    def button_request(self, body):
        """Apply the physical Clean button's idle→vacancy→now progression, idempotently."""
        gesture = str(body.get("gesture") or "")
        request_id = str(body.get("request_id") or "").strip()[:160]
        supplied = body.get("rooms")
        map_gestures = ("map_now_on", "map_now_off", "map_vacant_on", "map_vacant_off")
        if gesture not in ("single", "double", "long", "cancel", "maintenance_probe") + map_gestures:
            return {"ok": False, "rooms": [], "why": "unknown gesture"}
        if not request_id:
            return {"ok": False, "rooms": [], "why": "request_id is required"}
        if not isinstance(supplied, list) or any(not isinstance(r, str) for r in supplied):
            return {"ok": False, "rooms": [], "why": "rooms must be a list"}
        supplied = list(dict.fromkeys(supplied))
        unknown = [r for r in supplied if r not in SEGMENTS]
        if unknown:
            return {"ok": False, "rooms": [], "why": "unknown room: " + unknown[0]}
        tile = body.get("map_room")
        if tile is not None:
            if (gesture != "map_vacant_on" or not isinstance(tile, str) or not supplied or
                    any(tile not in self.confirmed_map_rooms({}, [seg]) for seg in supplied)):
                return {"ok": False, "rooms": [], "why": "room does not belong to the selected vacuum segments"}
        D = self.D
        D.setdefault("manual_queue", [])
        seen = D.setdefault("manual_seen", {})
        if request_id in seen:
            if seen[request_id].get("response"):
                return dict(seen[request_id]["response"])
            if seen[request_id].get("cancel"):
                return {"ok": True, "action": "cancelled_and_docking", "rooms": seen[request_id]["rooms"],
                        "why": "already cancelled"}
            return {"ok": True, "action": "clean_now_queued", "rooms": seen[request_id]["rooms"],
                    "why": "already queued"}
        now = time.time()
        if gesture == "cancel":
            if supplied:
                return {"ok": False, "rooms": [], "why": "floor cancellation takes no room selection"}
            return self._cancel_button_work(request_id, now, list(self.room_requests()))
        if gesture == "maintenance_probe" and supplied:
            return {"ok": False, "rooms": [], "why": "maintenance probe takes no room selection"}
        if gesture != "long" and not supplied:
            if gesture != "maintenance_probe":
                return {"ok": False, "rooms": [], "why": "this gesture selected no rooms"}

        # Floorplan controls are explicit idempotent set operations.  They never inherit the physical remote's
        # busy-floor cancel/dock behavior and never change the other kind of request.
        if gesture in map_gestures:
            if gesture.startswith("map_vacant_"):
                return self._map_vacancy_set(gesture, supplied, request_id, now, body)
            if gesture == "map_now_on":
                return self._map_now_on(supplied, request_id, now)
            return self._map_now_off(supplied, request_id, now)

        # Committed work has precedence over room accessibility. In particular, a long press on an active floor
        # must dock the robot even when every room in that long-press selection is currently protected or untidy.
        vs = self.get(VAC).get("state")
        incl = self.get(IN_CLEAN).get("state")
        busy = self._button_work(vs, incl, now)
        if busy:
            return self._cancel_button_work(request_id, now, supplied)
        if gesture == "maintenance_probe":
            result = {"ok": True, "action": "maintenance_allowed", "rooms": [],
                      "why": "%s robot has no committed or preparing work" % FLOOR.title()}
            self._remember_button_result(request_id, result, now)
            self.log("maintenance_allowed", request_id=request_id)
            self.save()
            return result

        skipped = []
        if gesture == "long":
            rooms, whole_floor, skipped = self.long_selection(supplied)
        else:
            rooms, whole_floor = supplied, False
        if not rooms:
            why = "no accessible allowed rooms"
            if skipped:
                why += ": " + "; ".join(skipped)
            return {"ok": False, "rooms": [], "why": why}

        requested = self.room_requests()
        if not all(room in requested for room in rooms):
            if not self._write_room_requests(rooms, True, now):
                return {"ok": False, "action": "rejected", "rooms": [],
                        "why": "this floor has no Clean when vacant request store"}
            result = {"ok": True, "action": "queued_when_vacant", "rooms": rooms,
                      "why": "queued for the next vacancy" +
                             ("; skipped " + ", ".join(skipped) if skipped else "")}
            self._remember_button_result(request_id, result, now)
            self.D["manual_seen"][request_id]["map_rooms"] = sorted(self.confirmed_map_rooms(body, rooms))
            self.log("physical_clean_when_vacant", request_id=request_id, gesture=gesture, rooms=rooms,
                     skipped=skipped)
            self.save()
            return result

        pending = {r for x in D["manual_queue"] for r in (x.get("rooms") or [])}
        already = [r for r in rooms if r in pending]
        existing_whole = any(x.get("whole_floor") or x.get("native_requested") for x in D["manual_queue"])
        queue_rooms = rooms if whole_floor else [r for r in rooms if r not in pending]
        append_item = bool(queue_rooms) and not (whole_floor and existing_whole)
        if append_item:
            item = {"id": request_id, "gesture": gesture, "source": "physical", "rooms": queue_rooms,
                    "whole_floor": whole_floor,
                    "native_requested": whole_floor, "requested_rooms": supplied if gesture == "long" else None,
                    "one_at_time": gesture == "long" and not whole_floor, "created": now,
                    "attempts": 0, "next_attempt": 0}
            D["manual_queue"].append(item)
        self.log("manual_queued" if append_item else "manual_deduplicated", request_id=request_id,
                 gesture=gesture, rooms=rooms, queued=queue_rooms if append_item else [], already=already,
                 skipped=skipped, position=len(D["manual_queue"]))
        self.save()
        behind = D.get("phase") != "idle" or self.get(VAC).get("state") not in ("docked", "idle", "charging") \
            or self.get(IN_CLEAN).get("state") == "on"
        why = ("queued whole floor" if whole_floor else "queued") if append_item else "already pending"
        if append_item:
            why += " behind current robot work" if behind else " next"
        if already:
            why += ("; already pending " if append_item else ": ") + ", ".join(already)
        if skipped:
            why += "; skipped " + ", ".join(skipped)
        result = {"ok": True, "action": "clean_now_queued", "rooms": rooms, "why": why}
        self._remember_button_result(request_id, result, now)
        self.save()
        return result

    def occ_quiet(self):
        now = time.time()
        who, _ = self.occ_presence()
        if who:
            self.D["occ_quiet_since"] = None
            return 0, who
        if not self.D.get("occ_quiet_since"):
            fl = ((self.get(OCC["sensor"]).get("attributes") or {}).get("floors") or {}).get(OCC["floor"]) or {}
            since = fl.get("since") if fl.get("state") == "vacant" else None
            self.D["occ_quiet_since"] = float(since) if isinstance(since, (int, float)) and since <= now else now
        return now - self.D["occ_quiet_since"], "presence tracker"

    def quiet_for(self, exclude=(), held=True):
        """seconds since the last person evidence downstairs (0 = someone there now), and its source.
        A held sensor's On is the moment it turned on, not someone there now; with held=False (a run under way, when
        the robot itself trips them and they stay On for half an hour) held sensors are left out."""
        if OCC:
            quiet, who = self.occ_quiet()
            for door, stamp in entry_edges.recent(time.time()).items():
                age = max(0, time.time() - stamp)
                if door in ENTRY_DOORS and age < quiet:
                    quiet, who = age, door
            return quiet, who
        now, q, who, seen = time.time(), 10 ** 9, None, 0
        for e, seg in SENSORS.items():
            if seg in exclude or (e in HELD and not held):
                continue
            s = self.get(e)
            st = s.get("state")
            if st == "on" and e in HELD:
                seen += 1
                if now - epoch(s) < q:
                    q, who = max(0, now - epoch(s)), e
                continue
            if st == "on":
                return 0, e
            if st == "off":
                seen += 1
                if now - epoch(s) < q:
                    q, who = now - epoch(s), e
        if seen < 3:                      # sensors down: never call that "empty"
            return 0, "sensors unavailable"
        for e, stamp in entry_edges.recent(now).items():
            if e in ENTRY_DOORS and max(0, now - stamp) < q:
                q, who = max(0, now - stamp), e
        for name, t in self.g["TVS"].items():
            if TV_IDS is not None and name not in TV_IDS:
                continue
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
        if OCC:
            who, _ = self.occ_presence(starting=False)
            if who or not ENTRY_DOORS:
                return who
        seen = D.setdefault("seen_on", {})
        trig = [t for t in D.setdefault("trig", []) if now - t[0] < 300]
        robot = self.robot_rooms(now)
        edges = entry_edges.recent(now)
        for e in list(SENSORS) + list(ENTRY_DOORS):
            st_ = self.get(e)
            buffered = edges.get(e, 0) if e in ENTRY_DOORS else 0
            if st_.get("state") != "on" and not buffered:
                continue
            t0 = max(buffered, epoch(st_) if st_.get("state") == "on" else 0)
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
                # Consume this arrival once, even if the door stays open or
                # the robot resumes within the path-evidence window.
                D["trig"] = [t for t in trig if not (t[0] == t0 and t[1] == e)]
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

    def immediate_presence_risk(self, now, include_path=False):
        """The earliest reason somebody may enter this floor. This is intentionally more conservative than a
        confirmed arrival: the camera lease and the vacuum both yield before a person can reach the floor."""
        for entity in sorted(APPROACH_SENSORS):
            state = self.get(entity)
            value = state.get("state")
            if value == "on":
                return {"reason_code": "approach_motion", "reason": "stairs/approach motion (%s)" % entity,
                        "sensor": entity, "observed_at": epoch(state) or now}
            if value in (None, "unknown", "unavailable"):
                return {"reason_code": "approach_sensor_unavailable",
                        "reason": "stairs/approach sensor unavailable (%s)" % entity,
                        "sensor": entity, "observed_at": now}
        edges = entry_edges.recent(now)
        for entity in sorted(ENTRY_DOORS):
            state = self.get(entity)
            stamp = edges.get(entity, 0)
            if state.get("state") == "on" or 0 <= now - stamp <= RESUME_QUIET_S:
                self.D.setdefault("seen_on", {})[entity] = max(
                    self.D.setdefault("seen_on", {}).get(entity, 0), stamp, epoch(state))
                return {"reason_code": "entry_door", "reason": "entry door opened (%s)" % entity,
                        "sensor": entity, "observed_at": max(stamp, epoch(state)) or now}
        if OCC:
            tracker = self.get(OCC["sensor"])
            attrs = tracker.get("attributes") or {}
            for room in sorted(APPROACH_ROOMS):
                info = (attrs.get("rooms") or {}).get(room) or {}
                state = info.get("state")
                if state not in (None, "clear", "vacant"):
                    return {"reason_code": "approach_room",
                            "reason": "stairs/approach room %s is %s" % (room, state),
                            "sensor": OCC["sensor"], "observed_at": float(info.get("since") or now)}
            who, _ = self.occ_presence(starting=True)
            if who:
                uncertain = any(word in who for word in ("unknown", "uncertain", "unavailable"))
                return {"reason_code": "floor_uncertain" if uncertain else "presence_detected",
                        "reason": ("floor placement uncertain: " if uncertain else "presence detected: ") + who,
                        "sensor": OCC["sensor"], "observed_at": now}
        if include_path:
            who = self.person_entered(now)
            if who:
                sensor = who if who in SENSORS or who in ENTRY_DOORS else None
                return {"reason_code": "person_path", "reason": "person path: " + who,
                        "sensor": sensor, "observed_at": now}
        return None

    def presence_pause(self, now, include_path=False):
        """Latch the shared camera/vacuum pause signal through the same quiet debounce used before resuming."""
        risk = self.immediate_presence_risk(now, include_path)
        previous = self.D.get("presence_pause") or {}
        if risk:
            risk.update(since=previous.get("since") or risk["observed_at"], last_seen=now, clearing=False)
        else:
            if OCC:
                self.occ_quiet()  # begin the resume timer while the privacy signal finishes its own debounce
            risk = dict(previous, clearing=True) if previous.get("last_seen") and \
                now - previous["last_seen"] < RESUME_QUIET_S else {}
        self.D["presence_pause"] = risk
        return risk

    def publish_presence_pause(self, risk, now):
        attrs = {"friendly_name": "%s presence pause" % FLOOR.title(), "icon": "mdi:motion-sensor",
                 "floor": FLOOR, "vacuum_entity": VAC, "reason_code": risk.get("reason_code"),
                 "reason": risk.get("reason"), "sensor": risk.get("sensor"),
                 "since": int(risk["since"]) if risk.get("since") else None,
                 "observed_at": int(risk["observed_at"]) if risk.get("observed_at") else None,
                 "clearing": bool(risk.get("clearing")), "published_at": int(now)}
        state = "on" if risk else "off"
        signature = json.dumps([state, {k: v for k, v in attrs.items() if k != "published_at"}], sort_keys=True)
        if signature == getattr(self, "last_pause_pub", None) and now - self.D.get("pause_pub_at", 0) < 30:
            return
        try:
            self.g["ha_post_state"](PRESENCE_PAUSE_PUBLISH, state, attrs, self.tok)
            self.last_pause_pub, self.D["pause_pub_at"] = signature, now
        except Exception as ex:  # noqa: BLE001
            print("vacuum presence-pause publish failed", ex, flush=True)

    def leftover(self, vs, incl, now):
        """Docked LEFTOVER_S+, battery LEFTOVER_BATTERY+, and the cleaning flag still on: the robot keeps an unfinished
        task it will never resume by itself (seen 11-17: a paused/resumed segment run docked at 19:52 and the flag stayed
        on all night). That is not "busy": the run is over and a new one may start."""
        if vs != "docked" or incl != "on":
            return False
        try:
            full = not BATTERY or float(self.get(BATTERY).get("state") or 0) >= LEFTOVER_BATTERY
        except (TypeError, ValueError):
            full = False
        return full and now - epoch(self.get(VAC)) >= LEFTOVER_S

    def eligible(self):
        sealed = self.sealed_segments()
        return [seg for seg, d in self.segment_status().items() if d["eligible"] and seg not in sealed]

    def remaining_rooms(self):
        """Segments not yet proved complete in the current run.

        A group is complete only after the robot returns to its dock normally. Merely spending
        DWELL_S in a room is useful visit history, but it is not proof that an interrupted
        segment clean finished.
        """
        groups = self.D.get("groups") or []
        i = int(self.D.get("gi") or 0)
        if self.D.get("phase") == "between":
            i += 1
        return list(dict.fromkeys(r for g in groups[i:] for r in (g.get("rooms") or [])))

    def completed(self, rooms):
        """Remove successfully completed segments from the persistent retry set."""
        done = set(rooms)
        self.D["incomplete"] = [r for r in (self.D.get("incomplete") or []) if r not in done]
        if not self.D["incomplete"]:
            self.D.pop("incomplete_since", None)
            self.D.pop("incomplete_why", None)

    def remember_incomplete(self, rooms, why, now):
        """Keep interrupted work visible and eligible even if dwell tracking credited a visit."""
        rooms = list(dict.fromkeys(rooms))
        if not rooms:
            return
        old = self.D.get("incomplete") or []
        self.D["incomplete"] = list(dict.fromkeys(old + rooms))
        self.D["incomplete_since"] = self.D.get("incomplete_since") or now
        self.D["incomplete_why"] = why

    def room_requests(self):
        """A persisted, explicit request is satisfied only by a later real visit.

        HA owns the small request helper. We never clear it here, avoiding a
        read/clear race with a person queuing another room during this tick.
        """
        return {seg: ts for seg, ts in self._stored_room_requests().items()
                if not self.request_serviced(seg, ts)}

    def segment_status(self):
        """Per robot segment: the autopilot's real decision and why (for dashboards).
        Goals (house.json vacuum.schedule): some rooms nightly (after a time; optionally only if dinner was eaten
        there, per a motion sensor), everything else every `every_h` hours (default 72). A freshly passed tidy also
        counts (the floor is clear). With a room-cleaning evidence source, confirmed manual and automatic
        cleans reset the clock. Never while a kid's job there is in progress or waiting for a check."""
        st = self.g["derive"]()
        b = st["board"]
        chores = st.get("config", {}).get("chores", [])
        busy = {a.get("chore") for a in st["active"].values()} | {z.get("chore") for z in st["paused"].values()}
        now = time.time()
        requests = self.room_requests()
        incomplete = set(self.D.get("incomplete") or [])
        all_spaces = self.g["HOUSE"].get("tidy_spaces") or []
        tidy = st.get("tidy_spaces") or {}
        out = {}
        for seg in list(dict.fromkeys(SEGMENTS + list(SEG_CHORE) + list(SCHEDULE))):
            cids = list(SEG_CHORE.get(seg) or [])
            # the map rooms this robot area really covers: explicit in house.json (a kids' job can span several robot
            # areas, e.g. "front hall + dining"), else derived from its jobs
            rooms = self.segment_rooms(seg, chores)
            confirmed = self.D["visits"].get(seg, 0)
            last = max(confirmed, self.D.get("prior_schedule_dates", {}).get(seg, 0))
            sch = SCHEDULE.get(seg) or {}
            d = {"chores": cids, "rooms": rooms, "last_robot": (confirmed if not getattr(self, "room_cleaning_sensor", None) or "room_credit_since" in self.D else 0), "mop": seg in MOP, "eligible": False}
            if getattr(self, "room_cleaning_sensor", None) and "room_credit_since" in self.D:
                d["last_completed"] = confirmed
            d["requested"] = seg in requests
            d["incomplete"] = seg in incomplete
            if d["incomplete"]:
                d["incomplete_since"] = self.D.get("incomplete_since")
                d["incomplete_why"] = self.D.get("incomplete_why")
            d["blocked"] = False
            xs = [b.get(c) or {} for c in cids]
            tidied = max([x.get("last_clean") or 0 for x in xs] or [0])
            d["tidied"] = tidied
            every = float(sch.get("every_h", EVERY_H)) * 3600
            d["next_due"] = last + every if last else now
            untidy = [sp["name"] for sp in all_spaces if set(sp.get("map_rooms") or [sp["id"]]) & set(rooms)
                      and (tidy.get(sp["id"]) or {}).get("state") == "untidy"]
            messy = untidy if UNTIDY_BLOCKS else []
            if any(c in busy for c in cids) or any(x.get("pending_by") for x in xs):
                d["block_kind"] = "chore"
                d["why"] = "a kid's job here is in progress or waiting for a check"
                d["blocked"] = True
            elif GUESTS.get(seg) and self.get(GUESTS[seg]).get("state") == "on":
                d["block_kind"] = "guest"
                d["why"] = "a guest is staying here"
                d["blocked"] = True
            elif seg in SLEEP_PROTECTION and self.get(SLEEP_PROTECTION[seg]).get("state") != "off":
                d["block_kind"] = "sleep"
                d["why"] = "waiting for the room's sleep protection to clear"
                d["blocked"] = True
            elif messy:
                d["block_kind"] = "untidy"
                d["why"] = "marked untidy (%s): tidy it first" % ", ".join(messy)
                d["blocked"] = True
            elif seg in incomplete:
                d["eligible"], d["why"] = True, "unfinished from an interrupted run"
            elif seg in requests:
                clearance = self.request_clearance([seg], now)
                d["eligible"] = True
                d["request_clearance"] = clearance
                d["why"] = "requested: " + (clearance["why"] if clearance.get("allowed") else
                                                "waiting for whole-floor vacancy; " + clearance["why"])
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
                if seg in requests and (d.get("request_clearance") or {}).get("allowed"):
                    d["why"] += " · may run while occupied elsewhere after 20 clear minutes"
                else:
                    d["why"] += " · goes next time the floor is empty 20 min"
            elif EXPLORE and untidy and (not d["blocked"] or messy and d["why"].startswith("marked untidy")):
                d["explore"] = True   # untidy, and not held back for any other reason
                d["why"] += " · the robot may explore it to check tidiness while the kids are home"
            out[seg] = d

        # A target can be tidy itself yet physically unreachable without crossing another blocked robot room.
        # `access_through` names only unavoidable transit rooms; alternate routes therefore stay usable. Propagate
        # transit blocks so a closet behind a blocked room behind another blocked room is also held. The dock/hub
        # room is deliberately not implicit: it should simply be omitted from access_through.
        for _ in range(len(out)):
            changed = False
            for seg, through in TRANSIT.items():
                if seg not in out or out[seg].get("blocked"):
                    continue
                blocker = next((name for name in through if (out.get(name) or {}).get("blocked")), None)
                if not blocker:
                    continue
                source = out[blocker]
                root = source.get("transit_root") or blocker
                kind = source.get("transit_block_kind") or source.get("block_kind")
                detail = {
                    "untidy": "is marked untidy",
                    "guest": "has a guest staying there",
                    "sleep": "is protected for sleep",
                    "chore": "has a kid's job in progress or waiting for a check",
                }.get(kind, "is blocked")
                row = out[seg]
                row.update(eligible=False, blocked=True, block_kind="transit", blocked_by=blocker,
                           transit_root=root, transit_block_kind=kind,
                           why="behind %s, which %s" % (root, detail))
                row.pop("explore", None)
                changed = True
            if not changed:
                break
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
            self.g["ilog"]({"src": LOG_SRC, "action": "dinner_check_failed", "error": repr(ex)})
            return False
        ate = dinner_from_rows(rows, q0.timestamp(), min(time.time(), q1.timestamp()), w0.timestamp(), w1.timestamp(), gap, need)
        cache[key] = ate
        self.D["dinner_ts"] = time.time()
        for k in sorted(cache)[:-10]:
            cache.pop(k)
        return ate

    # --- robot room tracking (always on, manual runs count too) -------------------------------
    def update_room_cleaning(self, room, now):
        """Only a confirmed completed job can refresh the room's cleaning date."""
        row = self.get(self.room_cleaning_sensor) or {}
        attrs = row.get("attributes") or {}
        rooms = attrs.get("rooms")
        if (row.get("state") != "ready" or not isinstance(rooms, dict)
                or not isinstance(attrs.get("updated_at"), (int, float)) or not 0 <= now - attrs["updated_at"] < 180):
            return 0
        if "room_credit_since" not in self.D:
            # Carry pre-migration schedule dates through their original due date.
            # They are never exposed as confirmed cleans or used for new requests.
            self.D["prior_schedule_dates"] = dict(self.D["visits"])
            self.D["visits"] = {}
            self.D["room_credit_since"] = now
        for name, info in rooms.items():
            if name not in SEGMENTS or not isinstance(info, dict):
                continue
            finished = info.get("last_completed")
            if type(finished) in (int, float) and self.D["visits"].get(name, 0) < finished <= now:
                self.D["visits"][name] = finished
                since = self.D.get("incomplete_since")
                if since and finished >= since and name in (self.D.get("incomplete") or []):
                    self.completed([name])
        # The native interruption record can arrive after a return to dock was
        # provisionally recorded as normal. Restore only this run's requested rooms.
        run = (self.D.get("runs") or [{}])[-1]
        if run.get("kind") == "auto" and run.get("why") in ("done", "robot reported interrupted cleaning"):
            targeted = [name for group in run.get("groups", []) for name in group.get("rooms", [])]
            interrupted = []
            for name in targeted:
                last = (rooms.get(name) or {}).get("last_job") or {}
                begin, end = last.get("begin"), last.get("end")
                if (last.get("targeted") is True and last.get("outcome") == "incomplete"
                        and type(begin) in (int, float) and type(end) in (int, float)
                        and run["start"] - 2 <= begin <= end <= run["end"] + 2
                        and self.D["visits"].get(name, 0) < end
                        and (run["why"] == "done" or name not in (self.D.get("incomplete") or []))):
                    interrupted.append(name)
            if interrupted:
                run["why"] = "robot reported interrupted cleaning"
                self.remember_incomplete(interrupted, run["why"], run["end"])
                self.log("native_interruption", rooms=interrupted)
        job = (rooms.get(room) or {}).get("current_job") or {}
        return job.get("seconds", 0) if job.get("targeted") else 0

    def request_serviced(self, seg, issued):
        last = self.D["visits"].get(seg, 0)
        if issued <= self.D.get("room_credit_since", 0):
            last = max(last, self.D.get("prior_schedule_dates", {}).get(seg, 0))
        return last >= issued

    def track(self, vs, room, now):
        """credit a room once the robot has spent DWELL_S actually CLEANING in it (pauses/returns don't count)"""
        D = self.D
        verified = bool(getattr(self, "room_cleaning_sensor", None))
        if verified:
            measured = self.update_room_cleaning(room, now)
        elif D.get("t_vs") == "cleaning" and D.get("cur"):
            D["cur_clean"] = D.get("cur_clean", 0) + min(now - D.get("t_last", now), 3600)
        D["t_last"], D["t_vs"] = now, vs
        cur = D.get("cur")
        if (vs == "cleaning" or (vs == "paused" and cur)) and room and room != cur:
            if not verified and cur and D.get("cur_clean", 0) >= DWELL_S:
                D["visits"][cur] = now
            D["prev"], D["prev_left"] = cur, now
            D["cur"], D["cur_clean"] = room, 0
        elif vs in ("docked", "returning", "idle", "error") and cur:
            if not verified and D.get("cur_clean", 0) >= DWELL_S:
                D["visits"][cur] = now
            D["prev"], D["prev_left"], D["cur"], D["cur_clean"] = cur, now, None, 0

        if verified:
            D["cur_clean"] = measured if D.get("cur") else 0

    def robot_rooms(self, now):
        ex = {self.D.get("cur")}
        if self.D.get("prev") and now - self.D.get("prev_left", 0) < PREV_GRACE_S:
            ex.add(self.D["prev"])
        return ex - {None}

    # --- runs ------------------------------------------------------------------------------------
    def smart_now(self):
        if SMART == "always":
            return True
        h = datetime.fromtimestamp(time.time()).hour
        return SMART == "day" and not (h >= NIGHT[0] or h < NIGHT[1])

    def launch(self, now):
        gr = self.D["groups"][self.D["gi"]]
        if gr["mode"] == "whole_floor":
            self.call("vacuum.start", {"entity_id": VAC})
            self.D.update(group_started=now, saw_clean=False, device_task_acknowledged=False, fan=None, docked_since=0)
            self.log("launch", rooms=[], mode="whole_floor")
            return
        fan = "smart_mode" if gr["mode"] == "smart_mode" else self.tier()
        self.call("select.select_option", {"entity_id": MODE_SEL, "option": gr["mode"]})
        self.call("vacuum.set_fan_speed", {"entity_id": VAC, "fan_speed": fan})
        self.call(CLEAN_SCRIPT, {"vacuum": VAC, "map_flag": MAP_FLAG, "rooms": gr["rooms"]})
        self.D.update(group_started=now, saw_clean=False, device_task_acknowledged=False, fan=fan, docked_since=0)
        self.log("launch", rooms=gr["rooms"], mode=gr["mode"], fan=fan)

    def explore_rooms(self, on, now):
        """rooms for a tidiness check now, or []: the autopilot is on, the kids are here today and tomorrow,
        it's the afternoon on a school day (or from the morning on a day off), not past the evening cut-off or
        bedtime, and no tidiness check yet today"""
        if not (EXPLORE and on):
            return []
        t = datetime.fromtimestamp(now)
        if self.D.get("explore_day") == t.date().isoformat():
            return []
        if not (schedule.here_on(t.date()) and schedule.here_on(t.date() + timedelta(days=1))):
            return []
        off = schedule.off_on(t.date())
        free = t.weekday() >= 5 or bool(off)
        start_h = EXPLORE.get("free_from_h", 9) if free else EXPLORE.get("school_from_h", 15)
        if not (start_h <= t.hour < EXPLORE.get("until_h", 19)):
            return []
        if (self.g["derive"]().get("bedtime") or {}).get("on"):
            return []
        homes = [k["home_entity"] for k in (self.g["cfg"]()["kids"] if self.g.get("cfg") else []) if k.get("home_entity")]
        if homes and not any(self.get(e).get("state") == "on" for e in homes):
            return []   # the calendar says they're here, but nobody is actually home to rescue it
        sealed = self.sealed_segments()
        return [seg for seg, d in self.segment_status().items() if d.get("explore") and seg not in sealed]

    def start(self, rooms, now, explore=False, run_kind="auto", request_id=None, whole_floor=False,
              one_at_time=False):
        a = self.get(VAC).get("attributes") or {}
        self.D["saved"] = {"fan": a.get("fan_speed"), "mode": self.get(MODE_SEL).get("state")}
        vac = [r for r in rooms if r not in MOP]
        mop = [r for r in rooms if r in MOP]
        if whole_floor:
            groups = [{"mode": "whole_floor", "rooms": []}]
        elif one_at_time:
            smart = self.smart_now()
            groups = [{"mode": "smart_mode" if smart else ("vac_and_mop" if r in MOP else "vacuum"), "rooms": [r]}
                      for r in rooms]
        else:
            groups = ([{"mode": "vacuum", "rooms": vac}] if vac else []) + \
                     ([{"mode": "vac_and_mop", "rooms": mop}] if mop else [])
        if not whole_floor and not one_at_time and self.smart_now():   # robot chooses vacuum/mop per room, one pass
            groups = [{"mode": "smart_mode", "rooms": list(rooms)}]
        self.D.update(phase="running", groups=groups, gi=0, run_started=now, paused_at=0, pauses=[], trouble=None,
                      clean_first=0, clean_last=0, saw_clean=False, docked_since=0, docking_since=0,
                      resume_fallback=False, explore=explore, run_kind=run_kind,
                      manual_active=request_id if run_kind == "manual" else None)
        if run_kind == "request":
            # Admission may occur while the floor-level pause signal names a remote resident. Do not carry that
            # broader pre-start latch into the room-scoped run; the next tick rebuilds it from target/route evidence.
            self.D["presence_pause"] = {}
        self.call("input_boolean.turn_on", {"entity_id": ACTIVE})
        self.log("start", rooms=rooms, saved=self.D["saved"], explore=explore,
                 run_kind=run_kind, request_id=request_id, whole_floor=whole_floor,
                 one_at_time=one_at_time)
        self.launch(now)

    def refresh_native_request(self, item):
        """A native long press can become a segmented fallback while it waits in the FIFO."""
        rooms, whole_floor, skipped = self.long_selection(item.get("requested_rooms") or [])
        item.update(rooms=rooms, whole_floor=whole_floor, one_at_time=not whole_floor,
                    launch_blocked=None if rooms else "no accessible allowed rooms")
        self.log("manual_rechecked", request_id=item.get("id"), rooms=rooms,
                 whole_floor=whole_floor, skipped=skipped)
        self.save()

    def room_holds(self, rooms, explicit):
        closed = self.closed_segments()
        status = self.segment_status()
        untidy = set() if explicit else self.untidy_segments()
        holds = {}
        for name in rooms:
            rule = status.get(name) or {}
            if name in closed:
                holds[name] = name + ": all monitored entrances are closed"
            elif rule.get("blocked") and rule.get("block_kind") != "untidy":
                holds[name] = name + ": " + rule["why"]
            elif name in untidy:
                holds[name] = name + ": marked untidy"
        return holds

    def manual_wait(self, vs, incl, now):
        """Why the FIFO head cannot launch yet; None means the robot is genuinely free."""
        q = self.D.get("manual_queue") or []
        if not q:
            return None
        item = q[0]
        if now < item.get("next_attempt", 0):
            return "retrying at %s" % datetime.fromtimestamp(item["next_attempt"]).strftime("%H:%M")
        if vs not in ("docked", "idle", "charging"):
            return "waiting for current robot work (%s)" % vs
        if incl == "on":
            return "waiting for the robot's current task to finish its dock break"
        if ERROR and self.get(ERROR).get("state") not in ("none", None):
            return "waiting for robot error to clear"
        try:
            if BATTERY and float(self.get(BATTERY).get("state") or 0) < 50:
                return "charging to 50% before the next queued clean"
        except (TypeError, ValueError):
            return "waiting for a valid battery reading"
        if item.get("gesture") != "long":
            return next(iter(self.room_holds(item["rooms"], explicit=True).values()), None)
        if item.get("attempts"):
            quiet, _ = self.quiet_for()
            if quiet is None or quiet < START_QUIET_S:
                return "waiting for %s to be empty before retrying" % FLOOR
        return None

    def finish_manual(self, ok, why, now):
        D = self.D
        request_id = D.get("manual_active")
        queue = D.setdefault("manual_queue", [])
        item = next((x for x in queue if x.get("id") == request_id), None)
        if not item:
            return
        if ok or D.get("saw_clean") or D.get("device_task_acknowledged") or D.get("trouble"):
            D["manual_queue"] = [x for x in queue if x.get("id") != request_id]
            self.log("manual_done" if ok else "manual_interrupted", request_id=request_id,
                     rooms=item.get("rooms"), why=why)
            return
        item["attempts"] = int(item.get("attempts") or 0) + 1
        waits = (5 * 60, 15 * 60, 30 * 60, 60 * 60)
        item["next_attempt"] = now + waits[min(item["attempts"], len(waits)) - 1]
        item["last_failure"] = why
        self.log("manual_retry", request_id=request_id, rooms=item.get("rooms"), attempts=item["attempts"],
                 next_attempt=item["next_attempt"], why=why)

    def finish(self, why, now):
        D = self.D
        run_kind = D.get("run_kind") or "auto"
        if (why == "done" or why.startswith("docked with the robot's task")) and "clean_first" in D:   # (a run from before this rule: not judged)
            span = (D.get("clean_last") or 0) - (D.get("clean_first") or 0)
            if span < FAILED_START_S:   # it set off and gave up at once (e.g. a map that no longer matches the house)
                other = self.other_map()
                why = "started %s and gave up after %d s%s" % (
                    datetime.fromtimestamp(D.get("run_started") or now).strftime("%H:%M"), max(0, span),
                    "; the robot went back to its %r map" % other.strip() if other else "")
                if run_kind != "manual":
                    n = D.get("fail_streak", 0) + 1
                    D.update(fail_streak=n, backoff_until=now + BACKOFF_S[min(n, len(BACKOFF_S)) - 1] if BACKOFF else 0,
                             failed_at=now)
                    self.log("failed_start", span=span, streak=n, map_other=other)
            elif run_kind != "manual":
                D.update(fail_streak=0, backoff_until=0)
        groups = D.get("groups") or []
        run_rooms = list(dict.fromkeys(r for g in groups for r in (g.get("rooms") or [])))
        cancel_forget = bool(D.pop("cancel_forget", False))
        if run_kind == "manual":
            self.finish_manual(why == "done", why, now)
        elif not D.get("explore") and not cancel_forget:
            if why == "done":
                self.completed(run_rooms)
            else:
                self.remember_incomplete(self.remaining_rooms(), why, now)
        if D.get("explore"):
            rooms = ", ".join(r for g in (D.get("groups") or []) for r in g.get("rooms", []))
            why = "🔍 tidiness check of %s: %s" % (rooms, "the robot made it through (floor passable)" if why == "done" else
                                                  "the robot got stuck: untidy confirmed, the kids rescue it"
                                                  if D.get("trouble") == "error" else why)
            D["explore"] = False
        sv = self.D.get("saved") or {}
        if sv.get("mode") in ("vacuum", "vac_and_mop", "mop", "custom", "smart_mode"):
            self.call("select.select_option", {"entity_id": MODE_SEL, "option": sv["mode"]})
        if sv.get("fan"):
            self.call("vacuum.set_fan_speed", {"entity_id": VAC, "fan_speed": sv["fan"]})
        self.call("input_boolean.turn_off", {"entity_id": ACTIVE})
        self.D["runs"] = (self.D["runs"] + [{"start": self.D.get("run_started"), "end": now, "why": why,
                                             "groups": self.D.get("groups"), "pauses": self.D.get("pauses"),
                                             "kind": run_kind, "request_id": self.D.get("manual_active")}])[-30:]
        self.D.update(phase="idle", groups=[], gi=0, paused_at=0, trouble=None,
                      run_kind=None, manual_active=None)
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
        st_ = self.get(STATUS).get("state") if STATUS else None
        if st_ in HANDS_OFF:   # e.g. mapping the floor: no command of any kind (no pause, dock, fan, voice, start)
            pause_signal = self.presence_pause(now)
            self.publish_presence_pause(pause_signal, now)
            if D.get("hands_off") != st_:
                D["hands_off"] = st_
                self.log("hands_off", status=st_, phase=D["phase"])
            self.publish({"why": "robot is %s: hands off until it's done" % st_}, self.get(VAC).get("state"), None)
            self.save()
            return
        if D.get("hands_off"):
            self.log("hands_off_over", status=st_)
            D["hands_off"] = None
            D["docked_since"] = 0
        self.night_voice()
        v = self.get(VAC)
        vs = v.get("state")
        self.pin_map(vs, now)
        room = self.get(ROOM_SENSOR).get("state")
        incl = self.get(IN_CLEAN).get("state")
        self.track(vs, room, now)
        self.return_lights(vs, now)
        self.dinner_reopens()
        ph = D["phase"]
        request_run = D.get("run_kind") == "request"
        pause_signal = self.requested_presence_pause(now) if request_run else self.presence_pause(
            now, include_path=ph == "running" and vs == "cleaning" and D.get("run_kind") != "manual")
        self.publish_presence_pause(pause_signal, now)
        info = {}
        if ph == "idle":
            # Physical button work is a separate durable FIFO. It always gets first admission, but it never
            # interrupts an autopilot run, a different manual clean, a pause, or a recharge/mop-wash dock break.
            manual = D.setdefault("manual_queue", [])
            while manual and not manual[0].get("whole_floor"):
                item = manual[0]
                serviced = [room for room in (item.get("rooms") or [])
                            if D.get("visits", {}).get(room, 0) >= item.get("created", now)]
                if not serviced:
                    break
                item["rooms"] = [room for room in item.get("rooms", []) if room not in serviced]
                self.log("named_clean_now_already_serviced", request_id=item.get("id"), rooms=serviced)
                if item["rooms"]:
                    break
                manual.pop(0)
            if manual:
                item = manual[0]
                wait = self.manual_wait(vs, incl, now)
                if not wait and item.get("gesture") == "long" and not item.get("native_requested"):
                    holds = self.room_holds(item["rooms"], explicit=False)
                    allowed = [r for r in item["rooms"] if r not in holds]
                    if allowed:
                        item["rooms"] = allowed
                    else:
                        wait = "no selected rooms currently available"
                if not wait and item.get("native_requested"):
                    self.refresh_native_request(item)
                    wait = item.get("launch_blocked")
                info.update(manual_queue=[{"id": x.get("id"), "gesture": x.get("gesture"),
                                          "source": x.get("source"), "rooms": x.get("rooms"),
                                          "attempts": x.get("attempts", 0), "whole_floor": bool(x.get("whole_floor"))}
                                         for x in manual],
                            eligible=item.get("rooms") or [])
                if wait:
                    info["why"] = "manual clean queued: " + wait
                else:
                    self.start(item["rooms"], now, run_kind="manual", request_id=item["id"],
                               whole_floor=bool(item.get("whole_floor")),
                               one_at_time=bool(item.get("one_at_time")))
                ph = D["phase"]
            else:
                D["manual_active"] = None
        if ph == "idle" and not D.get("manual_queue"):
            on = self.get(ENABLE).get("state") == "on"
            # the switch governs only the runs the autopilot starts by itself; a room a grown-up pressed
            # "clean when vacant" on still goes at the next vacancy with it off
            rooms = self.eligible() if on else [r for r in self.eligible() if r in self.room_requests()]
            if not on:
                info["why"] = "switched off" + ("; cleaning the requested rooms when vacant" if rooms else "")
            if rooms or on:
                info["eligible"] = rooms
                q, who = ((0, pause_signal["reason"]) if pause_signal else self.quiet_for()) if rooms else (None, None)
                info["quiet_min"] = None if q is None else round(min(q, 99 * 3600) / 60)
                # on another map: the clean script loads ours first (the robot is switched to the right map automatically)
                ok_robot = vs == "docked" and (incl != "on" or self.leftover(vs, incl, now)) and \
                    (not BATTERY or float(self.get(BATTERY).get("state") or 0) >= 50) and (not ERROR or self.get(ERROR).get("state") in ("none", None))
                reqs = self.room_requests()
                remote_run = False
                remote_wait = None
                if rooms and (q is None or q < START_QUIET_S):
                    requested = [seg for seg in rooms if seg in reqs]
                    entry_risk = self.request_entry_risk(now) if requested else None
                    clearances = [] if entry_risk else [
                        (seg, self.request_clearance([seg], now)) for seg in requested]
                    ready = [seg for seg, clearance in clearances
                             if clearance.get("allowed") and clearance.get("quiet_s", 0) >= START_QUIET_S]
                    if ready:
                        # Only explicit requests may use room-level clearance. Scheduled/due rooms remain behind the
                        # whole-floor vacancy rule, and one-at-a-time grouping permits a fresh check before each room.
                        rooms, remote_run, q, who = ready, True, START_QUIET_S, "requested room clearance"
                        info.update(eligible=rooms, quiet_min=START_QUIET_S // 60,
                                    request_clearance="room-level")
                    elif clearances:
                        remote_wait = next((clearance["why"] for _, clearance in clearances
                                            if not clearance.get("allowed")), clearances[0][1]["why"])
                    elif entry_risk:
                        remote_wait = entry_risk["reason"]
                # after failed starts, wait (2 h, 4 h, 8 h, then daily); a room pressed since the failure still goes
                held = now < D.get("backoff_until", 0) and not any(t > D.get("failed_at", 0) for t in reqs.values())
                hm = lambda t: datetime.fromtimestamp(t).strftime("%H:%M")
                pr = [] if rooms else self.explore_rooms(on, now)
                D["explore_next"] = bool(pr)
                if pr:
                    rooms = pr
                    info["eligible"], info["explore"] = rooms, True
                    q, who = self.quiet_for()
                    info["quiet_min"] = None if q is None else round(min(q, 99 * 3600) / 60)
                tag = "🔍 tidiness check of %s (the robot explores it; if it gets stuck the kids rescue it): " % ", ".join(pr) if pr else ""
                if not rooms:
                    info.setdefault("why", "nothing due: every room is cleaned, blocked or not asked for")
                elif not ok_robot and vs in ("cleaning", "returning", "paused"):
                    info["why"] = self.by_hand(vs, room)
                elif not ok_robot:
                    info["why"] = "not starting: the robot is %s%s" % (vs, ", charging" if vs == "docked" else "")
                elif held:
                    last = (D["runs"] or [{}])[-1].get("why", "")
                    info["why"] = "not starting before %s: the last %d start(s) failed (%s)" % (
                        hm(D["backoff_until"]), D.get("fail_streak", 0), last)
                elif q is None or q < START_QUIET_S:
                    info["why"] = tag + (("waiting for requested-room clearance: " + remote_wait) if remote_wait else
                        ("waiting for %s to be empty: quiet %d of %d minutes%s" % (
                        FLOOR, (q or 0) // 60, START_QUIET_S // 60, " (%s)" % self.who_text(who) if who else "")) if q is not None \
                        else "waiting: can't tell whether %s is empty" % FLOOR)
                elif now - D.get("last_attempt", 0) <= RETRY_S:
                    info["why"] = "waiting to retry at %s" % hm(D["last_attempt"] + RETRY_S)
                else:
                    D["last_attempt"] = now
                    if pr:
                        D["explore_day"] = datetime.fromtimestamp(now).date().isoformat()
                    self.start(rooms, now, explore=bool(pr), run_kind="request" if remote_run else "auto",
                               one_at_time=remote_run)
        elif ph == "running":
            if incl == "on":
                D["device_task_acknowledged"] = True
            if vs in ("error", "idle", "paused"):   # stuck, or a grown-up stopped it: once it's home, the run is over
                if not D.get("trouble"):
                    D["trouble"] = vs
                    self.log("trouble", state=vs, robot_in=D.get("cur"))
            if vs == "cleaning":
                D["saw_clean"] = True
                D["clean_first"] = D.get("clean_first") or now
                D["clean_last"] = now
                who = None if D.get("run_kind") == "manual" or not pause_signal else pause_signal["reason"]
                if who:
                    self.call("vacuum.pause", {"entity_id": VAC})
                    D.update(phase="paused", paused_at=now, pause_sent=now, resume_fallback=False, occ_quiet_since=None)
                    D["pauses"].append({"ts": now, "who": who, "robot_in": D.get("cur")})
                    self.log("pause", who=who, robot_in=D.get("cur"))
                elif (D.get("groups") or [{}])[D.get("gi", 0)].get("mode") != "smart_mode":
                    t = self.tier()
                    if t != D.get("fan") and (v.get("attributes") or {}).get("fan_speed") != t:
                        self.call("vacuum.set_fan_speed", {"entity_id": VAC, "fan_speed": t})
                        D["fan"] = t
                        self.log("fan", fan=t)
            left = self.leftover(vs, incl, now)
            done = D.get("saw_clean") and vs == "docked" and (incl != "on" or left)
            if done and left:
                self.finish("docked with the robot's task left unfinished (its cleaning flag stayed on)", now)
            elif done:
                D["docked_since"] = D.get("docked_since") or now
                if now - D["docked_since"] >= 60:
                    if D.get("trouble"):
                        self.finish("home after the robot was %s (stuck or stopped by hand): no more rooms this run" % D["trouble"], now)
                    elif now - D.get("run_started", now) > MAX_RUN_S:
                        self.finish("run too old to continue", now)
                    elif D["gi"] + 1 < len(D["groups"]):
                        self.completed(D["groups"][D["gi"]].get("rooms") or [])
                        D.update(phase="between", between_since=now)
                    else:
                        self.finish("done", now)
            else:
                D["docked_since"] = 0
            if D["phase"] == "running" and not D.get("saw_clean") and now - D.get("group_started", now) > 300:
                if incl == "on" and now - D.get("group_started", now) <= MAX_RUN_S:
                    info["why"] = "robot accepted the task and is preparing to clean"
                elif D.get("device_task_acknowledged"):
                    self.finish("accepted robot task ended or timed out before floor cleaning was observed", now)
                else:
                    self.finish("never started cleaning (state %s)" % vs, now)
        elif ph == "cancelled":
            if vs in ("docked", "charging"):
                self.finish(D.pop("cancel_reason", None) or "manual jobs cancelled", now)
        elif ph == "between":       # next group only if downstairs is still empty (a manual FIFO job needs no vacancy)
            manual = next((job for job in D.get("manual_queue", []) if job["id"] == D.get("manual_active")), None)
            next_group = D["groups"][D["gi"] + 1]
            holds = self.room_holds(next_group["rooms"], explicit=manual.get("gesture") != "long") if manual else {}
            hold = next(iter(holds.values()), None)
            next_clearance = self.request_clearance(next_group["rooms"], now) if request_run else None
            request_next_ready = bool(next_clearance and next_clearance.get("allowed") and
                                      next_clearance.get("quiet_s", 0) >= BETWEEN_QUIET_S)
            if hold and manual.get("gesture") == "long":
                D["groups"].pop(D["gi"] + 1)
                self.log("room_skipped", rooms=next_group["rooms"], why=hold)
                if D["gi"] + 1 == len(D["groups"]):
                    self.finish("done", now)
            elif now - D.get("run_started", now) > MAX_RUN_S:
                self.finish("run too old to continue", now)
            elif hold:
                info["why"] = "manual clean queued: " + hold
            elif request_run and not request_next_ready:
                info["why"] = "requested next room is waiting: " + (next_clearance or {}).get(
                    "why", "room-level clearance unavailable")
            elif manual or request_next_ready or (not pause_signal and
                                                   (self.quiet_for(held=False)[0] or 0) >= BETWEEN_QUIET_S):
                D["gi"] += 1
                D["phase"] = "running"
                self.launch(now)
            elif now - D.get("between_since", now) > PARK_DOCK_S:
                self.finish("people back between rooms", now)
        elif ph == "paused":
            q, who = ((0, pause_signal["reason"]) if pause_signal else
                      (RESUME_QUIET_S, "requested rooms and route clear")) if request_run else \
                ((0, pause_signal["reason"]) if pause_signal else
                 self.quiet_for(exclude={D.get("cur")} - {None}, held=False))
            age = max(0, now - D["paused_at"])
            if vs in ("returning", "docked"):
                D["phase"] = "docking"
                self.log("sent_home_elsewhere", robot_in=D.get("cur"))
            elif min(q, age) < RESUME_QUIET_S and age >= PARK_DOCK_S:
                self.call("vacuum.return_to_base", {"entity_id": VAC})
                D["phase"] = "docking"
                self.log("dock", who=who)
            elif vs == "cleaning":
                if min(q, age) >= RESUME_QUIET_S:
                    D["phase"] = "running"
                    self.log("resumed_elsewhere")
                elif now - D.get("pause_sent", D["paused_at"]) >= 30:
                    self.call("vacuum.pause", {"entity_id": VAC})
                    D["pause_sent"] = now
            elif vs == "paused" and min(q, age) >= RESUME_QUIET_S:
                self.call("vacuum.send_command", {"entity_id": VAC, "command": "resume_segment_clean"})
                D.update(phase="resuming", resume_at=now)
                self.log("resume")
            elif age >= PARK_DOCK_S:
                self.call("vacuum.return_to_base", {"entity_id": VAC})
                D["phase"] = "docking"
                self.log("dock", who=who)
        elif ph == "resuming":
            q, who = ((0, pause_signal["reason"]) if pause_signal else
                      (RESUME_QUIET_S, "requested rooms and route clear")) if request_run else \
                ((0, pause_signal["reason"]) if pause_signal else
                 self.quiet_for(exclude={D.get("cur")} - {None}, held=False))
            if vs in ("returning", "docked"):
                D["phase"] = "docking"
            elif q < RESUME_QUIET_S:
                if vs == "cleaning":
                    self.call("vacuum.pause", {"entity_id": VAC})
                D.update(phase="paused", pause_sent=now, resume_fallback=False)
                self.log("resume_held", who=who)
            elif vs == "cleaning":
                D["phase"] = "running"
            elif now - D.get("resume_at", now) > 30:
                if vs == "paused" and not D.get("resume_fallback"):
                    self.call("vacuum.send_command", {"entity_id": VAC, "command": "resume_segment_clean"})
                    D.update(resume_fallback=True, resume_at=now)
                    self.log("resume_retry")
                else:
                    self.call("vacuum.return_to_base", {"entity_id": VAC})
                    D["phase"] = "docking"
                    self.log("resume_failed")
        elif ph == "docking":
            if vs == "docked" and (incl != "on" or self.leftover(vs, incl, now)):
                self.finish("docked: person stayed " + FLOOR, now)
            elif vs == "docked":
                D["docking_since"] = D.get("docking_since") or now
                if now - D["docking_since"] > 120:
                    D["docking_since"] = 0
                    self.finish("docked", now)
        if "why" not in info:
            info["why"] = self.doing(D, vs) if ph != "idle" or vs not in ("cleaning", "returning", "paused") else self.by_hand(vs, room)
        info.update(self.robot_now(vs))
        self.publish(info, vs, room)
        self.save()

    def who_text(self, who):
        """a sensor id as words: the room it watches ("someone in Family Room")"""
        if isinstance(who, str) and who in SENSORS:
            return "someone in %s" % SENSORS[who]
        if isinstance(who, str) and "." in who:
            return (self.get(who).get("attributes") or {}).get("friendly_name") or who
        return who

    def robot_now(self, vs):
        """battery, the robot's own % done and minutes into this clean (the last two only while it is out)"""
        num = lambda e: (lambda x: float(x) if x not in (None, "", "unknown", "unavailable") else None)(self.get(e).get("state")) if e else None
        try:
            out = {"battery": num(BATTERY)}
            if vs in ("cleaning", "returning", "paused", "error"):
                out["progress"] = num(PROGRESS)
                t = num(CLEAN_TIME)
                out["clean_min"] = None if t is None else round(t / 60)
            return out
        except (TypeError, ValueError):
            return {}

    def by_hand(self, vs, room):
        """the robot is out on a clean the autopilot did not start (Clean now, the app, a remote)"""
        what = {"cleaning": "cleaning %s" % (room or "a room"), "returning": "heading back to its dock",
                "paused": "paused in %s" % (room or "a room")}[vs]
        return "%s (a clean started by hand, not by the autopilot)%s" % (what, self.progress_text(vs))

    def progress_text(self, vs):
        n = self.robot_now(vs)
        bits = ["%d%% done" % n["progress"] if n.get("progress") is not None else None,
                "%d min so far" % n["clean_min"] if n.get("clean_min") is not None else None,
                "battery %d%%" % n["battery"] if n.get("battery") is not None else None]
        return "".join(" · " + b for b in bits if b)

    def doing(self, D, vs):
        """one sentence: what a run is doing now"""
        ph = D["phase"]
        if ph == "idle":
            return "idle"
        if ph == "cancelled":
            return (D.get("cancel_reason") or "manual jobs cancelled") + "; waiting for the robot to dock"
        gr = (D.get("groups") or [{}])[D.get("gi", 0)] if D.get("groups") else {}
        rooms = gr.get("rooms") or []
        done = [r for r in rooms if D["visits"].get(r, 0) >= D.get("run_started", 0)]
        left = [r for r in rooms if r not in done and r != D.get("cur")]
        togo = ", %d room%s to go" % (len(left), "" if len(left) == 1 else "s") if left else ""
        tag = "🔍 tidiness check: " if D.get("explore") else ""
        if ph == "running":
            if vs == "cleaning":
                return tag + "cleaning %s%s%s" % (D.get("cur") or "(room unknown)", togo, self.progress_text(vs))
            if vs == "error" and D.get("explore"):
                return tag + "the robot is stuck in %s (untidy confirmed): waiting for the kids to rescue it" % (D.get("cur") or "a room")
            return "run under way: the robot is %s%s" % (vs, togo)
        if ph == "paused":
            p = (D.get("pauses") or [{}])[-1]
            return "paused: someone came in (%s); resumes after they leave" % (p.get("who") or "someone")
        if ph == "between":
            return "between room groups: waiting for %s to stay empty" % FLOOR
        if ph == "resuming":
            return "resuming the run"
        if ph == "docking":
            return "going home: people stayed, the run is over"
        return ph

    def pin_map(self, vs, now):
        """pin_map: docked on another map (the robot picks its newest map by itself), load ours back, once a minute at most"""
        D = self.D
        other = PIN_MAP and vs == "docked" and self.other_map()
        if other and now - D.get("pinned_at", 0) > 60:
            D["pinned_at"] = now
            self.call("select.select_option", {"entity_id": MAP_SELECT, "option": MAP_NAME})
            self.log("pin_map", was=other, to=MAP_NAME)

    def other_map(self):
        """the robot's selected map when it is a different one than our rooms belong to (None when it's ours or unknown);
        shown on the status only: the clean script switches the robot to our map before it sends the rooms"""
        if not (MAP_SELECT and MAP_NAME):
            return None
        m = self.get(MAP_SELECT).get("state")
        if m in (None, "unknown", "unavailable") or m.strip() == MAP_NAME.strip():
            return None
        return m

    def publish(self, info, vs, room):
        D = self.D
        attrs = {"friendly_name": NAME, "icon": "mdi:robot-vacuum",
                 **({"sealed_off": sorted(self.sealed_segments())} if OCC else {}),
                 "entry_events_connected": entry_edges.online,
                 "vacuum_entity": VAC, "map_flag": MAP_FLAG, "room_request_entity": REQUESTS,
                 **({"map_name": MAP_NAME, "map_selected": self.get(MAP_SELECT).get("state"),
                     "map_other": self.other_map()} if MAP_SELECT else {}),
                 "robot": vs, "robot_room": room, **info,
                 "groups": D.get("groups"), "gi": D.get("gi"),
                 "manual_queue": [{"request_id": x.get("id"), "gesture": x.get("gesture"),
                                   "source": x.get("source"),
                                   "rooms": x.get("rooms"), "attempts": x.get("attempts", 0),
                                   "next_attempt": x.get("next_attempt", 0),
                                   "whole_floor": bool(x.get("whole_floor"))} for x in (D.get("manual_queue") or [])],
                 "manual_active": D.get("manual_active"),
                 "incomplete": {"rooms": D.get("incomplete") or [], "since": D.get("incomplete_since"),
                                "why": D.get("incomplete_why")},
                 "visits": {k: datetime.fromtimestamp(t).strftime("%a %H:%M") for k, t in D["visits"].items()},
                 "segments": self.segment_status(), "suction_now": self.tier(), "phase": D["phase"],
                 "last_event": D.get("last_event"), "runs": D["runs"][-5:],
                 "last_run": ("%s: %s" % (datetime.fromtimestamp(D["runs"][-1]["end"]).strftime("%a %H:%M"), D["runs"][-1]["why"])
                              if D["runs"] and D["runs"][-1].get("end") else None),
                 "backoff_until": (datetime.fromtimestamp(D["backoff_until"]).strftime("%a %H:%M")
                                   if D.get("backoff_until", 0) > time.time() else None)}
        key = json.dumps([D["phase"], attrs], sort_keys=True, default=str)
        if key != self.last_pub or time.time() - D.get("pub_ts", 0) > 300:
            attrs["heartbeat"] = datetime.fromtimestamp(time.time()).astimezone().isoformat(timespec="seconds")   # so HA's last_updated moves
            try:
                self.g["ha_post_state"](PUBLISH, D["phase"], attrs, self.tok)
                self.last_pub, D["pub_ts"] = key, time.time()
            except Exception as ex:  # noqa: BLE001
                print("vacuum_autopilot publish failed", ex, flush=True)


def run(g, key="vacuum"):
    p = Pilot(g, key)
    while True:
        try:
            with p.lock:
                p.tick()
        except Exception as ex:  # noqa: BLE001
            print("vacuum autopilot error", repr(ex), flush=True)
            g["ilog"]({"src": LOG_SRC, "action": "error", "error": repr(ex)})
        time.sleep(10 if p.D["phase"] != "idle" else 30)
