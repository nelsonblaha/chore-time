"""Remember brief, live doorway openings between the vacuum's polling ticks."""
import threading
import time
from datetime import datetime


class EntryEdges:
    def __init__(self):
        self.lock = threading.Lock()
        self.edges = {}
        self.online = False

    def record(self, data, allowed, now=None):
        now = time.time() if now is None else now
        entity = data.get("entity_id")
        old, new = data.get("old_state") or {}, data.get("new_state") or {}
        # Restored/unknown states and attribute-only updates are not arrivals.
        if entity not in allowed or old.get("state") != "off" or new.get("state") != "on":
            return False
        try:
            stamp = datetime.fromisoformat(new["last_changed"]).timestamp()
        except (KeyError, TypeError, ValueError):
            return False
        if not -2 <= now - stamp <= 30:
            return False
        with self.lock:
            self.edges[entity] = max(stamp, self.edges.get(entity, 0))
        return True

    def recent(self, now=None, max_age=1200):
        now = time.time() if now is None else now
        with self.lock:
            self.edges = {e: t for e, t in self.edges.items() if -2 <= now - t <= max_age}
            return dict(self.edges)


entry_edges = EntryEdges()
