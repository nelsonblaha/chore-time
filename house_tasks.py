"""House tasks: the grown-ups' side, from Grocy.

Every minute: the grown-ups' chores that are due (Grocy chores not marked for the kids), the open one-off Grocy tasks,
and batteries that need replacing. Each item can carry context (Grocy userfields):

  ctx_show_on    displays it belongs on, comma list (empty = everywhere)
  ctx_show_when  only show while this Home Assistant condition holds, e.g.  person.ben = home
  ctx_done_when  done by itself when this holds, e.g.  binary_sensor.washer_running = off and sensor.x > 50
  ctx_rooms      where it's done, comma list of rooms. Each screen in house.json "house_tasks": {"displays":
                 {"kitchen": "Kitchen", "upstairs": "Upstairs"}, "floors": {"Downstairs": ["Kitchen", …]}} gets its own ranking: overdue, then
                 due today, then undated; a job in the screen's room (or anywhere on its floor, for a floor-wide screen) ranks
                 up a lot, one on the same floor a little.

Conditions: "<entity> <op> <value>" joined by "and"; op is one of = != > < >= <=. Besides Home Assistant entities,
now.hour, now.weekday (0 = Monday), now.month and now.day test the clock, e.g. "now.hour >= 17" or "now.weekday >= 5". A due item whose ctx_done_when
comes true is completed in Grocy (once). Batteries (Grocy batteries with userfields ha_entity: one or more battery
sensors of the device, comma separated, the newest reading counts; ha_low: the level that means replace) become
"Replace the battery" items while the Home Assistant level is at or below ha_low, and are tracked as replaced in
Grocy when the level comes back up. Everything is published as sensor.house_tasks for the boards. Tasks in the
house.json grocy "review_category" (an inbox, e.g. tasks imported from elsewhere) are only counted (to_review).
If Grocy offers /tasks/private-summary (encrypted private tasks), each owner's open/due/overdue counts are included.
"""
import json
import re
import time
import urllib.request
from datetime import datetime

OPS = ("!=", ">=", "<=", "=", ">", "<")


def check(cond, get):
    """True/False for a condition string; None when it can't be evaluated (unknown entity, bad syntax)."""
    if not cond or not cond.strip():
        return None
    for part in re.split(r"\s+and\s+", cond.strip(), flags=re.I):
        m = re.match(r"^\s*([a-z_]+\.[a-z0-9_]+)\s*(!=|>=|<=|==|=|>|<)\s*(.+?)\s*$", part)
        if not m:
            return None
        ent, op, want = m.group(1), m.group(2).replace("==", "="), m.group(3).strip().strip('"\'')
        have = get(ent).get("state")
        if have in (None, "unavailable", "unknown"):
            return None
        try:
            a, b = float(have), float(want)
        except ValueError:
            a, b = str(have).lower(), want.lower()
            if op not in ("=", "!="):
                return None
        ok = {"=": a == b, "!=": a != b, ">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}[op]
        if not ok:
            return False
    return True


def score(item, room, floor_of, now):
    """How much a job belongs on a screen in `room` right now: how due it is first, then how near it is."""
    due = item.get("due")
    if item.get("overdue"):
        s = 100 + min(30, (now - due) / 86400 if due else 0)
    elif due is not None and due <= now + 86400:
        s = 70
    else:
        s = 30                                                        # an open one-off with no date
    rooms = item.get("rooms") or []
    floors = {floor_of.get(r) for r in rooms}
    if room in rooms or room in floors:                              # a screen can stand for a room or a whole floor
        s += 40
    elif floor_of.get(room) and floor_of.get(room) in floors:
        s += 15
    return s


def is_here(item, room, floor_of):
    rooms = item.get("rooms") or []
    return room in rooms or room in {floor_of.get(r) for r in rooms}


def rank(items, room, floor_of, now):
    return sorted(items, key=lambda i: (-score(i, room, floor_of, now), i.get("due") or 9e12, i.get("name") or ""))


def now_value(ent, when=None):
    """now.hour (0-23), now.weekday (0 = Monday … 6 = Sunday), now.month (1-12), now.day: for 'evenings only' etc."""
    t = when or datetime.now()
    return {"now.hour": t.hour, "now.weekday": t.weekday(), "now.month": t.month, "now.day": t.day}.get(ent)


def battery_level(states):
    """The level from the newest reading among a device's battery entities (a 4-button remote often has 4, some stale)."""
    best = None
    for st in states:
        try:
            v = float(st.get("state"))
        except (TypeError, ValueError):
            continue
        t = ts(st.get("last_reported") or st.get("last_updated")) or 0
        if best is None or t > best[0]:
            best = (t, v)
    return best[1] if best else None


def ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace(" ", "T")).timestamp()
    except ValueError:
        return None


