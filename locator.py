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
Optional per robot: "dock_room", the room reported while docked or charging (otherwise unknown).
"""
import json
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
HOLD_S = 300        # while cleaning, a blank map room keeps the last room seen this long (the integration blanks often)
HEARTBEAT_S = 30    # re-send an unchanged sensor this often
AWAY_STATES = ("returning", "paused", "idle", "error")
DOCK_STATES = ("docked", "charging")
STAMPS = ("observed_at", "expires_at", "published_at", "held_since")


def epoch(s):
    try:
        return datetime.fromisoformat(s["last_changed"]).timestamp()
    except Exception:  # noqa: BLE001
        return 0


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
        self.pub = {}           # vacuum -> last published signature
        self.pub_at = {}        # vacuum -> when it was last sent

    def position(self, vac):
        """Live x/y from the robot, or None. (HA REST: call the service and return its response.)"""
        req = urllib.request.Request(
            f"{self.g['HA_URL']}/api/services/roborock/get_vacuum_current_position?return_response",
            data=json.dumps({"entity_id": vac}).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.tok}", "Content-Type": "application/json"})
        try:
            r = json.loads(urllib.request.urlopen(req, timeout=25).read())
            p = (r.get("service_response") or {}).get(vac) or {}
            return (int(p["x"]), int(p["y"])) if "x" in p else None
        except Exception:  # noqa: BLE001  (the robot answers "map failure" while it re-orients)
            return None

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
            for _ in range(3):
                self.learn(vac, *xy, t["room"])
            self.fix[vac] = {"xy": xy, "room": t["room"], "votes": {t["room"]: "camera"}, "at": now}
            changed = True
        return changed

    def tick(self):
        now = time.time()
        changed = self.teach(now)
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
            self.publish(vac, rc, self.locate(vac, rc, st, since, room_now, now), st, since, now)
        if changed:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.M))
            tmp.replace(self.path)

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
