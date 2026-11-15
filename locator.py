"""Robot locator: which room is each robot vacuum in, even when it isn't cleaning.

Home Assistant's Roborock "current room" sensor only updates while the robot cleans (the map is refreshed then).
Paused, lost or heading home, it freezes - so lights meant to follow the robot light the wrong room.
roborock.get_vacuum_current_position gives the live x/y any time, but HA exposes no room outlines. So we learn them:
while a robot cleans, pair its position with the room the map reports; later, the room of a position is the vote of
the learned points around it.

house.json:
  "robot_locator": {"robots": {"vacuum.downstairs_robot": {"room_sensor": "sensor.downstairs_robot_current_room",
                                                              "publish": "sensor.downstairs_robot_room_live"}}}
Publishes, per robot, a sensor whose state is the room name (same names as the room sensor) with
attributes source (map | position | unknown), x, y, votes, learned points.
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
AWAY_STATES = ("returning", "paused", "idle", "error")


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
        self.tok = g["ha_token"]()
        self.last = {}          # vacuum -> last position call ts
        self.pub = {}           # vacuum -> last published signature

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
                room, n = votes.most_common(1)[0]
                return room, dict(votes)
        return None, {}

    def tick(self):
        now = time.time()
        changed = False
        for vac, rc in self.cfg.items():
            st = self.g["ha_get"](vac, self.tok).get("state")
            rs = self.g["ha_get"](rc["room_sensor"], self.tok)
            room, src, xy, votes = rs.get("state"), "map", None, {}
            if st == "cleaning":
                if now - self.last.get(vac, 0) >= LEARN_S and room not in (None, "unknown", "unavailable"):
                    self.last[vac] = now
                    xy = self.position(vac)
                    if xy:
                        self.learn(vac, *xy, room)
                        changed = True
            elif st in AWAY_STATES:
                if now - self.last.get(vac, 0) >= LOCATE_S:
                    self.last[vac] = now
                    xy = self.position(vac)
                    if xy:
                        found, votes = self.classify(vac, *xy)
                        room, src = (found, "position") if found else ("unknown", "unknown")
                        self.M.setdefault("_last", {})[vac] = {"room": room, "src": src, "xy": xy, "votes": votes}
                    elif (self.M.get("_last") or {}).get(vac):
                        prev = self.M["_last"][vac]
                        room, src, xy, votes = prev["room"], prev["src"], prev["xy"], prev["votes"]
                else:
                    prev = (self.M.get("_last") or {}).get(vac) or {}
                    room, src, xy, votes = prev.get("room", room), prev.get("src", src), prev.get("xy"), prev.get("votes", {})
            else:   # docked / charging: the map's room is fine (it's at the dock)
                (self.M.get("_last") or {}).pop(vac, None)
            self.publish(vac, rc, room, src, xy, votes, st)
        if changed:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.M))
            tmp.replace(self.path)

    def publish(self, vac, rc, room, src, xy, votes, st):
        ent = rc.get("publish")
        if not ent:
            return
        cells = self.M.get(vac) or {}
        attrs = {"friendly_name": rc.get("name", vac.split(".")[1].replace("_", " ").title()) + " room (live)",
                 "icon": "mdi:robot-vacuum", "source": src, "robot": st, "x": xy[0] if xy else None, "y": xy[1] if xy else None,
                 "votes": votes, "learned_cells": len(cells), "learned_points": sum(sum(c.values()) for c in cells.values())}
        sig = json.dumps([room, attrs], sort_keys=True, default=str)
        if sig == self.pub.get(vac):
            return
        self.pub[vac] = sig
        try:
            self.g["ha_post_state"](ent, room or "unknown", attrs, self.tok)
        except Exception as ex:  # noqa: BLE001
            print("locator publish failed", ex, flush=True)


def run(g):
    loc = Locator(g)
    while True:
        try:
            loc.tick()
        except Exception as ex:  # noqa: BLE001
            print("robot locator error", repr(ex), flush=True)
        time.sleep(5)
