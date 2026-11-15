"""House tasks: the grown-ups' side, from Grocy.

Every minute: the grown-ups' chores that are due (Grocy chores not marked for the kids), the open one-off Grocy tasks,
and batteries that need replacing. Each item can carry context (Grocy userfields):

  ctx_show_on    displays it belongs on, comma list (empty = everywhere)
  ctx_show_when  only show while this Home Assistant condition holds, e.g.  person.ben = home
  ctx_done_when  done by itself when this holds, e.g.  binary_sensor.washer_running = off and sensor.x > 50

Conditions: "<entity> <op> <value>" joined by "and"; op is one of = != > < >= <=. A due item whose ctx_done_when
comes true is completed in Grocy (once). Batteries (Grocy batteries with userfields ha_entity, ha_low) become
"Replace the battery" items while the Home Assistant level is at or below ha_low, and are tracked as replaced in
Grocy when the level comes back up. Everything is published as sensor.house_tasks for the boards.
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
        for t in self.gc("GET", "/objects/tasks") or []:
            if str(t.get("done")) == "1":
                continue
            due = ts(t.get("due_date"))
            out.append({"kind": "task", "id": t["id"], "name": t["name"], "due": due, "overdue": bool(due and due < now),
                        "assigned": users.get(t.get("assigned_to_user_id")), "category_id": t.get("category_id"),
                        "uf": t.get("userfields") or {}})
        for b in self.gc("GET", "/objects/batteries") or []:
            uf = b.get("userfields") or {}
            ent, low = (uf.get("ha_entity") or "").strip(), uf.get("ha_low")
            if not ent:
                continue
            lvl = self.get(ent).get("state")
            try:
                lvl = float(lvl)
            except (TypeError, ValueError):
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
        attrs = {"friendly_name": "House tasks", "icon": "mdi:clipboard-check-outline", "items": due[:40],
                 "all": len(shown), "updated": now}
        sig = json.dumps(due, sort_keys=True, default=str)
        if sig != self.last_sig or now - self.D.get("pub", 0) > 600:
            self.g["ha_post_state"]("sensor.house_tasks", str(len(due)), attrs, self.tok)
            self.last_sig, self.D["pub"] = sig, now
        self.g["GUARD"]["house_tasks"] = due
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
