"""Robot locator: which room is each robot vacuum in, even when it isn't cleaning.

Home Assistant's Roborock "current room" sensor only updates while the robot cleans (the map is refreshed then).
Paused, lost or heading home, it freezes - so lights meant to follow the robot light the wrong room.
roborock.get_vacuum_current_position gives the live x/y any time, but HA exposes no room outlines. So we learn them:
while a robot cleans, pair its position with the room the map reports; later, the room of a position is the vote of
the learned points around it.

house.json:
  "robot_locator": {"robots": {"vacuum.downstairs_robot": {"room_sensor": "sensor.downstairs_robot_current_room",
                                                              "publish": "sensor.downstairs_robot_room_live"}}}
Publishes, per robot, a sensor whose state is the room name (same names as the room sensor), or "unknown", with
attributes:
  source       map (cleaning: the integration's own room) | position (learned outline) | dock | unknown
               (a camera sighting from room_eye teaches the outline and gives a fresh position fix)
  why          when unknown: no position yet, position too old, unlearned, tied, robot <state>
  observed_at  when the room was last actually seen (epoch seconds); expires_at = observed_at + 60 s. While cleaning, a
               map room that blanks for a moment keeps the last room for up to 5 minutes (why: last room held,
               held_since: when it was really seen), so a map doesn't jump the robot home and back
  robot, robot_changed   the vacuum's state and when it entered it (a fix from before that is never used)
  x, y, votes, learned_cells, learned_points, published_at
Off-cleaning the map room is never used (it is frozen there); a failed or missing position, an unlearned spot or a
tie between rooms reads unknown. Fixes are kept in memory only, so a restart never brings back an old one. The
sensor is re-sent at least every 30 s, so a consumer can expire it on its own clock if this stops.
Optional per robot: "dock_room", the room reported while docked or charging (otherwise unknown). "motion"
({motion sensor: robot room name}) and "motion_when" (the autopilot's status sensor): during an autopilot run a
motion sensor turning on is the robot passing, and teaches its outline that room (confirmed a minute later).
"radars" can add drawing geometry and distance entities; "audio" can add numeric sound-level changes. These and
camera/PIR events are attributed to the robot only when the robot estimate is in that room and retained occupancy
says the room is clear. The published `explains` map lets other automations suppress a false person hypothesis;
`plan_x`, `plan_y`, `plan_source`, and `plan_at` place the robot on the drawing. Sensor/map pairs are written to the
private data directory and radar pairs train a bounded affine robot-map-to-drawing transform.
"""
import json
import math
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime

CELL = 250          # map units per grid cell (Roborock maps use millimetres)
SEARCH = 3          # look this many cells around an unlearned position
LEARN_S = 30
LOCATE_S = 15
POS_TTL = 60        # a room is trusted this long after it was seen
MOTION_CONFIRM_S = 60   # a motion sighting counts only if the run is still going (not paused for a person) this long after
HOLD_S = 300        # while cleaning, a blank map room keeps the last room seen this long (the integration blanks often)
HEARTBEAT_S = 30    # re-send an unchanged sensor this often
AWAY_STATES = ("returning", "paused", "idle", "error")
DOCK_STATES = ("docked", "charging")
STAMPS = ("observed_at", "expires_at", "published_at", "held_since", "evidence")
# --- fusion: every live source is evidence; each source's trust is learned from moments the answer is known
WINDOW_S = 60       # evidence older than this is ignored (the same minute a position fix is trusted)
HALF_S = 30         # evidence counts half as much every 30 s
TRUTH_SLACK_S = 30  # evidence this close to a known-answer moment is graded against it
PRIOR = {"map": (8, 10), "position": (6, 10), "motion": (6, 10), "camera": (7, 10),
         "radar": (7, 10), "audio": (3, 10)}   # (hits, tries) before any data
LEARN_CONF = 0.7    # a fused estimate this sure teaches the room outlines
PLAN_TTL = 15       # a direct plan-coordinate fix is intentionally short lived
TRAIN_XY_TTL = 20   # pair sensor evidence only with a near-simultaneous robot-map position
PLAN_SAMPLES = 500  # bounded durable calibration history per robot


def epoch(s):
    try:
        return datetime.fromisoformat(s["last_changed"]).timestamp()
    except Exception:  # noqa: BLE001
        return 0


