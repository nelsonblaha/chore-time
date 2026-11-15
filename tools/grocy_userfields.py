"""Create the userfields Chore Time reads in Grocy: chores, tasks and batteries (idempotent). Never prints the API key.
Kids' names for the "Only for" checklist come from your chores.json."""
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _house import DATA, grocy  # noqa: E402

API, KEY = grocy()
KIDS = "\n".join(k["name"] for k in json.loads((DATA / "chores.json").read_text())["kids"])


def call(method, path, body=None):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"GROCY-API-KEY": KEY, "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        t = r.read()
        return json.loads(t) if t else None


FIELDS = [
    ("kids", "Kids' screens", "checkbox", None, 1),
    ("kid_points", "Kids' points (empty = grown-up sets)", "number-integral", None, 2),
    ("kid_icon", "Kids' icon (emoji code, e.g. 1f9f9)", "text-single-line", None, 3),
    ("kid_only", "Only for", "preset-checklist", KIDS, 4),
    ("kid_floor", "Floor", "preset-list", "Downstairs\nUpstairs\nOther", 5),
    ("kid_where", "Where", "text-single-line", None, 6),
    ("kid_map", "Map rooms (floor-plan ids, comma separated)", "text-single-line", None, 7),
    ("kid_key", "Chore app key (don't change)", "text-single-line", None, 8),
    ("kid_extra", "Chore app extras (JSON: shared, daylight, slot, each_daily, ...)", "text-multi-line", None, 9),
]
# the grown-ups' side (house_tasks.py): context on chores and tasks, and a Home Assistant link for batteries
HOUSE_ROOMS = "\n".join(r for rooms in ((json.loads((DATA / "house.json").read_text()).get("house_tasks") or {}).get("floors") or {}).values()
                        for r in rooms) if (DATA / "house.json").exists() else ""
CONTEXT = [
    ("ctx_priority", "Priority", "preset-list", "High\nNormal\nLow", 18),
    ("ctx_rooms", "Rooms (where it's done)", "preset-checklist" if HOUSE_ROOMS else "text-single-line", HOUSE_ROOMS or None, 19),
    ("ctx_show_on", "Show on (displays, comma separated; empty = everywhere)", "text-single-line", None, 20),
    ("ctx_show_when", "Show only when (HA condition or now.hour / now.weekday, e.g. person.jordan = home)", "text-single-line", None, 21),
    ("ctx_done_when", "Done by itself when (HA condition)", "text-single-line", None, 22),
]
BATTERIES = [
    ("ha_entity", "Home Assistant battery sensor(s), comma separated", "text-single-line", None, 1),
    ("ha_low", "Replace at or below (%)", "number-integral", None, 2),
]
have = {(u["entity"], u["name"]) for u in call("GET", "/objects/userfields")}
for entity, name, cap, typ, conf, sort in ([("chores",) + f for f in FIELDS + CONTEXT] + [("tasks",) + f for f in CONTEXT]
                                           + [("batteries",) + f for f in BATTERIES]):
    if (entity, name) in have:
        print("exists", entity, name)
        continue
    body = {"entity": entity, "name": name, "caption": cap, "type": typ, "sort_number": sort, "input_required": 0,
            "show_as_column_in_tables": 1 if name in ("kids", "kid_points") else 0}
    if conf:
        body["config"] = conf
    print("created", name, call("POST", "/objects/userfields", body))