class HouseTasks:
    def __init__(self, g):
        self.g = g
        self.tok = g["ha_token"]()
        self.path = g["DATA"] / "house_tasks.json"
        try:
            self.D = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.D = {}
        self.D.setdefault("auto_done", {})
        self.last_sig = None

    def get(self, ent):
        if ent.startswith("now."):                                    # clock conditions, no Home Assistant needed
            return {"state": now_value(ent)}
        return self.g["ha_get"](ent, self.tok)

    def gc(self, method, path, body=None):
        return self.g["gcall"](method, path, body)

    def items(self):
        now = time.time()
        out = []
        objs = {c["id"]: c for c in self.gc("GET", "/objects/chores") or []}
        users = {u["id"]: (u.get("display_name") or u.get("username")) for u in (self.gc("GET", "/users") or [])}
        for c in self.gc("GET", "/chores") or []:                      # due info per chore
            o = objs.get(c.get("chore_id") or c.get("id")) or {}
            uf = o.get("userfields") or {}
            if str(uf.get("kids")) == "1" or (uf.get("kid_key") or "").strip() or str(o.get("active", 1)) == "0":
                continue                                              # the kids' side handles those
            due = ts(c.get("next_estimated_execution_time"))
            last = ts(c.get("last_tracked_time"))
            if due is None and o.get("period_type") == "dynamic-regular":   # Grocy leaves this null once executed
                due = (last + float(o.get("period_days") or 0) * 86400) if last else now
            out.append({"kind": "chore", "id": o.get("id") or c.get("chore_id"), "name": o.get("name") or c.get("chore_name"),
                        "due": due, "overdue": bool(due and due < now), "assigned": users.get(c.get("next_execution_assigned_to_user_id")),
                        "last": ts(c.get("last_tracked_time")), "uf": uf})
        # Private tasks (a Grocy that encrypts them, e.g. the grocy-private fork): never names, only counts per owner.
        try:
            summary = self.gc("GET", "/tasks/private-summary") or []
        except Exception:  # noqa: BLE001  (a stock Grocy has no such endpoint)
            summary = []
        self.private = [{"owner": users.get(r.get("owner_user_id"), "?"), "open": r.get("open", 0),
                         "due_today": r.get("due_today", 0), "overdue": r.get("overdue", 0)}
                        for r in summary if isinstance(r, dict)]
        inbox = str((self.g["HOUSE"].get("grocy") or {}).get("review_category") or "").strip().lower()
        inbox_ids = {c["id"] for c in (self.gc("GET", "/objects/task_categories") or []) if inbox and c["name"].lower() == inbox}
        self.to_review = 0
        for t in self.gc("GET", "/objects/tasks") or []:
            if str(t.get("done")) == "1":
                continue
            if t.get("category_id") in inbox_ids:                     # imported, not yet sorted: counted, not listed
                self.to_review += 1
                continue
            due = ts(t.get("due_date"))
            out.append({"kind": "task", "id": t["id"], "name": t["name"], "due": due, "overdue": bool(due and due < now),
                        "assigned": users.get(t.get("assigned_to_user_id")), "category_id": t.get("category_id"),
                        "uf": t.get("userfields") or {}})
        for b in self.gc("GET", "/objects/batteries") or []:
            uf = b.get("userfields") or {}
            ents, low = [e.strip() for e in str(uf.get("ha_entity") or "").split(",") if e.strip()], uf.get("ha_low")
            lvl = battery_level([self.get(e) for e in ents])
            if lvl is None:
                continue
            key = "battery:%s" % b["id"]
            lowv = float(low or 15)
            if lvl <= lowv:
                self.D.setdefault("low", {})[key] = now
                out.append({"kind": "battery", "id": b["id"], "name": "Replace the battery: " + b["name"], "due": now,
                            "overdue": True, "level": lvl, "uf": {}})
            elif key in self.D.get("low", {}) and lvl >= lowv + 30:    # came back up: it was replaced
                self.gc("POST", "/batteries/%d/charge" % b["id"], {})
                self.D["low"].pop(key, None)
                self.g["ilog"]({"src": "house", "action": "battery_replaced", "battery": b["name"], "level": lvl})
        return out

    def tick(self):
        now = time.time()
        shown = []
        for it in self.items():
            uf = it.pop("uf")
            it["show_on"] = [x.strip() for x in str(uf.get("ctx_show_on") or "").split(",") if x.strip()]
            it["rooms"] = [x.strip() for x in str(uf.get("ctx_rooms") or "").split(",") if x.strip()]
            sw = check(uf.get("ctx_show_when"), self.get)
            it["visible"] = sw is not False
            dw = uf.get("ctx_done_when")
            due_now = it["kind"] == "task" or (it["due"] is not None and it["due"] <= now)
            if dw and due_now and check(dw, self.get):
                key = "%s:%s" % (it["kind"], it["id"])
                if now - self.D["auto_done"].get(key, 0) > 3600:      # once per hour at most
                    path = "/tasks/%s/complete" % it["id"] if it["kind"] == "task" else "/chores/%s/execute" % it["id"]
                    self.gc("POST", path, {} if it["kind"] == "task" else {"tracked_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                    self.D["auto_done"][key] = now
                    self.g["ilog"]({"src": "house", "action": "auto_done", "item": key, "name": it["name"], "when": dw})
                    continue
            shown.append(it)
        shown.sort(key=lambda i: (not i["overdue"], i["due"] or 9e12, i["name"]))
        due = [i for i in shown if i["visible"] and (i["kind"] != "chore" or (i["due"] and i["due"] <= now + 86400))]
        cfg = self.g["HOUSE"].get("house_tasks") or {}
        floor_of = {room: floor for floor, rooms in (cfg.get("floors") or {}).items() for room in rooms}
        by_display = {d: [dict(i, here=is_here(i, room, floor_of)) for i in rank(due, room, floor_of, now)[:int(cfg.get("per_display", 6))]]
                      for d, room in (cfg.get("displays") or {}).items()}
        attrs = {"friendly_name": "House tasks", "icon": "mdi:clipboard-check-outline", "items": due[:40],
                 "all": len(shown), "to_review": getattr(self, "to_review", 0),
                 "private": getattr(self, "private", []), "displays": by_display, "updated": now}
        sig = json.dumps(due, sort_keys=True, default=str)
        if sig != self.last_sig or now - self.D.get("pub", 0) > 600:
            self.g["ha_post_state"]("sensor.house_tasks", str(len(due)), attrs, self.tok)
            self.last_sig, self.D["pub"] = sig, now
        self.g["GUARD"]["house_tasks"] = due
        self.g["GUARD"]["house_tasks_displays"] = by_display
        self.g["GUARD"]["house_tasks_meta"] = {"private": attrs["private"], "to_review": attrs["to_review"]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.D))
        tmp.replace(self.path)


def run(g):
    h = HouseTasks(g)
    while True:
        try:
            h.tick()
        except Exception as ex:  # noqa: BLE001
            print("house tasks error", repr(ex), flush=True)
        time.sleep(60)