def solve3(matrix, vector):
    """Solve a 3x3 system with pivoting, or return None when the calibration geometry is singular."""
    a = [list(map(float, row)) + [float(vector[i])] for i, row in enumerate(matrix)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda row: abs(a[row][col]))
        if abs(a[pivot][col]) < 1e-9:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        scale = a[col][col]
        a[col] = [value / scale for value in a[col]]
        for row in range(3):
            if row == col:
                continue
            scale = a[row][col]
            a[row] = [a[row][i] - scale * a[col][i] for i in range(4)]
    return [a[i][3] for i in range(3)]


def affine(samples):
    """Least-squares robot-map -> drawing transform from direct position pairs."""
    if len(samples) < 3:
        return None
    normal = [[0.0] * 3 for _ in range(3)]
    bx, by = [0.0] * 3, [0.0] * 3
    for row in samples:
        v = [float(row["map_x"]), float(row["map_y"]), 1.0]
        for i in range(3):
            bx[i] += v[i] * float(row["plan_x"])
            by[i] += v[i] * float(row["plan_y"])
            for j in range(3):
                normal[i][j] += v[i] * v[j]
    ax, ay = solve3(normal, bx), solve3(normal, by)
    if not ax or not ay:
        return None
    error = 0.0
    for row in samples:
        v = [float(row["map_x"]), float(row["map_y"]), 1.0]
        px, py = sum(ax[i] * v[i] for i in range(3)), sum(ay[i] * v[i] for i in range(3))
        error += (px - float(row["plan_x"])) ** 2 + (py - float(row["plan_y"])) ** 2
    return {"x": ax, "y": ay, "samples": len(samples), "rms": round(math.sqrt(error / len(samples)), 1)}


def calibrated(samples):
    """A numerically solvable fit is not a floorplan calibration until independent sensors span two dimensions."""
    if len(samples) < 6 or len({row.get("sensor") for row in samples}) < 2:
        return False
    points = [(float(row["plan_x"]), float(row["plan_y"])) for row in samples]
    if max(x for x, _ in points) - min(x for x, _ in points) < 25 \
            or max(y for _, y in points) - min(y for _, y in points) < 25:
        return False
    # Reject observations that all lie along one beam/line (normalized covariance determinant, O(n)).
    mx, my = sum(x for x, _ in points) / len(points), sum(y for _, y in points) / len(points)
    xx = sum((x - mx) ** 2 for x, _ in points)
    yy = sum((y - my) ** 2 for _, y in points)
    xy = sum((x - mx) * (y - my) for x, y in points)
    return xx > 0 and yy > 0 and (xx * yy - xy * xy) / (xx * yy) >= 0.05


def robot_position(g, token, vac, timeout=25):
    """Live x/y from the robot, or None. (HA REST: call the service and return its response.)"""
    req = urllib.request.Request(
        f"{g['HA_URL']}/api/services/roborock/get_vacuum_current_position?return_response",
        data=json.dumps({"entity_id": vac}).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        p = (r.get("service_response") or {}).get(vac) or {}
        return (int(p["x"]), int(p["y"])) if "x" in p else None
    except Exception:  # noqa: BLE001  (the robot answers "map failure" while it re-orients)
        return None


class Locator:
    def __init__(self, g):
        self.g = g
        self.cfg = (g["HOUSE"].get("robot_locator") or {}).get("robots") or {}
        self.path = g["DATA"] / "robot_map.json"
        try:
            self.M = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.M = {}
        self.M.pop("_last", None)   # older versions saved the last fix; never trust one across a restart
        self.tok = g["ha_token"]()
        self.last = {}          # vacuum -> last position call ts
        self.fix = {}           # vacuum -> {"xy", "room", "votes", "at"}: the newest successful position (memory only)
        self.seen = {}          # vacuum -> (map room, when) last seen while cleaning
        self.mprev = {}         # motion sensor -> last state
        self.mwait = []         # motion sightings waiting to be confirmed: {vacuum, room, xy, at}
        self.ev = {}            # vacuum -> [{"src", "kind", "room", "at"}]: recent evidence
        self.xy = {}            # vacuum -> ((x, y), at): the newest live position
        self.camprev = {}       # camera event entity -> last state
        self.sensorprev = {}    # radar/audio entity -> last normalized reading
        self.plan = {}          # vacuum -> newest direct drawing-coordinate fix
        self.learned = {}       # vacuum -> when the fused estimate last taught the outlines
        self.plast = {}         # vacuum -> last position call while cleaning
        self.pub = {}           # vacuum -> last published signature
        self.pub_at = {}        # vacuum -> when it was last sent
        self.dirty = False      # calibration changed outside the ordinary outline-learning path
        self.training_path = g["DATA"] / "robot-location-training.jsonl"

    def position(self, vac):
        return robot_position(self.g, self.tok, vac)

    def cell(self, x, y):
        return f"{x // CELL},{y // CELL}"

    def learn(self, vac, x, y, room):
        cells = self.M.setdefault(vac, {})
        c = cells.setdefault(self.cell(x, y), {})
        c[room] = c.get(room, 0) + 1

    def classify(self, vac, x, y):
        cells = self.M.get(vac) or {}
        cx, cy = x // CELL, y // CELL
        for r in range(SEARCH + 1):           # nearest ring with any learned points wins
            votes = Counter()
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    for room, n in (cells.get(f"{cx + dx},{cy + dy}") or {}).items():
                        votes[room] += n
            if votes:
                top = votes.most_common(2)
                if len(top) > 1 and top[0][1] == top[1][1]:
                    return None, dict(votes)    # a tie: say so rather than pick one by ordering
                return top[0][0], dict(votes)
        return None, {}

    def locate(self, vac, rc, st, since, room_now, now):
        """{"room", "source", "why", "xy", "votes", "observed_at"} for one robot right now"""
        out = {"room": "unknown", "source": "unknown", "why": "", "xy": None, "votes": {}, "observed_at": None}
        if st == "cleaning":
            self.fix.pop(vac, None)
            held = self.seen.get(vac)
            if room_now not in (None, "", "unknown", "unavailable"):
                self.seen[vac] = (room_now, now)
                out.update(room=room_now, source="map", observed_at=now)
            elif held and held[1] >= since and now - held[1] <= HOLD_S:
                # the map room blanked for a moment (it does, mid-clean): the robot is still where it was last seen
                out.update(room=held[0], source="map", observed_at=now, held_since=held[1],
                           why="map room blank: last room held")
            else:
                out["why"] = "no map room"
            return out
        if st in DOCK_STATES:
            self.fix.pop(vac, None)
            if rc.get("dock_room"):
                out.update(room=rc["dock_room"], source="dock", observed_at=now)
            else:
                out["why"] = "robot " + st
            return out
        if st not in AWAY_STATES:
            self.fix.pop(vac, None)
            out["why"] = "robot %s" % st
            return out
        if now - self.last.get(vac, 0) >= LOCATE_S:
            self.last[vac] = now
            xy = self.position(vac)
            if xy:
                found, votes = self.classify(vac, *xy)
                self.fix[vac] = {"xy": xy, "room": found, "votes": votes, "at": now}
        f = self.fix.get(vac)
        if not f or f["at"] < since:
            out["why"] = "no position yet"
        elif now - f["at"] > POS_TTL:
            out["why"] = "position too old"
        else:
            out.update(xy=f["xy"], votes=f["votes"])
            if f["room"]:
                out.update(room=f["room"], source="position", observed_at=f["at"])
            else:
                out["why"] = "tied" if f["votes"] else "unlearned"
        return out

    def visual_sightings(self, now):
        changed = False
        cameras = (self.g['HOUSE'].get('room_eye') or {}).get('cameras',{})
        for camera, settings in cameras.items():
            cfg = settings.get('visual_robot')
            if not cfg:
                continue
            vac, room = cfg['vacuum'], cfg['room']
            if vac not in self.cfg:
                continue
            path = self.g['DATA'] / ('robot-camera-' + camera + '.json')
            if not path.exists():
                continue
            last = self.M.get('_camera_watermarks',{}).get(camera,0)
            try:
                stored = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(stored,list):
                continue
            events = [row for row in stored if isinstance(row,dict) and type(row.get('at')) in (int,float)
                      and last < row['at'] <= now and {'vacuum','room','camera','xy','map'} <= row.keys()]
            if not events or self.g['ha_get'](cfg['map_select'],self.tok).get('state') != cfg['map_name']:
                continue
            for event in events:
                at, xy = event['at'], event['xy']
                if (event['vacuum'] != vac or event['camera'] != camera or event['room'] != room
                        or event['map'] != cfg['map_name'] or now-at >= 86400):
                    continue
                # Replaying a verified pair can teach its historical map cell. It cannot create live presence.
                if isinstance(xy,(list,tuple)) and len(xy) == 2 and all(type(x) is int for x in xy):
                    self.learn(vac,*xy,room)
                if now-at <= 15 and self.floor_empty(self.cfg[vac],now):
                    robot = self.g['ha_get'](vac,self.tok)
                    if robot.get('state') in ('cleaning','returning') and epoch(robot) <= at:
                        self.add(vac, 'camera:' + camera, 'camera', room, at, map_xy=xy,
                                 position_source="visual_robot", location_source="visual_robot")
                self.M.setdefault('_camera_watermarks',{})[camera] = at
                changed = True
        return changed

    def teach(self, now):
        """Camera sightings (room_eye): a robot seen moving in a camera's room on an empty floor. Its live position
        learns that room (3 votes: a sighting is better evidence than one map sample) and becomes its fresh fix."""
        q = self.g.get("LOCATOR_TEACH") or []
        changed = False
        while q:
            t = q.pop(0)
            vac = t["vacuum"]
            if vac not in self.cfg or now - t.get("at", now) > 20:
                continue
            xy = self.position(vac)
            if not xy:
                continue
            for _ in range(int(t.get("weight", 3))):
                self.learn(vac, *xy, t["room"])
            self.fix[vac] = {"xy": xy, "room": t["room"], "votes": {t["room"]: t.get("how", "camera")}, "at": now}
            changed = True
        return changed

    # ---- fusion
    def floor_empty(self, rc, now):
        """nobody on this robot's floor, per the presence tracker (which tells the robot from people). (During an
        autopilot run without a tracker, motion still teaches the outlines: see motion(), confirmed a minute later.)"""
        occ = rc.get("occupancy") or {}
        if occ.get("sensor"):
            a = self.g["ha_get"](occ["sensor"], self.tok).get("attributes") or {}
            f = (a.get("floors") or {}).get(occ.get("floor")) or {}
            if f.get("state") == "vacant" and float(a.get("valid_until") or 0) > now:
                return True
        return False

    def room_empty(self, rc, room, now, occupancy_room=None):
        """A fresh room verdict is preferred; old installs without room ids retain the floor-vacant fallback."""
        occ = rc.get("occupancy") or {}
        if occ.get("sensor"):
            a = self.g["ha_get"](occ["sensor"], self.tok).get("attributes") or {}
            rid = occupancy_room or (rc.get("room_ids") or {}).get(room)
            rooms = a.get("rooms") or {}
            verdict = rooms.get(rid) if rid and isinstance(rooms, dict) else None
            if verdict is not None:
                valid = verdict.get("valid_until", a.get("valid_until"))
                return verdict.get("state") in ("clear", "vacant") and float(valid or 0) > now
        return self.floor_empty(rc, now)

    def train(self, vac, src, kind, room, at, reading=None, plan=None, map_xy=None):
        """Private append-only sensor/map pairs for reliability and drawing-transform calibration."""
        xy = map_xy
        if xy is None:
            current = self.xy.get(vac)
            if current and abs(current[1] - at) <= TRAIN_XY_TTL:
                xy = current[0]
        if xy is None:
            return
        row = {"t": round(at, 3), "robot": vac, "map_x": xy[0], "map_y": xy[1],
               "room": room, "sensor": src, "kind": kind, "reading": reading}
        if plan:
            row.update(plan_x=round(plan[0], 2), plan_y=round(plan[1], 2))
            samples = self.M.setdefault("_plan_samples", {}).setdefault(vac, [])
            samples.append({k: row[k] for k in ("t", "map_x", "map_y", "plan_x", "plan_y", "sensor", "room")})
            del samples[:-PLAN_SAMPLES]
            self.dirty = True
        try:
            with self.training_path.open("a", encoding="utf-8") as out:
                out.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
        except OSError as ex:
            print("locator training write failed", ex, flush=True)

    def add(self, vac, src, kind, room, at, reading=None, plan=None, map_xy=None,
            position_source=None, location_source=None):
        event = {"src": src, "kind": kind, "room": room, "at": at}
        if reading is not None:
            event["reading"] = reading
        if plan:
            event["plan"] = [round(plan[0], 2), round(plan[1], 2)]
            self.plan[vac] = {"x": plan[0], "y": plan[1], "room": room, "source": src, "at": at}
        position = (map_xy, at) if map_xy is not None else self.xy.get(vac)
        if position_source == "visual_robot":
            event.update(independent_position=True, position_source=position_source, position_at=at)
        elif position and abs(position[1] - at) <= TRAIN_XY_TTL:
            event.update(independent_position=True, position_source=position_source or "native_navigation",
                         position_at=position[1])
        else:
            event["independent_position"] = False
        if location_source:
            event["location_source"] = location_source
        self.ev.setdefault(vac, []).append(event)
        if kind not in ("map", "position", "person"):
            self.train(vac, src, kind, room, at, reading, plan, map_xy)

    def rel(self, vac, src, kind):
        h, n = ((self.M.get("_rel") or {}).get(vac) or {}).get(src) or PRIOR.get(kind, (1, 2))
        return (h + 1) / (n + 2)

    def grade(self, vac, room, at, skip=()):
        """a moment the answer is known: every source's evidence near it is right or wrong"""
        R = self.M.setdefault("_rel", {}).setdefault(vac, {})
        for e in self.ev.get(vac) or []:
            if e["src"] in skip or abs(e["at"] - at) > TRUTH_SLACK_S or e.get("graded") == at:
                continue
            e["graded"] = at
            h, n = R.get(e["src"]) or PRIOR.get(e["kind"], (1, 2))
            R[e["src"]] = [h + (1 if e["room"] == room else 0), n + 1]

    def radar_parts(self, entity, cfg):
        base = entity.removeprefix("binary_sensor.").removesuffix("_presence")
        return {"moving": cfg.get("moving", "binary_sensor." + base + "_moving_target"),
                "still": cfg.get("still", "binary_sensor." + base + "_still_target"),
                "moving_distance": cfg.get("moving_distance", "sensor." + base + "_moving_distance"),
                "still_distance": cfg.get("still_distance", "sensor." + base + "_still_distance"),
                "distance": cfg.get("distance", "sensor." + base + "_detection_distance")}

    def inches(self, entity):
        state = self.g["ha_get"](entity, self.tok)
        try:
            value = float(state.get("state"))
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        unit = str((state.get("attributes") or {}).get("unit_of_measurement") or "in")
        return value / 2.54 if unit == "cm" else value / 25.4 if unit == "mm" else \
            value * 39.37 if unit == "m" else value * 12 if unit == "ft" else value

    def radar(self, vac, rc, loc, now):
        for entity, spec in (rc.get("radars") or {}).items():
            cfg = {"room": spec} if isinstance(spec, str) else spec
            if not isinstance(cfg, dict) or not all(key in cfg for key in ("room", "x", "y", "direction")):
                continue
            parts = self.radar_parts(entity, cfg)
            moving = self.g["ha_get"](parts["moving"], self.tok).get("state") == "on"
            still = self.g["ha_get"](parts["still"], self.tok).get("state") == "on"
            present = self.g["ha_get"](entity, self.tok).get("state") == "on"
            distance = self.inches(parts["moving_distance"] if moving else parts["still_distance"] if still else parts["distance"])
            if distance is None:
                distance = self.inches(parts["distance"])
            snap = (present, moving, still, round(distance, 1) if distance else None)
            changed = snap != self.sensorprev.get("radar:" + entity)
            self.sensorprev["radar:" + entity] = snap
            if not changed or not present or distance is None:
                continue
            room = loc.get("room")
            allowed = cfg.get("rooms") or [cfg.get("room")]
            if room not in allowed or not self.room_empty(rc, room, now, cfg.get("occupancy_room")):
                continue
            bearing = None
            for candidate in sorted(cfg.get("bearings") or cfg.get("radar_bearings") or [],
                                    key=lambda value: value.get("from_in", 0), reverse=True):
                if distance < float(candidate.get("from_in", 0)):
                    continue
                when = candidate.get("when_on") or []
                if when and not any(self.g["ha_get"](item, self.tok).get("state") == "on" for item in when):
                    continue
                bearing = candidate
                break
            direction = float((bearing or {}).get("direction", cfg.get("direction", 0)))
            measured = distance
            if bearing and bearing.get("max_in") is not None:
                distance = min(distance, float(bearing["max_in"]))
            ppi = float(cfg.get("px_per_inch", rc.get("px_per_inch", 2.41)))
            angle = math.radians(direction)
            plan = (float(cfg["x"]) + distance * ppi * math.cos(angle),
                    float(cfg["y"]) + distance * ppi * math.sin(angle))
            reading = {"distance_in": round(measured, 1), "direction": direction,
                       "moving": moving, "still": still}
            self.add(vac, entity, "radar", room, now, reading=reading, plan=plan,
                     location_source=loc.get("source"))

    def audio(self, vac, rc, loc, now):
        for entity, spec in (rc.get("audio") or {}).items():
            cfg = {"room": spec} if isinstance(spec, str) else spec
            state = self.g["ha_get"](entity, self.tok)
            raw = state.get("state")
            if raw in (None, "unknown", "unavailable"):
                self.sensorprev["audio:" + entity] = raw
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                value = raw
            previous = self.sensorprev.get("audio:" + entity)
            self.sensorprev["audio:" + entity] = value
            if previous is None or value == previous:
                continue
            if isinstance(value, float) and cfg.get("min") is not None and value < float(cfg["min"]):
                continue
            if isinstance(value, float) and isinstance(previous, (int, float)) \
                    and abs(value - previous) < float(cfg.get("delta", 0)):
                continue
            room = cfg.get("room")
            if loc.get("room") != room or not self.room_empty(rc, room, now, cfg.get("occupancy_room")):
                continue
            self.add(vac, entity, "audio", room, now, reading=value, location_source=loc.get("source"))

    def gather(self, vac, rc, st, room_now, now, since=0, loc=None):
        """this tick's evidence from the robot, its map, the room sensors and the cameras"""
        out = st in ("cleaning",) + AWAY_STATES
        if not out:
            self.ev[vac] = []
            return
        if st == "cleaning" and room_now not in (None, "", "unknown", "unavailable"):
            self.add(vac, "map", "map", room_now, now)
        f = self.fix.get(vac)
        if st != "cleaning" and f and f["at"] == now:          # off-cleaning: locate() just asked the robot
            self.xy[vac] = (f["xy"], now)
            if f["room"]:
                self.add(vac, "position", "position", f["room"], now)
        elif st == "cleaning" and now - self.plast.get(vac, 0) >= LOCATE_S:
            self.plast[vac] = now
            xy = self.position(vac)
            if xy:
                self.xy[vac] = (xy, now)
                found, votes = self.classify(vac, *xy)
                if found:
                    self.add(vac, "position", "position", found, now)
        for ent, room in (rc.get("motion") or {}).items():
            if ent in (rc.get("radars") or {}):
                continue
            s_ = self.g["ha_get"](ent, self.tok).get("state")
            rising = s_ == "on" and self.mprev.get("f:" + ent) == "off"
            self.mprev["f:" + ent] = s_
            if rising and self.room_empty(rc, room, now):
                self.add(vac, ent, "motion", room, now, reading="on", location_source=(loc or {}).get("source"))
        for ent, room in (rc.get("cameras") or {}).items():
            s_ = self.g["ha_get"](ent, self.tok)
            v = s_.get("state")
            new = v not in (None, "unknown", "unavailable") and self.camprev.get(ent) not in (None, v)
            self.camprev[ent] = v
            event_type = (s_.get("attributes") or {}).get("event_type")
            if new and self.room_empty(rc, room, now) and event_type in ("camera_motion", "camera_sound"):
                self.add(vac, ent, "camera", room, now, reading=event_type,
                         location_source=(loc or {}).get("source"))
        self.radar(vac, rc, loc or {}, now)
        self.audio(vac, rc, loc or {}, now)
        self.ev[vac] = [e for e in self.ev.get(vac) or [] if now - e["at"] <= WINDOW_S and e["at"] >= since]
        # a known-answer moment: the robot's map room and its learned position agree right now
        m = [e for e in self.ev[vac] if e["src"] == "map" and e["at"] == now]
        p = [e for e in self.ev[vac] if e["src"] == "position" and e["at"] == now]
        if m and p and m[0]["room"] == p[0]["room"]:
            self.grade(vac, m[0]["room"], now, skip=("map", "position"))

    def fuse(self, vac, now):
        """(room, confidence, top source, [evidence]) from the recent evidence, or (None, 0, None, [])"""
        score, top = Counter(), {}
        for e in self.ev.get(vac) or []:
            w = self.rel(vac, e["src"], e["kind"]) * 0.5 ** ((now - e["at"]) / HALF_S)
            score[e["room"]] += w
            if w > top.get(e["room"], (0, None))[0]:
                top[e["room"]] = (w, e["src"])
        if not score:
            return None, 0.0, None, []
        room, best = score.most_common(1)[0]
        ev = [{"src": e["src"], "room": e["room"], "age": int(now - e["at"])} for e in (self.ev.get(vac) or [])][-12:]
        return room, round(best / sum(score.values()), 2), top[room][1], ev

    def explains(self, vac, room, now):
        out = {}
        for event in self.ev.get(vac) or []:
            if event["kind"] in ("map", "position", "person") or event["room"] != room:
                continue
            confidence = self.rel(vac, event["src"], event["kind"]) * 0.5 ** ((now - event["at"]) / HALF_S)
            current = out.get(event["src"])
            if current is None or event["at"] >= current["at"]:
                out[event["src"]] = {
                    "robot": vac, "at": int(event["at"]), "observed_at": int(event["at"]),
                    "valid_until": int(event["at"] + WINDOW_S), "confidence": round(confidence, 2),
                    "confidence_scale": "0..1", "room": room, "kind": event["kind"],
                    "independent_position": bool(event.get("independent_position")),
                    "position_source": event.get("position_source"),
                    "position_at": int(event["position_at"]) if event.get("position_at") else None,
                    "location_source": event.get("location_source"),
                }
        return out

    def plan_position(self, vac, room, now):
        direct = self.plan.get(vac)
        if direct and now - direct["at"] <= PLAN_TTL and direct["room"] == room:
            return direct["x"], direct["y"], direct["source"], direct["at"], None
        xy = self.xy.get(vac)
        samples = (self.M.get("_plan_samples") or {}).get(vac) or []
        model = affine(samples) if calibrated(samples) else None
        if not xy or now - xy[1] > POS_TTL or not model:
            return None
        vector = [float(xy[0][0]), float(xy[0][1]), 1.0]
        px = sum(model["x"][i] * vector[i] for i in range(3))
        py = sum(model["y"][i] * vector[i] for i in range(3))
        return px, py, "learned_transform", xy[1], {"samples": model["samples"], "rms": model["rms"]}

    def corrections(self, now):
        """a person said where the robot really is (POST /api/robot_seen): the best answer there is"""
        q = self.g.get("LOCATOR_TRUTH") or []
        changed = False
        while q:
            t = q.pop(0)
            vac = t["vacuum"]
            if vac not in self.cfg:
                continue
            self.grade(vac, t["room"], t.get("at", now))
            xy = (self.xy.get(vac) or (None, 0))
            if xy[0] and abs(xy[1] - t.get("at", now)) <= TRUTH_SLACK_S:
                for _ in range(3):
                    self.learn(vac, *xy[0], t["room"])
            self.add(vac, "person", "person", t["room"], t.get("at", now))
            changed = True
        return changed

    def motion(self, now):
        """Motion sensors as robot sightings: while this floor's autopilot is on a run (the floor was empty 20 minutes,
        and the autopilot pauses for a person), a room's motion sensor turning on is the robot passing. Its position
        then learns that room (1 vote: a motion sensor sees more than its own room), once the run is still going a
        minute later (a person would have paused it)."""
        changed = False
        for vac, rc in self.cfg.items():
            sensors, when = rc.get("motion") or {}, rc.get("motion_when")
            if not sensors or not when:
                continue
            running = self.g["ha_get"](when, self.tok).get("state") == "running"
            for ent, room in sensors.items():
                if ent in (rc.get("radars") or {}):
                    continue
                st = self.g["ha_get"](ent, self.tok).get("state")
                rising = st == "on" and self.mprev.get(ent) == "off"
                self.mprev[ent] = st
                if rising and running:
                    xy = self.position(vac)
                    if xy:
                        self.mwait.append({"vacuum": vac, "room": room, "xy": xy, "at": now, "when": when})
        keep = []
        for w in self.mwait:
            if now - w["at"] < MOTION_CONFIRM_S:
                keep.append(w)
            elif self.g["ha_get"](w["when"], self.tok).get("state") == "running":
                self.learn(w["vacuum"], *w["xy"], w["room"])
                changed = True
        self.mwait = keep
        return changed

    def tick(self):
        now = time.time()
        changed = self.visual_sightings(now)
        changed = self.teach(now) or changed
        changed = self.motion(now) or changed
        changed = self.corrections(now) or changed
        for vac, rc in self.cfg.items():
            vs = self.g["ha_get"](vac, self.tok)
            st, since = vs.get("state"), epoch(vs)
            room_now = self.g["ha_get"](rc["room_sensor"], self.tok).get("state")
            if st == "cleaning" and room_now not in (None, "", "unknown", "unavailable") \
                    and now - self.last.get(vac, 0) >= LEARN_S:
                self.last[vac] = now
                xy = self.position(vac)
                if xy:
                    self.learn(vac, *xy, room_now)
                    changed = True
            loc = self.locate(vac, rc, st, since, room_now, now)
            self.gather(vac, rc, st, room_now, now, since, loc)
            room, conf, src, ev = self.fuse(vac, now)
            if room and st not in DOCK_STATES:
                loc.update(fused_from=src, confidence=conf, evidence=ev)
                if room != loc["room"]:            # other evidence outweighs the robot's own reading: say so
                    loc.update(room=room, source="fused", why="", observed_at=now)
                    loc.pop("held_since", None)
                xy = self.xy.get(vac)
                if conf >= LEARN_CONF and xy and now - xy[1] <= LOCATE_S and src != "position" \
                        and now - self.learned.get(vac, 0) >= LEARN_S:
                    self.learned[vac] = now
                    self.learn(vac, *xy[0], room)
                    changed = True
            if self.xy.get(vac) and now - self.xy[vac][1] <= POS_TTL:
                loc["xy"] = self.xy[vac][0]
            final_room = loc["room"]
            loc["explains"] = self.explains(vac, final_room, now) if final_room != "unknown" else {}
            plan = self.plan_position(vac, final_room, now) if final_room != "unknown" else None
            if plan:
                loc.update(plan_x=round(plan[0], 2), plan_y=round(plan[1], 2), plan_source=plan[2],
                           plan_at=plan[3], plan_model=plan[4])
            self.publish(vac, rc, loc, st, since, now)
        if changed or self.dirty:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.M))
            tmp.replace(self.path)
            self.dirty = False

    def publish(self, vac, rc, loc, st, since, now=None):
        ent = rc.get("publish")
        if not ent:
            return
        now = now or time.time()
        cells = self.M.get(vac) or {}
        xy, seen = loc["xy"], loc["observed_at"]
        attrs = {"friendly_name": rc.get("name", vac.split(".")[1].replace("_", " ").title()) + " room (live)",
                 "icon": "mdi:robot-vacuum", "source": loc["source"], "why": loc["why"] or None,
                 "robot": st, "robot_changed": int(since) if since else None,
                 "observed_at": int(seen) if seen else None, "expires_at": int(seen + POS_TTL) if seen else None,
                 "x": xy[0] if xy else None, "y": xy[1] if xy else None, "votes": loc["votes"],
                 "held_since": int(loc["held_since"]) if loc.get("held_since") else None,
                 "fused_from": loc.get("fused_from"), "confidence": loc.get("confidence"), "evidence": loc.get("evidence"),
                 "explain_schema": 1, "explains": loc.get("explains") or {},
                 "plan_x": loc.get("plan_x"), "plan_y": loc.get("plan_y"),
                 "plan_source": loc.get("plan_source"), "plan_at": int(loc["plan_at"]) if loc.get("plan_at") else None,
                 "plan_model": loc.get("plan_model"),
                 "reliability": {k: round((v[0] + 1) / (v[1] + 2), 2) for k, v in ((self.M.get("_rel") or {}).get(vac) or {}).items()},
                 "learned_cells": len(cells), "learned_points": sum(sum(c.values()) for c in cells.values())}
        sig = json.dumps([loc["room"], {k: v for k, v in attrs.items() if k not in STAMPS}], sort_keys=True, default=str)
        if sig == self.pub.get(vac) and now - self.pub_at.get(vac, 0) < HEARTBEAT_S:
            return
        attrs["published_at"] = int(now)
        try:
            self.g["ha_post_state"](ent, loc["room"], attrs, self.tok)
        except Exception as ex:  # noqa: BLE001
            print("locator publish failed", ex, flush=True)
            return
        self.pub[vac], self.pub_at[vac] = sig, now


def run(g):
    loc = Locator(g)
    while True:
        try:
            loc.tick()
        except Exception as ex:  # noqa: BLE001
            print("robot locator error", repr(ex), flush=True)
        time.sleep(5)
