#!/usr/bin/env python3
"""Chore app: kid touch UI + inspection API. Append-only JSONL ledger; balances derived.

Events (one JSON object per line):
  report   {kid, chore, feeling, points|None}            -> pending until inspected
  inspect  {ref, by, points, ok:bool}                    -> ok=True credits points
  spend    {kid, reward, cost}
  adjust   {kid, points(+/-), reason, by}
  undo     {ref, by}                                     -> voids a report/spend/adjust/inspect
"""
import hashlib
import hmac
import json
from tidy_spaces import space_states
import stepped_jobs
import ipaddress
import math, random, os, re, stat, threading, time, uuid, urllib.request, urllib.parse, urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = HERE / "example-config" / "chores.json"
DATA = Path(os.environ.get("CHORE_DATA", Path.home() / ".local/share/chore-time")).expanduser()
HOUSE_FILE = Path(os.environ.get("CHORE_HOUSE", DATA / "house.json")).expanduser()


def _load_house():
    """house.json: everything about THIS home (devices, people, hours, optional modules). Missing file or section =
    that module is off; the core (chores, points, rewards, the kid/parent/TV-lock pages) needs none of it."""
    try:
        return json.loads(HOUSE_FILE.read_text())
    except (OSError, ValueError):
        return {}


HOUSE = _load_house()
from failover import Authority, FencedError  # noqa: E402
AUTHORITY = Authority(HOUSE.get("failover"), DATA)
import envelopes  # noqa: E402
import vacuum_help  # noqa: E402
envelopes.configure(HOUSE.get("envelopes"), str(DATA / "envelopes_printed.json"))
import schedule  # noqa: E402
schedule.configure(HOUSE.get("kid_calendar"), str(DATA / "kid_calendar.json"))
HA = HOUSE.get("home_assistant") or {}   # empty = no Home Assistant: TV locks, Hubs, lights, vacuum, watch login all off
REWARD_BLOCK_CACHE = {"ts": 0, "value": {"on": False, "items": []}}
PACKING_PROGRESS_FILE = DATA / "packing_progress.json"
PACKING_LOCK = threading.Lock()


def packing_config():
    """Private trip details may live outside house.json so they can be revised without touching code."""
    raw_path = HOUSE.get("packing_game_file")
    path = Path(str(raw_path)).expanduser() if raw_path else None
    if path:
        try:
            value = json.loads(path.read_text())
            if isinstance(value, dict):
                return packing_public(value)
        except (OSError, ValueError):
            pass
    value = HOUSE.get("packing_game")
    return packing_public(value) if isinstance(value, dict) else value


def departure_display(now=None):
    """Fresh, Travel-owned departure-day snapshot for the household TV page.

    Travel owns whether this is active and refreshes it with the trip watcher.
    Chore only validates and renders it; a stale or malformed snapshot disappears.
    """
    raw_path = HOUSE.get("departure_display_file")
    if not raw_path:
        return None
    try:
        value = json.loads(Path(str(raw_path)).expanduser().read_text())
        now = time.time() if now is None else float(now)
        if not isinstance(value, dict) or value.get("schema") != 1 or value.get("active") is not True:
            return None
        updated = float(value.get("updated_at"))
        wake = float(value.get("wake_at"))
        board = float(value.get("board_at"))
        if not now - 2700 <= updated <= now + 300 or not wake < board or board - wake > 86400:
            return None
        if board < now - 300 or wake > now + 6 * 3600:
            return None
        mode = str(value.get("mode") or "")
        if mode not in ("train", "flight"):
            return None
        out = {"schema": 1, "updated_at": updated, "trip_id": str(value.get("trip_id") or "")[:80],
               "mode": mode, "title": str(value.get("title") or ("Train day" if mode == "train" else "Flight day"))[:100],
               "wake_at": wake, "board_at": board, "board_label": str(value.get("board_label") or "Board")[:80]}
        travelers = value.get("travelers")
        if isinstance(travelers, list):
            clean_travelers = []
            for traveler in travelers[:12]:
                traveler = str(traveler).strip().lower()
                if re.fullmatch(r"[a-z0-9_-]{1,40}", traveler) and traveler not in clean_travelers:
                    clean_travelers.append(traveler)
            if clean_travelers:
                out["travelers"] = clean_travelers
        if mode == "train" and value.get("pull_in_at") is not None and value.get("leave_home_at") is not None:
            pull_in = float(value["pull_in_at"])
            leave_home = float(value["leave_home_at"])
            # Travel's walking milestone is 30 minutes on foot plus a 15-minute
            # station buffer, counted back from pull-in (never train departure).
            if wake <= leave_home < pull_in <= board and abs((pull_in - leave_home) - 2700) <= 60:
                out["pull_in_at"] = pull_in
                out["leave_home_at"] = leave_home
        route = value.get("map_route")
        if mode == "train" and "leave_home_at" in out and isinstance(route, dict):
            def map_point(point):
                if not isinstance(point, dict):
                    return None
                try:
                    lat, lon = float(point.get("lat")), float(point.get("lon"))
                except (TypeError, ValueError):
                    return None
                return {"lat": lat, "lon": lon} if 15 <= lat <= 60 and -130 <= lon <= -60 else None

            def map_line(points):
                if not isinstance(points, list) or not 2 <= len(points) <= 300:
                    return None
                clean = [map_point(point) for point in points]
                return clean if all(clean) else None

            traveled, remaining = map_line(route.get("traveled")), map_line(route.get("remaining"))
            train, home = map_point(route.get("train")), map_point(route.get("home"))
            if traveled and remaining and train and home:
                out["map_route"] = {"traveled": traveled, "remaining": remaining, "train": train,
                                    "home": dict(home, label=str((route.get("home") or {}).get("label") or "Home")[:40])}
        near = value.get("nearest")
        if mode == "train" and isinstance(near, dict) and str(near.get("name") or "").strip():
            arr = float(near.get("arrival_at"))
            dep = float(near.get("departure_at"))
            if arr <= dep and board - 86400 <= arr <= board + 86400:
                out["nearest"] = {"name": str(near["name"]).strip()[:80], "arrival_at": arr,
                                  "departure_at": dep, "at_now": near.get("at_now") is True}
        return out
    except (OSError, ValueError, TypeError):
        return None


def departure_house_tasks(now=None):
    """Sanitized Travel-owned checklist, with timing recomputed against Chore's clock."""
    raw_path = HOUSE.get("departure_house_tasks_file")
    if not raw_path:
        return None
    try:
        value = json.loads(Path(str(raw_path)).expanduser().read_text())
        now = time.time() if now is None else float(now)
        updated = float(value.get("updated_at"))
        leave = float(value.get("leave_home_at"))
        if (not isinstance(value, dict) or value.get("schema") != 1 or value.get("active") is not True
                or not now - 2700 <= updated <= now + 300 or not now - 86400 <= leave <= now + 86400):
            return None
        tasks = []
        allowed_tiers = ("last day", "departure day", "last 4 hours", "last hour", "last minute")
        for raw in (value.get("tasks") or [])[:60]:
            if not isinstance(raw, dict) or raw.get("tier") not in allowed_tiers:
                continue
            item_id = str(raw.get("id") or "")[:80]
            name = str(raw.get("name") or "")[:160]
            opens, due = float(raw.get("opens_at")), float(raw.get("due_at"))
            if not item_id or not name or opens > due or due - opens > 172800:
                continue
            done = raw.get("status") == "done" or raw.get("done_at") is not None
            status = "done" if done else "upcoming" if now < opens else "overdue" if now > due else "open"
            task = {"id": item_id, "name": name, "tier": raw["tier"], "opens_at": opens,
                    "due_at": due, "status": status}
            if raw.get("after"):
                task["after"] = str(raw["after"])[:80]
            tasks.append(task)
        return {"schema": 1, "updated_at": updated, "trip_id": str(value.get("trip_id") or "")[:80],
                "title": str(value.get("title") or "Before you leave")[:120],
                "leave_home_at": leave, "tasks": tasks}
    except (OSError, ValueError, TypeError):
        return None


def house_path(p):
    return Path(str(p)).expanduser() if p else None


def ha_token():
    try:
        return HA_TOKEN_FILE.read_text().strip()
    except (OSError, NameError):
        return ""


def reward_block_state(fresh=False):
    """Live, fail-open household gates that apply only to spending points.

    A blocker is active only when Home Assistant explicitly reports one of its
    configured active states. Missing entities, transport errors, unknown and
    unavailable therefore never strand a child at the purchase screen.
    """
    rules = HOUSE.get("reward_blockers") or []
    if not rules or not HA_URL:
        return {"on": False, "items": []}
    now = time.time()
    if not fresh and now - REWARD_BLOCK_CACHE["ts"] < 2:
        return REWARD_BLOCK_CACHE["value"]
    items = []
    try:
        tok = ha_token()
        for rule in rules:
            entity = str(rule.get("entity") or "")
            if not entity:
                continue
            state = str((ha_get(entity, tok) or {}).get("state") or "").lower()
            active_states = {str(x).lower() for x in (rule.get("states") or ["on", "open"])}
            if state in active_states:
                items.append({"name": rule.get("name") or "Household check",
                              "message": rule.get("message") or "Please fix this, then try again.",
                              "icon": rule.get("icon") or "1f6aa"})
    except Exception:  # sensor/API/token trouble is deliberately fail-open
        items = []
    value = {"on": bool(items), "items": items}
    REWARD_BLOCK_CACHE.update(ts=now, value=value)
    return value
LEDGER = DATA / "ledger.jsonl"
ILOG = DATA / "interactions.jsonl"
VACUUM_BUTTON_TOKEN_FILE = Path(os.environ.get("VACUUM_BUTTON_TOKEN_FILE", DATA / "vacuum-button.token")).expanduser()
NEWS_SEEN = DATA / "news_seen.json"   # whats-new version acknowledged, per device IP   # every UI tap/screen + TV guard decision, for later study
INSPECTION_SEEN_FILE = DATA / "inspection_seen_displays.json"  # read-only copies of each real Hub's local seen_<kid>
PAPER_SCAN_FILE = DATA / "paper_scans.json"   # short-lived kid claim for the next Brother-to-Drive scan
PEER_PIN_FILE = DATA / "peer_pin_resets.json"
HUB_PLACEMENT_FILE = DATA / "hub_placement.json"
PEER_PIN_CFG = HOUSE.get("peer_pin_reset") or {}
PEER_LOCK = threading.Lock()
INSPECTION_SEEN_LOCK = threading.Lock()
HUB_CHILD_LOCK = threading.Lock()
SERVER_STARTED_AT = time.time()
APP_REVISION = str(os.environ.get("CHORE_REVISION") or "unknown")


def ilog(rec):
    rec = {"ts": time.time(), **rec}
    try:
        with LOCK, open(ILOG, "a") as f:
            f.write(json.dumps(rec)[:4000] + "\n")
    except OSError:
        pass
PORT = int(os.environ.get("CHORE_PORT", HOUSE.get("port", 8790)))
HA_URL = os.environ.get("HA_URL", HA.get("url", ""))
HA_TOKEN_FILE = house_path(os.environ.get("HA_TOKEN_FILE") or HA.get("token_file")) or (DATA / "ha-token")
LOCK = threading.Lock()
PROJECT_LOCK = threading.Lock()
SHARED_REWARD_LOCK = threading.Lock()
TV_REWARD_LOCK = threading.Lock()
CT_SWITCH = HA.get("chore_time_switch", "input_boolean.chore_time")      # mirrors chore time in HA
CT_HOURS = HA.get("chore_time_hours", "input_number.chore_time_hours")
RELAY_TEXT = HA.get("relay_text", "input_text.chore_action")              # HA dashboards post actions here
SUN = HA.get("sun", "sun.sun")
CONFIG = DATA / "chores.json"
DATA.mkdir(parents=True, exist_ok=True)
if not CONFIG.exists():
    AUTHORITY.require_mutation()
    CONFIG.write_text(SEED.read_text())


def load_paper_scans():
    try:
        rows = json.loads(PAPER_SCAN_FILE.read_text())
        return rows if isinstance(rows, dict) else {}
    except (OSError, ValueError):
        return {}


PAPER_SCANS = load_paper_scans()


def load_private_json(path, default):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default


PEER_PIN_RESETS = load_private_json(PEER_PIN_FILE, {})
HUB_PLACEMENT = load_private_json(HUB_PLACEMENT_FILE, {"trusted": False, "pending": bool(PEER_PIN_CFG)})


def save_private_json(path, value):
    AUTHORITY.require_mutation()
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", opener=lambda name, flags: os.open(name, flags, 0o600)) as output:
        output.write(json.dumps(value, indent=2))
    tmp.replace(path)
    os.chmod(path, 0o600)


def packing_packer_ids(profile):
    """All people assigned to a list. ``kids`` retains its separate gate/award meaning."""
    ids = list(profile.get("kids") or [])
    for packer in profile.get("packers") or []:
        packer_id = packer.get("id") if isinstance(packer, dict) else packer
        if packer_id and packer_id not in ids:
            ids.append(str(packer_id))
    return ids


def packing_profile(game, packer):
    """The one packing profile assigned to a child or authenticated grown-up packer."""
    return next((p for p in (game.get("profiles") or []) if packer in packing_packer_ids(p)), None)


def packing_item_ids(profile, packer):
    return {x["id"] for pile in (profile.get("piles") or []) for x in (pile.get("items") or [])
            if x.get("id") and (not x.get("only") or packer in x["only"])}


def sanitize_packing_progress(game, packer, raw):
    profile = packing_profile(game, packer)
    if not profile or not isinstance(raw, dict):
        return None
    item_ids = packing_item_ids(profile, packer)
    final_ids = {x["id"] for x in (profile.get("final_checks") or []) if x.get("id")}
    bools = lambda name, allowed: {str(k): True for k, v in (raw.get(name) or {}).items()
                                   if str(k) in allowed and v is True}
    checks = lambda name, allowed: {str(k): str(v) for k, v in (raw.get(name) or {}).items()
                                    if str(k) in allowed and v in ("pass", "fix")}
    area_ids = {str(x.get("id")) for x in (profile.get("areas") or [])
                if isinstance(x, dict) and x.get("id")}
    locations = {str(k): str(v) for k, v in (raw.get("locations") or {}).items()
                 if str(k) in item_ids and str(v) in area_ids}
    phase = str(raw.get("phase") or "layout")
    if phase not in ("layout", "inspect", "pack", "final", "complete"):
        phase = "layout"
    try:
        seq = max(0, int(raw.get("seq") or 0))
    except (TypeError, ValueError):
        seq = 0
    return {"version": str(game.get("version") or ""), "profile": profile.get("id"), "packer": packer,
            "seq": seq,
            "phase": phase, "ready": bools("ready", item_ids), "checked": checks("checked", item_ids),
            "packed": bools("packed", item_ids), "finals": checks("finals", final_ids),
            "locations": locations}


def packing_complete(game, packer, progress):
    profile = packing_profile(game, packer)
    if not profile or not progress or progress.get("phase") != "complete":
        return False
    items = packing_item_ids(profile, packer)
    finals = {x["id"] for x in (profile.get("final_checks") or []) if x.get("id")}
    return (all(progress.get("checked", {}).get(x) == "pass" and progress.get("packed", {}).get(x) is True
                for x in items)
            and all(progress.get("finals", {}).get(x) == "pass" for x in finals))


PACKING_AWARD_STAGES = ("layout", "inspect", "pack", "complete")
PACKING_AWARD_DEFAULT_LABELS = {
    "layout": "Packing: laid everything out",
    "inspect": "Packing: every item passed inspection",
    "pack": "Packing: everything is in the right bag",
    "complete": "Finished train packing",
}


def packing_award_plan(profile):
    """Stage -> points. Old profiles keep their one completion award."""
    configured = profile.get("awards")
    if isinstance(configured, dict):
        out = {}
        for stage in PACKING_AWARD_STAGES:
            try:
                out[stage] = max(0, min(50, int(configured.get(stage) or 0)))
            except (TypeError, ValueError):
                out[stage] = 0
        return out
    return {stage: int(profile.get("points") or 0) if stage == "complete" else 0
            for stage in PACKING_AWARD_STAGES}


def packing_milestones_reached(game, packer, progress):
    profile = packing_profile(game, packer)
    if not profile or not progress:
        return set()
    items = packing_item_ids(profile, packer)
    ready = all(progress.get("ready", {}).get(x) is True
                or progress.get("checked", {}).get(x) == "pass" for x in items)
    inspected = ready and all(progress.get("checked", {}).get(x) == "pass" for x in items)
    packed = inspected and all(progress.get("packed", {}).get(x) is True for x in items)
    reached = set()
    if ready and progress.get("phase") in ("inspect", "pack", "final", "complete"):
        reached.add("layout")
    if inspected and progress.get("phase") in ("pack", "final", "complete"):
        reached.add("inspect")
    if packed and progress.get("phase") in ("final", "complete"):
        reached.add("pack")
    if packing_complete(game, packer, progress):
        reached.add("complete")
    return reached


def active_packing_award_events(game):
    """Packing awards still active after append-only undo events, keyed by event id."""
    events = read_events()
    undone = {e.get("ref") for e in events if e.get("type") == "undo"}
    return {e["id"]: e for e in events if e.get("type") == "adjust" and e.get("id") not in undone
            and e.get("packing_version") == game.get("version")}


def packing_progress(game):
    saved = load_private_json(PACKING_PROGRESS_FILE, {})
    rows = saved.get("kids") or {} if saved.get("version") == game.get("version") else {}
    active_awards = active_packing_award_events(game)
    out = {}
    for profile in game.get("profiles") or []:
        for packer in packing_packer_ids(profile):
            if packer not in rows:
                continue
            clean = sanitize_packing_progress(game, packer, rows.get(packer) or {})
            if clean:
                clean["complete"] = packing_complete(game, packer, clean)
                saved_row = rows.get(packer) or {}
                plan = packing_award_plan(profile)
                award_refs = saved_row.get("awards") if isinstance(saved_row.get("awards"), dict) else {}
                clean["awards"] = {stage: plan[stage] for stage in PACKING_AWARD_STAGES
                                   if award_refs.get(stage) in active_awards and plan[stage]}
                clean["awarded_points"] = sum(clean["awards"].values())
                clean["award_total"] = sum(plan.values())
                clean["complete_prize"] = plan.get("complete", 0)
                clean["awarded"] = bool(saved_row.get("awarded")) or clean["awarded_points"] >= clean["award_total"]
                seq_time = float(clean.get("seq") or 0) / 1000 if float(clean.get("seq") or 0) >= 1e12 else None
                clean["started_at"] = saved_row.get("started_at") or seq_time
                clean["updated_at"] = saved_row.get("updated_at") or seq_time
                had_physical_progress = (clean.get("phase") != "layout"
                                         or any(clean.get(k) for k in ("ready", "checked", "packed", "finals")))
                clean["interacted_at"] = saved_row.get("interacted_at") or (
                    clean["updated_at"] if had_physical_progress else None)
                out[packer] = clean
    return out


def packing_activity_changed(old, new):
    """Sequence/open heartbeats are not physical packing interactions."""
    fields = ("phase", "ready", "checked", "packed", "finals")
    empty_open = not old and new.get("phase") == "layout" and not any(new.get(k) for k in fields[1:])
    return not empty_open and any((old or {}).get(k) != new.get(k) for k in fields)


def child_packing_update_error(game, packer, old, new):
    """Return why a child-originated transition is invalid; inspection fields belong to adults."""
    old = old or {}
    if new.get("checked") != (old.get("checked") or {}) or new.get("finals") != (old.get("finals") or {}):
        return "Grown-up inspection happens in the parent Chores page."
    if new.get("locations") != (old.get("locations") or {}):
        return "A grown-up controls the packing-area passes."
    if new.get("phase") == "complete":
        return "The grown-up must finish the final packing check."
    old_phase = old.get("phase") or "layout"
    phase = new.get("phase") or "layout"
    allowed = {"layout": {"layout", "inspect"},
               "inspect": {"inspect", "layout"},
               "pack": {"pack", "layout", "final"},
               "final": {"final"}}
    if phase not in allowed.get(old_phase, {old_phase}):
        return "Wait for the grown-up before moving to the next packing stage."
    item_ids = packing_item_ids(packing_profile(game, packer), packer)
    if phase == "inspect" and not all(new.get("ready", {}).get(x) is True for x in item_ids):
        return "Lay out every item before asking for inspection."
    if old_phase != "pack" and new.get("packed") != (old.get("packed") or {}):
        return "Items go into bags only after the grown-up inspection."
    if phase == "final" and not all(new.get("packed", {}).get(x) is True for x in item_ids):
        return "Put every inspected item into its bag before the final check."
    return None


def packing_private_config():
    """Return the editable private source and its configured path, never a public-code fallback."""
    raw_path = HOUSE.get("packing_game_file")
    path = Path(str(raw_path)).expanduser() if raw_path else None
    if not path:
        return None, None
    value = load_private_json(path, {})
    return (value, path) if value else (None, path)


def packing_edit_config(game, body):
    """Apply one bounded category/item edit while retaining the trip version and saved progress."""
    action = str(body.get("action") or "")
    profiles = game.get("profiles") or []
    profile = next((p for p in profiles if p.get("id") == body.get("profile")), None)
    if not profile:
        raise ValueError("unknown packing list")

    def text_value(name, default="", limit=160):
        value = str(body.get(name, default) or "").strip()
        if name in ("title", "name") and not value:
            raise ValueError("a name is required")
        return value[:limit]

    piles = profile.setdefault("piles", [])
    pile = next((x for x in piles if x.get("id") == body.get("pile")), None)
    if action == "add_category":
        piles.append({"id": "pile_" + uuid.uuid4().hex[:10], "title": text_value("title"),
                      "icon": text_value("icon", "1f9f3", 40), "note": text_value("note", limit=240),
                      "items": []})
        return
    if not pile:
        raise ValueError("unknown packing category")
    if action == "edit_category":
        pile.update({"title": text_value("title", pile.get("title")),
                     "icon": text_value("icon", pile.get("icon") or "1f9f3", 40),
                     "note": text_value("note", pile.get("note") or "", 240)})
        return
    if action == "remove_category":
        piles.remove(pile)
        return
    items = pile.setdefault("items", [])
    item = next((x for x in items if x.get("id") == body.get("item")), None)
    if action == "add_item":
        item_id = "item_" + uuid.uuid4().hex[:10]
        new_item = {"id": item_id, "name": text_value("name"),
                    "icon": text_value("icon", pile.get("icon") or "1f4e6", 40),
                    "note": text_value("note", limit=240)}
        only = str(body.get("only") or "")
        if only and only in packing_packer_ids(profile):
            new_item["only"] = [only]
        items.append(new_item)
        return {"item": item_id, "only": only or None}
    if not item:
        raise ValueError("unknown packing item")
    if action == "edit_item":
        item.update({"name": text_value("name", item.get("name")),
                     "icon": text_value("icon", item.get("icon") or pile.get("icon") or "1f4e6", 40),
                     "note": text_value("note", item.get("note") or "", 240)})
        return
    if action == "remove_item":
        items.remove(item)
        return
    raise ValueError("unknown packing edit")


def packing_public(game):
    public = json.loads(json.dumps(game))
    if public.get("departure_house_checks"):
        lead = globals().get("LEAD_ADULT")
        profile = packing_profile(public, lead) if lead else None
        if profile:
            checks = profile.setdefault("final_checks", [])
            existing = {x.get("id") for x in checks}
            checks_to_add = [
                {"id": "house_smelly_trash", "name": "All smelly trash is out",
                 "icon": "1f5d1", "within_leave_s": 86400,
                 "note": "Do this within the final 24 hours before actually leaving home."},
                {"id": "house_toilets_flushed", "name": "Every toilet is checked and flushed",
                 "icon": "1f6bd", "within_leave_s": 3600,
                 "note": "Do this within the final hour before leaving home, before any toilet water is shut off."},
            ]
            trip = public.get("trip") if isinstance(public.get("trip"), dict) else {}
            try:
                overnight = bool(public.get("overnight") or trip.get("overnight")
                                 or int(trip.get("nights") or 0) > 0)
            except (TypeError, ValueError):
                overnight = bool(public.get("overnight") or trip.get("overnight"))
            if overnight:
                checks_to_add.extend([
                    {"id": "house_overnight_shower",
                     "name": "Shower during the last hour before leaving home",
                     "icon": "1f6bf", "within_leave_s": 3600,
                     "note": "For an overnight away: shower within the final hour before leaving home."},
                    {"id": "house_nest_cameras",
                     "name": "Re-enable the Nest Hub Max cameras during the last minute before leaving",
                     "icon": "1f4f7", "within_leave_s": 60,
                     "note": "For an overnight away: re-enable the cameras within the final minute."},
                ])
            for check in checks_to_add:
                if check["id"] not in existing:
                    checks.append(check)
    public["progress"] = packing_progress(public)
    return public


def packing_up_next(game, packer, limit=6):
    """Small read-only projection for ambient dashboards.

    An active, unfinished grown-up list temporarily replaces ordinary house jobs
    on Faults displays.  Keep the projection deliberately narrow so those
    displays do not have to understand or mutate the packing workflow.
    """
    if not isinstance(game, dict) or not game.get("activated") or not packer:
        return None
    profile = packing_profile(game, packer)
    if not profile:
        return None
    progress = (game.get("progress") or {}).get(packer) or {}
    if progress.get("complete") or packing_complete(game, packer, progress):
        return None
    packed = progress.get("packed") or {}
    locations = progress.get("locations") or {}
    area_defs = [x for x in (profile.get("areas") or []) if isinstance(x, dict) and x.get("id")]
    default_area = profile.get("default_area") or (area_defs[0].get("id") if area_defs else None)
    items = []
    for pile in profile.get("piles") or []:
        groups = {x.get("id"): x for x in (pile.get("groups") or []) if isinstance(x, dict) and x.get("id")}
        for item in pile.get("items") or []:
            if (not item.get("id") or (item.get("only") and packer not in item["only"])
                    or packed.get(item["id"]) is True):
                continue
            group = groups.get(item.get("group")) or {}
            area = locations.get(item["id"]) or item.get("area") or pile.get("area") or default_area
            items.append({"id": item["id"], "name": str(item.get("name") or "Packing item")[:120],
                          "icon": str(item.get("icon") or pile.get("icon") or "1f4e6")[:40],
                          "pile_id": pile.get("id"),
                          "pile": str(group.get("title") or pile.get("title") or "Packing")[:80],
                          "container": str(pile.get("title") or "Packing")[:80] if group else None,
                          "area": area,
                          "kind": "item"})
    active_area = next((x for x in area_defs if any(i.get("area") == x["id"] for i in items)), None)
    if active_area:
        items = [x for x in items if x.get("area") == active_area["id"]]
    if not items:
        finals = progress.get("finals") or {}
        for check in profile.get("final_checks") or []:
            if check.get("id") and finals.get(check["id"]) != "pass":
                items.append({"id": check["id"], "name": str(check.get("name") or "Final check")[:120],
                              "icon": str(check.get("icon") or "2705")[:40],
                              "pile_id": "final", "pile": "Final checks", "kind": "check"})
    return {"active": True, "packer": packer, "profile": profile.get("id"),
            "title": str(profile.get("name") or game.get("title") or "Packing")[:120],
            "phase": str(progress.get("phase") or "layout"),
            "area": active_area.get("id") if active_area else None,
            "area_name": active_area.get("name") if active_area else None,
            "items": items[:max(1, min(int(limit), 12))]}


def packing_final_check_available(check, state, now=None):
    """Whether a time-sensitive house check is inside its window before leaving home."""
    try:
        within = int(check.get("within_leave_s") or 0)
    except (TypeError, ValueError):
        within = 0
    if within <= 0:
        return True, None
    leave = ((state.get("departure_display") or {}).get("leave_home_at")
             if isinstance(state, dict) else None)
    if not isinstance(leave, (int, float)):
        return False, None
    not_before = float(leave) - min(within, 7 * 86400)
    return (time.time() if now is None else float(now)) >= not_before, not_before


def packing_reward_gate(state, participants):
    game = state.get("packing_game") or {}
    if not game.get("activated") or not game.get("required_before_rewards"):
        return None
    for kid in participants:
        if packing_profile(game, kid) and not ((game.get("progress") or {}).get(kid) or {}).get("complete"):
            name = next((x.get("name") for x in (state.get("config") or {}).get("kids", [])
                         if x.get("id") == kid), kid)
            return {"kid": kid, "name": name,
                    "message": "Pack for the train first! Mandatory jobs, getting dressed, and brushing teeth stay open."}
    return None


def packing_chore_gate(state, kid, chore_id):
    """Hold ordinary jobs while this child's required packing quest is incomplete.

    Required household work always wins over the trip gate. The explicit list covers
    ordinary morning/evening duties; live mandatory gates contribute their current
    chore (or the built-in clean-up job) so overlapping policies cannot deadlock.
    """
    game = state.get("packing_game") or {}
    if not game.get("activated") or not game.get("required_before_other_chores"):
        return None
    if not packing_profile(game, kid) or ((game.get("progress") or {}).get(kid) or {}).get("complete"):
        return None
    allowed = set(game.get("chores_allowed_while_required") or ("get_dressed", "brush_teeth"))
    routine = ((state.get("evening_routine") or {}).get(kid) or {}).get("items") or []
    if "shower" in routine:  # a due shower is mandatory; an optional one is not
        allowed.add("shower")
    must = (state.get("must_do_first") or {}).get(kid) or {}
    if (must.get("on") or must.get("rewards")) and must.get("chore"):
        allowed.add(must["chore"])
    first = (state.get("chores_first") or {}).get(kid) or {}
    if first.get("on"):
        allowed.add(first.get("chore") or "cleanup_" + str(kid))
    chore = next((x for x in (state.get("config") or {}).get("chores", []) if x.get("id") == chore_id), {})
    if chore_id in allowed or chore.get("packing_priority") or chore.get("mandatory"):
        return None
    name = next((x.get("name") for x in (state.get("config") or {}).get("kids", [])
                 if x.get("id") == kid), kid)
    return {"kid": kid, "name": name,
            "message": "Pack for the train first! You can still do mandatory jobs, get dressed, and brush your teeth.",
            "allowed_chores": sorted(x for x in allowed if x)}


def kid_workflow_open(state):
    """A child is actively using the chore flow, so a dismissed Hub should return promptly."""
    if state.get("active"):
        return True
    game = state.get("packing_game") or {}
    if not game.get("activated"):
        return False
    progress = game.get("progress") or {}
    return any(not (progress.get(kid) or {}).get("complete")
               for profile in (game.get("profiles") or []) for kid in (profile.get("kids") or []))


def save_peer_pin_resets():
    save_private_json(PEER_PIN_FILE, PEER_PIN_RESETS)


def save_hub_placement():
    save_private_json(HUB_PLACEMENT_FILE, HUB_PLACEMENT)


def save_paper_scans():
    AUTHORITY.require_mutation()
    tmp = PAPER_SCAN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(PAPER_SCANS))
    tmp.replace(PAPER_SCAN_FILE)


JOB_MILESTONES = [1, 5, 10, 25, 50, 100]
POINT_MILESTONES = [10, 25, 50, 100, 250]
DAY_MILESTONES = [3, 7, 14, 30]


def fmt_s(sec):
    sec = int(round(sec))
    return "%d:%02d" % (sec // 60, sec % 60)


FAMILIES = [  # Duolingo-style tiered achievements
    ("jobs", "Hard Worker", "1f522", [1, 5, 10, 25, 50, 100], "jobs"),
    ("earned", "Star Collector", "1f4b0", [10, 25, 50, 100, 250], "points"),
    ("days", "Busy Bee", "1f41d", [3, 7, 14, 30], "days"),
    ("explorer", "Explorer", "1f5fa", [3, 6, 10, 15], "different chores"),
    ("team", "Team Player", "1f91d", [1, 3, 10, 25], "team jobs"),
    ("speed", "Speed Star", "26a1", [1, 3, 5, 10], "records"),
    ("streak", "Streak", "1f525", [2, 3, 5, 7, 14, 30], "days in a row"),
]
TIERS = ["bronze", "bronze", "silver", "silver", "gold", "diamond", "diamond"]


def achievements(c, reports, earned, aw):
    """Per kid: family levels with progress to the next level, plus the day streak."""
    from datetime import datetime, timedelta, date
    kids = [k["id"] for k in c["kids"]]
    passed = [r for r in reports.values() if r["status"] == "inspected" and not r.get("partial") and r["chore"] != "toy_swap"]
    out, streak, levelups = {}, {}, {}
    for k in kids:
        mine = [r for r in passed if k in (r.get("team") or [r["kid"]])]
        days_ = sorted({datetime.fromtimestamp(r["ts"]).date() for r in mine})
        today = date.today()
        hd = (GUARD.get("home_days") or load_home_days()).get(k)
        arr = (GUARD.get("home_arrivals") or load_home_arrivals()).get(k) or {}
        late = HOUSE.get("arrival_grace_hour", 19)

        def away(d):   # a day the kid wasn't home never breaks the streak; unknown history = home
            # an arrival day counts as away too when they got home at or after the grace hour (too late for a job)
            if hd is None or d >= today:
                return False
            return d.isoformat() not in hd or (arr.get(d.isoformat()) or "00:00") >= "%02d:00" % late
        cur = 0
        d = today if today in days_ else today - timedelta(days=1)
        for _ in range(400):
            if d in days_:
                cur += 1
            elif not away(d):
                break
            d -= timedelta(days=1)
        best, run, prev = 0, 0, None
        for d in days_:
            gap_ok = prev is not None and all(away(prev + timedelta(days=i)) for i in range(1, (d - prev).days))
            run = run + 1 if prev and gap_ok else 1
            best, prev = max(best, run), d
        streak[k] = {"current": cur, "best": best, "today": today in days_}
        vals = {"jobs": len(mine), "earned": earned.get(k, 0), "days": len(days_),
                "explorer": len({r["chore"] for r in mine}),
                "team": sum(1 for r in mine if len(r.get("team") or []) > 1),
                "speed": sum(1 for a in aw.get(k, []) if a["id"].startswith("record:")),
                "streak": max(cur, best)}
        fams = []
        for fid, name, icon, steps, unit in FAMILIES:
            v = vals[fid]
            lvl = sum(1 for x in steps if v >= x)
            nxt = steps[lvl] if lvl < len(steps) else None
            prv = steps[lvl - 1] if lvl else 0
            fams.append({"id": fid, "name": name, "icon": icon, "level": lvl, "max": len(steps), "value": v, "next": nxt,
                         "prev": prv, "unit": unit, "tier": TIERS[min(lvl, len(TIERS) - 1)] if lvl else "locked"})
        out[k] = fams
    return out, streak


def expected_times(c, reports, typical):
    """Per kid x chore expected minutes: recency-weighted own history (half-life 5 jobs), shrunk toward the
    everyone-else median (prior worth 2 jobs), prior = typical / adult estimate / 10 min. Plus personal bests
    (solo and per team size). Only inspected, timed, non-partial jobs; 30 s - 4 h; Tukey outliers dropped."""
    import math
    def clean(xs):
        xs = sorted(xs)
        if len(xs) >= 4:
            q1, q3 = xs[len(xs) // 4], xs[(3 * len(xs)) // 4]
            iqr = q3 - q1
            xs = [x for x in xs if q1 - 1.5 * iqr <= x <= q3 + 1.5 * iqr] or xs
        return xs
    def med(xs):
        xs = sorted(xs)
        return None if not xs else (xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2)
    runs = {}   # (chore, size) -> [(ts, team, minutes)]
    for r in reports.values():
        if r["status"] == "inspected" and r.get("started") and not r.get("partial"):
            m = (r["ts"] - r["started"]) / 60
            if 0.5 <= m <= 240:
                team = r.get("team") or [r["kid"]]
                runs.setdefault((r["chore"], len(team)), []).append((r["ts"], team, m))
    out, pb = {}, {}
    kids = [k["id"] for k in c["kids"]]
    for ch in c["chores"]:
        cid = ch["id"]
        prior = (typical.get(cid) or {}).get("minutes") or ch.get("est_minutes") or 10
        for size in (1, 2, 3):
            rs = runs.get((cid, size), [])
            for k in kids:
                mine = sorted([(ts, m) for ts, team, m in rs if k in team])
                others = clean([m for ts, team, m in rs if k not in team])
                if mine and cid != "other" and not ch.get("no_record"):   # "something else": nothing to compare
                    pb.setdefault(k, {})["%s@%d" % (cid, size) if size > 1 else cid] = round(min(m for _, m in mine) * 60)
                base = med(others) if others else (prior if size == 1 else None)
                if base is None:   # team size never seen: solo estimate, a bit faster per extra helper
                    solo = (out.get(k, {}).get(cid) or {}).get("minutes") or prior
                    base = solo / (size ** 0.7)
                own = clean([m for _, m in mine])
                own_sorted = [m for _, m in mine if m in own]
                w = [0.5 ** ((len(own_sorted) - 1 - i) / 5) for i in range(len(own_sorted))]
                own_avg = sum(a * b for a, b in zip(own_sorted, w)) / sum(w) if w else None
                n_eff = sum(w)
                K0 = 2.0
                est = base if own_avg is None else (n_eff * own_avg + K0 * base) / (n_eff + K0)
                key = cid if size == 1 else "%s@%d" % (cid, size)
                if size == 1 or rs or own_sorted:
                    out.setdefault(k, {})[key] = {"minutes": round(est, 1), "n_own": len(own_sorted), "n_other": len(others)}
    return out, pb


def compute_awards(c, reports, evs, earned, typical):
    """Awards + records from the ledger. Only INSPECTED jobs count (a quick 'done' that gets sent back never scores)."""
    from datetime import datetime
    chores = {x["id"]: x for x in c["chores"]}
    kids = [k["id"] for k in c["kids"]]
    aw = {k: [] for k in kids}
    def add(k, aid, icon, title, sub, ts):
        if k in aw and not any(a["id"] == aid for a in aw[k]):
            aw[k].append({"id": aid, "icon": icon, "title": title, "sub": sub, "ts": ts})
    passed = sorted([r for r in reports.values() if r["status"] == "inspected" and r["chore"] != "toy_swap" and not r.get("partial")],
                    key=lambda r: r.get("inspected_ts") or r["ts"])
    records, jobs, days = {}, {k: 0 for k in kids}, {k: set() for k in kids}
    pbs = {}   # each kid's own best time per job: {chore: {kid: {"sec", "ts"}}}
    school_dress = [e for e in evs if e.get("type") == "dress" and e.get("school")]

    def school_dress_tier(kid, completed):
        """Absolute-time school-morning award for an inspected Get dressed report."""
        session = next((e for e in reversed(school_dress) if e["ts"] <= completed), None)
        if not session or not session.get("on") or kid not in (session.get("kids") or []):
            return None
        done = datetime.fromtimestamp(completed)
        if datetime.fromtimestamp(session["ts"]).date() != done.date():
            return None
        for tier in (SCHOOL.get("awards") or []):
            try:
                hh, mm = (int(x) for x in str(tier["by"]).split(":"))
            except (KeyError, TypeError, ValueError):
                continue
            if (done.hour, done.minute, done.second) <= (hh, mm, 59):
                return tier
        return None

    expanded = []
    for r in passed:
        for m in (r.get("team") or [r["kid"]]):
            expanded.append(dict(r, kid=m, _team=len(r.get("team") or []) > 1))
    for r in expanded:
        k, cid = r["kid"], r["chore"]
        if k not in aw:
            continue
        ch = chores.get(cid, {})
        ts = r.get("inspected_ts") or r["ts"]
        jobs[k] += 1
        days[k].add(datetime.fromtimestamp(r["ts"]).date().isoformat())
        if jobs[k] in JOB_MILESTONES:
            add(k, "jobs:%d" % jobs[k], "1f522", "%d job%s done!" % (jobs[k], "" if jobs[k] == 1 else "s"), "Passed inspection", ts)
        if len(days[k]) in DAY_MILESTONES:
            add(k, "days:%d" % len(days[k]), "1f4c5", "Busy %d days!" % len(days[k]), "Jobs on %d different days" % len(days[k]), ts)
        if not ch.get("cleanup_for"):   # no medal for a "chores first" clean-up
            add(k, "first:" + cid, ch.get("icon", "1f31f"), "First " + ch.get("name", cid) + "!", "Your first time", ts)
        if ch.get("trophy"):   # a job that pays in trophies, every time (e.g. fixing something that got unplugged)
            t_ = ch["trophy"] if isinstance(ch["trophy"], dict) else {}
            took_ = r["ts"] - r["started"] if r.get("started") and 0 <= r["ts"] - r["started"] < 4 * 3600 else None
            add(k, "trophy:%s:%d" % (cid, int(ts)), t_.get("icon", "1f3c6"), t_.get("title", ch.get("name", cid) + "!"),
                t_.get("sub") or ("in " + fmt_s(took_) if took_ is not None else datetime.fromtimestamp(ts).strftime("%b %-d")), ts)
        if cid == "get_dressed":
            tier_ = school_dress_tier(k, r["ts"])
            if tier_:
                day_ = datetime.fromtimestamp(r["ts"]).date().isoformat()
                add(k, "school-dressed:%s" % day_, tier_.get("icon", "1f680"),
                    tier_.get("title", "School ready!"),
                    tier_.get("sub") or ("Dressed by " + str(tier_["by"])), ts)
        if ch.get("bundle"):
            add(k, "bundle:" + cid, "1f3e0", ch.get("name", cid) + "!", "The big job", ts)
        timed = cid != "other" and not ch.get("no_record")   # "something else" can be anything: no record to beat
        if timed and r.get("started") and r.get("_team"):   # team records: per chore AND team size
            sec = r["ts"] - r["started"]
            team = r.get("team") or [k]
            key = "%s@%d" % (cid, len(team))
            if 30 <= sec <= 4 * 3600:
                best = records.get(key)
                if not best or sec < best["sec"] or (best["ts"] == ts):
                    if not best or best["ts"] != ts:
                        records[key] = {"kid": team[0], "team": list(team), "size": len(team), "sec": sec, "ts": ts, "prev": best}
                    add(k, "record:%s:%d" % (key, int(ts)), "1f947", "Fastest %d-person %s ever!" % (len(team), ch.get("name", cid)),
                        fmt_s(sec) + ("" if not records[key].get("prev") else " (beat " + fmt_s(records[key]["prev"]["sec"]) + ")"), ts)
        if timed and r.get("started") and not r.get("_team"):
            sec = r["ts"] - r["started"]
            if 30 <= sec <= 4 * 3600:
                best = records.get(cid)
                mine = pbs.setdefault(cid, {}).get(k)
                if not best or sec < best["sec"]:
                    records[cid] = {"kid": k, "sec": sec, "ts": ts, "prev": best}
                    add(k, "record:%s:%d" % (cid, int(ts)), "1f947", "Fastest " + ch.get("name", cid) + " ever!",
                        fmt_s(sec) + ("" if not best else " (beat " + fmt_s(best["sec"]) + ")"), ts)
                elif mine and sec < mine["sec"]:   # not the fastest kid, but faster than they've ever been
                    add(k, "pb:%s:%d" % (cid, int(ts)), "1f3c5", "Your best " + ch.get("name", cid) + "!",
                        fmt_s(sec) + " (beat your " + fmt_s(mine["sec"]) + ")", ts)
                if not mine or sec < mine["sec"]:
                    pbs[cid][k] = {"sec": sec, "ts": ts}
                T = (typical.get(cid) or {}).get("minutes")
                if T and sec < T * 60 * 0.8 and (typical[cid]["n"] >= 3):
                    add(k, "speed:" + cid, "26a1", "Speedy " + ch.get("name", cid) + "!", "Faster than usual: " + fmt_s(sec), ts)
    # point milestones: stamped with the moment the running total crossed them (not "now", or they re-pop forever)
    gains = []
    for r in reports.values():
        if r["status"] == "inspected":
            for m_, v in shares(r, r["awarded"]).items():
                gains.append((r.get("inspected_ts") or r["ts"], m_, v))
    for e in evs:
        if e["type"] == "adjust":
            gains.append((e["ts"], e["kid"], int(e["points"])))
    run = {k: 0 for k in kids}
    for ts_, k, v in sorted(gains):
        if k not in run:
            continue
        before, run[k] = run[k], run[k] + v
        for m in POINT_MILESTONES:
            if before < m <= run[k]:
                add(k, "earned:%d" % m, "1f4b0", "%d points earned!" % m, "All-time", ts_)
    for r in reports.values():
        if r["chore"] == "toy_swap" and r["status"] == "inspected" and r["kid"] in aw:
            add(r["kid"], "swap:first", "1f381", "Toy swap!", "Old toy out, present in", r.get("inspected_ts") or r["ts"])
    seen = {}
    for e in evs:
        if e["type"] == "awards_seen":
            seen[e["kid"]] = e["ts"]
    nxt = {}
    for k in kids:
        n_j = next((m for m in JOB_MILESTONES if m > jobs[k]), None)
        n_p = next((m for m in POINT_MILESTONES if m > earned.get(k, 0)), None)
        n_d = next((m for m in DAY_MILESTONES if m > len(days[k])), None)
        nxt[k] = [x for x in [
            n_j and {"icon": "1f522", "title": "%d job%s done" % (n_j, "" if n_j == 1 else "s"), "sub": "%d to go" % (n_j - jobs[k])},
            n_p and {"icon": "1f4b0", "title": "%d points earned" % n_p, "sub": "%d to go" % (n_p - earned.get(k, 0))},
            n_d and {"icon": "1f4c5", "title": "Busy %d days" % n_d, "sub": "%d to go" % (n_d - len(days[k]))}] if x]
    # level-ups for the newer families, stamped when the level was reached
    for k in kids:
        mine = sorted([r for r in expanded if r["kid"] == k], key=lambda r: r.get("inspected_ts") or r["ts"])
        seen_c, team_n = set(), 0
        for r in mine:
            ts_ = r.get("inspected_ts") or r["ts"]
            if r["chore"] not in seen_c:
                seen_c.add(r["chore"])
                if len(seen_c) in (3, 6, 10, 15):
                    lv = [3, 6, 10, 15].index(len(seen_c)) + 1
                    add(k, "lvl:explorer:%d" % lv, "1f5fa", "Explorer LV %d!" % lv, "%d different chores" % len(seen_c), ts_)
            if r.get("_team"):
                team_n += 1
                if team_n in (1, 3, 10, 25):
                    lv = [1, 3, 10, 25].index(team_n) + 1
                    add(k, "lvl:team:%d" % lv, "1f91d", "Team Player LV %d!" % lv, "%d team jobs" % team_n, ts_)
    recs = {cid: {"kid": v["kid"], "sec": round(v["sec"]), "ts": v["ts"], **({"team": v["team"], "size": v["size"]} if v.get("team") else {})}
            for cid, v in records.items()}
    prs = {cid: {k: {"sec": round(v["sec"]), "ts": v["ts"]} for k, v in by.items()} for cid, by in pbs.items() if by}
    return {"awards": {k: sorted(v, key=lambda a: a["ts"]) for k, v in aw.items()}, "records": recs, "personal_records": prs,
            "awards_seen": seen, "next_awards": nxt}


GET_DRESSED = {"id": "get_dressed", "name": "Get dressed", "icon": "1f455", "points": 0, "cooldown_h": 0, "shared": True, "est_minutes": 5,
               "hidden": True, "desc": "Shirt, pants, underwear, socks and shoes on. Then show a grown-up.",
               "trophy": {"icon": "1f3c3", "title": "Dressed!"}}   # the "Put on clothes!" job: a timed race, a trophy each time


def cleanup_job(kid):
    """built in, one per kid: the "chores first" clean-up a grown-up lists; hidden unless that kid is blocked.
    It goes through inspection like any job, and passing it lifts the block."""
    return {"id": "cleanup_" + kid, "name": "Tidy up first!", "icon": "1f9f9", "points": 0, "cooldown_h": 0, "only": [kid],
            "cleanup_for": kid, "household": "any", "order": -100,
            "desc": "A grown-up listed what to tidy up. Then rewards come back."}


def cfg():
    c = json.loads(CONFIG.read_text())
    if not any(x.get("id") == "get_dressed" for x in c.get("chores") or []):
        c.setdefault("chores", []).append(dict(GET_DRESSED))   # built in; never saved unless the config is edited
    have = {x.get("id") for x in c.get("chores") or []}
    c["chores"] += [cleanup_job(k["id"]) for k in c.get("kids") or [] if "cleanup_" + k["id"] not in have]
    built_help = {x["id"]: x for x in vacuum_help.chores(HOUSE.get("vacuum_help"))}
    # Old config edits accidentally persisted generated robot jobs. Overlay their canonical metadata so a new
    # field/fix reaches those homes too, while retaining any unrelated local extension keys.
    for ch in c["chores"]:
        if ch.get("id") in built_help:
            ch.update(built_help.pop(ch["id"]))
    c["chores"] += list(built_help.values())
    return c


def solo_job(ch):
    """a job one kid does for themselves, never as a team: brushing teeth, a shower, a job only one kid may do,
    a claimed slot, a kid's own "clean up first", getting dressed, toys (or "solo": true on any chore)"""
    ch = ch or {}
    return bool(ch.get("solo") or ch.get("each_daily") or ch.get("slot") or ch.get("cleanup_for")
                or len(ch.get("only") or []) == 1 or ch.get("id") in ("get_dressed", "toy_swap", "toy_out"))


def is_system(cid):
    return cid in SYSTEM_CHORES or str(cid).startswith(("cleanup_", "help_"))


def save_cfg(c):
    AUTHORITY.require_mutation()
    tmp = CONFIG.with_suffix(".tmp")
    with open(tmp, "w", opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
        output.write(json.dumps(c, indent=2))
    tmp.replace(CONFIG)
    os.chmod(CONFIG, 0o600)


def read_events():
    if not LEDGER.exists():
        return []
    out = []
    for line in LEDGER.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def core_state_checkpoint():
    """Payload-free identity for the quiesced state that every writer must share."""
    allow_absent_ledger = (not AUTHORITY.enabled or
                           AUTHORITY.config.get("allow_absent_empty_ledger") is True)
    with LOCK:
        rows = {}
        for name, path in (("ledger", LEDGER), ("chores", CONFIG), ("house", HOUSE_FILE)):
            try:
                info = path.lstat()
            except FileNotFoundError:
                allowed = name == "ledger" and allow_absent_ledger
                rows[name] = {"present": False, "read_ok": allowed,
                              "absence_allowed": allowed,
                              "absence_policy": "explicit_empty_ledger" if allowed else None,
                              "error": None if allowed else "missing"}
                if allowed:
                    rows[name].update(sha256=hashlib.sha256(b"").hexdigest(), bytes=0,
                                      event_count=0, tail_complete=None)
                continue
            except OSError as ex:
                rows[name] = {"present": None, "read_ok": False, "absence_allowed": False,
                              "absence_policy": None,
                              "error": "stat_%s" % (getattr(ex, "errno", None) or "failed")}
                continue
            if not stat.S_ISREG(info.st_mode):
                rows[name] = {"present": True, "read_ok": False, "absence_allowed": False,
                              "absence_policy": None, "error": "not_regular"}
                continue
            try:
                # O_NOFOLLOW keeps a path-swap from turning the checked regular file into a
                # symlink, and O_NONBLOCK prevents a swapped FIFO/device from stalling health
                # while the writer lock is held. fstat verifies the object actually opened.
                flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) \
                    | getattr(os, "O_NONBLOCK", 0)
                descriptor = os.open(path, flags)
                with os.fdopen(descriptor, "rb") as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        rows[name] = {"present": True, "read_ok": False, "absence_allowed": False,
                                      "absence_policy": None, "error": "not_regular"}
                        continue
                    raw = source.read()
            except OSError as ex:
                rows[name] = {"present": True, "read_ok": False, "absence_allowed": False,
                              "absence_policy": None,
                              "error": "read_%s" % (getattr(ex, "errno", None) or "failed")}
                continue
            rows[name] = {"present": True, "read_ok": True, "absence_allowed": False,
                          "absence_policy": None, "error": None,
                          "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
            if name == "ledger":
                rows[name]["event_count"] = sum(1 for line in raw.splitlines() if line.strip())
                rows[name]["tail_complete"] = not raw or raw.endswith(b"\n")
        ready = all(rows[name].get("read_ok") for name in ("ledger", "chores", "house")) \
            and rows["ledger"].get("tail_complete") is not False
        joined = "\n".join("%s:%s:%s" % (name, int(bool(rows[name].get("present"))),
                                            rows[name].get("sha256"))
                           for name in ("ledger", "chores", "house")).encode() if ready else None
    return {"contract": "chore-core-checkpoint-v2", "app_revision": APP_REVISION,
            "ready": ready, "core_sha256": hashlib.sha256(joined).hexdigest() if joined else None,
            **rows}


def _write_event_locked(ev):
    """Append one already-built event while LOCK is held."""
    authority = AUTHORITY.require_mutation()
    if AUTHORITY.enabled and ev.get("_fence_epoch") != authority["epoch"]:
        raise FencedError(AUTHORITY.status())
    # Guard: never glue onto a line without a trailing newline. A malformed
    # final JSON record still fails read_events and must be reconciled; this
    # only repairs the safe case where a complete final record lacks LF.
    if LEDGER.exists() and LEDGER.stat().st_size:
        with open(LEDGER, "rb") as f:
            f.seek(-1, 2)
            if f.read(1) != b"\n":
                with open(LEDGER, "a") as g:
                    g.write("\n")
                    if AUTHORITY.enabled:
                        g.flush()
                        os.fsync(g.fileno())
    with open(LEDGER, "a") as f:
        f.write(json.dumps(ev) + "\n")
        if AUTHORITY.enabled:
            f.flush()
            os.fsync(f.fileno())


def append(ev):
    DATA.mkdir(parents=True, exist_ok=True)
    with LOCK:
        ev = {"id": uuid.uuid4().hex[:12], "ts": time.time(), **ev, **AUTHORITY.event_fields()}
        _write_event_locked(ev)
    return ev


def append_once(ev, request_id, created_fields=None):
    """Durably append an idempotent operation, or return its existing event."""
    digest = hashlib.sha256(json.dumps(ev, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with LOCK:
        old = next((row for row in read_events() if row.get("_idempotency_key") == request_id), None)
        if old:
            if old.get("_request_digest") != digest:
                raise KeyError("transaction_id was already used with a different payload")
            return old, False
        row = {"id": uuid.uuid4().hex[:12], "ts": time.time(), **ev, **(created_fields or {}),
               "_idempotency_key": request_id, "_request_digest": digest,
               **AUTHORITY.event_fields()}
        _write_event_locked(row)
        return row, True


def record_vacuum_ready(floor, request_id, map_rooms, observed_at):
    """Record one room-level confirmation per accepted operation, under the floor's planner lock."""
    if any(ev.get("type") == "vacuum_ready" and ev.get("floor") == floor and ev.get("request_id") == request_id
           for ev in read_events()):
        return False
    spaces = [space["id"] for space in HOUSE.get("tidy_spaces", [])
              if map_rooms.intersection(space.get("map_rooms") or [space["id"]])]
    if not spaces:
        return False
    append({"type": "vacuum_ready", "floor": floor, "request_id": request_id, "spaces": spaces, "ts": observed_at})
    return True


def shares(r, total):
    """Split points across a team. A custom split gives set amounts first ('he gets one point'),
    the rest is shared equally by the others; otherwise equal shares, odd points to the reporter first."""
    team = r.get("team") or [r["kid"]]
    total = int(total or 0)
    fixed = {m: int(v) for m, v in (r.get("split") or {}).items() if m in team}
    out, left = {}, total
    for m in team:
        if m in fixed:
            out[m] = max(0, min(fixed[m], left))
            left -= out[m]
    rest = [m for m in team if m not in fixed] or ([team[0]] if left > 0 else [])
    if rest:
        base, extra = divmod(left, len(rest))
        for i, m in enumerate(rest):
            out[m] = out.get(m, 0) + base + (1 if i < extra else 0)
    return {m: out.get(m, 0) for m in team}


AUTO_VERIFY_LOCK = threading.Lock()


def auto_verify_chore(cid, verified_at, by, reason):
    """Finish a claimed job when a trustworthy physical signal proves the result.

    A verification must be newer than the child's start/report. With no claimant, nothing is awarded. This is shared
    by robot docking, resolved robot-water sensors, and devices coming back online after a plug-it-in job.
    """
    with AUTO_VERIFY_LOCK:
        st = derive()
        pending = sorted((r for r in st.get("history") or [] if r.get("type") == "report"
                          and r.get("chore") == cid and r.get("status") == "pending"
                          and float(r.get("ts") or 0) <= verified_at
                          and float(r.get("started") or r.get("ts") or 0) <= verified_at),
                         key=lambda r: r["ts"])
        if pending:
            report = pending[0]
            points = report.get("points")
        else:
            jobs = []
            seen = set()
            for paused, rows in ((False, st.get("active") or {}), (True, st.get("paused") or {})):
                for kid, job in rows.items():
                    if job.get("chore") != cid:
                        continue
                    team = tuple(job.get("team") or [kid])
                    if team in seen:
                        continue
                    seen.add(team)
                    started = float(job.get("ts") or 0) - (float(job.get("elapsed") or 0) if paused else 0)
                    if started <= verified_at:
                        jobs.append((started, kid, team))
            if not jobs:
                return []
            started, kid, team = min(jobs)
            points = (st.get("board", {}).get(cid) or {}).get("price")
            if points is None:
                return []       # a grown-up must still assess variable-value work
            report = append({"type": "report", "ts": verified_at, "kid": kid, "team": list(team), "chore": cid, "feeling": None,
                             "points": int(points), "started": started, "auto_verified": reason})
        if points is None:
            return []
        append({"type": "inspect", "ts": verified_at, "ref": report["id"], "by": by, "ok": True, "points": int(points),
                "label": reason, "trusted": True, "auto_verified": True})
        ilog({"src": "auto_verify", "chore": cid, "ref": report["id"], "by": by,
              "reason": reason, "verified_at": verified_at})
        if "clear_report_notice" in globals():
            threading.Thread(target=clear_report_notice, args=(report["id"],), daemon=True).start()
        return [report["id"]]


PAUSE_DAYS = 7   # legacy holds created before indefinite holds retain their recorded expiry


def slot_on(ch, d):
    """is there a service on this date for the kids here? The shared calendar decides when it covers the day
    (holidays, swaps); otherwise the chore's every-N-weeks rule from its anchor date (default: every week)."""
    from datetime import date as _date
    cal = schedule.here_on(d)
    if cal is not None:
        return cal
    sl = ch.get("slot") or {}
    ew, anchor = int(sl.get("every_weeks") or 1), sl.get("anchor")
    if ew <= 1 or not anchor:
        return True
    return ((d - _date.fromisoformat(anchor)).days // 7) % ew == 0


def next_slot(ch, after=None):
    """the upcoming service this slot chore is for (epoch); the service day counts until midnight.
    Weeks the kids aren't here (every other week, or the calendar says so) are skipped."""
    from datetime import datetime, timedelta
    sl = ch.get("slot") or {}
    hh, mm = (int(x) for x in str(sl.get("time", "11:00")).split(":"))
    d = datetime.fromtimestamp(after) if after else datetime.now()
    days = (int(sl.get("dow", 6)) - d.weekday()) % 7
    first = (d + timedelta(days=days)).replace(hour=hh, minute=mm, second=0, microsecond=0)
    for w in range(27):
        cand = first + timedelta(weeks=w)
        if slot_on(ch, cand.date()):
            return cand.timestamp()
    return first.timestamp()


def certify_at(ch, slot):
    """when a grown-up may confirm a certified slot chore happened (e.g. noon, after church); None = kids report it"""
    from datetime import datetime
    ca = (ch.get("slot") or {}).get("certify_after")
    if not ca:
        return None
    hh, mm = (int(x) for x in str(ca).split(":"))
    return datetime.fromtimestamp(slot).replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()


# Fields a grown-up can set on a chart chore from the parents' page (the full chore form). Only keys present in the
# request change; an empty/false value removes the key so chores.json stays tidy.
CHORE_FLAGS = ("once", "each_daily", "on_demand", "daylight", "adult_first")


def chore_fields(ch, c):
    import re as _re
    out = {}
    if "desc" in ch:
        out["desc"] = str(ch.get("desc") or "").strip()[:200]
    for k in CHORE_FLAGS:
        if k in ch:
            out[k] = bool(ch.get(k))
    if "cooldown_h" in ch:
        try:
            out["cooldown_h"] = max(0.0, min(8760.0, float(ch.get("cooldown_h") or 0)))
        except (TypeError, ValueError):
            pass
    if "where" in ch:
        where = str(ch.get("where") or "").strip()[:60]
        out["where"] = where
        floors = (HOUSE.get("house_tasks") or {}).get("floors") or {}
        floor = next((f for f, rooms in floors.items() if where in rooms), "")
        out["floor"] = str(ch.get("floor") or floor or ("Other" if where else ""))[:30]
        known = {m for x in c.get("chores", []) for m in x.get("map_rooms") or []}
        slug = _re.sub(r"[^a-z0-9]+", "_", where.lower().replace("'", "")).strip("_")
        out["map_rooms"] = [slug] if slug in known else []
    if "only" in ch:
        kid_ids = [k["id"] for k in c.get("kids", [])]
        only = [k for k in (ch.get("only") or []) if k in kid_ids]
        out["only"] = only if 0 < len(only) < len(kid_ids) else []
    if "household" in ch:
        ids = [h["id"] for h in c.get("households") or []]
        hh = ch.get("household") or ""
        out["household"] = hh if hh == "any" or (hh in ids and ids and hh != ids[0]) else ""
    for k, pat in (("from", r"\d{4}-\d{2}-\d{2}"), ("until", r"\d{4}-\d{2}-\d{2}"), ("after", r"\d{1,2}:\d{2}"), ("before", r"\d{1,2}:\d{2}")):
        if k in ch:
            v = str(ch.get(k) or "").strip()
            out[k] = v if _re.fullmatch(pat, v) else ""
    if "est_minutes" in ch:
        try:
            out["est_minutes"] = max(1, min(600, int(ch.get("est_minutes")))) if ch.get("est_minutes") not in (None, "") else 0
        except (TypeError, ValueError):
            pass
    return out


def apply_chore_fields(x, more):
    for k, v in more.items():
        if v in ("", [], False, 0) and k != "cooldown_h":
            x.pop(k, None)
        else:
            x[k] = v
    return x


def grocy_period(x, more):
    """the Grocy schedule for a chart chore the form changed: back after N days = dynamic-regular, else manually.
    Daily/weekly chores keep the schedule Grocy already has."""
    if "cooldown_h" not in more or x.get("daily") or x.get("weekly"):
        return {}
    days = int(round(float(x.get("cooldown_h") or 0) / 24))
    return {"period_type": "dynamic-regular", "period_days": max(1, days)} if float(x.get("cooldown_h") or 0) > 0 else {"period_type": "manually"}


GROCY_EXTRA_KEYS = ("once", "each_daily", "on_demand", "daylight", "adult_first", "household", "from", "until", "after", "before", "est_minutes")


def grocy_chore_userfields(gid, x, more, new=False):
    """Grocy is the master list, so the form's fields go to the chore's userfields (the next pull rebuilds from them)."""
    uf = {}
    if "where" in more:
        uf.update({"kid_where": x.get("where", ""), "kid_floor": x.get("floor", ""), "kid_map": ",".join(x.get("map_rooms") or [])})
    if "only" in more:
        uf["kid_only"] = ",".join(x.get("only") or [])
    if any(k in more for k in GROCY_EXTRA_KEYS) or "cooldown_h" in more:
        try:
            extra = {} if new else json.loads((gcall("GET", "/userfields/chores/%d" % gid) or {}).get("kid_extra") or "{}")
        except ValueError:
            extra = {}
        for k in GROCY_EXTRA_KEYS:
            if k in more:
                if more[k] in ("", [], False, 0, None):
                    extra.pop(k, None)
                else:
                    extra[k] = more[k]
        if "cooldown_h" in more and not (x.get("daily") or x.get("weekly")):
            extra["room"] = bool(x.get("room"))   # explicit: a Grocy "every N days" chore counts as a room unless told otherwise
        uf["kid_extra"] = json.dumps(extra, sort_keys=True) if extra else ""
    return uf


# A "fix it" job that only exists while a device is down: chores[].when_down = [HA entities] shows the job once any of
# them has been unavailable for down_min minutes (default 10), e.g. "plug the sensor back in". watch_when_down keeps DOWN.
DOWN = {}   # entity -> when it went unavailable


def device_down(ch, now=None):
    now = now or time.time()
    return any(now - DOWN[e] >= float(ch.get("down_min", 10)) * 60 for e in ch.get("when_down") or [] if e in DOWN)


def next_step(ch, team, act):
    """what a kid still has to do for a job in progress (chores[].remind: shown each time they open their screen)"""
    if ch.get("id") in ((envelopes.public() or {}).get("chores") or []):
        printed = max([envelopes.PRINTED.get(m, 0) for m in team] or [0]) >= act.get("ts", 0) - 60
        return envelopes.public()["handoff"] + " Then tap " + envelopes.public()["handoff_done"] if printed else \
            "Finish writing, then tap I'm done on the Google screen to print the envelope."
    return ch.get("remind_text") or "Finish it, then tap I'm done."


def watch_when_down():
    while True:
        try:
            tok, changed, recovered = ha_token(), False, []
            config = cfg()
            ents = {e for ch in config["chores"] for e in ch.get("when_down") or []}
            for e in ents:
                row_ = ha_get(e, tok) or {}
                st_ = row_.get("state")
                if st_ in (None, "unavailable", "unknown"):
                    if e not in DOWN:
                        DOWN[e] = time.time()
                        ilog({"src": "when_down", "entity": e, "state": "down"})
                    changed = changed or any(time.time() - DOWN[e] >= float(ch.get("down_min", 10)) * 60 - 60
                                             for ch in cfg()["chores"] if e in (ch.get("when_down") or []))
                elif e in DOWN:
                    DOWN.pop(e)
                    ilog({"src": "when_down", "entity": e, "state": "back"})
                    try:
                        from datetime import datetime as _dt
                        back_at_ = _dt.fromisoformat(str(row_.get("last_changed")).replace("Z", "+00:00")).timestamp()
                    except (TypeError, ValueError):
                        back_at_ = time.time()
                    recovered.append((e, back_at_))
                    changed = True
            for e in [x for x in DOWN if x not in ents]:
                DOWN.pop(e)
            done_ = set()
            for e, back_at_ in recovered:
                for ch in config["chores"]:
                    if ch["id"] not in done_ and e in (ch.get("when_down") or []) and not device_down(ch):
                        if auto_verify_chore(ch["id"], back_at_, "sensor", "Device came back online"):
                            done_.add(ch["id"])
            if changed:
                after_change()
        except Exception as ex:  # noqa: BLE001
            print("when_down error", ex, flush=True)
        time.sleep(60)


# "Put on clothes!" (a grown-up's switch on the parents' page): the listed kids can't do other jobs or spend until a
# grown-up passes their "Get dressed" (shirt, pants, underwear, socks, shoes); the switch turns itself off when all are ready.
# Spoken reminders every 30 s on house.json dress.speakers (plain speakers: a Hub would drop its kids' screen).
DRESS = HOUSE.get("dress") or {}


def dress_state(evs, reports, awake=None):
    """School mornings switch it on before anyone is up. Their result is judged by
    the wall-clock finish time; manually enabled dressing keeps the elapsed race."""
    last = next((e for e in reversed(evs) if e["type"] == "dress"), None)
    if not last or not last.get("on"):
        return {"on": False, "off_ts": last["ts"] if last else None, "off_by": last.get("by") if last else None,
                **({"next": school_next()} if SCHOOL else {})}
    out = {}
    for k in last.get("kids") or []:
        mine = [r for r in reports.values() if r["chore"] == "get_dressed" and r["kid"] == k and r["ts"] >= last["ts"]]
        out[k] = "ready" if any(r["status"] == "inspected" for r in mine) else \
            "waiting" if any(r["status"] == "pending" for r in mine) else "todo"
    res = {"on": True, "since": last["ts"], "go": last["ts"], "by": last.get("by"), "kids": out}
    if last.get("school"):
        from datetime import datetime as _dt
        go = max(last["ts"], _dt.fromtimestamp(last["ts"]).replace(hour=BED_END_H, minute=0, second=0, microsecond=0).timestamp())
        if awake and go < awake <= time.time():
            go = awake
        targets = [{k: x.get(k) for k in ("by", "icon", "title", "sub")} for x in SCHOOL.get("awards") or [] if x.get("by")]
        res.update(school=True, go=go, until=SCHOOL.get("until") or "08:00", targets=targets)
    return res


def dress_departure_step(vacancy, st=None):
    """Clear a dressing session once HA positively observes the house becoming vacant.

    Departure is useful physical evidence that the family got dressed and left, but
    it is not evidence for a speed award or stars.  Active work and uninspected
    dressing taps are temporary session state, so remove them rather than leaving
    children blocked or a grown-up with meaningless inspection rows on return.
    """
    st = st or derive()
    dress = st.get("dress") or {}
    if not dress.get("on") or not DRESS.get("house_vacancy"):
        return False
    vacant_states = {str(x).lower() for x in DRESS.get("vacant_states") or ["vacant", "on"]}
    if str((vacancy or {}).get("state") or "").lower() not in vacant_states:
        return False
    try:
        from datetime import datetime as _dt
        changed = _dt.fromisoformat(str(vacancy.get("last_changed") or "").replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return False
    if changed <= float(dress.get("since") or 0):
        return False       # it was already vacant before this dressing session; no departure was observed

    ended = set()
    for paused, jobs in ((False, st.get("active") or {}), (True, st.get("paused") or {})):
        for kid, job in jobs.items():
            if job.get("chore") != "get_dressed":
                continue
            team = tuple(job.get("team") or [kid])
            key = (paused, team)
            if key in ended:
                continue
            ended.add(key)
            append({"type": "abandon", "kid": kid, "team": list(team), "chore": "get_dressed",
                    "paused": paused, "by": "vacancy", "why": "family left the house"})
    withdrawn = []
    for report in st.get("history") or []:
        if report.get("type") == "report" and report.get("chore") == "get_dressed" \
                and report.get("status") == "pending" and float(report.get("ts") or 0) >= float(dress.get("since") or 0):
            append({"type": "withdraw", "ref": report["id"], "by": "vacancy", "why": "family left the house"})
            withdrawn.append(report["id"])
    append({"type": "dress", "on": False, "kids": [], "by": "vacancy", "why": "house became vacant"})
    ilog({"src": "dress", "action": "clear_on_departure", "vacant_at": changed,
          "active_ended": len(ended), "reports_withdrawn": len(withdrawn)})
    after_change()
    return True


# School mornings (house.json dress.school_mornings): "Put on clothes!" switches itself on at "at" on school days
# for the kids at this house (home switch on), quietly (no speakers, no chimes), and off again at "until".
# A school day: one of "weekdays" (Mon=0) that the kids' calendar doesn't mark as no school for that kid.
SCHOOL = DRESS.get("school_mornings") or {}


def _hm(v, dflt):
    h, m = (int(x) for x in str(v or dflt).split(":"))
    return h, m


def school_off_kids(kids, d):
    """kid ids with no school that day (the calendar's no-school events; one naming a kid is that kid's only)"""
    out = set()
    for t in schedule.off_on(d) or []:
        named = {k["id"] for k in kids if re.search(r"\b%s\b" % re.escape(k["name"]), t, re.I)}
        out |= named or {k["id"] for k in kids}
    return out


def school_next(now=None, kids=None):
    """the next school morning's start {"ts", "day"} (within two weeks), or None"""
    from datetime import datetime as _dt, timedelta as _td
    now = now or _dt.now()
    kids = kids if kids is not None else cfg()["kids"]
    h, m = _hm(SCHOOL.get("at"), "06:00")
    uh, um = _hm(SCHOOL.get("until"), "08:00")
    for i in range(15):
        d = (now + _td(days=i)).replace(hour=h, minute=m, second=0, microsecond=0)
        if d.weekday() not in (SCHOOL.get("weekdays") or [0, 1, 2, 3, 4]):
            continue
        if i == 0 and now >= d.replace(hour=uh, minute=um):
            continue
        if len(school_off_kids(kids, d.date())) < len(kids):
            return {"ts": d.timestamp(), "day": d.strftime("%a")}
    return None


def school_morning_step(now, evs, kids, home_kids):
    """what the school-morning switch does now: ("on", [kids]), ("off", None) or None.
    `home_kids()` lists the kids at this house (asked only when it might switch on).
    Once a morning: a grown-up switching it off (or on) that morning is left alone."""
    if not SCHOOL:
        return None
    h, m = _hm(SCHOOL.get("at"), "06:00")
    uh, um = _hm(SCHOOL.get("until"), "08:00")
    t_at, t_until = now.replace(hour=h, minute=m, second=0, microsecond=0), now.replace(hour=uh, minute=um, second=0, microsecond=0)
    last = next((e for e in reversed(evs) if e["type"] == "dress"), None)
    if last and last.get("on") and last.get("school") and (now >= t_until or last["ts"] < t_at.timestamp()):
        return ("off", None)
    if now.weekday() not in (SCHOOL.get("weekdays") or [0, 1, 2, 3, 4]) or not (t_at <= now < t_until):
        return None
    if any(e["type"] == "dress" and e["ts"] >= t_at.timestamp() for e in evs) or (last and last.get("on")):
        return None
    off = school_off_kids(kids, now.date())
    who = [k for k in home_kids() if k not in off]
    return ("on", who) if who else None


def watch_school_mornings():
    from datetime import datetime as _dt
    while True:
        try:
            c, tok = cfg(), ha_token()
            ent = SCHOOL.get("awake")
            if ent:   # when the kids are up (a bedroom motion sensor's "awake" switch): the race clock starts then
                s_ = ha_get(ent, tok) or {}
                ts_ = None
                if s_.get("state") == "on" and s_.get("last_changed"):
                    try:
                        ts_ = _dt.fromisoformat(s_["last_changed"].replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        ts_ = None
                GUARD["awake_ts"] = ts_ if ts_ and _dt.fromtimestamp(ts_).date() == _dt.now().date() else None
            step = school_morning_step(_dt.now(), read_events(), c["kids"], lambda: dress_kids(c))
            if step and step[0] == "on":
                ev = append({"type": "dress", "on": True, "kids": step[1], "by": "auto", "school": True})
                ilog({"src": "school", "action": "dress_on", "event": ev})
                after_change()
            elif step:
                ev = append({"type": "dress", "on": False, "kids": [], "by": "auto", "school": True, "why": "school time"})
                ilog({"src": "school", "action": "dress_off", "event": ev})
                after_change()
        except Exception as ex:  # noqa: BLE001
            print("school mornings error", ex, flush=True)
        time.sleep(30)


def chores_first_state(evs, kids, reports=None):
    """"Chores first": a grown-up blocks a kid's rewards and lists what to clean up (e.g. messes left out).
    The kid does it as the job "cleanup_<kid>"; when that passes inspection the block lifts by itself, or a
    grown-up turns it off. {kid: {on, since, by, items, lifted?}}"""
    out = {k: {"on": False, "since": None, "by": None, "items": []} for k in kids}
    pos = {e["id"]: i for i, e in enumerate(evs)}   # ledger order (timestamps can tie)
    passed_at = {e["ref"]: pos[e["id"]] for e in evs if e["type"] == "inspect" and e.get("ok")}

    def lift(k, v, until=None):   # the clean-up was reported after the block began and passed (before `until`)
        if not v["on"]:
            return v
        target = v.get("chore") or "cleanup_" + k
        ok = [r for r in (reports or {}).values() if r["chore"] == target and pos.get(r["id"], -1) > v["_at"]
              and r["status"] == "inspected" and not r.get("partial") and (until is None or passed_at.get(r["id"], 1e18) < until)]
        marked = [e for e in evs if v.get("chore") and e.get("type") == "mark_clean" and e.get("chore") == target
                  and pos[e["id"]] > v["_at"] and (until is None or pos[e["id"]] < until)]
        if marked:
            return dict(v, on=False, lifted={"by": marked[-1].get("by"), "ts": marked[-1]["ts"], "manual": True})
        return dict(v, on=False, lifted={"by": ok[-1].get("inspected_by"), "ts": ok[-1].get("inspected_ts")}) if ok else v
    for e in evs:
        if e["type"] == "chores_first" and e.get("kid") in out:
            prev = lift(e["kid"], out[e["kid"]], pos[e["id"]])
            if e.get("on") and prev["on"]:   # editing the list keeps the start time
                out[e["kid"]] = dict(prev, items=list(e.get("items") or []),
                                      chore=e.get("chore", prev.get("chore")), name=e.get("name", prev.get("name")))
            else:
                out[e["kid"]] = {"on": bool(e.get("on")), "since": e["ts"], "_at": pos[e["id"]], "by": e.get("by"),
                                 "items": list(e.get("items") or []), "chore": e.get("chore"), "name": e.get("name")}
                if not e.get("on") and e.get("done"):
                    out[e["kid"]]["lifted"] = {"by": e.get("by"), "ts": e["ts"], "manual": True}
    return {k: {x: y for x, y in lift(k, v).items() if x != "_at"} for k, v in out.items()}


def adult_first_state(c, evs, reports, now=None):
    """First-time teaching jobs: per-child training, a live approval, and per-adult snoozes."""
    now = time.time() if now is None else now
    out = {}
    for ch in c.get("chores") or []:
        if not ch.get("adult_first"):
            continue
        cid = ch["id"]
        trained = {m for r in (reports or {}).values() if r.get("chore") == cid and r.get("status") == "inspected"
                   and not r.get("partial") for m in (r.get("team") or [r.get("kid")])}
        snoozed_until = adult_busy_until(evs, now)
        approved = {e.get("kid") for e in evs if e.get("type") == "assist_ok" and e.get("chore") == cid
                    }
        out[cid] = {"snoozed_until": snoozed_until if snoozed_until > now else 0,
                    "kids": {k["id"]: {"trained": k["id"] in trained, "approved": k["id"] in approved}
                             for k in c.get("kids") or []}}
    return out


def adult_busy_until(evs, now=None):
    """When every configured adult is busy, return the first time one becomes available."""
    now = time.time() if now is None else now
    busy = adult_busy_state(evs, now)
    adults = list(ADULTS) if "ADULTS" in globals() else []
    if not adults or any(busy.get(a, 0) <= now for a in adults):
        return 0
    return min(busy[a] for a in adults)


def adult_busy_state(evs, now=None):
    """Per-adult one-hour snoozes; legacy unowned snoozes apply to every adult."""
    now = time.time() if now is None else now
    adults = list(ADULTS) if "ADULTS" in globals() else []
    out = {a: 0 for a in adults}
    for e in evs:
        if e.get("type") not in ("assist_snooze", "adult_busy"):
            continue
        until = float(e.get("until") or 0)
        targets = [e.get("by")] if e.get("by") in out else adults
        for adult in targets:
            out[adult] = max(out.get(adult, 0), until)
    return {a: u for a, u in out.items() if u > now}


def shower_requirement_state(c, reports, now=None, home_days=None, current_houses=None):
    """A shower is due on a child's second consecutive night at the same parent's house.

    Changing houses supplies an implicit last-night shower credit. A pending or passed
    shower report supplies real credit immediately, so the rest of the bedtime routine
    can continue without waiting for inspection.
    """
    from datetime import datetime, timedelta
    now = now or datetime.now()
    today = now.date()
    home_days = home_days if home_days is not None else (GUARD.get("home_days") or load_home_days())
    current_houses = current_houses or {k["id"]: kid_where(c, k["id"]) for k in c.get("kids") or []}
    primary = home_of(c)
    households = [h["id"] for h in c.get("households") or []]

    def house_on(kid, day):
        if day == today:
            return current_houses.get(kid) or primary
        at_primary = day.isoformat() in set(home_days.get(kid) or [])
        if at_primary or len(households) < 2:
            return primary
        return households[1]

    out = {}
    for k in c.get("kids") or []:
        kid = k["id"]
        house = current_houses.get(kid) or primary
        start = today
        for n in range(1, 31):
            d = today - timedelta(days=n)
            if house_on(kid, d) != house:
                break
            start = d
        implicit = start - timedelta(days=1)   # return from the other parent = presumed showered last night
        shower_dates = []
        for r in (reports or {}).values():
            if r.get("chore") == "shower" and r.get("status") in ("pending", "inspected") and not r.get("partial") \
                    and kid in (r.get("team") or [r.get("kid")]):
                shower_dates.append(datetime.fromtimestamp(r["ts"]).date())
        credit = max([implicit] + shower_dates)
        nights = max(0, (today - credit).days)
        out[kid] = {"due": nights >= 2, "household": house, "stay_started": start.isoformat(),
                    "last_credit": credit.isoformat(), "nights_without": nights,
                    "presumed_on_return": credit == implicit and start == today}
    return out


def music_block_state(evs, chores_first, must_first, present=None, reports=None, arrivals=None, now=None):
    """Parent preference and the required-work gates that currently make it effective.

    Home Assistant owns media enforcement.  Keeping the reasons here lets that side
    stop Spotify and show a useful, silent explanation without reimplementing chore
    rules or guessing which child is blocked.
    """
    enabled, by, since = False, None, None
    for e in evs:
        if e["type"] == "music_block":
            enabled, by, since = bool(e.get("on")), e.get("by"), e.get("ts")
    blockers = []
    for kid, gate in chores_first.items():
        if gate.get("on"):
            blockers.append({"kid": kid, "kind": "required_room" if gate.get("chore") else "cleanup",
                             "name": gate.get("name") or "Mandatory chores",
                             "items": list(gate.get("items") or []),
                             **({"chore": gate["chore"]} if gate.get("chore") else {})})
    for kid, gate in must_first.items():
        if gate.get("rewards"):
            blockers.append({"kid": kid, "kind": "must_do_first", "name": gate.get("name") or "Required chore",
                             "chore": gate.get("chore")})
    # With no explicit parent/priority gate, the switch still means one approved
    # chore per child who is physically here, reset each day and on arrival.
    if enabled and not blockers and present is not None:
        from datetime import datetime
        now_ = now or datetime.now()
        midnight = now_.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        arrivals = arrivals or {}
        for kid in present:
            baseline = max(float(since or 0), midnight)
            at = (arrivals.get(kid) or {}).get(now_.date().isoformat())
            if at:
                try:
                    baseline = max(baseline, datetime.strptime(now_.date().isoformat() + " " + at,
                                                               "%Y-%m-%d %H:%M").timestamp())
                except (TypeError, ValueError):
                    pass
            done = any(r.get("status") == "inspected" and not r.get("partial")
                       and float(r.get("inspected_ts") or 0) >= baseline
                       and kid in (r.get("team") or [r.get("kid")]) for r in (reports or {}).values())
            # A grown-up's explicit "Finished — mark done" is an approval too. This matters when a mandatory
            # item carried overnight: clearing it after midnight must not immediately replace it with the generic
            # one-approved-chore fallback.
            done = done or any(e.get("type") == "chores_first" and e.get("kid") == kid and e.get("done")
                               and float(e.get("ts") or 0) >= baseline for e in evs)
            if not done:
                blockers.append({"kid": kid, "kind": "one_chore", "name": "One approved chore",
                                 "items": ["Finish one chore and have a grown-up approve it"]})
    return {"enabled": enabled, "effective": bool(enabled and blockers), "by": by, "since": since,
            "blockers": blockers}


def spotify_volume_cap_state(evs):
    """Latest grown-up Spotify cap; old households have unrestricted volume until one is saved."""
    cap = 100
    for e in evs:
        if e.get("type") == "spotify_volume_cap" and type(e.get("cap")) is int and 0 <= e["cap"] <= 100:
            cap = e["cap"]
    return cap


def spotify_exempt_rooms():
    """Physical room ids where chore policy must never touch Spotify playback."""
    raw = (HOUSE.get("spotify") or {}).get("exempt_rooms") or []
    return list(dict.fromkeys(str(room).strip() for room in raw
                              if isinstance(room, str) and room.strip()))


def spotify_room_key(room):
    """Canonical room id for policy receipts; aliases keep display and speaker names equivalent."""
    room = str(room or "").strip()
    aliases = (HOUSE.get("spotify") or {}).get("room_aliases") or {}
    seen = set()
    while room and room not in seen and isinstance(aliases.get(room), str):
        seen.add(room)
        room = aliases[room].strip()
    return room


def kid_notes(evs):
    """grown-ups' one-time messages to a kid ("we gave your stars back"): unseen ones, shown when they open their screen"""
    seen = {e["ref"] for e in evs if e["type"] == "kid_note_seen"}
    out = {}
    for e in evs:
        if e["type"] == "kid_note" and e["id"] not in seen and time.time() - e["ts"] < 7 * 86400:
            out.setdefault(e["kid"], []).append({"id": e["id"], "ts": e["ts"], "text": e["text"], "icon": e.get("icon") or "1f4ac"})
    return out


def reward_requests(evs):
    """rewards bought that wait for a grown-up to start them (VR: they help set it up): {id, kid, reward, cost, ts}.
    The stars are paid at purchase; the grown-up's "start" begins the timer, "not now" refunds it."""
    undone = {e["ref"] for e in evs if e["type"] == "undo"}
    started = {e["ref"] for e in evs if e["type"] == "reward_start"}
    return [{k: e.get(k) for k in ("id", "kid", "reward", "cost", "ts", "reward_name", "reward_icon")}
            for e in evs if e["type"] == "spend" and e.get("await_ok") and e["id"] not in undone and e["id"] not in started]


def dress_kids(c):
    """who gets dressed by default: kids at this house whose home switch is on"""
    tok = ha_token()
    return [k["id"] for k in c["kids"] if kid_where(c, k["id"]) == home_of(c)
            and (not k.get("home_entity") or ha_get(k["home_entity"], tok).get("state") == "on")]


def kid_uses_timer(c, kid, chore):
    """A kid's standing opt-out wins even when an old client still sends Start."""
    profile = next((k for k in c["kids"] if k["id"] == kid), {})
    return chore not in (profile.get("untimed_chores") or [])


def sound_enabled():
    """One house-wide gate for every sound initiated by the kids' chore system."""
    return HOUSE.get("sound_effects") is not False


def dress_announce(first=False):
    if not sound_enabled():
        return
    d_ = derive().get("dress") or {}
    todo = [k for k, v in (d_.get("kids") or {}).items() if v == "todo"]
    if not d_.get("on") or not todo or d_.get("school"):   # school mornings are quiet (kids may still be asleep)
        return
    names = " and ".join(next((x["name"] for x in cfg()["kids"] if x["id"] == k), k) for k in todo)
    msg = (DRESS.get("message") or "{names}: put on your shirt, pants, underwear, socks and shoes right now! Then show a grown-up.").format(names=names)
    spk = DRESS.get("speakers") or ([ANNOUNCE_SPEAKER] if ANNOUNCE_SPEAKER else [])
    if ANNOUNCE_TTS and spk:
        ilog({"src": "announce", "msg": msg, "speaker": spk})
        ha_call("tts.speak", {"entity_id": ANNOUNCE_TTS, "media_player_entity_id": spk, "message": msg, "cache": True}, ha_token())


# Hubs grab attention: house.json hubs.<id>.motion = [HA sensors near that Hub]. A sensor turning on after 2+ quiet
# minutes stamps GUARD["hub_motion"][hub]; that Hub's page then chimes and flashes the kids who have something waiting.
def watch_hub_motion():
    prev, quiet_since = {}, {}
    while True:
        try:
            tok = ha_token()
            for hub, hcfg in (HOUSE.get("hubs") or {}).items():
                for e in hcfg.get("motion") or []:
                    st_ = (ha_get(e, tok) or {}).get("state")
                    if st_ == "on" and prev.get(e) == "off" and time.time() - quiet_since.get(e, 0) >= 120:
                        GUARD.setdefault("hub_motion", {})[hub] = time.time()
                    if st_ != "on" and prev.get(e) == "on":
                        quiet_since[e] = time.time()
                    elif st_ == "off" and e not in quiet_since:
                        quiet_since[e] = time.time()
                    prev[e] = st_
        except Exception as ex:  # noqa: BLE001
            print("hub motion error", ex, flush=True)
        time.sleep(3)


def dress_displays(on, started):
    """the Hubs show it big: kids' Hubs get their screen back, other displays (dress.displays) get the clothes screen"""
    tok = ha_token()
    for h in HUBS.values():
        hs = ha_get(h["entity"], tok) if h.get("entity") else {}
        if hs.get("state") in ("playing", "buffering", "paused") and (hs.get("attributes") or {}).get("app_name") != "DashCast":
            continue   # never cut off music (Spotify) on a Hub; the speakers and the TVs still carry the message
        if on and not hub_showing_chores(h["ip"]):
            ilog({"src": "dress", "action": "cast_hub", "ip": h["ip"]})
            cast(h["ip"], "/")
    for d in DRESS.get("displays") or []:
        if on and d["ip"] not in started:
            ilog({"src": "dress", "action": "cast_display", "ip": d["ip"]})
            cast(d["ip"], d.get("path", "/tv"))
            started.add(d["ip"])
        elif not on and d["ip"] in started:
            ilog({"src": "dress", "action": "stop_display", "ip": d["ip"]})
            subprocess.run([CATT, "-d", d["ip"], "stop"], timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            started.discard(d["ip"])


def watch_dress():
    n, was, started = 0, False, set()
    while True:
        time.sleep(5)
        n += 1
        try:
            if DRESS.get("house_vacancy"):
                vacancy_ = ha_get(DRESS["house_vacancy"], ha_token())
                dress_departure_step(vacancy_)
            d_ = derive().get("dress") or {}
            on = bool(d_.get("on")) and (not d_.get("school") or d_.get("go", 0) <= time.time())   # screens wait until they're up
            if on != was or (on and n % 12 == 0):   # on/off, and once a minute while on (someone may have stopped it)
                dress_displays(on, started)
            was = on
            if on and n % max(1, int(DRESS.get("every_s", 30)) // 5) == 0:
                dress_announce()
        except Exception as ex:  # noqa: BLE001
            print("dress error", ex, flush=True)


def too_early(ch, bd, kid=None):
    """slot "claim_days": kids only see and claim the service this many days ahead (e.g. 4: from Wednesday
    for a Sunday); a kid who already holds it still sees it"""
    cd = (ch.get("slot") or {}).get("claim_days")
    if cd is None or bd.get("days_until") is None:
        return False
    if bd.get("claimed_by") and (kid is None or bd["claimed_by"] == kid):
        return False
    return bd["days_until"] > int(cd)


def reward_cost(r, watching=1):
    """what each kid pays: reward "together_cost" {"2": 12, "3": 10} = per kid when that many watch together
    (more than the largest listed size pays that size's price); otherwise its "cost"."""
    tc = {int(k): v for k, v in (r.get("together_cost") or {}).items()}
    if watching > 1 and tc:
        fit = [n for n in tc if n <= watching]
        if fit:
            return tc[max(fit)]
    return r["cost"]


def shared_reward_costs(reward, participants, mode=None, player=None):
    """Per-child prices for one shared purchase, including asymmetric game roles."""
    participants = list(dict.fromkeys(participants or []))
    if not participants:
        raise ValueError("Choose at least one participant.")
    custom_games = (reward.get("id") == "games"
                    and (reward.get("two_player_split") or reward.get("watcher_cost") is not None))
    if not custom_games or len(participants) == 1:
        cost = int(reward_cost(reward, len(participants)))
        return {kid: cost for kid in participants}
    if mode == "two_player":
        if not reward.get("two_player_split") or len(participants) != 2:
            raise ValueError("A split game needs exactly two players.")
        total = int(reward["cost"])
        if total % 2:
            raise ValueError("This game price cannot be split evenly.")
        return {kid: total // 2 for kid in participants}
    if mode == "player_watch":
        if player not in participants:
            raise ValueError("Choose which child is playing.")
        try:
            watcher_cost = int(reward["watcher_cost"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Watcher pricing is unavailable.") from None
        if watcher_cost < 0:
            raise ValueError("Watcher pricing is unavailable.")
        return {kid: int(reward["cost"]) if kid == player else watcher_cost for kid in participants}
    raise ValueError("Choose two players or one player with watchers.")


def saved_movie_remaining_fraction(playback):
    """Safe public fraction from a private paused-player snapshot, or None."""
    try:
        duration = float((playback or {})["dur"])
        position = float((playback or {})["pos"])
    except (KeyError, TypeError, ValueError):
        return None
    if duration <= 0 or position < 0 or position > duration:
        return None
    return max(0.0, min(1.0, (duration - position) / duration))


def reward_join_price(reward, watching, started_at, until, tv=None, session_id=None, now=None,
                      timeline_get=None, movie_progress=None):
    """Price only the late joiner's remaining share; never extend the clock.

    A verified movie cursor is the best measure of what the newcomer receives.
    If it is unavailable, use the purchased reward clock. The existing viewers'
    earlier watching is never retroactively prorated.
    """
    now = float(time.time() if now is None else now)
    full = int(reward_cost(reward, watching))
    total = max(1.0, float(until) - float(started_at))
    fraction = max(0.0, min(1.0, (float(until) - now) / total))
    source = "reward_time"
    saved_fraction = (saved_movie_remaining_fraction(movie_progress)
                      if reward.get("id") == "movie" else None)
    if saved_fraction is not None:
        fraction, source = saved_fraction, "movie_time"
    room = (TVS.get(tv) or {}).get("reward_timeline_room") if "TVS" in globals() else None
    if source != "movie_time" and reward.get("id") == "movie" and room and session_id:
        try:
            if timeline_get is None:
                req = urllib.request.Request(
                    f"{HA_URL}/api/reward_movie_timeline/{urllib.parse.quote(str(room), safe='')}",
                    headers={"Authorization": f"Bearer {ha_token()}", "Cache-Control": "no-store"})
                timeline = json.loads(urllib.request.urlopen(req, timeout=4).read())
            else:
                timeline = timeline_get(room)
            if (timeline.get("schema_version") == 1 and timeline.get("grant_id") == session_id
                    and timeline.get("state") in ("playing", "paused", "seeking")):
                duration = float(timeline["duration_ms"])
                position = float(timeline["position_ms"])
                if timeline["state"] == "playing":
                    position += max(0.0, min(now, float(timeline["valid_until"]))
                                    - float(timeline["observed_at"])) * 1000
                if duration > 0 and 0 <= position <= duration:
                    fraction = max(0.0, min(1.0, (duration - position) / duration))
                    source = "movie_time"
        except (KeyError, TypeError, ValueError, OSError, urllib.error.URLError, json.JSONDecodeError):
            pass
    # A nonzero late share always costs at least one star. At the exact end the
    # active-session check rejects the join before this function is called.
    return {"cost": max(1, int(math.ceil(full * fraction - 1e-9))), "full_cost": full,
            "fraction": fraction, "source": source}


def reward_closed(r, now=None):
    """A reward may have its own evening cutoff; other rewards remain available."""
    from datetime import date, datetime
    now = now or datetime.now()
    if not r or not r.get("before"):
        return False
    if r.get("before_from"):
        try:
            if now.date() < date.fromisoformat(str(r["before_from"])):
                return False
        except ValueError:
            pass
    h, m = (int(x) for x in str(r["before"]).split(":"))
    return now >= now.replace(hour=h, minute=m, second=0, microsecond=0)


def chore_closed(ch, board):
    """why a chore is unavailable right now (None = open): daylight-only after dark, one-time already done,
    outside its from/until dates (e.g. cleaning the car only in daylight; a one-time job)"""
    from datetime import date
    today = date.today().isoformat()
    if ch.get("when_down") and not device_down(ch):
        return "fine"   # nothing unplugged: no job
    if ch.get("daylight") and not (GUARD.get("sun_up", True) if "GUARD" in globals() else True):
        return "dark"
    if ch.get("once") and (board.get(ch["id"]) or {}).get("last_clean"):
        return "done"
    if ch.get("slot") and (board.get(ch["id"]) or {}).get("claim_done"):
        return "done"
    if ch.get("slot") and too_early(ch, board.get(ch["id"]) or {}):
        return "soon"
    if ch.get("cleanup_for") and not (board.get(ch["id"]) or {}).get("blocked"):
        return "fine"   # only while a grown-up has this kid on "chores first"
    if ch.get("on_demand") and not (board.get(ch["id"]) or {}).get("opened") and not (board.get(ch["id"]) or {}).get("pending_by"):
        return "on_demand"   # e.g. laundry: only after a grown-up opens it (machines ran), gone again once it's done
    if (ch.get("until") and today > ch["until"]) or (ch.get("from") and today < ch["from"]):
        return "dates"
    if ch.get("after") or ch.get("before"):   # brush teeth from 7:30 PM; shower 7:30-8:30 PM
        from datetime import datetime
        now_ = datetime.now()
        at_ = lambda t: now_.replace(hour=int(str(t).split(":")[0]), minute=int(str(t).split(":")[1]), second=0, microsecond=0)
        if ch.get("after") and now_ < at_(ch["after"]):
            return "later"
        if ch.get("before") and now_ >= at_(ch["before"]):
            return "over"
    return None


WET_WEATHER_STATES = {"rainy", "pouring", "lightning-rainy", "snowy-rainy", "hail"}
DRY_WEATHER_STATES = {"sunny", "clear-night", "partlycloudy", "cloudy", "windy", "windy-variant", "fog"}


def _ha_timestamp(row):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(row.get("last_updated") or row.get("last_changed") or "").replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0


def read_outdoor_context(tok=None, now=None):
    """Read configured weather/rain entities into allowed/blocked/unknown.

    Stale zero rain is not evidence of dry weather.  One fresh wet signal wins;
    otherwise at least one fresh recognized signal is needed for ``allowed``.
    """
    now = now or time.time()
    cfg_ = HOUSE.get("weather_context") or {}
    max_age = float(cfg_.get("max_age_s") or 600)
    evidence, allowed, blocked = [], False, False
    for entity in cfg_.get("weather") or []:
        row = ha_get(entity, tok or ha_token()) or {}
        state, observed = str(row.get("state") or "").lower(), _ha_timestamp(row)
        fresh = bool(observed and now - observed <= max_age)
        evidence.append({"entity": entity, "state": state, "observed_at": observed or None, "fresh": fresh})
        if fresh and state in WET_WEATHER_STATES:
            blocked = True
        elif fresh and state in DRY_WEATHER_STATES:
            allowed = True
    for entity in cfg_.get("rain") or []:
        row = ha_get(entity, tok or ha_token()) or {}
        state, observed = row.get("state"), _ha_timestamp(row)
        fresh = bool(observed and now - observed <= max_age)
        try:
            amount = float(state)
        except (TypeError, ValueError):
            amount = None
        evidence.append({"entity": entity, "state": state, "observed_at": observed or None, "fresh": fresh})
        if fresh and amount is not None:
            blocked = blocked or amount > float(cfg_.get("rain_threshold") or 0)
            allowed = allowed or amount <= float(cfg_.get("rain_threshold") or 0)
    state = "blocked" if blocked else "allowed" if allowed else "unknown"
    return {"state": state, "reason": "rain" if blocked else "dry" if allowed else "weather unavailable",
            "checked_at": now, "evidence": evidence}


def watch_project_context():
    """Refresh safety context independently of lights or any other optional worker."""
    while True:
        try:
            tok, old_ = ha_token(), (GUARD.get("sun_up"), GUARD.get("outdoor_context"))
            sun_state_ = (ha_get(SUN, tok) or {}).get("state")
            GUARD["sun_up"] = True if sun_state_ == "above_horizon" else False if sun_state_ == "below_horizon" else None
            GUARD["sun_ts"] = time.time()
            GUARD["outdoor_context"] = read_outdoor_context(tok)
            if old_ != (GUARD["sun_up"], GUARD["outdoor_context"]):
                after_change()
            project_safety_stop("context")
        except Exception as ex:  # noqa: BLE001
            GUARD["sun_up"], GUARD["sun_ts"] = None, time.time()
            GUARD["outdoor_context"] = {"state": "unknown", "reason": "weather unavailable",
                                         "checked_at": time.time(), "evidence": []}
            project_safety_stop("context")
            print("project context error", ex, flush=True)
        time.sleep(30)


def stepped_context(ch, evs=None, now=None):
    """Combine every requested outdoor condition with fail-safe three-state semantics."""
    now = now or time.time()
    requested = list(((stepped_jobs.definition(ch) or {}).get("contexts") or []))
    items = []
    for name in requested:
        if name == "daylight":
            fresh = now - float(GUARD.get("sun_ts") or 0) <= 120
            state = ("allowed" if GUARD.get("sun_up") is True else "blocked" if GUARD.get("sun_up") is False else "unknown") if fresh else "unknown"
            items.append({"context": name, "state": state,
                          "reason": "daylight" if state == "allowed" else "dark" if state == "blocked" else "sun unavailable"})
        elif name == "dry":
            row = GUARD.get("outdoor_context") or {"state": "unknown", "reason": "weather unavailable"}
            items.append({"context": name, "state": row["state"], "reason": row.get("reason")})
        else:
            items.append({"context": name, "state": "unknown", "reason": "unsupported context"})
    state = "blocked" if any(x["state"] == "blocked" for x in items) else \
        "unknown" if any(x["state"] == "unknown" for x in items) else "allowed"
    approval = max((e["ts"] for e in (evs or []) if e.get("type") == "context_approve"
                    and e.get("chore") == ch.get("id") and now - e["ts"] <= 1800), default=0)
    if state == "unknown" and approval:
        return {"state": "allowed", "base_state": "unknown", "manual": True,
                "approved_at": approval, "items": items}
    return {"state": state, "manual": False, "items": items}


def cf_board(c, evs, reports, board):
    """mark each kid's clean-up job open (blocked) with the grown-up's list as its note"""
    for k, v in chores_first_state(evs, [x["id"] for x in c["kids"]], reports).items():
        b = board.get("cleanup_" + k)
        if b is not None:
            b["blocked"] = bool(v["on"] and not v.get("chore"))
            if b["blocked"]:
                b["note"] = "\n".join(v["items"]) or None
                b["note_by"] = None
    return board


def live_cfg(c, board, assisted=None):
    out = []
    for ch in c["chores"]:
        ast = (assisted or {}).get(ch["id"]) or {}
        why = None if ch.get("hidden") else ("snoozed" if ast.get("snoozed_until", 0) > time.time()
                                               else chore_closed(ch, board))
        out.append(dict(ch, hidden=True, closed=why) if why else ch)
    kids = []
    for kid in c["kids"]:
        public = {key: value for key, value in kid.items() if key != "pin_hash"}
        if kid.get("pin_hash"):
            public["pin_required"] = True
        kids.append(public)
    return dict(c, chores=out, kids=kids)


def verify_kid_pin(kid, supplied):
    """Verify a private PBKDF2 value without ever placing the PIN in public state or logs."""
    try:
        scheme, rounds, salt, expected = str(kid.get("pin_hash") or "").split("$", 3)
        if scheme != "pbkdf2_sha256" or not str(supplied).isdigit() or not 3 <= len(str(supplied)) <= 12:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", str(supplied).encode(), bytes.fromhex(salt), int(rounds)).hex()
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def make_kid_pin_hash(pin):
    pin = str(pin)
    if not pin.isdigit() or not 3 <= len(pin) <= 12:
        raise ValueError("Use 3 to 12 numbers.")
    rounds, salt = 120000, os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, rounds).hex()
    return f"pbkdf2_sha256${rounds}${salt.hex()}${digest}"


def home_of(c):
    """Households (optional, chores.json "households"): the first is the default home of every job and reward."""
    hh = c.get("households") or []
    return hh[0]["id"] if hh else None


def kid_where(c, kid):
    """Which household a kid is at now: kids[].where = {"entity", "on": household, "off": household} (Home Assistant),
    refreshed by watch_kid_where; a recent login on a household-labelled display is stronger evidence."""
    seen = (((GUARD.get("kid_display_where") or {}).get(kid) or {}) if "GUARD" in globals() else {})
    if float(seen.get("valid_until") or 0) > time.time() and seen.get("household") in {
            h.get("id") for h in c.get("households") or []}:
        return seen["household"]
    return ((GUARD.get("kid_where") or {}) if "GUARD" in globals() else {}).get(kid) or home_of(c)


def record_kid_display_presence(c, kid, ip, verified=False, now=None):
    """Use a known kid display as short-lived house/room evidence and give HA the same expiring observation."""
    display = display_for_ip(ip)
    household_ids = {h.get("id") for h in c.get("households") or []}
    if not display or display.get("household") not in household_ids:
        return None
    now = time.time() if now is None else float(now)
    ttl = max(30, min(3600, int((HOUSE.get("kid_display_presence") or {}).get("valid_seconds", 900))))
    row = {"schema": 1, "kid": kid, "household": display["household"], "room": display.get("room"),
           "display_id": display.get("id"), "display_kind": display.get("kind"),
           "source": "verified_kid_pin" if verified else "kid_profile_open",
           "observed_at": now, "valid_until": now + ttl}
    GUARD.setdefault("kid_display_where", {})[kid] = row
    ilog({"src": "kid_display_presence", "kid": kid, "ip": ip, "household": row["household"],
          "room": row.get("room"), "display": row.get("display_id"), "verified": bool(verified),
          "valid_until": row["valid_until"]})
    threading.Thread(target=ha_fire_event,
                     args=((HOUSE.get("kid_display_presence") or {}).get("event") or "chore_kid_display_presence",
                           row, ha_token()), daemon=True).start()
    return row


def peer_reset_display(ip):
    display = display_for_ip(ip)
    allowed = set(PEER_PIN_CFG.get("displays") or [])
    return display if display and display.get("kind") == "hub" and display.get("id") in allowed else None


def issue_kid_session(kid, ip, now=None):
    now = time.time() if now is None else float(now)
    token = uuid.uuid4().hex
    sessions = GUARD.setdefault("kid_sessions", {})
    for old_token, row in list(sessions.items()):
        if float(row.get("until") or 0) <= now:
            sessions.pop(old_token, None)
    sessions[token] = {"kid": kid, "ip": ip, "until": now + 600}
    return token


def valid_kid_session(token, kid, ip, now=None):
    now = time.time() if now is None else float(now)
    row = (GUARD.get("kid_sessions") or {}).get(str(token or "")) or {}
    return row.get("kid") == kid and row.get("ip") == ip and float(row.get("until") or 0) > now


HUB_CHILD_REFRESH_S = 30
HUB_CHILD_OFFLINE_S = 10
HUB_CHILD_VALID_S = 75


def observe_hub_child(ip, kid=None, token=None, now=None):
    """Record the foreground child only when a configured physical Hub proves a live child session."""
    now = time.time() if now is None else float(now)
    display = display_for_ip(ip)
    if not display or display.get("kind") != "hub" or display.get("id") not in HUBS:
        return None
    known = {row["id"] for row in cfg()["kids"]}
    accepted = kid if isinstance(kid, str) and kid in known and valid_kid_session(token, kid, ip, now) else None
    if accepted:
        # A foreground heartbeat keeps the already authenticated display-bound credential alive.
        (GUARD.get("kid_sessions") or {})[str(token)]["until"] = now + 600
    with HUB_CHILD_LOCK:
        sessions = GUARD.setdefault("hub_child_sessions", {})
        old = sessions.get(display["id"]) or {}
        changed = not old or old.get("kid") != accepted
        row = dict(old)
        row.update(kid=accepted,
                   since=(old.get("since") if accepted and old.get("kid") == accepted else
                          (now if accepted else None)),
                   last_seen=now)
        sessions[display["id"]] = row
        refresh = now - float(GUARD.get("hub_child_last_refresh") or 0) >= HUB_CHILD_REFRESH_S
        publish = changed or refresh
        if publish:
            # Snapshot both Hubs together. The private last_seen clock advances every heartbeat,
            # while public observation times advance only on a change or one global refresh.
            for current in sessions.values():
                seen = float(current.get("last_seen") or current.get("last_observed") or 0)
                if seen:
                    current.update(last_observed=seen, valid_until=seen + HUB_CHILD_VALID_S,
                                   published_at=now)
            GUARD["hub_child_last_refresh"] = now
    return {"display_id": display["id"], "kid": accepted, "publish": publish}


def expire_hub_child_sessions(now=None):
    """Clear a foreground name promptly when its physical Hub stops heartbeating."""
    now = time.time() if now is None else float(now)
    changed = False
    with HUB_CHILD_LOCK:
        for row in (GUARD.get("hub_child_sessions") or {}).values():
            seen = float(row.get("last_seen") or row.get("last_observed") or 0)
            if row.get("kid") and now - seen > HUB_CHILD_OFFLINE_S:
                row.update(kid=None, since=None, published_at=now)
                changed = True
        if changed:
            GUARD["hub_child_last_refresh"] = now
    return changed


def hub_child_sessions_manifest(now=None):
    """Small public HA manifest: no credential, IP, PIN, or historical login evidence."""
    now = time.time() if now is None else float(now)
    names = {row["id"]: row["name"] for row in cfg()["kids"]}
    out = {}
    with HUB_CHILD_LOCK:
        rows = {key: dict(value) for key, value in (GUARD.get("hub_child_sessions") or {}).items()}
    for display_id, config in HUBS.items():
        row = rows.get(display_id) or {}
        observed = float(row.get("last_observed") or 0)
        seen = float(row.get("last_seen") or observed)
        kid = row.get("kid") if seen and now - seen <= HUB_CHILD_OFFLINE_S else None
        if kid not in names:
            kid = None
        room = str(config.get("floorplan_room") or config.get("room") or display_id)
        out[room] = {"display_id": display_id, "entity": config.get("entity"),
                     "kid": kid, "name": names.get(kid) if kid else None,
                     "since": row.get("since") if kid else None,
                     "last_observed": observed or None,
                     "valid_until": (float(row.get("valid_until") or 0) or None)}
    return out


def valid_shared_reward_sessions(body, participants, ip):
    """Require a fresh, display-bound identity session for every child being charged."""
    supplied = body.get("kid_sessions") or {}
    return isinstance(supplied, dict) and all(valid_kid_session(supplied.get(kid), kid, ip)
                                               for kid in participants)


def active_peer_pin_reset(now=None):
    now = time.time() if now is None else float(now)
    for nonce, row in list(PEER_PIN_RESETS.items()):
        if row.get("status") in ("waiting", "helping") and float(row.get("until") or 0) > now:
            return nonce, row
    return None, None


def peer_helper_holding(row, now=None):
    now = time.time() if now is None else float(now)
    hold_s = max(.5, min(3, float(PEER_PIN_CFG.get("hold_fresh_seconds") or 1.5)))
    return bool(row.get("holding")) and now - float(row.get("last_hold") or 0) <= hold_s


def peer_pin_reset_public(ip, now=None, nonce=None):
    """Return only the active ceremony fields relevant to this one fixed display; never a PIN or session token."""
    now = time.time() if now is None else float(now)
    if nonce:
        row = PEER_PIN_RESETS.get(str(nonce))
        if not row or float(row.get("until") or 0) <= now:
            row = None
    else:
        nonce, row = active_peer_pin_reset(now)
    display = peer_reset_display(ip)
    if not nonce or not row or not display:
        return None
    role = "target" if ip == row.get("target_ip") else "helper" if display.get("id") != row.get("target_display") else None
    if not role:
        return None
    return {"nonce": nonce, "role": role, "target": row["target"], "helper": row.get("helper"),
            "status": row.get("status"), "holding": peer_helper_holding(row, now),
            "target_display": row.get("target_display"), "helper_display": row.get("helper_display"),
            "until": row.get("until"), "placement_trusted": bool(HUB_PLACEMENT.get("trusted"))}


def state_for_display(ip):
    state = derive()
    state["peer_pin_reset"] = peer_pin_reset_public(ip)
    state["peer_pin_reset_capable"] = bool(peer_reset_display(ip))
    add_seen_projection(state, ip, "ann:" + ip)
    return state


def _seen_state():
    """Read UI acknowledgements without creating or updating any acknowledgement."""
    try:
        value = json.loads(NEWS_SEEN.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def add_seen_projection(state, news_key, announcement_key):
    """Add the unread/news projection used by a real kid login, without marking anything seen."""
    seen = _seen_state()
    state["news_unseen"] = bool(state["whatsnew"]) and seen.get(news_key) != state["whatsnew"]["v"]
    state["news_seen_kids"] = {k["id"]: seen.get("kid:" + k["id"]) for k in state["config"]["kids"]}
    state["ann_seen_kids"] = {k["id"]: seen.get("ann:kid:" + k["id"], []) for k in state["config"]["kids"]}
    shut = {x["id"] for x in state["config"]["chores"] if x.get("closed")}
    state["announce_all"] = [{"id": b["bonus_id"], "chore": cid, "price": b["price"], "base": b["base"]}
                             for cid, b in state["board"].items()
                             if b.get("bonus") and b.get("bonus_id") and not b["clean_until"] and cid not in shut]
    acked = set(seen.get(announcement_key, []))
    state["announce"] = [row for row in state["announce_all"] if row["id"] not in acked]
    return state


def inspection_seen_snapshots(kid, now=None):
    """Lead-adult-only projection of what each configured Hub last reported from localStorage.

    `present: true, value: 0` is deliberately different from a missing key and from a
    Hub that has never reported. Device identity comes only from trusted house config.
    """
    now = time.time() if now is None else float(now)
    saved = load_private_json(INSPECTION_SEEN_FILE, {})
    displays = saved.get("displays") if isinstance(saved.get("displays"), dict) else {}
    out = []
    for display_id, config in (HOUSE.get("hubs") or {}).items():
        row = displays.get(display_id) if isinstance(displays.get(display_id), dict) else {}
        observed = (row.get("kids") or {}).get(kid) if isinstance(row.get("kids"), dict) else None
        at = float((observed or {}).get("observed_at") or 0) if isinstance(observed, dict) else 0
        out.append({"display_id": display_id, "room": config.get("room") or display_id,
                    "entity": config.get("entity"), "source": "trusted_configured_hub",
                    "observed": isinstance(observed, dict),
                    "present": observed.get("present") if isinstance(observed, dict) else None,
                    "value": observed.get("value") if isinstance(observed, dict) else None,
                    "observed_at": at or None, "age_s": max(0, round(now - at, 3)) if at else None})
    return out


def inspection_watermarks(kid, now=None):
    """Compact, stable preview contract consumed by the HA read-only wrapper."""
    rows = inspection_seen_snapshots(kid, now)
    return {
        "default_display": rows[0]["display_id"] if rows else None,
        "displays": [{"id": row["display_id"], "label": row["room"],
                      # A real stored zero must survive; missing/absent is deliberately null.
                      "seen": row["value"] if row["observed"] and row["present"] else None,
                      "observed_at": row["observed_at"], "age": row["age_s"]}
                     for row in rows],
    }


def save_inspection_seen_snapshot(ip, payload, now=None):
    """Copy, but never alter, local inspection pointers from a configured physical Hub."""
    display = next(((display_id, config) for display_id, config in (HOUSE.get("hubs") or {}).items()
                    if config.get("ip") == ip), None)
    if not display or not isinstance(payload, dict):
        return None
    allowed = {row["id"] for row in cfg()["kids"]}
    rows = {}
    for kid, observed in payload.items():
        if kid not in allowed or not isinstance(observed, dict) or type(observed.get("present")) is not bool:
            continue
        present = observed["present"]
        value = observed.get("value")
        if present and (not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0):
            continue
        rows[kid] = {"present": present, "value": float(value) if present else None,
                     "observed_at": time.time() if now is None else float(now)}
    if not rows:
        return None
    display_id, _ = display
    with INSPECTION_SEEN_LOCK:
        saved = load_private_json(INSPECTION_SEEN_FILE, {})
        saved.setdefault("displays", {}).setdefault(display_id, {}).setdefault("kids", {}).update(rows)
        save_private_json(INSPECTION_SEEN_FILE, saved)
    return display_id, list(rows)


def kid_preview_state(kid):
    """Canonical next-login state for one child, for the lead adult's authenticated read-only HA preview.

    This deliberately does not issue a kid session, verify a PIN, record display presence, consume an
    adult-login request, or acknowledge news/awards/announcements. The HA wrapper must treat the returned
    projection as inert data and must not forward preview interactions to ordinary mutation endpoints.
    """
    state = derive()
    if kid not in {row["id"] for row in state["config"]["kids"]}:
        return None
    add_seen_projection(state, "kid:" + kid, "ann:kid:" + kid)
    snapshots = inspection_seen_snapshots(kid)
    state.update({
        "preview": {"kid": kid, "read_only": True, "skip_pin": True,
                    "canonical_state": True, "mutation_transport": None,
                    "inspection_watermarks": {
                        "default_display": snapshots[0]["display_id"] if snapshots else None,
                        "displays": [{"id": row["display_id"], "label": row["room"],
                                      "seen": row["value"] if row["observed"] and row["present"] else None,
                                      "observed_at": row["observed_at"], "age": row["age_s"]}
                                     for row in snapshots],
                    },
                    # Kept for one release so an older wrapper can still consume the richer rows.
                    "inspection_seen_displays": snapshots},
        "hub": None, "hub_motion": None, "display": None, "music_interruption": None,
        "coin_flip": None, "peer_pin_reset": None, "peer_pin_reset_capable": False,
        "pin_reset_open": {},
    })
    state.pop("adult_go", None)
    return state


def hub_placement_review_state(now=None):
    if not PEER_PIN_CFG or HUB_PLACEMENT.get("trusted"):
        return None
    now = time.time() if now is None else float(now)
    displays = []
    for display_id in PEER_PIN_CFG.get("displays") or []:
        cfg_ = HUBS.get(display_id) or {}
        displays.append({"id": display_id, "room": cfg_.get("room") or display_id,
                         "fresh": now - float((GUARD.get("hub_heartbeat") or {}).get(display_id) or 0)
                                  <= float(PEER_PIN_CFG.get("offline_seconds") or 8)})
    return {"nonce": HUB_PLACEMENT.get("nonce"), "adult": str(PEER_PIN_CFG.get("adult") or LEAD_ADULT or ""),
            "reason": HUB_PLACEMENT.get("reason") or "Hub placement needs checking",
            "detected_at": HUB_PLACEMENT.get("detected_at"), "displays": displays}


def stale_peer_hubs(now=None):
    now = time.time() if now is None else float(now)
    max_age = max(4, min(60, float(PEER_PIN_CFG.get("offline_seconds") or 8)))
    heartbeats = GUARD.get("hub_heartbeat") or {}
    return [display_id for display_id in PEER_PIN_CFG.get("displays") or []
            if now - float(heartbeats.get(display_id) or 0) > max_age]


def invalidate_hub_placement(reason, missing=None, now=None):
    now = time.time() if now is None else float(now)
    if not PEER_PIN_CFG or not HUB_PLACEMENT.get("trusted"):
        return False
    HUB_PLACEMENT.update({"trusted": False, "pending": True, "nonce": uuid.uuid4().hex[:8],
                          "reason": reason, "missing": list(missing or []), "detected_at": now,
                          "phone_notified": False, "watch_notified": False})
    save_hub_placement()
    nonce, row = active_peer_pin_reset(now)
    if row:
        row.update(status="cancelled", reason="Hub placement must be checked again", holding=False)
        save_peer_pin_resets()
    after_change()
    return True


def is_here(item, c, kid):
    """A job or reward belongs to one household ("household": id, default the first) or to "any" (homework...)."""
    if not c.get("households"):
        return True
    hh = item.get("household") or home_of(c)
    return hh == "any" or hh == kid_where(c, kid)


def where_of(k, tok):
    """kids[].where = {"entity", "on": household, "off": household}; unknown/unavailable -> None (the default)."""
    w = k.get("where") or {}
    st = (ha_get(w["entity"], tok) or {}).get("state")
    return w.get("on") if st in ("on", "home", "true") else w.get("off") if st in ("off", "not_home", "false") else None


def watch_kid_where():
    while True:
        try:
            c, tok = cfg(), ha_token()
            out = {k["id"]: where_of(k, tok) for k in c["kids"] if (k.get("where") or {}).get("entity")}
            if out != GUARD.get("kid_where"):
                GUARD["kid_where"] = out
                after_change()
                project_safety_stop("household")
        except Exception as ex:  # noqa: BLE001
            print("kid where error", ex, flush=True)
        time.sleep(30)


BED_END_H = int((HOUSE.get("hours") or {}).get("bed_end", 5))   # bedtime holds until then; jobs held at bedtime resume


def bedtime_hold(st):
    """At bedtime every ordinary job goes on an indefinite hold. One per call; teams pause together."""
    chs = {x["id"]: x for x in st["config"]["chores"]}
    hold = [(k, a) for k, a in st["active"].items() if not (chs.get(a["chore"]) or {}).get("bedtime_ok")]   # the evening routine goes on
    if not (st.get("bedtime") or {}).get("on") or not hold:
        return False
    kid_, act_ = hold[0]
    append({"type": "pause", "kid": kid_, "chore": act_["chore"], "elapsed": time.time() - act_["ts"],
            "team": act_.get("team") or [kid_], "notimer": bool(act_.get("notimer")),
            "hold": "indefinite", "why": "bedtime"})
    ilog({"src": "bedtime", "action": "hold", "kid": kid_, "chore": act_["chore"]})
    after_change()
    return True


def project_safety_stop(reason):
    """Move every affected active stepped session into its mandatory stop sequence."""
    stopped = 0
    with PROJECT_LOCK:
        c_ = cfg()
        chores_ = {x["id"]: x for x in c_["chores"]}
        events_ = read_events()
        for project_ in stepped_jobs.derive(c_["chores"], events_):
            session_ = project_.get("active_session")
            if not session_ or session_.get("stop_mode"):
                continue
            ch_ = chores_.get(project_["chore"]) or {}
            affected_ = reason == "bedtime"
            if reason == "context":
                affected_ = stepped_context(ch_, events_)["state"] != "allowed"
            elif reason == "household":
                affected_ = any(not is_here(ch_, c_, k) for k in session_["team"])
            if not affected_:
                continue
            append({"type": "project_stop", "instance": project_["instance"], "session": session_["id"],
                    "mode": "pause", "reason": reason, "system": True})
            ilog({"src": "project", "action": "safety_stop", "chore": project_["chore"],
                  "instance": project_["instance"], "reason": reason})
            stopped += 1
    if stopped:
        after_change()
    return stopped


def watch_bedtime():
    while True:
        try:
            while bedtime_hold(derive()):
                pass
            if (derive().get("bedtime") or {}).get("on"):
                project_safety_stop("bedtime")
        except Exception as ex:  # noqa: BLE001
            print("bedtime hold error", ex, flush=True)
        time.sleep(10)


def wind_down_state(c):
    """chores.json "rewards_until": "19:30" -> from then until morning (BED_END_H) no rewards can be bought:
    it's tooth-brushing time and bedtime is coming. {on, at}"""
    from datetime import datetime
    at = c.get("rewards_until")
    if not at:
        return {"on": False, "at": None}
    now = datetime.now()
    hh, mm = (int(x) for x in str(at).split(":"))
    return {"on": now.hour < BED_END_H or (now.hour, now.minute) >= (hh, mm), "at": at}


def evening_routine_window(c):
    """Optional pre-bed routine gate, separate from bedtime and reward-specific cutoffs."""
    from datetime import datetime
    now = datetime.now()
    start, end = c.get("routine_at"), c.get("bedtime")
    if not start or not end or start in (False, "off") or end in (False, "off"):
        return {"on": False, "at": start, "until": end}
    def at(v):
        h, m = (int(x) for x in str(v).split(":"))
        return now.replace(hour=h, minute=m, second=0, microsecond=0)
    return {"on": at(start) <= now < at(end), "at": start, "until": end}


def must_do_first_state(c, board, now=None, arrivals=None):
    """Configured after-arrival jobs that temporarily replace a kid's other chores.

    A report waiting for inspection counts as done for this gate. If it is returned,
    the gate comes back; the configured end time always releases it for the evening.
    """
    from datetime import datetime
    now = now or datetime.now()
    arrivals = arrivals if arrivals is not None else (GUARD.get("home_arrivals") or load_home_arrivals())
    out = {k["id"]: {"on": False} for k in c["kids"]}
    clock = now.strftime("%H:%M")
    for ch in c["chores"]:
        rule = ch.get("must_do_first") or {}
        if not rule or len(ch.get("only") or []) != 1:
            continue
        kid = ch["only"][0]
        if kid not in out:
            continue
        weekday = int(rule.get("weekday", now.weekday()))
        until = str(rule.get("until") or c.get("rewards_until") or "23:59")
        eligible = []
        for day, at in (arrivals.get(kid) or {}).items():
            try:
                began = datetime.strptime(day + " " + at, "%Y-%m-%d %H:%M")
            except (TypeError, ValueError):
                continue
            if began.weekday() == weekday and began <= now:
                eligible.append(began)
        began = max(eligible) if eligible else None
        bd = board.get(ch["id"]) or {}
        done = bool(began and (float(bd.get("last_clean") or 0) >= began.timestamp() or kid in (bd.get("pending_by") or [])))
        rewards = bool(began and not done)
        on = bool(rewards and began.date() == now.date() and clock < until)
        out[kid] = {"on": on, "rewards": rewards, "chore": ch["id"], "name": ch["name"],
                    "since": began.timestamp() if began else None, "until": until}
    return out


def bedtime_state(c, evs):
    """Bedtime: from c["bedtime"] (default 20:30) until configured BED_END_H, or when a parent presses it."""
    from datetime import datetime, timedelta
    now = datetime.now()
    day0 = (now if now.hour >= BED_END_H else now - timedelta(days=1)).replace(hour=BED_END_H, minute=0, second=0, microsecond=0)
    bt = c.get("bedtime", "20:30")
    hh, mm = (int(x) for x in str(bt if bt not in (None, False, "off") else "20:30").split(":"))
    auto = day0.replace(hour=hh, minute=mm)
    marks = ([(auto.timestamp(), True, None)] if now >= auto and bt not in (None, False, "off") else []) + \
        [(e["ts"], bool(e.get("on")), e.get("by")) for e in evs if e["type"] == "bedtime" and e["ts"] >= day0.timestamp()]
    marks.sort(key=lambda m: m[0])
    on, since, by = (marks[-1] if marks else (0, False, None))[1], (marks[-1][0] if marks else 0), (marks[-1][2] if marks else None)
    return {"on": on, "since": since, "by": by, "at": auto.strftime("%H:%M")}


def derive():
    c = cfg()
    raw_evs = read_events()
    undone = {e["ref"] for e in raw_evs if e["type"] == "undo"}
    refundable = {k["id"]: [] for k in c["kids"]}
    for e in raw_evs:
        if e["type"] == "spend" and e["id"] not in undone and not e.get("with_buyer") \
                and e.get("by") not in ("share_join", "share_resume") and time.time() - e["ts"] <= 120:
            refundable.setdefault(e["kid"], []).append({k: e.get(k) for k in ("id", "ts", "reward", "reward_name", "reward_icon", "cost", "tv", "group")})
    evs = [e for e in raw_evs if e["id"] not in undone]
    chores = {x["id"]: x for x in c["chores"]}
    reports = {e["id"]: dict(e, status="pending") for e in evs if e["type"] == "report"}
    for e in evs:
        if e["type"] == "withdraw" and e.get("ref") in reports:
            reports.pop(e["ref"], None)   # temporary report canceled without a pass, fail, history row, or points
            continue
        if e["type"] == "document_host" and e.get("ref") in reports:
            reports[e["ref"]]["document_pages"] = list(e.get("pages") or [])
            reports[e["ref"]].pop("document_url", None)
            if e.get("title"):
                reports[e["ref"]]["document_title"] = e["title"]
        if e["type"] == "inspect_start" and e.get("ref") in reports:
            reports[e["ref"]]["inspecting"] = {"by": e["by"], "ts": e["ts"]}
        if e["type"] == "split" and e.get("ref") in reports:
            reports[e["ref"]]["split"] = dict(e["split"])
        if e["type"] == "delivered" and e.get("ref") in reports:   # a letter handed over after its report
            reports[e["ref"]]["delivered"] = e["ts"]
        if e["type"] == "reteam" and e.get("ref") in reports:   # a grown-up corrected who did it
            reports[e["ref"]]["team"] = list(e["team"])
            reports[e["ref"]]["kid"] = e["team"][0]
        if e["type"] == "reaward" and e.get("ref") in reports and reports[e["ref"]]["status"] == "inspected":   # a grown-up changed an award
            r_ = reports[e["ref"]]
            r_["team"], r_["kid"] = list(e["team"]), e["team"][0]
            r_["split"], r_["awarded"] = dict(e["award"]), sum(e["award"].values())
            r_["reawarded"] = e["ts"]
        if e["type"] == "priority" and e["ref"] in reports:
            reports[e["ref"]]["priority"] = e["ts"]
        if e["type"] == "refeel" and e["ref"] in reports:
            reports[e["ref"]]["feeling"] = e["feeling"]
        if e["type"] == "inspect" and e["ref"] in reports:
            r = reports[e["ref"]]
            r["status"] = "inspected" if e.get("ok") else "returned" if e.get("keep") else "rejected"   # returned: "keep working", not a fail
            r["inspected_by"] = e.get("by")
            r["inspected_ts"] = e["ts"]
            r["awarded"] = e.get("points") if e.get("ok") else 0
            r["inspect_id"] = e["id"]
            r["prank"] = bool(e.get("prank"))   # "reject with penalty": asked for an inspection with nothing done
            if e.get("label"):
                r["label"] = e["label"]
            if e.get("present"):
                r["present"] = e["present"]
            if e.get("feedback"):
                r["feedback"] = e["feedback"]
            if e.get("partial_pass"):
                r["partial"] = True
                r["leftover_note"] = e.get("leftover_note")
    papers = {k["id"]: [] for k in c["kids"]}
    for r in reports.values():
        ch_ = chores.get(r.get("chore")) or {}
        if ch_.get("document_review") and r.get("document_pages"):
            papers.setdefault(r["kid"], []).append({"id": r["id"], "ts": r["ts"], "kind": "school",
                                                     "title": r.get("document_title") or "School paper",
                                                     "pages": r["document_pages"], "status": r["status"]})
    for e in evs:
        if e["type"] == "personal_paper" and e.get("kid") in papers and e.get("document_pages"):
            papers[e["kid"]].append({"id": e["id"], "ts": e["ts"], "kind": "personal",
                                      "title": e.get("document_title") or "Personal paper",
                                      "pages": e["document_pages"], "status": "saved"})
    for rows_ in papers.values():
        rows_.sort(key=lambda x: x["ts"], reverse=True)
        del rows_[20:]   # keep the HA state compact; Drive remains the long-term archive
    bal = {k["id"]: 0 for k in c["kids"]}
    pend = {k["id"]: 0 for k in c["kids"]}
    for r in reports.values():
        if r["status"] == "inspected":
            for m, v in shares(r, r["awarded"]).items():
                if m in bal:
                    bal[m] += v
        elif r["status"] == "pending":
            for m, v in shares(r, r.get("points")).items():
                if m in pend:
                    pend[m] += v
    active, shelf = {}, {}   # shelf: each kid's jobs saved for later, newest last (starting another job keeps them)

    def unshelve(k, chore=None, hold_id=None):   # drop newest, or an explicitly selected saved job
        st_ = shelf.get(k) or []
        hit = next((z for z in reversed(st_) if (chore is None or z["chore"] == chore)
                    and (hold_id is None or z.get("id") == hold_id)), None)
        if hit:
            st_.remove(hit)
        return hit
    for e in evs:
        if e["type"] == "start":
            unshelve(e["kid"], e["chore"])
            active[e["kid"]] = {"chore": e["chore"], "ts": e["ts"], "team": [e["kid"]],
                                **({"notimer": True} if e.get("notimer") else {})}
        elif e["type"] == "team":
            lead = next((m for m in e["members"] if active.get(m, {}).get("chore") == e["chore"]), None)
            ts = active[lead]["ts"] if lead else e["ts"]
            nt = bool(lead and active[lead].get("notimer"))
            for m in e["members"]:
                active[m] = {"chore": e["chore"], "ts": ts, "team": list(e["members"]), "notimer": nt}
        elif e["type"] == "pause" and e["kid"] in active:
            gone = active[e["kid"]]
            for m in gone.get("team") or [e["kid"]]:
                if active.get(m, {}).get("chore") == gone["chore"]:
                    active.pop(m, None)
            unshelve(e["kid"], e["chore"])
            shelf.setdefault(e["kid"], []).append(
                {"chore": e["chore"], "elapsed": e["elapsed"], "team": e.get("team") or [e["kid"]],
                 "notimer": e.get("notimer"), "ts": e["ts"], "until": e.get("until"),
                 "indefinite": e.get("hold") == "indefinite", "id": e["id"], "kid": e["kid"],
                 **{k: gone[k] for k in ("split", "round", "rework", "title") if gone.get(k)}})   # a sent-back team job keeps its deal
        elif e["type"] == "resume" and shelf.get(e["kid"]):
            pz = unshelve(e["kid"], e.get("chore"), e.get("hold_id"))
            if not pz:
                continue
            for m in pz["team"]:
                active[m] = {"chore": pz["chore"], "ts": e["ts"] - pz["elapsed"], "team": list(pz["team"]), "notimer": pz.get("notimer"),
                             **({"title": pz["title"]} if pz.get("title") else {})}
        elif e["type"] == "release":
            unshelve(e["kid"], e.get("chore"))
        elif e["type"] == "title":   # a kid names their job ("organizing rocks"), at the start or any time after
            a = active.get(e["kid"])
            if a and a["chore"] == e["chore"]:
                for m in a.get("team") or [e["kid"]]:
                    if active.get(m, {}).get("chore") == e["chore"]:
                        active[m]["title"] = e["title"]
            else:
                z = next((z for z in reversed(shelf.get(e["kid"]) or []) if z["chore"] == e["chore"]), None)
                if z:
                    z["title"] = e["title"]
        elif e["type"] == "keep":   # a grown-up keeps a saved job longer (e.g. over days at the other house)
            z = next((z for z in reversed(shelf.get(e["kid"]) or []) if z["chore"] == e["chore"]), None)
            if z:
                if not z.get("indefinite"):
                    z["until"] = e["until"]
        elif e["type"] == "rework":
            for m in e["team"]:
                active[m] = {"chore": e["chore"], "ts": e["ts"] - e["worked"], "team": list(e["team"]),
                             "notimer": not e.get("timed"), "rework": {"feedback": e.get("feedback"), "by": e["by"], "ref": e["ref"]},
                             "round": e.get("round", 2), **({"split": e["split"]} if e.get("split") else {}),
                             **({"keep": True} if e.get("keep") else {}), **({"title": e["title"]} if e.get("title") else {})}
                unshelve(m, e["chore"])   # only this job's pause: a teammate's other saved job stays
        elif e["type"] == "team_split" and e["kid"] in active:
            for m in active[e["kid"]].get("team") or [e["kid"]]:
                if m in active:
                    active[m]["split"] = dict(e["split"])
        elif e["type"] == "timer_pref" and e["kid"] in active:
            for m in active[e["kid"]].get("team") or [e["kid"]]:
                if m in active:
                    active[m]["notimer"] = not e.get("on")
        elif e["type"] == "leave_team" and e["kid"] in active:
            gone = active.pop(e["kid"])
            for m in gone.get("team", []):
                if m in active:
                    active[m]["team"] = [x for x in active[m]["team"] if x != e["kid"]]
        elif e["type"] == "abandon":   # a grown-up ends a job the kids forgot about (no points), for the whole team
            for m in e.get("team") or [e["kid"]]:
                if e.get("paused"):
                    unshelve(m, e.get("chore"))
                elif active.get(m, {}).get("chore") == e.get("chore"):
                    active.pop(m)
        elif e["type"] == "stop" and e.get("kid") in active:
            gone = active.pop(e["kid"])
            for m in gone.get("team", []):
                if m in active:
                    active[m]["team"] = [x for x in active[m]["team"] if x != e["kid"]]
        elif e["type"] == "report":
            a = active.get(e.get("kid"))
            if a and a["chore"] == e.get("chore"):
                for m in a.get("team", [e["kid"]]):
                    if active.get(m, {}).get("chore") == e["chore"]:
                        active.pop(m, None)
            if e.get("kid") in shelf:
                unshelve(e["kid"], e.get("chore"))
    shelf = {k: [z for z in v if z.get("indefinite") or float(z.get("until") or 0) > time.time()] for k, v in shelf.items()}
    paused = {k: v[-1] for k, v in shelf.items() if v}   # the one a kid's screen shows; older ones come back after it
    paused_all = [dict(z, kid=k) for k, rows in shelf.items() for z in rows]
    durs = {}
    for r in reports.values():
        if r.get("started") and r["status"] != "rejected" and not r.get("partial"):
            m = (r["ts"] - r["started"]) / 60
            if 0.5 <= m <= 240:
                durs.setdefault(r["chore"], []).append(m)
    typical = {}
    for cid, xs in durs.items():
        xs = sorted(xs)
        if len(xs) >= 4:   # drop statistical outliers (Tukey fences)
            q1, q3 = xs[len(xs) // 4], xs[(3 * len(xs)) // 4]
            iqr = q3 - q1
            xs = [x for x in xs if q1 - 1.5 * iqr <= x <= q3 + 1.5 * iqr] or xs
        med = xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2
        typical[cid] = {"minutes": round(med, 1), "n": len(xs)}
    goals, likes, goal_since = {}, {}, {}
    for e in evs:
        if e["type"] == "goal":
            goals[e["kid"]] = e.get("reward")
            goal_since[e["kid"]] = e["ts"]   # when they last picked (or went back to Auto)
        # learn what each kid likes: what they buy and what they pick as a goal (newest wins ties)
        if (e["type"] == "spend" and e.get("by") in (None, e.get("kid"))) or (e["type"] == "goal" and e.get("reward")):
            d = likes.setdefault(e["kid"], {})
            n, _ = d.get(e["reward"], (0, 0))
            d[e["reward"]] = (n + 1, e["ts"])
    favorite = {k: max(d.items(), key=lambda kv: kv[1])[0] for k, d in likes.items() if d}
    earned = {k["id"]: 0 for k in c["kids"]}
    for r in reports.values():
        if r["status"] == "inspected":
            for m, v in shares(r, r["awarded"]).items():
                if m in earned:
                    earned[m] += v
    for e in evs:
        if e["type"] == "spend":
            bal[e["kid"]] -= int(e["cost"])
        elif e["type"] == "adjust":
            bal[e["kid"]] += int(e["points"])
            if e.get("earned", True):
                earned[e["kid"]] += int(e["points"])
        elif e["type"] == "transfer":
            if e.get("from") in bal and e.get("to") in bal:
                bal[e["from"]] -= int(e.get("points") or 0)
                bal[e["to"]] += int(e.get("points") or 0)
    for r in reports.values():
        if r.get("partial_of") and r["status"] == "inspected" \
                and int(r.get("awarded") or 0) <= int(r.get("rescue_base") or 0):
            # The kid received rescue pay only. Do not retain or display a claim that they also tidied the room.
            r.pop("partial_of", None)
            r.pop("rescue_base", None)
        ch = chores.get(r["chore"], {})
        r["chore_name"] = r.get("label") or (r.get("note") and r["chore"] == "other" and r["note"].capitalize()) \
            or ch.get("name", r["chore"])
        if r.get("partial"):
            r["chore_name"] += " (partly)"
        if r["chore"] == "toy_out":
            r["chore_name"] = "Toy goodbye: " + (r.get("label") or (r.get("note") or "a toy").capitalize())
        if r.get("covers") and ch.get("bundle"):
            r["chore_name"] += " (" + ", ".join(chores.get(x, {}).get("name", x) for x in r["covers"]) + ")"
        if r.get("partial_of"):
            r["chore_name"] += " + tidied part of " + ", ".join(chores.get(x, {}).get("name", x) for x in r["partial_of"])
        if r["chore"] == "toy_swap":
            kc = next((k for k in c["kids"] if k["id"] == r["kid"]), {})
            on = r.get("label") or r.get("old_toy_name") or next(
                (t["name"] for t in kc.get("old_toys", []) if t["id"] == r.get("old_toy")), None)
            r["old_name"] = on
            r["chore_name"] = "Toy swap: " + (on or "an old toy") + " out" + \
                (" → present #%d in" % r["present"] if r.get("present") else "")
            for step in ("old_out", "opened"):
                last = [e for e in evs if e["type"] == "swap_step" and e.get("ref") == r["id"] and e["step"] == step]
                r[step] = bool(last) and not last[-1].get("undo")
        r["chore_icon"] = ch.get("icon")
    # per-chore board status: price (one-time override), pending, presumed-clean cooldown
    board = {}
    def covers(r):
        return r.get("covers") or [r["chore"]]
    for cid, ch in chores.items():
        if ch.get("bundle"):
            continue
        oks = [r for r in reports.values() if cid in covers(r) and r["status"] == "inspected" and not r.get("partial")]
        last_ok = max((r["inspected_ts"] for r in oks), default=0)
        marked = max((e["ts"] for e in evs if e["type"] == "mark_clean" and e.get("chore") == cid), default=0)
        if marked > last_ok:   # an adult said "it's clean" (no points to anyone)
            last_ok = marked
        from datetime import date as _date
        price = (ch.get("price_dates") or {}).get(_date.today().isoformat(), ch.get("points"))   # e.g. worth more on one special day
        bonus = price != ch.get("points")
        bonus_id, rest_note, is_rest = None, None, False
        for e in evs:
            if e["type"] == "price" and e.get("chore") == cid and e["ts"] > last_ok and e.get("rest"):
                price, bonus, bonus_id, rest_note, is_rest = e.get("points"), True, e["id"], e.get("note"), True
                continue
            if e["type"] == "price" and e.get("chore") == cid and e["ts"] > last_ok:
                is_rest, rest_note = False, None
                if e.get("points") == ch.get("points"):          # set back to the usual price
                    price, bonus, bonus_id = ch.get("points"), False, None
                else:
                    price, bonus, bonus_id = e.get("points"), True, e["id"]
        # partly-done work already paid since the last full cleaning comes off the price (a 6-point room with 2 already paid for a part costs 4)
        part_paid = sum(int(r.get("awarded") or 0) for r in reports.values()
                        if r.get("partial") and r["chore"] == cid and r["status"] == "inspected" and (r.get("inspected_ts") or 0) > last_ok)
        part_paid += sum(max(0, int(r.get("awarded") or 0) - int(r.get("rescue_base") or 0)) // len(r["partial_of"])   # a robot
                         for r in reports.values() if cid in (r.get("partial_of") or []) and r["status"] == "inspected"   # rescue's
                         and (r.get("inspected_ts") or 0) > last_ok)                                                     # tidy part
        if part_paid and price is not None and not is_rest:
            price = max(0, price - part_paid)
            bonus = True if price != ch.get("points") else bonus
        closed_at = max([e["ts"] for e in evs if e["type"] == "close_od" and e.get("chore") == cid] or [0])   # a grown-up closed it again
        reopened = any(e["type"] == "reopen" and e.get("chore") == cid and e["ts"] > max(last_ok, closed_at) for e in evs)
        cd = float(ch.get("cooldown_h", 72 if ch.get("room") else 0))   # rooms stay clean 3 days unless an adult reopens
        clean_until = last_ok + cd * 3600 if (last_ok and cd and not reopened) else 0
        if ch.get("daily") and last_ok and not reopened:   # e.g. mail: once a day, available again after midnight
            from datetime import datetime, timedelta
            nm = datetime.combine((datetime.fromtimestamp(last_ok) + timedelta(days=1)).date(), datetime.min.time()).timestamp()
            clean_until = max(clean_until, nm)
        if ch.get("weekly"):   # e.g. homework: assigned Monday, due Friday; done = done for the week
            from datetime import datetime, timedelta
            now_ = datetime.now()
            wk = datetime.combine((now_ - timedelta(days=now_.weekday())).date(), datetime.min.time())
            nxt = (wk + timedelta(days=7)).timestamp()
            od, dd = ch["weekly"].get("open_dow", 0), ch["weekly"].get("due_dow", 4)
            if (last_ok >= wk.timestamp() and not reopened) or not (od <= now_.weekday() <= dd):
                clean_until = nxt
        pending_reports = [r for r in reports.values()
                           if cid in covers(r) and r["status"] == "pending" and not r.get("partial")]
        pend_by = list(dict.fromkeys(m for r in pending_reports for m in (r.get("team") or [r["kid"]])))
        # A pre-fix client could start this same job again while its earlier report was still waiting,
        # then Stop. That left the kid with neither an active job nor a usable tile: the old report won
        # and the tile only said it had already been turned in. Mark only that provable stale-report
        # shape as resumable; an ordinary pending inspection must remain waiting.
        resume_pending_by = list(dict.fromkeys(
            m for r in pending_reports for m in (r.get("team") or [r["kid"]])
            if any(e["type"] == "start" and e.get("kid") == m and e.get("chore") == cid
                   and e["ts"] > r["ts"] for e in evs)
            and (active.get(m) or {}).get("chore") != cid))
        clean_by = (max(oks, key=lambda r: r["inspected_ts"])["kid"] if oks else None) if last_ok != marked or not marked else None
        note_, note_by = None, None
        for e in evs:
            if e.get("chore") == cid and e["type"] in ("reopen", "price", "note") and e.get("note") is not None and e["ts"] > (last_ok if e["type"] != "reopen" else 0):
                if e["type"] == "reopen" or e["ts"] > last_ok:
                    note_, note_by = e["note"] or None, e.get("by")
        board[cid] = {"price": price, "bonus": bonus, "bonus_id": bonus_id, "base": ch.get("points"), "clean_by": clean_by,
                      "part_paid": part_paid, "rest_note": rest_note, "is_rest": is_rest, "note": note_, "note_by": note_by,
                      "tour_seen": max([e["ts"] for e in evs if e["type"] in ("tour_seen", "price", "note", "reopen", "mark_clean") and e.get("chore") == cid] or [0]),
                      "clean_until": clean_until if clean_until > time.time() else 0,
                      "last_clean": last_ok, "pending_by": pend_by,
                      "resume_pending_by": resume_pending_by, "opened": reopened}
    # once per day per kid (e.g. brush teeth): who has already done it today (pending or passed)
    from datetime import datetime as _dt
    midnight = _dt.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    for cid, ch in chores.items():
        if ch.get("each_daily") and cid in board:
            since_ = midnight - (int(ch.get("each_days") or 1) - 1) * 86400   # shower: every other night (each_days 2)
            board[cid]["done_today"] = sorted({m for r in reports.values() if r["chore"] == cid and r["ts"] >= since_
                                              and r["status"] in ("pending", "inspected") and not r.get("partial")
                                              for m in (r.get("team") or [r["kid"]])})
    # claim-a-slot chores (e.g. one kid per Saturday lesson slot, claimed ahead of time)
    for cid, ch in chores.items():
        if not ch.get("slot") or cid not in board:
            continue
        def claimant(sl_):
            w_ = None
            for e in evs:
                if e["type"] == "claim" and e.get("chore") == cid and e.get("slot") == sl_:
                    w_ = None if e.get("release") else e["kid"]
            return w_

        def checked(sl_):   # a grown-up's answer for that service: True / False / None
            v_ = None
            for e in evs:
                if e["type"] == "certify" and e.get("chore") == cid and e.get("slot") == sl_:
                    v_ = bool(e.get("ok"))
            return v_
        def passed(sl_):   # the kid's "I did it" for that service, already inspected
            return any(r["chore"] == cid and sl_ - 1800 <= r["ts"] < sl_ + 6 * 86400 and r["status"] == "inspected"
                       and not r.get("partial") for r in reports.values())
        slot = next_slot(ch)
        if certify_at(ch, slot) is not None:   # last service still waiting for the grown-up: keep showing it
            prev = next_slot(ch, after=time.time() - 6 * 86400)
            if prev < slot and prev < time.time() and claimant(prev) and checked(prev) is None and not passed(prev):
                slot = prev
        who = claimant(slot)
        done = [r for r in reports.values() if r["chore"] == cid and r["ts"] > slot - 6 * 86400
                and r["status"] in ("pending", "inspected") and not r.get("partial")]
        ca_ = certify_at(ch, slot)
        board[cid].update(slot=slot, claimed_by=who, claim_done=bool(done) or checked(slot) is not None,
                          slot_done_by=done[-1]["kid"] if done else None, certify_at=ca_, certified=checked(slot),
                          days_until=max(0, (_dt.fromtimestamp(slot).date() - _dt.now().date()).days))
        if who and ca_ and time.time() >= slot:   # the kid's HA "home" switch never on that day: probably not here for it
            hd_ = (GUARD.get("home_days") or load_home_days()).get(who)
            board[cid]["home_that_day"] = None if hd_ is None else _dt.fromtimestamp(slot).date().isoformat() in hd_
    toys = {}
    named = {}
    for e in evs:
        if e["type"] == "present_named":
            named[(e["kid"], e["present"])] = e["name"]
    for k in c["kids"]:
        if not k.get("toy_swap"):
            continue
        n = int(k.get("presents") or 0)
        swaps = [r for r in reports.values() if r["kid"] == k["id"] and r["chore"] == "toy_swap"]
        opened = {r["present"]: r for r in swaps if r["status"] == "inspected" and r.get("present")}
        toys[k["id"]] = {"total": n, "left": [i for i in range(1, n + 1) if i not in opened],
                         "opened": [{"present": i, "ref": opened[i]["id"], "gave": opened[i].get("old_name"),
                                     "got": named.get((k["id"], i)), "old_out": opened[i].get("old_out"),
                                     "unwrapped": opened[i].get("opened"), "ts": opened[i].get("inspected_ts")}
                                    for i in sorted(opened)],
                         "pending": sum(1 for r in swaps if r["status"] == "pending")}
    reward_ready = {}
    for e in evs:
        if e["type"] == "spend":
            rw = next((r for r in c["rewards"] if r["id"] == e["reward"]), {})
            st_ = e["ts"] if not e.get("await_ok") else next((x["ts"] for x in evs if x["type"] == "reward_start" and x.get("ref") == e["id"]), None)
            if rw.get("break_min") and st_:
                t = st_ + (rw.get("minutes", 0) + rw["break_min"]) * 60
                d = reward_ready.setdefault(e["kid"], {})
                d[e["reward"]] = max(d.get(e["reward"], 0), t)
    for cid, ch in chores.items():   # bundles: the sum of their still-available rooms
        if not ch.get("bundle"):
            continue
        comp = [x for x in ch["bundle"] if x in board]
        mine_pend = [r["kid"] for r in reports.values() if r["chore"] == cid and r["status"] == "pending"]
        avail = [x for x in comp if not board[x]["clean_until"] and not board[x]["pending_by"]]
        board[cid] = {"price": sum(int(board[x]["price"] or 0) for x in avail), "bonus": any(board[x]["bonus"] for x in avail),
                      "bonus_id": None, "base": sum(int(board[x]["base"] or 0) for x in avail), "covers": avail,
                      "clean_until": 0 if (avail or mine_pend) else min([board[x]["clean_until"] or time.time() for x in comp] or [0]),
                      "last_clean": 0, "pending_by": mine_pend}
        bst = ch.get("boost") or {}   # big-job boost (a kid's idea): +pct when >= min rooms, rounded to nearest point
        if bst and ch.get("points") is None and len(avail) >= bst.get("min", 3):
            raw = board[cid]["price"]
            board[cid].update(price=int(raw * (100 + bst.get("pct", 25)) / 100 + 0.5), boost_pct=bst.get("pct", 25), boost_from=raw)
        if ch.get("points") is not None:   # fixed-price bundle (e.g. a shared kids' office): all rooms or nothing; one-time price allowed
            if len(avail) < len(comp):
                board[cid].update(covers=[], clean_until=0 if mine_pend else max([board[x]["clean_until"] or 0 for x in comp] + [time.time() + 60]))
            done = max([r["inspected_ts"] for r in reports.values() if r["chore"] == cid and r["status"] == "inspected"] + [0])
            ov = [e for e in evs if e["type"] == "price" and e.get("chore") == cid and e["ts"] > done]
            board[cid].update(base=ch["points"], price=ov[-1]["points"] if ov else ch["points"], bonus=bool(ov),
                              bonus_id=ov[-1]["id"] if ov else None)
    # screen breaks: 2 shows in a row -> 1 h break; after a movie -> 1 h; after games -> 1 h
    started_ = {}
    for e in evs:   # first start wins, and a backdated "at" counts (same rule as tv_grants)
        if e["type"] == "tv_started" and e.get("ref") not in started_:
            started_[e["ref"]] = e.get("at") or e["ts"]
    mins_ov = {e["ref"]: e["minutes"] for e in evs if e["type"] == "grant_minutes"}
    rwmap = {r["id"]: r for r in c["rewards"]}
    screen_break = {}
    for k in [x["id"] for x in c["kids"]]:
        row, prev_end, brk = 0, 0, 0
        for e in sorted([e for e in evs if e["type"] == "spend" and e.get("kid") == k and e.get("reward") in ("show", "movie", "games")],
                        key=lambda e: e["ts"]):
            st0 = started_.get(e["id"]) or e.get("started_at") or e["ts"]
            mins = mins_ov.get(e["id"]) or e.get("minutes") or rwmap.get(e["reward"], {}).get("tv_minutes") or 30
            end = st0 + mins * 60
            if e["ts"] - prev_end >= 3600 or (brk and e["ts"] >= brk):
                row = 0
            if e["reward"] == "show":
                row += 1
                if row >= 2:
                    brk, row = end + 3600, 0
            else:
                brk, row = end + 3600, 0
            prev_end = max(prev_end, end)
        if brk > time.time():
            screen_break[k] = brk
            d = reward_ready.setdefault(k, {})
            for rid in ("show", "movie", "games"):
                d[rid] = max(d.get(rid, 0), brk)
    history = sorted(
        list(reports.values()) + [e for e in evs if e["type"] in ("spend", "adjust", "transfer")],
        key=lambda e: e["ts"], reverse=True)
    tvs = {k: {"name": v["name"], "short": v.get("short") or v["name"].replace(" TV", ""), "icon": v.get("icon", "1f4fa"), **GUARD["status"].get(k, {})} for k, v in TVS.items()} if "TVS" in globals() else {}
    if "TVS" in globals():
        try:
            kid_order_ = {kid_["id"]: i for i, kid_ in enumerate(c["kids"])}
            for saved_ in saved_tv_rewards(evs):
                if saved_["tv"] in tvs:
                    saved_["kids"].sort(key=lambda kid_: kid_order_.get(kid_, len(kid_order_)))
                    public_saved_ = {k: v for k, v in saved_.items() if k != "playback"}
                    public_saved_["position_saved"] = bool(saved_.get("playback"))
                    movie_fraction_ = saved_movie_remaining_fraction(saved_.get("playback"))
                    if movie_fraction_ is not None:
                        public_saved_["movie_remaining_fraction"] = movie_fraction_
                    tvs[saved_["tv"]].setdefault("saved_rewards", []).append(public_saved_)
                    # The TV lock page reads this exact row to explain why playback stopped.
                    tvs[saved_["tv"]]["paused_reward"] = public_saved_
                    # Keep the session in the HA audience contract, explicitly non-active. Room/camera consumers
                    # stop watching while floorplans can still show the banked entitlement and its transition time.
                    tvs[saved_["tv"]].setdefault("reward_audiences", []).append({
                        "session_id": saved_["session_id"], "reward": saved_["reward"],
                        "expected_kids": list(saved_["kids"]), "expected_count": len(saved_["kids"]),
                        "state": "paused", "remaining_s": saved_["remaining_s"],
                        "paused_at": saved_["paused_at"], "started_at": saved_["paused_at"],
                        "valid_until": saved_["paused_at"], "kinds": saved_.get("kinds") or []})
            for tid, us in active_unlocks_raw(evs).items():
                if tid in tvs and us:
                    tvs[tid]["unlocked_until"] = max(u["until"] for u in us)
                    tvs[tid]["unlock_why"] = sorted({u["what"] for u in us})
                    tvs[tid]["grants"] = [{k: u.get(k) for k in ("id", "until", "start", "kid", "what", "by", "kinds", "armed", "mins", "group")} for u in us]
                    # Camera/room automation can compare the actual child audience with the children who paid for
                    # this exact reward session. Waiting purchases are published but are not active monitoring
                    # windows; a watch-together group has one session id and one expected-kids set.
                    audience_ = {}
                    for u in us:
                        if u.get("what") not in ("show", "movie", "games") or not u.get("kid"):
                            continue
                        session_ = u.get("group") or u["id"]
                        row_ = audience_.setdefault(session_, {"session_id": session_, "reward": u["what"],
                                                                 "expected_kids": [], "active": False,
                                                                 "valid_until": u["until"], "kinds": u.get("kinds") or []})
                        if u["kid"] not in row_["expected_kids"]:
                            row_["expected_kids"].append(u["kid"])
                        row_["active"] = row_["active"] or not u.get("armed")
                        row_["valid_until"] = max(row_["valid_until"], u["until"])
                        if not u.get("armed"):
                            row_["started_at"] = min(row_.get("started_at", u["start"]), u["start"])
                    for row_ in audience_.values():
                        row_["expected_kids"].sort(key=lambda kid_: kid_order_.get(kid_, len(kid_order_)))
                        row_["expected_count"] = len(row_["expected_kids"])
                        row_["state"] = "active" if row_.pop("active") else "waiting"
                    if audience_:
                        tvs[tid]["reward_audiences"] = sorted(list(audience_.values()) + tvs[tid].get("reward_audiences", []), key=lambda row_: (
                            row_["state"] != "active", row_.get("started_at") or row_["valid_until"]))
                    # when it actually locks: the running clocks, then any queued purchases back to back (armed = not started)
                    run_ = [u["until"] for u in us if not u.get("armed") and u["what"] != "vr"]
                    if run_:
                        la, gseen = max(run_), {u.get("group") for u in us if not u.get("armed") and u.get("group")}
                        for u in sorted([u for u in us if u.get("armed") and u["what"] != "vr"], key=lambda u: u["ts"]):
                            if u.get("group") and u["group"] in gseen:
                                continue
                            gseen.add(u.get("group"))
                            la += (u.get("mins") or 30) * 60
                        tvs[tid]["locks_at"] = la
                    else:
                        tvs[tid]["waiting"] = [u["id"] for u in us if u.get("armed")]
        except Exception:  # noqa: BLE001
            pass
    import hashlib
    ver = hashlib.md5((HERE / "static/kids.html").read_bytes()).hexdigest()[:10]
    ver_tv = hashlib.md5((HERE / "static/tvlock.html").read_bytes()).hexdigest()[:10]   # the TV lock page reloads itself on change
    try:
        news = json.loads((DATA / "whatsnew.json").read_text())
    except (OSError, ValueError):
        news = []
    try:
        kid_open = json.loads(KID_OPEN.read_text())
    except (OSError, ValueError):
        kid_open = {}
    must_first = must_do_first_state(c, board)
    chores_first = chores_first_state(evs, [k["id"] for k in c["kids"]], reports)
    assisted = adult_first_state(c, evs, reports)
    shower = shower_requirement_state(c, reports)
    if "shower" in board:
        # `each_daily` normally means once per day. A shower is instead offered
        # only when this child's two-night rule says it is due.
        board["shower"]["done_today"] = sorted(k for k, v in shower.items() if not v.get("due"))
    evening_routine = {}
    for k_ in c["kids"]:
        kid_ = k_["id"]
        items_ = []
        if kid_ not in ((board.get("brush_teeth") or {}).get("done_today") or []):
            items_.append("brush_teeth")
        if (shower.get(kid_) or {}).get("due"):
            items_.append("shower")
        evening_routine[kid_] = {"due": bool(items_), "items": items_}
    present = sorted(GUARD.get("home_now") or []) if "GUARD" in globals() else []
    assist_requests = [{"nonce": n, "kid": r.get("kid"), "chore": r.get("chore"), "reward": r.get("reward"),
                        "first_pin": bool(r.get("first_pin")),
                        "purpose": r.get("purpose"), "who": r.get("who"), "ts": r["ts"],
                        "expires": r["ts"] + ADULT_REQUEST_TTL_S}
                       for n, r in ADULT_REQ.items() if r.get("purpose") in ("assist", "reward", "pin_reset")
                       and r.get("status") == "pending" and time.time() - r["ts"] <= ADULT_REQUEST_TTL_S] \
        if "ADULT_REQ" in globals() else []
    pin_reset_open = {r["kid"]: {"until": r.get("until"), "nonce": n,
                                  "first_pin": bool(r.get("first_pin"))}
                      for n, r in ADULT_REQ.items() if r.get("purpose") == "pin_reset" and r.get("status") == "ok"
                      and not r.get("used") and float(r.get("until") or 0) > time.time()} \
        if "ADULT_REQ" in globals() else {}
    for gate_ in must_first.values():
        if gate_.get("rewards") and gate_.get("chore") in board:
            board[gate_["chore"]]["clean_until"] = 0   # an unfinished carry-over stays visible until it is reported
    dress = dict(dress_state(evs, reports, GUARD.get("awake_ts")),
                 started={k: a["ts"] for k, a in active.items()
                          if a.get("chore") == "get_dressed" and not a.get("notimer")})
    laundry_chore = DRESS.get("laundry_chore")
    if dress.get("on") and laundry_chore and (board.get(laundry_chore) or {}).get("opened"):
        dress["laundry_hint"] = DRESS.get("laundry_message") or "A lot of your favorite clothes are in the laundry pile."
    pin_epoch = hashlib.sha256("|".join(k["id"] + ":" + str(k.get("pin_hash") or "") for k in c["kids"]).encode()).hexdigest()[:12]
    projects_ = stepped_jobs.derive(c["chores"], evs)
    project_contexts_ = {ch_["id"]: stepped_context(ch_, evs) for ch_ in c["chores"] if stepped_jobs.definition(ch_)}
    packing_ = packing_config()
    return {"version": ver, "ver_tv": ver_tv, "sound_effects": sound_enabled(),
            "pin_epoch": pin_epoch,
            "lead_adult": LEAD_ADULT if "LEAD_ADULT" in globals() else None, "kid_open": kid_open, "whatsnew": news[-1] if news else None, "whatsnew_all": news[-8:],
            "chore_mode": GUARD.get("mode") if "GUARD" in globals() else None, "toys": toys, "reward_ready": reward_ready,
            "timers": timed_rewards(raw_evs), "screen_break": screen_break, "bedtime": bedtime_state(c, evs), "refundable": refundable,
            "wind_down": wind_down_state(c), "evening_routine_window": evening_routine_window(c),
            "config": live_cfg(c, cf_board(c, evs, reports, board), assisted), "balance": bal, "earned": earned, "goals": goals, "goal_since": goal_since, "favorite": favorite, "active": active, "paused": paused,
            "paused_all": paused_all, "projects": [stepped_jobs.public_project(p) for p in projects_],
            "project_contexts": project_contexts_, "typical": typical,
            **(lambda A: {**A, **dict(zip(("achievements", "streak"), achievements(c, reports, earned, A["awards"])))})(compute_awards(c, reports, evs, earned, typical)),
            **dict(zip(("expected", "personal_best"), expected_times(c, reports, typical))), "pending_points": pend, "board": board,
            "tv": {"lock_until": GUARD.get("lock_until", 0) if "GUARD" in globals() else 0, "tvs": tvs},
            "history": history[:200], "now": time.time(),
            "tidy_spaces": space_states(HOUSE.get("tidy_spaces", []), board, active, paused, evs, time.time()),
            "households": c.get("households") or [], "kid_where": {k["id"]: kid_where(c, k["id"]) for k in c["kids"]},
            "present_kids": present,
            "dress": dress, "chores_first": chores_first,
            "must_do_first": must_first, "adult_first": assisted, "assist_requests": assist_requests,
            "pin_reset_open": pin_reset_open,
            "adult_busy_until": adult_busy_until(evs), "adult_busy": adult_busy_state(evs),
            "hub_placement_review": hub_placement_review_state(),
            "shower": shower, "evening_routine": evening_routine,
            "calendar": schedule.agenda(), "packing_game": packing_,
            "packing_up_next": packing_up_next(packing_, LEAD_ADULT if "LEAD_ADULT" in globals() else None),
            "departure_display": departure_display(),
            "departure_house_tasks": departure_house_tasks(),
            "papers": papers, "paper_scan_ready": bool((HOUSE.get("paper_scanner") or {}).get("ready")),
            "music_block": music_block_state(evs, chores_first, must_first, present, reports,
                                               GUARD.get("home_arrivals") or load_home_arrivals()),
            "spotify_volume_cap": spotify_volume_cap_state(evs),
            "spotify_exempt_rooms": spotify_exempt_rooms(),
            "reward_block": reward_block_state(),
            "kid_notes": kid_notes(evs), "requests": reward_requests(evs),
            "room_choices": (HOUSE.get("house_tasks") or {}).get("floors") or {}, "envelope": envelopes.public(),
            "next_step": {m: next_step(chores.get(a["chore"]) or {}, a.get("team") or [k], a) for k, a in active.items()
                          if (chores.get(a["chore"]) or {}).get("remind") for m in (a.get("team") or [k])}}


def push_ha(state):
    """Mirror balances into HA as sensor.chore_points_<kid> (best effort)."""
    if not HA_URL:   # no Home Assistant configured
        return None
    try:
        AUTHORITY.require_effect()
    except FencedError:
        return None
    try:
        tok = ha_token()
    except OSError:
        return
    for k in state["config"]["kids"]:
        kid = k["id"]
        body = json.dumps({"state": state["balance"][kid], "attributes": {
            "friendly_name": f"{k['name']} points", "unit_of_measurement": "pts",
            "icon": "mdi:star-circle", "pending_points": state["pending_points"][kid],
            "waiting_for_inspection": sum(1 for r in state["history"]
                                          if r.get("type") == "report" and r["kid"] == kid
                                          and r["status"] == "pending")}}).encode()
        req = urllib.request.Request(f"{HA_URL}/api/states/sensor.chore_points_{kid}", data=body,
                                     headers={"Authorization": f"Bearer {tok}",
                                              "Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=5).read()
        except Exception as ex:  # noqa: BLE001
            print("HA push failed", kid, ex, flush=True)


def room_log(state):
    """Per chore: recent cleanings + adult clean/dirty calls, for the floor-plan Clean lens."""
    evs = read_events()
    undone = {e["ref"] for e in evs if e["type"] == "undo"}
    out = {}
    for r in state["history"]:
        if r.get("type") != "report" or r.get("status") not in ("inspected",):
            continue
        sec = (r["ts"] - r["started"]) if r.get("started") else None
        item = {"t": r.get("inspected_ts") or r["ts"], "k": "part" if r.get("partial") else "done", "who": r.get("team") or [r["kid"]],
                "sec": round(sec) if sec and 30 <= sec <= 4 * 3600 else None, "pts": r.get("awarded"), "by": r.get("inspected_by")}
        out.setdefault(r["chore"], []).append(item)
    for e in evs:
        if e["id"] in undone or e["type"] not in ("mark_clean", "reopen", "price") or not e.get("chore"):
            continue
        if e["type"] == "price" and (e.get("rest") or e.get("ref")):
            continue
        out.setdefault(e["chore"], []).append({"t": e["ts"], "k": {"mark_clean": "clean", "reopen": "dirty", "price": "price"}[e["type"]],
                                                "by": e.get("by"), **({"pts": e["points"]} if e["type"] == "price" else {}),
                                                **({"note": e["note"][:80]} if e.get("note") else {})})
    res = {}
    now = time.time()
    for cid, items in out.items():
        items.sort(key=lambda x: x["t"], reverse=True)
        done = [x for x in items if x["k"] == "done"]
        gaps = [(a["t"] - b["t"]) / 86400 for a, b in zip(done, done[1:])]
        secs = [x["sec"] for x in done if x.get("sec")]
        res[cid] = {"log": items[:8], "n30": sum(1 for x in done if now - x["t"] < 30 * 86400),
                    "gap_d": round(sum(gaps) / len(gaps), 1) if gaps else None,
                    "avg_min": round(sum(secs) / len(secs) / 60, 1) if secs else None}
    return res


DEMO_DROP = ("households", "kid_where", "present_kids", "envelope", "kid_notes", "requests", "calendar", "packing_game", "packing_up_next", "departure_display", "departure_house_tasks", "music_block", "spotify_exempt_rooms", "reward_block", "room_choices", "hub_placement_review",
             "projects", "project_contexts", "paused_all",
             "adult_first", "assist_requests", "adult_busy", "adult_busy_until", "shower", "last_result", "inspect_photos", "inspect_media")
DEMO_FREE_TEXT = ("note", "note_by", "rest_note", "feedback", "leftover_note", "ask", "reason", "label", "present",
                  "document_url", "document_title")


def demo_attrs(attrs):
    """sensor.chore_state for a visitor's demo (house.json demo_state names the sensor): no free text written by
    the family, no other household, no where-the-kids-are, no addresses for letters, nothing a screen didn't show"""
    d = json.loads(json.dumps({k: v for k, v in attrs.items() if k not in DEMO_DROP}, default=str))
    d["friendly_name"] = "Chore demo state"
    c = d.get("config") or {}
    c.pop("households", None)
    c["kids"] = [{k: x.get(k) for k in ("id", "name", "color", "icon")} for x in c.get("kids") or []]
    c["inspectors"] = [{k: x.get(k) for k in ("id", "name", "icon")} for x in (c.get("inspectors") or [])[:1]]   # the lead grown-up only
    private_chores = {x.get("id") for x in c.get("chores") or [] if x.get("private")}
    c["chores"] = [x for x in c.get("chores") or [] if x.get("id") not in private_chores]
    for cid in private_chores:
        (d.get("board") or {}).pop(cid, None)
    d["history"] = [x for x in d.get("history") or [] if x.get("chore") not in private_chores]
    for ch in c.get("chores") or []:
        for k in ("note", "home_entity"):
            ch.pop(k, None)
    for h in d.get("history") or []:
        for k in DEMO_FREE_TEXT:
            h.pop(k, None)
    for b in (d.get("board") or {}).values():
        if isinstance(b, dict):
            for k in DEMO_FREE_TEXT:
                b.pop(k, None)
    if isinstance(d.get("chore_mode"), dict):
        d["chore_mode"]["home"] = []
    for tv_ in ((d.get("tv") or {}).get("tvs") or {}).values():
        if isinstance(tv_, dict):
            tv_.pop("adult_presence", None)
    for k in ("dress", "chores_first"):
        if isinstance(d.get(k), dict):
            d[k].pop("items", None)
            for v in d[k].values():
                if isinstance(v, dict):
                    v.pop("items", None)
    return d


HA_STATE_ATTR_TARGET = 12000   # Recorder rejects one entity's attributes at 16,384 bytes; retain ample JSON overhead.


def chunked_state_payload(attrs, attr_target=HA_STATE_ATTR_TARGET):
    """Encode an arbitrarily rich UI state as bounded HA sensor attribute chunks.

    The manifest is written last, so a browser either reconstructs one complete
    revision or briefly keeps waiting while its parts are being replaced.
    """
    payload = json.dumps(attrs, ensure_ascii=False, separators=(",", ":"), default=str)
    revision = hashlib.sha256(payload.encode()).hexdigest()[:16]
    chunks, pos = [], 0
    while pos < len(payload):
        lo, hi, best = 1, len(payload) - pos, 1
        while lo <= hi:
            mid = (lo + hi) // 2
            probe = {"friendly_name": "Chore state part 999", "revision": revision,
                     "part": 999, "parts": 999, "payload": payload[pos:pos + mid]}
            size = len(json.dumps(probe, ensure_ascii=False, separators=(",", ":")).encode())
            if size <= attr_target:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        chunks.append(payload[pos:pos + best])
        pos += best
    return revision, chunks


def publish_chunked_state(entity, attrs, tok, guard_key, manifest_fields=None):
    """Publish UI state without creating an oversized Recorder attribute row.

    Returns False when the content is unchanged. Part entities are opaque
    transport; the named entity is a small, stable manifest and action receipt.
    """
    revision, chunks = chunked_state_payload(attrs)
    content_changed = GUARD.get(guard_key) != revision
    stem = entity.removeprefix("sensor.")
    total = len(chunks)
    if content_changed:
        for i, payload in enumerate(chunks, 1):
            ha_post_state(f"sensor.{stem}_part_{i}", revision,
                          {"friendly_name": f"Chore state part {i}", "revision": revision,
                           "part": i, "parts": total, "payload": payload}, tok)
    manifest = {"friendly_name": attrs.get("friendly_name") or "Chore state",
                "chunked": True, "encoding": "json", "revision": revision,
                "parts": total, "payload_bytes": len(json.dumps(attrs, ensure_ascii=False,
                                                                   separators=(",", ":"), default=str).encode())}
    manifest.update(manifest_fields or {})
    manifest_sig = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                                                default=str).encode()).hexdigest()
    manifest_changed = GUARD.get(guard_key + "_manifest") != manifest_sig
    if manifest_changed:
        ha_post_state(entity, revision, manifest, tok)
        GUARD[guard_key + "_manifest"] = manifest_sig
    if content_changed:
        GUARD[guard_key] = revision
    return content_changed or manifest_changed


def push_state_sensor(state=None, last_result=None):
    """Publish everything the HA-served screens need through bounded, deduplicated sensor chunks."""
    if not HA_URL:   # no Home Assistant configured
        return None
    try:
        tok = ha_token()
    except OSError:
        return
    state = state or derive()
    keep = ("id", "ts", "type", "kid", "chore", "chore_name", "chore_icon", "status", "points", "awarded",
            "feeling", "inspected_by", "inspect_id", "reason", "by", "reward", "reward_name", "reward_icon", "cost", "tv",
            "toy", "toy_name", "label", "present", "old_name", "old_out", "opened", "priority", "note", "started", "team", "partial", "full_price", "worked", "split", "inspecting", "round", "feedback", "leftover_note", "ask", "delivered", "inspected_ts", "certified", "prank",
            "document_url", "document_title", "document_pages")
    hist = [{k: h[k] for k in keep if k in h} for h in state["history"][:40]]
    attrs = {"friendly_name": "Chore state", "config": state["config"], "balance": state["balance"],
             "earned": state.get("earned"), "active": state.get("active"), "typical": state.get("typical"),
             "records": state.get("records"), "personal_records": state.get("personal_records"), "paused": state.get("paused"),
             "paused_all": state.get("paused_all"), "projects": state.get("projects"),
             "project_contexts": state.get("project_contexts"),
             "expected": state.get("expected"), "personal_best": state.get("personal_best"),
             "achievements": state.get("achievements"), "streak": state.get("streak"), "chore_mode": state.get("chore_mode"),
             "pending_points": state["pending_points"], "board": state["board"], "history": hist,
             "tv": state.get("tv"), "toys": state.get("toys"), "tidy_spaces": state.get("tidy_spaces", {}),
             "reward_ready": state.get("reward_ready"), "timers": state.get("timers"), "refundable": state.get("refundable"),
             # for the kids' screen inside HA (a child at another home, signed in by their parent's HA login)
             "awards": state.get("awards"), "awards_seen": state.get("awards_seen"), "goals": state.get("goals"), "goal_since": state.get("goal_since"), "favorite": state.get("favorite"),
             "screen_break": state.get("screen_break"), "bedtime": state.get("bedtime"), "wind_down": state.get("wind_down"), "version": state.get("version"),
             "households": state.get("households"), "kid_where": state.get("kid_where"),
             "present_kids": state.get("present_kids"),
             "dress": state.get("dress"), "chores_first": state.get("chores_first"), "must_do_first": state.get("must_do_first"),
             "music_block": state.get("music_block"),
             "spotify_volume_cap": state.get("spotify_volume_cap", 100),
             "spotify_exempt_rooms": state.get("spotify_exempt_rooms", []),
             "calendar": state.get("calendar"), "packing_game": state.get("packing_game"),
             "packing_up_next": state.get("packing_up_next"), "departure_display": state.get("departure_display"),
             "papers": state.get("papers"),
             "paper_scan_ready": state.get("paper_scan_ready"),
             "kid_notes": state.get("kid_notes"), "requests": state.get("requests"),
             "assist_requests": state.get("assist_requests"), "envelope": state.get("envelope"),
             "adult_busy": state.get("adult_busy"), "adult_busy_until": state.get("adult_busy_until"),
             "reward_block": state.get("reward_block"),
             "next_step": state.get("next_step"),
             "room_choices": state.get("room_choices")}
    try:   # before/after pictures for inspections (room eye): which exist; the pictures themselves stay out of HA's states
        import room_eye
        if room_eye.EYE and room_eye.EYE.S.get("photos"):
            attrs["inspect_photos"] = [{k: x.get(k) for k in ("key", "chore", "kid", "ref", "before", "after")} for x in room_eye.EYE.S["photos"][-20:]]
            attrs["inspect_media"] = ((HOUSE.get("room_eye") or {}).get("inspect_shots") or {}).get("media_source")
    except Exception:  # noqa: BLE001
        pass
    if last_result is not None:
        attrs["last_result"] = last_result
    elif GLOBAL_LAST.get("r"):
        attrs["last_result"] = GLOBAL_LAST["r"]
    try:   # floor-plan Clean lens: per-room log, only re-sent when it changes
        rl = room_log(state)
        sig = json.dumps(rl, sort_keys=True)
        if sig != GUARD.get("room_log_sig") or time.time() - GUARD.get("room_log_ts", 0) > 600:
            ha_post_state("sensor.chore_rooms", str(len(rl)), {"friendly_name": "Chore rooms", "icon": "mdi:broom", "chores": rl}, tok)
            GUARD["room_log_sig"], GUARD["room_log_ts"] = sig, time.time()
    except Exception as ex:  # noqa: BLE001
        print("room log push failed", ex, flush=True)
    if HOUSE.get("demo_state"):   # a visitor's read-only demo of the kids' screen: the same, minus private details
        try:
            demo = demo_attrs(attrs)
            publish_chunked_state(HOUSE["demo_state"], demo, tok, "demo_sig",
                                  {"version": demo.get("version")})
        except Exception as ex:  # noqa: BLE001
            print("demo state push failed", ex, flush=True)
    try:
        publish_chunked_state("sensor.chore_state", attrs, tok, "state_sig",
                              {"version": attrs.get("version"), "last_result": attrs.get("last_result"),
                               "spotify_volume_cap": attrs.get("spotify_volume_cap", 100),
                               "spotify_exempt_rooms": attrs.get("spotify_exempt_rooms", []),
                               "hub_child_sessions": hub_child_sessions_manifest()})
    except Exception as ex:  # noqa: BLE001
        print("HA state push failed", ex, flush=True)


def queue_hub_child_state_push():
    """Coalesce the two Hubs' fast observations into one background HA manifest update."""
    with HUB_CHILD_LOCK:
        if GUARD.get("hub_child_push_pending"):
            return
        GUARD["hub_child_push_pending"] = True

    def run():
        try:
            push_state_sensor()
        finally:
            with HUB_CHILD_LOCK:
                GUARD["hub_child_push_pending"] = False
    threading.Thread(target=run, name="hub-child-publish", daemon=True).start()


def watch_hub_child_sessions():
    while True:
        if expire_hub_child_sessions():
            queue_hub_child_state_push()
        time.sleep(2)


GLOBAL_LAST = {}


ADULTS = HOUSE.get("adults") or {}   # grown-up id -> {presence, home_state, notify: [HA notify services]}
ADULT_NOTIFY = {k: v.get("notify") or [] for k, v in ADULTS.items() if v.get("notify")}
ADULT_BUZZ_NOTIFY = {k: v.get("buzz_notify") or [] for k, v in ADULTS.items() if v.get("buzz_notify")}
LEAD_ADULT = next(iter(ADULT_NOTIFY), next(iter(ADULTS), None))   # gets the phone/watch pings (first adult with notify)
PARENT_NOTIFY = ADULT_NOTIFY.get(LEAD_ADULT, [])


def available_request_adult(state):
    """Prefer an available adult with a push target; dashboard-only adults remain valid fallbacks."""
    busy = state.get("adult_busy") or {}
    available = [a for a in ADULTS if float(busy.get(a) or 0) <= time.time()]
    return next((a for a in available if ADULT_NOTIFY.get(a)), available[0] if available else None)


def notify_parents(ev):
    try:
        tok = ha_token()
    except OSError:
        return
    c = cfg()
    k = next((x for x in c["kids"] if x["id"] == ev["kid"]), {"name": ev["kid"]})
    where = "every TV" if ev.get("tv") == "*" else (TVS.get(ev.get("tv")) or {}).get("name", "")
    mins = ev.get("minutes") or next((r.get("minutes") for r in c["rewards"] if r["id"] == ev["reward"]), None)
    msg = f"{k['name']} bought {ev.get('reward_name', ev['reward'])}" + (f" on {where}" if where else "") + \
        (f" — {mins} min" if mins else "") + f" ({ev['cost']}⭐). Tap to adjust."
    acts = []
    if ev.get("tv") in TVS or ev.get("tv") == "*":
        tv = ev["tv"]
        if ev.get("together"):
            msg = msg.replace(" bought ", " + ".join(k_["name"] for k_ in c["kids"] if k_["id"] in ev["together"][1:]).join([" (watching together with ", ") bought "]), 1)
        acts = [{"action": "CHORE_TVPLUS_" + tv, "title": "+10 min"}, {"action": "CHORE_TVFIVE_" + tv, "title": "5-min warning"},
                {"action": "CHORE_TVLOCK_" + tv, "title": "Lock now"}]
    if not parent_available(tok):
        ilog({"src": "notify", "action": "skipped_quiet_or_away", "what": "spend", "ref": ev.get("id")})
        return
    for svc in PARENT_NOTIFY:
        data = phone_notice_data(LEAD_ADULT, {"clickAction": "/lovelace/chores",
                                 "tag": "chore-tv-" + str(ev.get("tv")), "group": "chores",
                                 "priority": "high", "ttl": 0, "actions": acts}, tok)
        if data is not None:
            ha_call(svc, {"title": "Chores: TV time", "message": msg, "data": data}, tok)


def notify_request(ev):
    """a kid asked for a reward that needs a grown-up (VR): phone notice with Accept / Not now"""
    try:
        tok = ha_token()
    except OSError:
        return
    k = next((x for x in cfg()["kids"] if x["id"] == ev["kid"]), {"name": ev["kid"]})
    if not parent_available(tok):
        ilog({"src": "notify", "action": "skipped_quiet_or_away", "what": "request", "ref": ev.get("id")})
        return
    for svc in PARENT_NOTIFY:
        data = phone_notice_data(LEAD_ADULT, {"clickAction": "/lovelace/chores", "tag": "chore-req-" + ev["id"],
                                 "group": "chores", "priority": "high", "ttl": 0,
                                 "actions": [{"action": "CHORE_REQOK_" + ev["id"], "title": "Start the timer"},
                                             {"action": "CHORE_REQNO_" + ev["id"], "title": "Not now (refund)"}]},
                                 tok, durable=True)
        if data is not None:
            ha_call(svc, {"title": "Chores: %s bought %s" % (k["name"], ev.get("reward_name", ev["reward"])),
                          "message": "Paid %d⭐. Set it up with them, then tap Start to begin the %s-minute timer." % (
                              ev["cost"], next((r.get("minutes") for r in cfg()["rewards"] if r["id"] == ev["reward"]), "")),
                          "data": data}, tok)


# The phone carries action buttons. Optional buzz_notify targets alert Wear OS directly,
# where Home Assistant supports the notification but not its action buttons.
ADULT_REQ_FILE = DATA / "adult_requests.json"
ADULT_REQUEST_TTL_S = 10 * 60


def load_adult_requests():
    """Keep short-lived consent dialogs through an ordinary service reload."""
    try:
        saved = json.loads(ADULT_REQ_FILE.read_text())
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {str(n): r for n, r in saved.items() if isinstance(r, dict)
            and now - float(r.get("ts") or 0) < (ADULT_REQUEST_TTL_S if r.get("purpose") in ("assist", "reward", "pin_reset") else 300)
            and (r.get("status") == "pending" or (r.get("status") == "ok" and not r.get("used")))}


def save_adult_requests():
    active = {n: r for n, r in ADULT_REQ.items()
              if r.get("status") == "pending" or (r.get("status") == "ok" and not r.get("used"))}
    try:
        pending = ADULT_REQ_FILE.with_suffix(".tmp")
        with LOCK:
            pending.write_text(json.dumps(active))
            pending.replace(ADULT_REQ_FILE)
    except OSError:
        pass


ADULT_REQ = load_adult_requests()   # nonce -> {who, ip, ts, status}


def send_adult_request(nonce, who, where):
    try:
        tok = ha_token()
    except OSError:
        return
    req = ADULT_REQ.get(nonce) or {}
    if req.get("purpose") in ("assist", "reward", "pin_reset"):
        c = cfg()
        kn = next((k["name"] for k in c["kids"] if k["id"] == req.get("kid")), req.get("kid", "A kid"))
        if req.get("purpose") == "pin_reset":
            if req.get("first_pin"):
                title, message = "Chores: first PIN", "%s wants to set their first PIN. Approve a 10-minute setup window?" % kn
            else:
                title, message = "Chores: forgotten PIN", "%s forgot their PIN. Open a 10-minute first-come window to set a new one?" % kn
        elif req.get("purpose") == "reward":
            rw = next((x for x in c["rewards"] if x["id"] == req.get("reward")), {})
            title, message = "Chores: VR permission", "%s wants to redeem %s for %s⭐. Are you available to help?" % (
                kn, rw.get("name", "VR"), rw.get("cost", "?"))
        else:
            ch = next((x for x in c["chores"] if x["id"] == req.get("chore")), {})
            title, message = "Chores: first-time help needed", "%s wants to do %s (%s⭐). Can you teach and help this first time?" % (
                kn, ch.get("name", "this chore"), ch.get("points", "?"))
        actions = [{"action": "CHORE_OK_" + nonce, "title": ("Approve setup" if req.get("first_pin") else "Open reset") if req.get("purpose") == "pin_reset" else "Allow now" if req.get("purpose") == "reward" else "Help now"},
                   {"action": "CHORE_NO_" + nonce, "title": "Dismiss" if req.get("purpose") == "pin_reset" else "Busy, later (1 hour)"}]
    else:
        title = "Chores: is this you?"
        message = f"Someone at the {where} wants grown-up mode as {who.title()}."
        actions = [{"action": "CHORE_OK_" + nonce, "title": "It's me"},
                   {"action": "CHORE_NO_" + nonce, "title": "Not me"}]
    accepted = []
    for svc in ADULT_NOTIFY.get(who, []):
        data = phone_notice_data(who, {"tag": "chore-adult-" + nonce, "priority": "high", "ttl": 0,
                                      "actions": actions}, tok, durable=True)
        if data is not None and ha_call(svc, {"title": title, "message": message, "data": data}, tok):
            accepted.append(svc)
    buzzed = []
    for svc in ADULT_BUZZ_NOTIFY.get(who, []):
        # A forgotten-PIN request is an attended child safety/recovery action, not an ordinary chore nudge.
        # Forgotten-PIN recovery must reach the configured watch even while the daily routine buzz gate is quiet.
        if req.get("purpose") == "pin_reset" or adult_buzz_allowed(who, tok):
            if ha_call(svc, {"title": title, "message": message + " Open the phone or parent Chores dashboard to answer.",
                             "data": {"tag": "chore-adult-" + nonce + "-buzz", "priority": "high", "ttl": 0,
                                      "channel": "Chore approvals", "importance": "high"}}, tok):
                buzzed.append(svc)
        else:
            ilog({"src": "notify", "action": "watch_buzz_suppressed", "nonce": nonce,
                  "purpose": req.get("purpose"), "who": who, "service": svc})
    ilog({"src": "notify", "action": "adult_request_accepted_by_ha" if accepted else "adult_request_failed",
          "nonce": nonce, "purpose": req.get("purpose"), "to": accepted, "buzz": buzzed})


def parent_quiet():
    """house quiet_hours: [{"weekdays": [5, 6], "until": "09:00"}] - no pings before then (Mon=0)."""
    from datetime import datetime
    n = datetime.now()
    for q in HOUSE.get("quiet_hours") or []:
        if n.weekday() in (q.get("weekdays") or range(7)) and n.strftime("%H:%M") < str(q.get("until", "00:00")):
            return True
    return False


def adult_home(who, tok):
    a = ADULTS.get(who) or {}
    return bool(a.get("presence")) and ha_get(a["presence"], tok).get("state") == a.get("home_state", "home")


def adult_buzz_allowed(who, tok):
    """Optional daily HA gate for a watch buzz. Unknown, offline, or a stale previous cycle fails quiet."""
    gate = (ADULTS.get(who) or {}).get("buzz_gate") or {}
    if not gate.get("entity"):
        return True
    snapshot = ha_get(gate["entity"], tok) or {}
    state = str(snapshot.get("state") or "").lower()
    allowed = {str(x).lower() for x in gate.get("states") or ["on"]}
    if state not in allowed:
        return False
    try:
        from datetime import datetime, timedelta
        now = datetime.now().astimezone()
        boundary_h = int((HOUSE.get("hours") or {}).get("bed_end", 5)) % 24
        boundary = now.replace(hour=boundary_h, minute=0, second=0, microsecond=0)
        if now < boundary:
            boundary -= timedelta(days=1)
        changed = datetime.fromisoformat(str(snapshot.get("last_changed") or "").replace("Z", "+00:00"))
        if changed.tzinfo is None:
            changed = changed.replace(tzinfo=now.tzinfo)
        if changed.astimezone(now.tzinfo) < boundary:
            return False
        cycle = (snapshot.get("attributes") or {}).get("cycle_started_at")
        if cycle:
            cycle_at = (datetime.fromtimestamp(float(cycle), now.tzinfo)
                        if isinstance(cycle, (int, float))
                        else datetime.fromisoformat(str(cycle).replace("Z", "+00:00")))
            if cycle_at.tzinfo is None:
                cycle_at = cycle_at.replace(tzinfo=now.tzinfo)
            if cycle_at.astimezone(now.tzinfo) < boundary:
                return False
    except (TypeError, ValueError):
        return False
    return True


def phone_notice_data(who, data, tok, durable=False):
    """Prevent Android from bridging a pre-gate phone notice to Wear OS.

    Actionable notices with a reliable clear path become ongoing on the phone;
    Android Wear excludes those from bridging. Ephemeral notices without a
    clear path remain dashboard-only until the daily gate opens.
    """
    payload = dict(data or {})
    if adult_buzz_allowed(who, tok):
        payload.pop("persistent", None)
        return payload
    if durable and payload.get("tag"):
        payload["persistent"] = True
        ilog({"src": "notify", "action": "phone_notice_ongoing_no_wear", "who": who,
              "tag": payload.get("tag")})
        return payload
    ilog({"src": "notify", "action": "phone_notice_dashboard_only", "who": who,
          "tag": payload.get("tag")})
    return None


def notify_hub_placement_review():
    if not HUB_PLACEMENT.get("pending") or not HUB_PLACEMENT.get("nonce"):
        return
    who = str(PEER_PIN_CFG.get("adult") or LEAD_ADULT or "")
    if not who:
        return
    tok, nonce = ha_token(), HUB_PLACEMENT["nonce"]
    title = "Chores: check both kid Nest Hubs"
    message = "A kid Nest Hub stopped checking in. Verify both Hubs are back in their proper rooms before restoring kid-to-kid PIN resets."
    if not HUB_PLACEMENT.get("phone_notified"):
        accepted = []
        for svc in ADULT_NOTIFY.get(who, []):
            data = phone_notice_data(who, {"tag": "chore-hub-placement", "priority": "high", "ttl": 0,
                                        "clickAction": "/lovelace/chore-inspection",
                                        "actions": [{"action": "CHORE_HUBS_OK_" + nonce,
                                                     "title": "I checked both are in place"}]}, tok, durable=True)
            if data is not None and ha_call(svc, {"title": title, "message": message, "data": data}, tok):
                accepted.append(svc)
        if accepted:
            HUB_PLACEMENT["phone_notified"] = True
            save_hub_placement()
    if not HUB_PLACEMENT.get("watch_notified"):
        accepted = [svc for svc in ADULT_BUZZ_NOTIFY.get(who, []) if adult_buzz_allowed(who, tok) and ha_call(
            svc, {"title": title, "message": message + " Open the parent Chores dashboard after you physically check them.",
                  "data": {"tag": "chore-hub-placement-watch", "priority": "high", "ttl": 0,
                           "channel": "Chore approvals", "importance": "high"}}, tok)]
        if accepted:
            HUB_PLACEMENT["watch_notified"] = True
            save_hub_placement()


def attest_hub_placement(nonce, by):
    if not HUB_PLACEMENT.get("pending") or not hmac.compare_digest(str(nonce or ""), str(HUB_PLACEMENT.get("nonce") or "")):
        return False, "That placement check is no longer waiting."
    missing = stale_peer_hubs()
    if missing:
        return False, "Both Nest Hubs must be online in their proper rooms before you confirm."
    HUB_PLACEMENT.update({"trusted": True, "pending": False, "attested_at": time.time(), "attested_by": by,
                          "reason": None, "missing": [], "phone_notified": False, "watch_notified": False})
    save_hub_placement()
    tok = ha_token()
    for svc in ADULT_NOTIFY.get(by, []) + ADULT_BUZZ_NOTIFY.get(by, []):
        ha_call(svc, {"message": "clear_notification", "data": {"tag": "chore-hub-placement"}}, tok)
        ha_call(svc, {"message": "clear_notification", "data": {"tag": "chore-hub-placement-watch"}}, tok)
    ilog({"src": "peer_pin_reset", "action": "hub_placement_attested", "by": by,
          "displays": list(PEER_PIN_CFG.get("displays") or [])})
    after_change()
    return True, None


def watch_hub_placement():
    """Any real heartbeat outage revokes the physical-separation assumption until an adult checks both rooms."""
    while True:
        try:
            if HUB_PLACEMENT.get("trusted") and time.time() - SERVER_STARTED_AT >= 30:
                missing = stale_peer_hubs()
                if missing:
                    invalidate_hub_placement("One or more kid Nest Hubs stopped checking in", missing)
            if HUB_PLACEMENT.get("pending"):
                notify_hub_placement_review()
        except Exception as ex:  # noqa: BLE001
            print("hub placement watcher error", ex, flush=True)
        time.sleep(2)


def parent_available(tok):
    """The lead grown-up can take a ping now: home, and not in quiet hours. Held pings go out when this turns true."""
    return adult_home(LEAD_ADULT, tok) and not parent_quiet()


WORDNUM = {"zero": 0, "none": 0, "one": 1, "won": 1, "two": 2, "to": 2, "too": 2, "three": 3, "four": 4, "for": 4,
           "five": 5, "six": 6, "seven": 7, "eight": 8, "ate": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
           "fifteen": 15, "twenty": 20}


def parse_points(text):
    """'3', 'three', 'three points', 'give her 4' -> (n, True); 'no' / 'not yet' -> (None, False); else (None, True)."""
    import re
    t = text.lower().strip()
    if not t:
        return None, True
    if re.search(r"\b(no|nope|not yet|redo|again)\b", t):
        return None, False
    m = re.search(r"\d+", t)
    if m:
        return max(0, min(50, int(m.group()))), True
    for w in re.findall(r"[a-z]+", t):
        if w in WORDNUM:
            return WORDNUM[w], True
    return None, True


def notify_report(ev, st, force=False):
    """Ready for inspection -> the lead grown-up's watch + phone, with Pass / Not yet buttons when the points are known.
    Only while they're home and outside quiet hours; otherwise held and sent when they're back (watch_ha_actions)."""
    try:
        tok = ha_token()
    except OSError:
        return
    if not force and not parent_available(tok):
        GUARD.setdefault("held_reports", set()).add(ev["id"])
        ilog({"src": "notify", "action": "held_until_home", "ref": ev["id"]})
        return
    c = cfg()
    kn = {k["id"]: k["name"] for k in c["kids"]}
    rep = next((r for r in st["history"] if r.get("id") == ev["id"]), ev)
    team = rep.get("team") or [ev["kid"]]
    who = " + ".join(kn.get(k, k) for k in team)
    name = rep.get("chore_name") or ev["chore"]
    pts = ev.get("points")
    took = ""
    if ev.get("started"):
        sec = int(ev["ts"] - ev["started"])
        took = " · took %d:%02d" % (sec // 60, sec % 60)
    msg = f"{who}: {name} is done" + (f" ({pts}⭐)" if pts is not None else " (you set the points)") + took + \
        (f" · round {ev['round']}" if ev.get("round") else "")
    reply = {"action": "CHORE_PTS_" + ev["id"], "title": "✍️ Points…", "behavior": "textInput"}
    if ev.get("ask") is not None and ev["chore"] != "toy_out":
        ask = ev["ask"]
        msg = f"{who}: {name} — asks {ask}⭐" + took
        actions = [{"action": "CHORE_INSP_" + ev["id"], "title": "🔍 Start inspection"},
                   {"action": "CHORE_PTS_" + ev["id"], "title": "✍️ Points…", "behavior": "textInput"},
                   {"action": "CHORE_FIX_" + ev["id"], "title": "✍️ Not yet: fix…", "behavior": "textInput"}]
    elif ev["chore"] == "toy_out":
        ask = ev.get("ask") or 1
        msg = f"{who} wants to give away {ev.get('note') or 'a toy'} — asks {ask}⭐"
        actions = [{"action": "CHORE_ADD%d_" % min(2, max(1, ask)) + ev["id"], "title": f"Give {min(2, max(1, ask))}⭐"},
                   {"action": "CHORE_PTS_" + ev["id"], "title": "✍️ Points…", "behavior": "textInput"},
                   {"action": "CHORE_NOTYET_" + ev["id"], "title": "Not yet"}]
    elif ev["chore"] == "toy_swap":
        actions = [{"action": "CHORE_SWAPOK_" + ev["id"], "title": "✍️ Approve (old toy?)", "behavior": "textInput"},
                   {"action": "CHORE_NOTYET_" + ev["id"], "title": "Not yet"}]
        msg = f"{who} is ready to swap an old toy" + (f" ({rep.get('old_name')})" if rep.get("old_name") else "") + " for a present"
    elif pts is not None and not ev.get("partial"):
        actions = [{"action": "CHORE_INSP_" + ev["id"], "title": "🔍 Start inspection"},
                   {"action": "CHORE_PASS_" + ev["id"], "title": f"Pass +{pts}"},
                   {"action": "CHORE_FIX_" + ev["id"], "title": "✍️ Not yet: fix…", "behavior": "textInput"}]
    else:
        if ev["chore"] == "other":   # kids spam "something else" -> one tap settles it at 0
            actions = [{"action": "CHORE_ZERO_" + ev["id"], "title": "0⭐"},
                       {"action": "CHORE_ADD1_" + ev["id"], "title": "+1"}, reply]
        else:
            actions = [{"action": "CHORE_ADD1_" + ev["id"], "title": "+1"},
                       {"action": "CHORE_ADD2_" + ev["id"], "title": "+2"}, reply]
    for svc in PARENT_NOTIFY:
        data = phone_notice_data(LEAD_ADULT, {"tag": "chore-insp-" + ev["id"], "group": "chores",
                                 "priority": "high", "ttl": 0, "channel": "Chores", "importance": "high",
                                 "clickAction": "/lovelace/chores", "actions": actions}, tok, durable=True)
        if data is not None:
            ha_call(svc, {"title": "Chores: ready to inspect", "message": msg, "data": data}, tok)
    ilog({"src": "notify", "action": "sent", "ref": ev["id"], "to": PARENT_NOTIFY})


def notify_started(kid, chore_id, team=None, what="started"):
    """Heads-up when a kid starts / teams up on / pauses a chore. One per kid, replaced as it changes."""
    try:
        tok = ha_token()
    except OSError:
        return
    if not parent_available(tok):
        return
    c = cfg(); st = derive()
    kn = {k["id"]: k["name"] for k in c["kids"]}
    ch = next((x for x in c["chores"] if x["id"] == chore_id), {"name": chore_id})
    T = (st["typical"].get(chore_id) or {}).get("minutes") or ch.get("est_minutes")
    who = " + ".join(kn.get(k, k) for k in (team or [kid]))
    price = (st["board"].get(chore_id) or {}).get("price")
    msg = {"started": f"{who} started {ch['name']}", "team": f"{who} are doing {ch['name']} together",
           "paused": f"{who} will finish {ch['name']} later"}[what] + \
        (f" · worth {price}⭐" if price is not None else "") + (f" · usually ~{round(T)} min" if T and what != "paused" else "")
    if not parent_available(tok):
        return
    for svc in PARENT_NOTIFY:
        data = phone_notice_data(LEAD_ADULT, {"tag": "chore-doing-" + kid, "group": "chores",
                                 "clickAction": "/lovelace/chores"}, tok)
        if data is not None:
            ha_call(svc, {"title": "Chores", "message": msg, "data": data}, tok)


def notify_inspecting(r, by):
    try:
        tok = ha_token()
    except OSError:
        return
    kn = {k["id"]: k["name"] for k in cfg()["kids"]}
    who = " + ".join(kn.get(k, k) for k in (r.get("team") or [r["kid"]]))
    pts = r.get("points")
    acts = ([{"action": "CHORE_PASS_" + r["id"], "title": f"Pass +{pts}"}] if pts is not None else
            [{"action": "CHORE_ZERO_" + r["id"], "title": "0⭐"}] if r.get("chore") == "other" else
            [{"action": "CHORE_ADD1_" + r["id"], "title": "+1"}]) + \
        [{"action": "CHORE_FIX_" + r["id"], "title": "✍️ Not yet: fix…", "behavior": "textInput"},
         {"action": "CHORE_PTS_" + r["id"], "title": "✍️ Points…", "behavior": "textInput"}]
    for svc in PARENT_NOTIFY:
        data = phone_notice_data(LEAD_ADULT, {"tag": "chore-insp-" + r["id"], "group": "chores",
                                 "priority": "high", "ttl": 0, "chronometer": True,
                                 "when": int(time.time()), "actions": acts}, tok, durable=True)
        if data is not None:
            ha_call(svc, {"title": "Chores: inspecting", "message": f"🔍 Inspecting {who}: {r.get('chore_name')}",
                          "data": data}, tok)


def clear_report_notice(ref):
    try:
        tok = ha_token()
    except OSError:
        return
    for svc in PARENT_NOTIFY:
        ha_call(svc, {"message": "clear_notification", "data": {"tag": "chore-insp-" + ref}}, tok)


def clear_reward_request_notice(ref):
    try:
        tok = ha_token()
    except OSError:
        return
    for svc in PARENT_NOTIFY:
        ha_call(svc, {"message": "clear_notification", "data": {"tag": "chore-req-" + ref}}, tok)


def clear_adult_request(nonce, who):
    try:
        tok = ha_token()
    except OSError:
        return
    for svc in ADULT_NOTIFY.get(who, []):
        ha_call(svc, {"message": "clear_notification", "data": {"tag": "chore-adult-" + nonce}}, tok)
    for svc in ADULT_BUZZ_NOTIFY.get(who, []):
        ha_call(svc, {"message": "clear_notification", "data": {"tag": "chore-adult-" + nonce + "-buzz"}}, tok)


def expire_adult_requests(now=None):
    """Close timed-out actionable notices even if the child walked away from the waiting screen."""
    now = time.time() if now is None else float(now)
    expired = []
    for nonce, row in ADULT_REQ.items():
        ttl = ADULT_REQUEST_TTL_S if row.get("purpose") in ("assist", "reward", "pin_reset") else 120
        if row.get("status") == "pending" and now - float(row.get("ts") or 0) > ttl:
            row["status"] = "timeout"
            expired.append((nonce, row.get("who")))
    if expired:
        save_adult_requests()
        after_change()
        for nonce, who in expired:
            threading.Thread(target=clear_adult_request, args=(nonce, who), daemon=True).start()
    return [nonce for nonce, who in expired]


def resolve_assist_request(nonce, ok, by):
    """Resolve one adult-required invitation exactly once."""
    r = ADULT_REQ.get(nonce)
    if not r or r.get("purpose") not in ("assist", "reward", "pin_reset") or r.get("status") != "pending":
        return None
    r["status"] = "ok" if ok else "no"
    if ok and r.get("purpose") == "pin_reset":
        r["until"] = time.time() + ADULT_REQUEST_TTL_S
    elif ok and r.get("purpose") == "assist":
        append({"type": "assist_ok", "kid": r["kid"], "chore": r["chore"], "by": by})
    elif not ok and r.get("purpose") != "pin_reset":
        r["until"] = time.time() + 3600
        append({"type": "adult_busy", "kid": r["kid"], "chore": r.get("chore"), "reward": r.get("reward"),
                "by": by, "until": r["until"]})
    save_adult_requests()
    after_change()
    threading.Thread(target=clear_adult_request, args=(nonce, r["who"]), daemon=True).start()
    ilog({"src": "adult", "action": r["status"], "purpose": r["purpose"], "kid": r["kid"],
          "chore": r.get("chore"), "reward": r.get("reward"), "by": by})
    return r


def resolve_phone_adult_request(nonce, ok):
    """Apply a phone/watch notification answer with the same rules as the dashboard."""
    r = ADULT_REQ.get(nonce)
    if not r or r.get("status") != "pending":
        return None
    if r.get("purpose") in ("assist", "reward", "pin_reset"):
        return resolve_assist_request(nonce, ok, r["who"])
    r["status"] = "ok" if ok else "no"
    save_adult_requests()
    ilog({"src": "adult", "action": r["status"], "who": r["who"], "ip": r["ip"]})
    print("grown-up request", nonce, r["status"], flush=True)
    return r


def watch_ha_events():
    """Notification buttons and brief doorway edges used by the vacuum."""
    import asyncio
    from entry_edges import entry_edges
    entry_doors = set((HOUSE.get("vacuum") or {}).get("entry_doors") or [])
    try:
        import websockets
    except ImportError:
        print("websockets not installed: grown-up confirmation disabled", flush=True)
        return
    tok = ha_token()
    async def run():
        while True:
            try:
                async with websockets.connect(HA_URL.replace("http", "ws") + "/api/websocket", max_size=None) as ws:
                    await ws.recv()
                    await ws.send(json.dumps({"type": "auth", "access_token": tok}))
                    await ws.recv()
                    # REST-created sensors disappear across an HA restart. A
                    # fresh event socket proves HA is back, so republish one
                    # complete revision even when the chore data did not change.
                    GUARD.pop("state_sig", None)
                    GUARD.pop("demo_sig", None)
                    threading.Thread(target=push_state_sensor, daemon=True).start()
                    await ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "mobile_app_notification_action"}))
                    if entry_doors:
                        await ws.send(json.dumps({"id": 2, "type": "subscribe_events", "event_type": "state_changed"}))
                    print("HA event socket: subscribed to notification actions", flush=True)
                    while True:
                        m = json.loads(await ws.recv())
                        if m.get("type") == "result":
                            if m.get("id") == 2:
                                entry_edges.online = bool(m.get("success"))
                            print("HA event socket: subscribe result", m.get("success"), flush=True)
                            continue
                        data_ = ((m.get("event") or {}).get("data") or {})
                        if m.get("id") == 2:
                            entry_edges.record(data_, entry_doors)
                            continue
                        act = data_.get("action", "")
                        ilog({"src": "watch", "action": "raw_event", "act": act, "device": data_.get("device_id") or data_.get("webhook_id")})
                        if act.startswith("CHORE_TV"):
                            kind, tv = act.split("_", 2)[1], act.split("_", 2)[2]
                            a_ = {"TVPLUS": "plus", "TVFIVE": "five", "TVLOCK": "lock"}.get(kind)
                            if a_:
                                req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/tv/adjust", data=json.dumps({"by": LEAD_ADULT, "tv": tv, "action": a_}).encode(),
                                                             headers={"Content-Type": "application/json"}, method="POST")
                                try:
                                    urllib.request.urlopen(req, timeout=10).read()
                                except Exception as ex:  # noqa: BLE001
                                    print("watch tv adjust failed", ex, flush=True)
                                ilog({"src": "watch", "action": "tv_" + a_, "tv": tv})
                        if act.startswith("CHORE_REQOK_") or act.startswith("CHORE_REQNO_"):
                            ref = act[len("CHORE_REQOK_"):]
                            req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/request_answer", data=json.dumps(
                                {"ref": ref, "by": LEAD_ADULT, "ok": act.startswith("CHORE_REQOK_")}).encode(),
                                headers={"Content-Type": "application/json"}, method="POST")
                            try:
                                urllib.request.urlopen(req, timeout=10).read()
                            except Exception as ex:  # noqa: BLE001
                                print("watch request answer failed", ex, flush=True)
                            ilog({"src": "watch", "action": "request_answer", "ref": ref, "ok": act.startswith("CHORE_REQOK_")})
                        if act.startswith("CHORE_SWAPOK_"):
                            ref = act[len("CHORE_SWAPOK_"):]
                            req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/inspect",
                                                         data=json.dumps({"ref": ref, "by": LEAD_ADULT, "ok": True, "points": 0,
                                                                          "label": (data_.get("reply_text") or "").strip()[:60]}).encode(),
                                                         headers={"Content-Type": "application/json"}, method="POST")
                            try:
                                urllib.request.urlopen(req, timeout=10).read()
                            except Exception as ex:  # noqa: BLE001
                                print("watch swap approve failed", ex, flush=True)
                            ilog({"src": "watch", "action": "swap_ok", "ref": ref, "reply": data_.get("reply_text")})
                        if act.startswith("CHORE_INSP_"):
                            ref = act[len("CHORE_INSP_"):]
                            req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/inspect_start", data=json.dumps({"ref": ref, "by": LEAD_ADULT}).encode(),
                                                         headers={"Content-Type": "application/json"}, method="POST")
                            try:
                                urllib.request.urlopen(req, timeout=10).read()
                            except Exception as ex:  # noqa: BLE001
                                print("watch inspect_start failed", ex, flush=True)
                            ilog({"src": "watch", "action": "inspect_start", "ref": ref})
                        if act.startswith("CHORE_FIX_"):
                            ref = act[len("CHORE_FIX_"):]
                            req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/inspect", data=json.dumps(
                                {"ref": ref, "by": LEAD_ADULT, "ok": False, "feedback": (data_.get("reply_text") or "").strip()}).encode(),
                                headers={"Content-Type": "application/json"}, method="POST")
                            try:
                                urllib.request.urlopen(req, timeout=10).read()
                            except Exception as ex:  # noqa: BLE001
                                print("watch fix failed", ex, flush=True)
                            ilog({"src": "watch", "action": "not_yet_fix", "ref": ref, "reply": data_.get("reply_text")})
                        if act.startswith("CHORE_") and act.split("_")[1] in ("PASS", "NOTYET", "ADD1", "ADD2", "PTS", "ZERO"):
                            kind, ref = act.split("_", 2)[1], act.split("_", 2)[2]
                            rep = next((r for r in derive()["history"] if r.get("id") == ref), None)
                            ok, pts_ = True, None
                            if kind == "NOTYET":
                                ok = False
                            elif kind == "PASS":
                                pts_ = rep.get("points") if rep else None
                            elif kind in ("ADD1", "ADD2", "ZERO"):
                                pts_ = {"ADD1": 1, "ADD2": 2, "ZERO": 0}[kind]
                            else:   # spoken / typed reply from phone or watch
                                pts_, ok = parse_points(data_.get("reply_text") or "")
                                if pts_ is None and ok:
                                    ilog({"src": "watch", "action": "reply_unparsed", "ref": ref, "text": data_.get("reply_text")})
                                    rep = None
                            if rep and rep.get("status") == "pending":
                                body = {"ref": ref, "by": LEAD_ADULT, "ok": ok}
                                if ok:
                                    body["points"] = pts_
                                req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/inspect", data=json.dumps(body).encode(),
                                                             headers={"Content-Type": "application/json", "X-From": "watch"}, method="POST")
                                try:
                                    urllib.request.urlopen(req, timeout=10).read()
                                except Exception as ex:  # noqa: BLE001
                                    print("watch inspect failed", ex, flush=True)
                                ilog({"src": "watch", "action": ("pass %s" % pts_) if ok else "not_yet", "ref": ref,
                                      "reply": data_.get("reply_text")})
                        if act.startswith("CHORE_HUBS_OK_"):
                            nonce = act[len("CHORE_HUBS_OK_"):]
                            req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/hub_placement_attest",
                                                         data=json.dumps({"nonce": nonce, "by": LEAD_ADULT}).encode(),
                                                         headers={"Content-Type": "application/json", "X-From": "watch"}, method="POST")
                            try:
                                urllib.request.urlopen(req, timeout=10).read()
                            except Exception as ex:  # noqa: BLE001
                                print("watch hub placement attestation failed", ex, flush=True)
                            ilog({"src": "watch", "action": "hub_placement_attest", "nonce": nonce})
                        if act.startswith("CHORE_OK_") or act.startswith("CHORE_NO_"):
                            nonce = act[9:]
                            resolve_phone_adult_request(nonce, act.startswith("CHORE_OK_"))
            except Exception as ex:  # noqa: BLE001
                entry_edges.online = False
                print("HA event socket error", ex, flush=True)
                await asyncio.sleep(5)
    asyncio.run(run())


def after_change():
    threading.Thread(target=lambda: (push_ha(derive()), push_state_sensor()), daemon=True).start()


def verified_ha_adult(headers, claimed):
    """Bind sensitive parent controls to the HA account that created the relay context."""
    user = str(headers.get("X-HA-User") or "")
    allowed = (HA.get("user_adults") or {}).get(user)
    if isinstance(allowed, str):
        allowed = [allowed]
    return bool(user and claimed and claimed in (allowed or []))


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        print("%s %s" % (self.address_string(), fmt % a), flush=True)

    def display_client_ip(self):
        """A known physical display behind the dedicated trusted proxy, or the actual TCP peer."""
        if not hasattr(self, "_display_client_ip"):
            self._display_client_ip = trusted_display_client_ip(self.client_address[0], self.headers)
        return self._display_client_ip

    def send(self, code, body, ctype="application/json"):
        if code >= 400 and self.command == "POST":
            ilog({"src": "api", "ip": self.client_address[0], "path": self.path, "rejected": code,
                  "body": body if isinstance(body, dict) else None})
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        display_ip_ = self.display_client_ip()
        if p in ("/tv", "/api/state"):
            GUARD["seen"][display_ip_] = (time.time(), self.headers.get("Referer") or "")
        if p in ("/", "/kids"):
            return self.send(200, (HERE / "static/kids.html").read_bytes(), "text/html; charset=utf-8")
        if p == "/tv":
            return self.send(200, (HERE / "static/tvlock.html").read_bytes(), "text/html; charset=utf-8")
        if p == "/us-map.json":
            return self.send(200, (HERE / "static/us48.json").read_bytes(), "application/json")
        if p == "/music-block":   # silent Cast/Nest explanation when required chores pause Spotify
            return self.send(200, (HERE / "static/music-block.html").read_bytes(), "text/html; charset=utf-8")
        if p == "/now":   # grown-up "right now" board (e.g. a kitchen Hub Max)
            return self.send(200, (HERE / "static/now.html").read_bytes(), "text/html; charset=utf-8")
        if p == "/inspect":
            return self.send(200, (HERE / "static/inspect.html").read_bytes(), "text/html; charset=utf-8")
        if p == "/packing":
            return self.send(200, (HERE / "static/packing.html").read_bytes(), "text/html; charset=utf-8")
        if p == "/api/envelope_status":   # the kids' screen follows its print job (and sees a jam)
            job = (urllib.parse.parse_qs(self.path.partition("?")[2]).get("job") or [""])[0]
            try:
                return self.send(200, envelopes.status(job))
            except Exception as ex:  # noqa: BLE001
                return self.send(200, {"state": "unknown", "problem": None, "error": str(ex)[:120]})
        if p == "/api/kid_preview":
            # X-HA-User is trusted only from the loopback HA relay, which derives it from the authenticated
            # HA request context. Never accept a browser-supplied identity over the LAN.
            if self.client_address[0] not in ("127.0.0.1", "::1") \
                    or not verified_ha_adult(self.headers, LEAD_ADULT):
                return self.send(403, {"error": "lead-adult-only Home Assistant preview"})
            q_ = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            kid_ = str((q_.get("kid") or [""])[0])
            state_ = kid_preview_state(kid_)
            if state_ is None:
                return self.send(404, {"error": "unknown kid"})
            return self.send(200, {"ok": True, "kid": kid_, "read_only": True, "state": state_})
        if p.startswith("/api/chore_photo/"):   # a photo that shows the kids where a job is (chores[].photo, in data/photos)
            ch_ = next((x for x in cfg()["chores"] if x["id"] == p.rsplit("/", 1)[1]), None)
            f = (DATA / "photos" / str((ch_ or {}).get("photo") or "-"))
            if not ch_ or not ch_.get("photo") or not f.is_file() or f.resolve().parent != (DATA / "photos").resolve():
                return self.send(404, {"error": "no photo"})
            return self.send(200, f.read_bytes(), "image/jpeg")
        if p == "/api/envelope_photo":   # how to put an envelope in the printer (a photo in the data folder)
            f = DATA / str(envelopes.CFG.get("load_photo") or "-")
            if not envelopes.CFG.get("load_photo") or not f.is_file() or f.resolve().parent != DATA.resolve():
                return self.send(404, {"error": "no photo"})
            return self.send(200, f.read_bytes(), "image/jpeg")
        if p == "/api/state":
            st = derive()
            hub_ = next((h for h, x in (HOUSE.get("hubs") or {}).items() if x.get("ip") == display_ip_), None)
            st["hub"], st["hub_motion"] = hub_, (GUARD.get("hub_motion") or {}).get(hub_)
            display_ = display_for_ip(display_ip_)
            coin_cfg_ = HOUSE.get("coin_flip") or {}
            coin_location_ = ha_get(coin_cfg_.get("location_entity"), ha_token()) \
                if coin_cfg_.get("location_entity") and display_ and st.get("present_kids") else None
            st["coin_flip"] = coin_flip_state(st["config"], st.get("present_kids"), display_, coin_location_,
                                                 now=st.get("now"), settings=coin_cfg_)
            st["peer_pin_reset"] = peer_pin_reset_public(display_ip_)
            st["peer_pin_reset_capable"] = bool(peer_reset_display(display_ip_))
            interrupt_ = active_spotify_interruption((display_ or {}).get("room"))
            st["display"], st["music_interruption"] = display_, interrupt_
            add_seen_projection(st, display_ip_, "ann:" + display_ip_)
            for r_ in ADULT_REQ.values():   # the grown-up said "It's me": this Hub opens their screen even if its page reloaded meanwhile
                if r_["ip"] == display_ip_ and r_["status"] == "ok" and not r_.get("used") and time.time() - r_["ts"] < 180 \
                        and r_.get("purpose", "login") == "login":
                    r_["used"] = True
                    st["adult_go"] = r_["who"]
            return self.send(200, st)
        if p == "/api/failover/status":
            return self.send(200, AUTHORITY.public_status())
        if p == "/api/house_jobs":   # the grown-ups' house jobs (Grocy), ranked for one screen or overall
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            disp = (q.get("display") or [(HOUSE.get("house_tasks") or {}).get("board_display", "")])[0]
            shown = GUARD.get("house_tasks_displays", {}).get(disp) if disp and disp != "all" else None
            meta = GUARD.get("house_tasks_meta", {})
            return self.send(200, {"items": shown if shown is not None else (GUARD.get("house_tasks") or [])[:20],
                                   "private": meta.get("private", []), "to_review": meta.get("to_review", 0),
                                   "on": "house_tasks" in GUARD})
        if p == "/api/room_eye":   # cameras learning tidy rooms: label queue (no pictures here), recent checks, counts
            import room_eye
            e_ = room_eye.EYE
            if not e_:
                return self.send(200, {"on": False})
            return self.send(200, {"on": True, "queue": e_.open_queue(), "checks": e_.S["checks"][-20:],
                                   "pending": e_.S["pending"], "samples": {c_: {lab: sum(1 for x in e_.S["samples"]
                                                                                  if x["camera"] == c_ and x["label"] == lab)
                                                                           for lab in ("tidy", "untidy")} for c_ in e_.cfg["cameras"]}})
        if p == "/health":
            checkpoint_ = core_state_checkpoint()
            return self.send(200 if checkpoint_["ready"] else 503,
                             {"ok": checkpoint_["ready"], "failover": AUTHORITY.public_status(),
                              "checkpoint": checkpoint_})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if self.headers.get("X-HA-User") and self.client_address[0] != "127.0.0.1":
            return self.send(403, {"error": "forbidden"})
        display_ip_ = self.display_client_ip()
        n = int(self.headers.get("Content-Length") or 0)
        try:
            b = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.send(400, {"error": "bad json"})
        if self.path == "/api/failover/probe":
            if not AUTHORITY.check_probe_token(self.headers.get("X-Chore-Failover-Probe")):
                return self.send(403, {"error": "forbidden"})
            try:
                request_ = AUTHORITY.validate_probe(b)
                if not any(k.get("id") == "failover_test" for k in cfg()["kids"]):
                    raise ValueError("synthetic failover_test profile is absent")
                before_ = derive()["balance"].get("failover_test", 0)
                event_, created_ = append_once({"type": "adjust", "kid": "failover_test",
                                                "points": request_["points"],
                                                "reason": "isolated failover replay probe",
                                                "by": "failover_probe"},
                                               request_["transaction_id"],
                                               {"_probe_before_balance": before_})
                after_ = derive()["balance"].get("failover_test", 0)
                receipt = {"transaction_id": request_["transaction_id"],
                           "event_id": event_["id"], "created": created_,
                           "before_balance": event_.get("_probe_before_balance", 0),
                           "balance": after_, "node_id": event_.get("_fence_node"),
                           "epoch": event_.get("_fence_epoch")}
            except FencedError as ex:
                return self.send(503, {"error": "Chore Time is temporarily read-only during failover.",
                                       "failover": ex.status})
            except KeyError as ex:
                return self.send(409, {"error": str(ex.args[0])})
            except ValueError as ex:
                return self.send(400, {"error": str(ex)})
            return self.send(200, {"ok": True, "receipt": receipt,
                                   "effects_suppressed": AUTHORITY.rehearsal})
        if AUTHORITY.enabled and AUTHORITY.rehearsal:
            return self.send(503, {"error": "Failover rehearsal accepts only the synthetic probe profile.",
                                   "failover": AUTHORITY.status()})
        try:
            AUTHORITY.require_mutation()
        except FencedError as ex:
            return self.send(503, {"error": "Chore Time is temporarily read-only during failover.",
                                   "failover": ex.status})
        if self.path == "/api/hub_heartbeat":
            display_ = display_for_ip(display_ip_)
            if not display_ or display_.get("kind") != "hub" or display_.get("id") not in HUBS:
                return self.send(403, {"error": "configured physical Hubs only"})
            GUARD.setdefault("hub_heartbeat", {})[display_["id"]] = time.time()
            foreground_ = observe_hub_child(display_ip_, b.get("kid"), b.get("kid_session"))
            if foreground_ and foreground_["publish"]:
                queue_hub_child_state_push()
            return self.send(200, {"ok": True, "hub": display_["id"],
                                   "active_kid": foreground_["kid"] if foreground_ else None})
        if self.path == "/api/inspection_seen_snapshot":
            saved_ = save_inspection_seen_snapshot(display_ip_, b.get("kids"))
            if not saved_:
                return self.send(403, {"error": "configured physical Hub snapshots only"})
            ilog({"src": "inspection_seen_snapshot", "display": saved_[0], "kids": saved_[1]})
            return self.send(200, {"ok": True, "display": saved_[0]})
        if self.path == "/api/vacuum/button":
            try:
                expected = VACUUM_BUTTON_TOKEN_FILE.read_text().strip()
            except OSError:
                return self.send(503, {"ok": False, "rooms": [], "why": "vacuum button authentication is not configured"})
            supplied_token = self.headers.get("X-Vacuum-Button") or ""
            if not expected or not hmac.compare_digest(supplied_token, expected):
                return self.send(403, {"ok": False, "rooms": [], "why": "forbidden"})
            floor = str(b.get("floor") or "")
            planner = globals().get("VACUUM_PLANNERS", {}).get(floor)
            if floor not in ("upstairs", "downstairs"):
                return self.send(400, {"ok": False, "rooms": [], "why": "unknown floor"})
            if not planner:
                return self.send(503, {"ok": False, "rooms": [], "why": "vacuum planner is starting"})
            with planner.lock:
                result = planner.button_request(b)
                if result.get("ok") and result.get("action") in ("map_vacant_on", "queued_when_vacant"):
                    request_id = str(b["request_id"]).strip()[:160]
                    receipt = planner.D["manual_seen"][request_id]
                    rooms = (set(receipt["map_rooms"]) if "map_rooms" in receipt
                             else planner.confirmed_map_rooms(b, result["rooms"]))
                    try:
                        recorded = record_vacuum_ready(floor, request_id, rooms, receipt["ts"])
                    except OSError:
                        return self.send(503, {"ok": False, "why": "vacancy queued; room confirmation needs retry"})
                    if not rooms and b.get("gesture") == "map_vacant_on":
                        result = dict(result, why=result["why"] + "; refresh the floorplan to confirm the tidy room")
                    if recorded:
                        threading.Thread(target=push_state_sensor, daemon=True).start()
            ilog({"src": "vacuum_button", "ip": display_ip_, "floor": floor,
                  "gesture": b.get("gesture"), "request_id": str(b.get("request_id") or "")[:160],
                  "result": result})
            return self.send(200 if result.get("ok") else 400, result)
        if self.path == "/api/room_eye/reference":   # trusted local handoff: import an existing full-room reference
            import base64, room_eye
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                return self.send(403, {"error": "loopback only"})
            if not room_eye.EYE:
                return self.send(503, {"error": "room eye is off"})
            encoded = str(b.get("jpeg") or "")
            if len(encoded) > 4 * 1024 * 1024:
                return self.send(413, {"error": "image too large"})
            try:
                jpeg = base64.b64decode(encoded, validate=True)
                sid, added = room_eye.EYE.import_reference(str(b.get("camera") or ""), jpeg,
                                                            str(b.get("label") or "tidy"),
                                                            str(b.get("why") or "manual reference")[:120])
            except (ValueError, OSError) as ex:
                return self.send(400, {"error": str(ex)[:120]})
            return self.send(200, {"ok": True, "sample_id": sid, "added": added})
        c = cfg()
        kids = {k["id"] for k in c["kids"]}
        chores = {x["id"]: x for x in c["chores"]}
        rewards = {x["id"]: x for x in c["rewards"]}
        insp = {x["id"] for x in c["inspectors"]}
        p = self.path
        st = derive()
        allowed_kids = (HA.get("user_kids") or {}).get(self.headers.get("X-HA-User") or "")
        if allowed_kids is not None and p != "/api/log":   # e.g. a parent's HA login may act only for their own child
            acting = [b.get("kid")] + list(b.get("with") or []) + list(b.get("team") or [])
            if any(k and k not in allowed_kids for k in acting):
                return self.send(403, {"error": "not your kid"})
        if p == "/api/packing_update":
            packer_ = b.get("packer") or b.get("kid")
            game_ = packing_config() or {}
            profile_ = packing_profile(game_, packer_)
            if not game_.get("activated") or not profile_:
                return self.send(409, {"error": "Packing is not open for this packer."})
            is_child_ = packer_ in (profile_.get("kids") or [])
            ha_child_ = is_child_ and allowed_kids is not None and packer_ in allowed_kids
            adult_child_ = is_child_ and verified_ha_adult(self.headers, b.get("by"))
            adult_ = not is_child_ and verified_ha_adult(self.headers, b.get("by")) and b.get("by") == packer_
            if not adult_ and not adult_child_ and not ha_child_ \
                    and not valid_kid_session(b.get("kid_session"), packer_, display_ip_):
                return self.send(403, {"error": "Open your profile with your PIN first." if is_child_
                                                   else "Use the signed-in grown-up Chore dashboard."})
            with PACKING_LOCK:
                saved_ = load_private_json(PACKING_PROGRESS_FILE, {})
                if saved_.get("version") != game_.get("version"):
                    saved_ = {"version": game_.get("version"), "kids": {}}
                old_ = (saved_.get("kids") or {}).get(packer_) or {}
                raw_ = b.get("progress") or {}
                mark_ = b.get("mark")
                stage_ = b.get("stage")
                item_action_ = b.get("item_action")
                if sum(x is not None for x in (mark_, stage_, item_action_)) > 1:
                    return self.send(400, {"error": "Choose one packing action at a time."})
                if item_action_ is not None:
                    if not adult_ or not isinstance(item_action_, dict):
                        return self.send(403, {"error": "Only the signed-in packer can change this item."})
                    item_ = str(item_action_.get("item") or "")
                    item_ids_ = packing_item_ids(profile_, packer_)
                    if item_ not in item_ids_:
                        return self.send(400, {"error": "That packing item is not on this list."})
                    raw_ = json.loads(json.dumps(old_))
                    if "area" in item_action_:
                        area_ = str(item_action_.get("area") or "")
                        area_ids_ = {str(x.get("id")) for x in (profile_.get("areas") or [])
                                     if isinstance(x, dict) and x.get("id")}
                        if area_ not in area_ids_:
                            return self.send(400, {"error": "That packing area is not available."})
                        raw_.setdefault("locations", {})[item_] = area_
                    elif isinstance(item_action_.get("packed"), bool):
                        packed_ = item_action_["packed"]
                        for key_ in ("ready", "checked", "packed"):
                            raw_.setdefault(key_, {})
                        if packed_:
                            raw_["ready"][item_] = True
                            raw_["checked"][item_] = "pass"
                            raw_["packed"][item_] = True
                        else:
                            raw_["ready"].pop(item_, None)
                            raw_["checked"].pop(item_, None)
                            raw_["packed"].pop(item_, None)
                        all_packed_ = all(raw_["packed"].get(x) is True for x in item_ids_)
                        final_ids_ = {x["id"] for x in (profile_.get("final_checks") or []) if x.get("id")}
                        finals_pass_ = all((raw_.get("finals") or {}).get(x) == "pass" for x in final_ids_)
                        raw_["phase"] = "complete" if all_packed_ and finals_pass_ else "pack"
                    else:
                        return self.send(400, {"error": "Choose a bag state or packing area."})
                    raw_["seq"] = max(int(old_.get("seq") or 0) + 1,
                                      int(item_action_.get("seq") or 0), int(time.time() * 1000))
                elif mark_ is not None:
                    if not adult_child_ or not isinstance(mark_, dict):
                        return self.send(403, {"error": "Only a signed-in grown-up can inspect packing items."})
                    item_ = str(mark_.get("item") or "")
                    value_ = str(mark_.get("value") or "")
                    if item_ not in packing_item_ids(profile_, packer_) or value_ not in ("present", "missing"):
                        return self.send(400, {"error": "That packing inspection choice did not make sense."})
                    raw_ = json.loads(json.dumps(old_))
                    raw_.setdefault("ready", {})
                    raw_.setdefault("checked", {})
                    raw_.setdefault("packed", {})
                    if value_ == "present":
                        raw_["ready"][item_] = True
                        raw_["checked"][item_] = "pass"
                        raw_["phase"] = "inspect"
                    else:
                        raw_["ready"].pop(item_, None)
                        raw_["packed"].pop(item_, None)
                        raw_["checked"][item_] = "fix"
                        raw_["phase"] = "layout"
                    raw_["seq"] = max(int(old_.get("seq") or 0) + 1, int(time.time() * 1000))
                elif stage_ is not None:
                    if not adult_child_ or stage_ not in ("layout", "inspect", "pack", "complete_stage"):
                        return self.send(403, {"error": "Only a signed-in grown-up can change inspection stages."})
                    item_ids_ = packing_item_ids(profile_, packer_)
                    raw_ = json.loads(json.dumps(old_))
                    if stage_ == "complete_stage":
                        phase_ = str(old_.get("phase") or "layout")
                        if phase_ == "layout":
                            raw_.setdefault("ready", {}).update({x: True for x in item_ids_})
                            raw_["phase"] = "inspect"
                        elif phase_ == "inspect":
                            if not all((old_.get("ready") or {}).get(x) is True for x in item_ids_):
                                return self.send(409, {"error": "Complete the gathering stage first."})
                            raw_.setdefault("checked", {}).update({x: "pass" for x in item_ids_})
                            raw_["phase"] = "pack"
                        elif phase_ == "pack":
                            if not all((old_.get("checked") or {}).get(x) == "pass" for x in item_ids_):
                                return self.send(409, {"error": "Complete the inspection stage first."})
                            raw_.setdefault("packed", {}).update({x: True for x in item_ids_})
                            raw_["phase"] = "final"
                        elif phase_ == "final":
                            if not all((old_.get("packed") or {}).get(x) is True for x in item_ids_):
                                return self.send(409, {"error": "Complete the bagging stage first."})
                            final_ids_ = {x["id"] for x in (profile_.get("final_checks") or []) if x.get("id")}
                            raw_.setdefault("finals", {}).update({x: "pass" for x in final_ids_})
                            raw_["phase"] = "complete"
                        else:
                            return self.send(409, {"error": "This packing list is already complete."})
                    else:
                        if stage_ == "inspect" and not all((old_.get("ready") or {}).get(x) is True
                                                           for x in item_ids_):
                            return self.send(409, {"error": "Every item must be laid out before inspection."})
                        if stage_ == "pack" and not all((old_.get("checked") or {}).get(x) == "pass"
                                                        for x in item_ids_):
                            return self.send(409, {"error": "Inspect every item before packing begins."})
                        raw_["phase"] = stage_
                    raw_["seq"] = max(int(old_.get("seq") or 0) + 1, int(time.time() * 1000))
                clean_ = sanitize_packing_progress(game_, packer_, raw_)
                if not clean_:
                    return self.send(400, {"error": "That packing update did not make sense."})
                if clean_["phase"] == "complete" and not packing_complete(game_, packer_, clean_):
                    return self.send(409, {"error": "Every packed item and final check must pass first."})
                final_defs_ = {x.get("id"): x for x in (profile_.get("final_checks") or []) if x.get("id")}
                for final_id_, final_value_ in (clean_.get("finals") or {}).items():
                    if final_value_ == "pass" and (old_.get("finals") or {}).get(final_id_) != "pass":
                        available_, not_before_ = packing_final_check_available(final_defs_.get(final_id_, {}), st)
                        if not available_:
                            if not_before_ is None:
                                return self.send(409, {"error": "Wait for Travel's live leave-home time before doing that house check."})
                            return self.send(409, {"error": "That house check must be done closer to the actual leave-home time.",
                                                   "available_at": not_before_})
                child_error_ = child_packing_update_error(game_, packer_, old_, clean_) \
                    if is_child_ and not adult_child_ else None
                if child_error_:
                    return self.send(403, {"error": child_error_})
                incoming_is_newer_ = int(old_.get("seq") or 0) <= int(clean_.get("seq") or 0)
                if int(old_.get("seq") or 0) > int(clean_.get("seq") or 0):
                    clean_ = old_   # a slower response from an earlier tap cannot rewind the packing flow
                if packing_complete(game_, packer_, old_):
                    clean_ = old_   # completion is durable; a stray Reset cannot relock rewards or repay points
                complete_ = packing_complete(game_, packer_, clean_)
                now_ = time.time()
                interacted_ = packing_activity_changed(old_, clean_) if incoming_is_newer_ else False
                clean_["started_at"] = old_.get("started_at") or now_
                clean_["updated_at"] = now_ if incoming_is_newer_ else old_.get("updated_at") or now_
                clean_["interacted_at"] = now_ if interacted_ else old_.get("interacted_at")
                events_ = [e for e in active_packing_award_events(game_).values() if e.get("kid") == packer_]
                legacy_awarded_ = bool(old_.get("awarded")) or any(not e.get("packing_milestone") for e in events_)
                active_ids_ = {e["id"] for e in events_}
                award_refs_ = {stage_: ref_ for stage_, ref_ in (old_.get("awards") or {}).items()
                               if ref_ in active_ids_} if isinstance(old_.get("awards"), dict) else {}
                for event_ in events_:
                    if event_.get("packing_milestone") in PACKING_AWARD_STAGES:
                        award_refs_[event_["packing_milestone"]] = event_["id"]
                plan_ = packing_award_plan(profile_)
                labels_ = profile_.get("award_labels") if isinstance(profile_.get("award_labels"), dict) else {}
                if not legacy_awarded_:
                    reached_ = packing_milestones_reached(game_, packer_, clean_)
                    for stage_ in PACKING_AWARD_STAGES:
                        points_ = plan_[stage_]
                        if stage_ not in reached_ or not points_ or award_refs_.get(stage_):
                            continue
                        award_event_ = append({"type": "adjust", "kid": packer_, "points": points_,
                                               "reason": labels_.get(stage_) or (
                                                   game_.get("award_reason") if stage_ == "complete" else None
                                               ) or PACKING_AWARD_DEFAULT_LABELS[stage_],
                                               "by": "packing", "icon": game_.get("icon") or "1f9f3",
                                               "packing_version": game_.get("version"),
                                               "packing_milestone": stage_})
                        award_refs_[stage_] = award_event_["id"]
                clean_["awards"] = award_refs_
                clean_["awarded"] = legacy_awarded_ or (
                    complete_ and all(not plan_[stage_] or award_refs_.get(stage_)
                                      for stage_ in PACKING_AWARD_STAGES))
                saved_.setdefault("kids", {})[packer_] = clean_
                save_private_json(PACKING_PROGRESS_FILE, saved_)
            ilog({"src": "packing", "packer": packer_, "phase": clean_["phase"],
                  "complete": packing_complete(game_, packer_, clean_), "interaction": interacted_})
            after_change()
            return self.send(200, {"ok": True, "state": derive()})
        if p == "/api/packing_admin":
            by_ = b.get("by")
            if not verified_ha_adult(self.headers, by_):
                return self.send(403, {"error": "Use the signed-in grown-up Chore dashboard."})
            with PACKING_LOCK:
                private_, private_path_ = packing_private_config()
                if not private_ or not private_path_:
                    return self.send(409, {"error": "Packing edits require a private packing_game_file."})
                try:
                    edit_ = packing_edit_config(private_, b)
                except ValueError as ex:
                    return self.send(400, {"error": str(ex)})
                save_private_json(private_path_, private_)
                if str(b.get("action") or "") == "add_item":
                    saved_ = load_private_json(PACKING_PROGRESS_FILE, {})
                    profile_ = next((x for x in (private_.get("profiles") or [])
                                     if x.get("id") == b.get("profile")), None)
                    targets_ = [edit_["only"]] if edit_ and edit_.get("only") else packing_packer_ids(profile_ or {})
                    changed_ = False
                    for packer_ in targets_:
                        row_ = (saved_.get("kids") or {}).get(packer_)
                        if not row_ or packing_complete(private_, packer_, row_):
                            continue
                        row_["phase"] = "layout"
                        row_["seq"] = max(int(row_.get("seq") or 0) + 1, int(time.time() * 1000))
                        row_["updated_at"] = time.time()
                        changed_ = True
                    if changed_:
                        save_private_json(PACKING_PROGRESS_FILE, saved_)
            ilog({"src": "packing_admin", "by": by_, "action": str(b.get("action") or ""),
                  "profile": str(b.get("profile") or "")})
            after_change()
            return self.send(200, {"ok": True, "state": derive()})
        if p in ("/api/tv/reward_pause", "/api/tv/reward_resume"):
            if not verified_ha_adult(self.headers, b.get("by")):
                return self.send(403, {"error": "Use the signed-in grown-up Chore dashboard."})
            session_ = str(b.get("session_id") or "")
            tv_ = str(b.get("tv") or "")
            if not session_ or tv_ not in TVS:
                return self.send(400, {"error": "unknown reward session / TV"})
            with TV_REWARD_LOCK:
                raw_ = read_events()
                saved_ = next((x for x in saved_tv_rewards(raw_) if x["session_id"] == session_ and x["tv"] == tv_), None)
                live_ = [x for x in tv_grants(raw_).get(tv_, []) if (x.get("group") or x["id"]) == session_
                         and x.get("what") in ("show", "movie", "games")]
                if p.endswith("reward_pause"):
                    if saved_:   # a lost HA relay response / repeat press is a read-only success
                        return self.send(200, {"ok": True, "already": True, "saved_reward": saved_, "state": derive()})
                    live_ = [x for x in live_ if not x.get("armed")]
                    if not live_:
                        return self.send(409, {"error": "That reward is not actively playing, or its time already ended."})
                    now_ = time.time()
                    remaining_ = max(0.0, max(x["until"] for x in live_) - now_)
                    if remaining_ < 1:
                        return self.send(409, {"error": "That reward time has already ended."})
                    try:
                        playback_ = playing_item(TVS[tv_], ha_token())
                        if playback_:
                            playback_["app"] = app_of(TVS[tv_], ha_token())
                    except Exception:  # preserving the time never depends on a player's optional metadata
                        playback_ = None
                    ev_ = append({"type": "tv_reward_pause", "session": session_, "tv": tv_,
                                  "remaining_s": remaining_, "reward": live_[0]["what"],
                                  "kids": sorted({x["kid"] for x in live_ if x.get("kid")}),
                                  "playback": playback_, "by": b["by"]})
                    GUARD.setdefault("hard_lock", {})[tv_] = now_
                    GUARD.setdefault("finish", {}).pop(tv_, None)
                    GUARD.setdefault("grace", {}).pop(tv_, None)
                    queue_tv_reward_pause(tv_, playback_)
                    after_change()
                    saved_ = next(x for x in saved_tv_rewards(read_events()) if x["session_id"] == session_)
                    ilog({"src": "tv_reward", "action": "pause", "tv": tv_, "session": session_,
                          "remaining_s": round(remaining_), "by": b["by"], "position_saved": bool(playback_)})
                    return self.send(200, {"ok": True, "event": ev_, "saved_reward": saved_,
                                           "lock_requested": True, "state": derive()})
                if not saved_:
                    if live_:   # repeat Resume after the first response was lost
                        return self.send(200, {"ok": True, "already": True, "state": derive()})
                    return self.send(409, {"error": "That saved reward is no longer available."})
                competing_ = [x for x in tv_grants(raw_).get(tv_, [])
                              if (x.get("group") or x["id"]) != session_ and x.get("what") != "vr"]
                if competing_:
                    return self.send(409, {"error": "Another reward is using or waiting for this TV. The saved time is still safe."})
                now_ = time.time()
                remaining_ = float(saved_["remaining_s"])
                ev_ = append({"type": "tv_reward_resume", "session": session_, "tv": tv_,
                              "remaining_s": remaining_, "until": now_ + remaining_,
                              "playback": saved_.get("playback"), "by": b["by"]})
                GUARD.setdefault("hard_lock", {}).pop(tv_, None)
                GUARD.setdefault("standby", {})[tv_] = False
                queue_tv_reward_resume(tv_, saved_.get("playback"))
                after_change()
                ilog({"src": "tv_reward", "action": "resume", "tv": tv_, "session": session_,
                      "remaining_s": round(remaining_), "by": b["by"],
                      "position_restore_requested": bool(saved_.get("playback"))})
                return self.send(200, {"ok": True, "event": ev_, "remaining_s": remaining_,
                                       "position_restore_requested": bool(saved_.get("playback")), "state": derive()})
        if p == "/api/tv/reward_resume_child":
            session_, tv_, kid_ = str(b.get("session_id") or ""), str(b.get("tv") or ""), b.get("kid")
            if not session_ or tv_ not in TVS or kid_ not in kids:
                return self.send(400, {"error": "unknown saved movie / TV"})
            with TV_REWARD_LOCK:
                raw_ = read_events()
                saved_ = next((x for x in saved_tv_rewards(raw_)
                               if x["session_id"] == session_ and x["tv"] == tv_), None)
                if not saved_:
                    live_ = [x for x in tv_grants(raw_).get(tv_, [])
                             if (x.get("group") or x["id"]) == session_]
                    if live_ and any(x.get("kid") == kid_ for x in live_):
                        return self.send(200, {"ok": True, "already": True, "state": derive()})
                    return self.send(409, {"error": "That saved movie is no longer waiting."})
                current_ = list(saved_.get("kids") or [])
                if kid_ not in current_:
                    return self.send(403, {"error": "That saved movie belongs to someone else."})
                joiners_ = [x for x in dict.fromkeys(b.get("with") or []) if x in kids and x not in current_]
                participants_ = current_ + joiners_
                if joiners_:
                    if not valid_shared_reward_sessions(b, participants_, display_ip_):
                        return self.send(403, {"error": "Everyone sharing must enter their own PIN."})
                elif not valid_kid_session(b.get("kid_session"), kid_, display_ip_):
                    return self.send(403, {"error": "Open your profile with your PIN first."})

                reward_ = rewards.get(saved_["reward"])
                fresh_ = derive()
                if not reward_ or saved_["reward"] not in ("show", "movie", "games"):
                    return self.send(409, {"error": "That saved reward cannot be started here."})
                if fresh_["bedtime"]["on"] or reward_closed(reward_):
                    return self.send(409, {"error": "Movie time is not open now. Your saved time is safe."})
                blocked_ = reward_block_state(fresh=True)
                if blocked_["on"]:
                    return self.send(409, {"error": blocked_["items"][0]["message"]})
                packing_gate_ = packing_reward_gate(fresh_, participants_)
                if packing_gate_:
                    return self.send(409, {"error": packing_gate_["name"] + " must finish packing first. "
                                                   "The saved movie time is safe."})
                if any(not is_here(reward_, c, who_) for who_ in participants_):
                    return self.send(409, {"error": "Everyone sharing must be at this house."})
                if fresh_.get("present_kids") and any(who_ not in fresh_["present_kids"] for who_ in participants_):
                    return self.send(409, {"error": "Everyone sharing must be home."})
                for who_ in participants_:
                    if (fresh_.get("dress") or {}).get("on") \
                            and (fresh_["dress"]["kids"].get(who_) or "ready") != "ready":
                        return self.send(409, {"error": "Put on your clothes first!"})
                    gate_ = ((fresh_.get("must_do_first") or {}).get(who_) or {})
                    if gate_.get("rewards"):
                        return self.send(409, {"error": (gate_.get("name") or "Mandatory chore") +
                                                       " must be done first. Your saved time is safe."})
                    if ((fresh_.get("chores_first") or {}).get(who_) or {}).get("on"):
                        return self.send(409, {"error": "Finish the chores-first job before movie time. Your saved time is safe."})
                for who_ in joiners_:
                    ready_ = (fresh_.get("reward_ready") or {}).get(who_, {}).get(saved_["reward"], 0)
                    if ready_ > time.time():
                        return self.send(409, {"error": "The kid joining needs a screen break first.",
                                               "ready_at": ready_})
                competing_ = [x for x in tv_grants(raw_).get(tv_, [])
                              if (x.get("group") or x["id"]) != session_ and x.get("what") != "vr"]
                if competing_:
                    return self.send(409, {"error": "Another reward is using this TV. Your saved time is safe."})

                refunds_, cost_each_, join_cost_, price_source_, cost_by_kid_ = {}, None, None, None, {}
                if joiners_:
                    undone_ = {e["ref"] for e in raw_ if e.get("type") == "undo" and e.get("ref")}
                    old_spends_ = [e for e in raw_ if e.get("type") == "spend" and e["id"] not in undone_
                                   and (e.get("group") or e["id"]) == session_]
                    old_spends_ = list({e["id"]: e for e in old_spends_}.values())
                    if {e.get("kid") for e in old_spends_} != set(current_):
                        return self.send(409, {"error": "That saved movie changed. Refresh and try again."})
                    old_costs_ = {e["kid"]: int(e["cost"]) for e in old_spends_}
                    now_price_ = time.time()
                    price_ = reward_join_price(reward_, len(participants_),
                                               now_price_ + float(saved_["remaining_s"])
                                               - float(reward_.get("tv_minutes") or 1) * 60,
                                               now_price_ + float(saved_["remaining_s"]), tv_, session_, now=now_price_,
                                               movie_progress=saved_.get("playback"))
                    cost_each_, join_cost_, price_source_ = price_["full_cost"], price_["cost"], price_["source"]
                    cost_by_kid_ = {who_: min(old_costs_[who_], cost_each_) for who_ in current_}
                    cost_by_kid_.update({who_: join_cost_ for who_ in joiners_})
                    for who_ in participants_:
                        adjustment_ = cost_by_kid_[who_] - old_costs_.get(who_, 0)
                        if fresh_["balance"].get(who_, 0) < adjustment_:
                            name_ = next((x["name"] for x in c["kids"] if x["id"] == who_), who_)
                            return self.send(409, {"error": name_ + " does not have enough stars to watch."})
                    for old_ in old_spends_:
                        append({"type": "undo", "ref": old_["id"], "by": "share_resume"})

                now_, remaining_ = time.time(), float(saved_["remaining_s"])
                until_ = now_ + remaining_
                if joiners_:
                    primary_ = current_[0]
                    for who_ in participants_:
                        append({"type": "spend", "kid": who_, "reward": saved_["reward"], "cost": cost_by_kid_[who_],
                                "tv": tv_, "until": until_, "minutes": remaining_ / 60,
                                "started_at": now_, "group": session_, "together": participants_,
                                "by": "share_resume", **({"with_buyer": primary_} if who_ != primary_ else {}),
                                **({"join_prorated": True, "price_fraction": price_["fraction"],
                                    "price_source": price_source_} if who_ in joiners_ else {}),
                                "reward_name": reward_["name"], "reward_icon": reward_["icon"]})
                    refunds_ = {who_: max(0, old_costs_.get(who_, 0) - cost_by_kid_[who_]) for who_ in current_}
                ev_ = append({"type": "tv_reward_resume", "session": session_, "tv": tv_,
                              "remaining_s": remaining_, "until": until_,
                              "playback": saved_.get("playback"), "by": kid_,
                              **({"together": participants_} if joiners_ else {})})
                GUARD.setdefault("hard_lock", {}).pop(tv_, None)
                GUARD.setdefault("standby", {})[tv_] = False
                queue_tv_reward_resume(tv_, saved_.get("playback"))
                after_change()
                ilog({"src": "tv_reward", "action": "child_resume", "tv": tv_, "session": session_,
                      "remaining_s": round(remaining_), "kid": kid_, "participants": participants_,
                      "cost_each": cost_each_, "join_cost": join_cost_, "price_source": price_source_})
                return self.send(200, {"ok": True, "event": ev_, "remaining_s": remaining_,
                                       "participants": participants_, "refunds": refunds_,
                                       "cost_each": cost_each_, "join_cost": join_cost_,
                                       "cost_by_kid": cost_by_kid_, "price_source": price_source_,
                                       "state": derive()})
        if p == "/api/project/context_approve":
            ch_ = chores.get(b.get("chore")) or {}
            if b.get("by") not in insp or not stepped_jobs.definition(ch_):
                return self.send(400, {"error": "unknown grown-up or project"})
            ev_ = append({"type": "context_approve", "chore": ch_["id"], "by": b["by"], "until": time.time() + 1800})
            after_change()
            return self.send(200, {"ok": True, "event": ev_, "state": derive()})
        if p == "/api/project/start":
            with PROJECT_LOCK:
                st = derive()
                ch_ = chores.get(b.get("chore")) or {}
                try:
                    step_cfg_ = stepped_jobs.validate_definition(stepped_jobs.definition(ch_))
                except (TypeError, ValueError) as ex:
                    return self.send(400, {"error": "project configuration: " + str(ex)})
                leader_ = b.get("kid")
                team_ = list(dict.fromkeys([leader_] + list(b.get("team") or b.get("partners") or [])))
                if leader_ not in kids or any(x not in kids for x in team_):
                    return self.send(400, {"error": "unknown kid"})
                packing_gate_ = next((gate_ for gate_ in
                                      (packing_chore_gate(st, who_, ch_.get("id")) for who_ in team_) if gate_), None)
                if packing_gate_:
                    return self.send(409, {"error": packing_gate_["message"],
                                           "packing_required": packing_gate_})
                if ch_.get("only") and any(x not in ch_["only"] for x in team_):
                    return self.send(409, {"error": "That team is not eligible for this project."})
                if any(not is_here(ch_, c, x) for x in team_):
                    return self.send(409, {"error": "Everyone on the team must be at this house."})
                if len(team_) < step_cfg_["min_team"]:
                    return self.send(409, {"error": "Choose at least %d kids." % step_cfg_["min_team"]})
                occupied_ = [x for x in team_ if x in st["active"] or any(
                    p_.get("active_session") and x in p_["active_session"].get("team", []) for p_ in st.get("projects") or [])]
                if occupied_:
                    return self.send(409, {"error": "%s already has a job in progress." % occupied_[0]})
                context_ = stepped_context(ch_, read_events())
                if context_["state"] == "blocked":
                    why_ = next((x["reason"] for x in context_["items"] if x["state"] == "blocked"), "not safe outside")
                    return self.send(409, {"error": "Not now: %s." % why_, "context": context_})
                if context_["state"] == "unknown":
                    return self.send(409, {"error": "Ask a grown-up to confirm the weather is safe.",
                                           "context": context_, "needs_adult": True})
                raw_projects_ = stepped_jobs.derive(c["chores"], read_events())
                project_ = stepped_jobs.find_open(raw_projects_, ch_["id"])
                if project_ and project_["status"] != "held":
                    return self.send(409, {"error": "This project is already active or waiting for final inspection.",
                                           "project": stepped_jobs.public_project(project_)})
                if not project_:
                    instance_ = uuid.uuid4().hex[:12]
                    append({"type": "project_start", "instance": instance_, "chore": ch_["id"],
                            "definition_version": step_cfg_["version"], "kid": leader_})
                else:
                    instance_ = project_["instance"]
                session_ = uuid.uuid4().hex[:12]
                ev_ = append({"type": "project_session_start", "instance": instance_, "session": session_,
                              "team": team_, "kid": leader_})
                after_change()
                ilog({"src": "project", "action": "session_start", "chore": ch_["id"],
                      "instance": instance_, "session": session_, "team": team_})
                return self.send(200, {"ok": True, "event": ev_, "state": derive()})
        if p in ("/api/project/step", "/api/project/stop", "/api/project/award", "/api/project/abandon"):
            with PROJECT_LOCK:
                st = derive()
                project_public_ = next((x for x in st.get("projects") or [] if x["instance"] == b.get("instance")), None)
                raw_ = next((x for x in stepped_jobs.derive(c["chores"], read_events())
                             if x["instance"] == b.get("instance")), None)
                if not raw_ or not project_public_:
                    return self.send(404, {"error": "project not found"})
                try:
                    stepped_jobs.require_revision(raw_, b.get("revision"))
                except stepped_jobs.TransitionError as ex:
                    return self.send(409, {"error": str(ex), "project": project_public_, "state": st})
                ch_ = chores.get(raw_["chore"]) or {}
                cfg_ = raw_.get("config")
                if not cfg_:
                    return self.send(409, {"error": "This project definition changed. A grown-up must migrate it deliberately."})
                nxt_ = stepped_jobs.current(raw_)
                session_ = raw_.get("active_session")
                if p == "/api/project/abandon":
                    if b.get("by") not in insp:
                        return self.send(403, {"error": "grown-ups only"})
                    ev_ = append({"type": "project_abandon", "instance": raw_["instance"], "by": b["by"]})
                elif p == "/api/project/stop":
                    if nxt_["kind"] != "work" or not session_ or b.get("kid") not in session_["team"]:
                        return self.send(409, {"error": "The project is not at a work stop.", "project": project_public_})
                    mode_ = b.get("mode") if b.get("mode") in ("pause", "finish") else "pause"
                    ev_ = append({"type": "project_stop", "instance": raw_["instance"], "session": session_["id"],
                                  "mode": mode_, "kid": b["kid"], "reason": b.get("reason") or "kid"})
                elif p == "/api/project/step":
                    if nxt_["kind"] != "step" or (nxt_["step"] or {}).get("id") != b.get("step"):
                        return self.send(409, {"error": "That is not the current step.", "project": project_public_})
                    step_ = nxt_["step"]
                    if step_["actor"] == "adult":
                        if b.get("by") not in insp or (step_.get("adult_role") == "lead" and b.get("by") != LEAD_ADULT):
                            return self.send(403, {"error": "The designated grown-up must confirm this step."})
                        actor_ = {"by": b["by"]}
                    else:
                        emergency_ = bool(step_.get("safety") and b.get("emergency") and b.get("by") in insp)
                        if not emergency_ and (not session_ or b.get("kid") not in session_["team"]):
                            return self.send(403, {"error": "A participating kid must confirm this step."})
                        actor_ = ({"by": b["by"], "emergency": True} if emergency_ else {"kid": b["kid"]})
                    ev_ = append({"type": "project_step", "instance": raw_["instance"],
                                  "session": session_["id"] if session_ else None, "step": step_["id"],
                                  "scope": step_["scope"], **actor_})
                    raw_after_ = next(x for x in stepped_jobs.derive(c["chores"], read_events()) if x["instance"] == raw_["instance"])
                    if stepped_jobs.current(raw_after_)["kind"] == "complete":
                        append({"type": "project_complete", "instance": raw_["instance"], **actor_})
                else:   # /api/project/award
                    if b.get("by") not in insp or (cfg_.get("award_adult") == "lead" and b.get("by") != LEAD_ADULT):
                        return self.send(403, {"error": "The designated grown-up awards this session."})
                    if nxt_["kind"] != "award" or not session_:
                        return self.send(409, {"error": "Finish every put-away step before awarding stars.",
                                               "project": project_public_})
                    try:
                        awards_ = {k: int((b.get("awards") or {})[k]) for k in session_["team"]}
                    except (KeyError, TypeError, ValueError):
                        return self.send(400, {"error": "Choose stars separately for every participant."})
                    if any(v < 0 or v > 50 for v in awards_.values()) or set(b.get("awards") or {}) != set(session_["team"]):
                        return self.send(400, {"error": "Choose 0 to 50 stars separately for every participant."})
                    ev_ = append({"type": "project_session_award", "instance": raw_["instance"],
                                  "session": session_["id"], "awards": awards_, "by": b["by"]})
                    for member_, points_ in awards_.items():
                        report_ = append({"type": "report", "kid": member_, "chore": raw_["chore"],
                                          "feeling": None, "points": points_, "project_instance": raw_["instance"],
                                          "project_session": session_["id"]})
                        append({"type": "inspect", "ref": report_["id"], "by": b["by"], "ok": True,
                                "points": points_, "label": ch_.get("session_label") or ch_.get("name")})
                    append({"type": "project_session_close", "instance": raw_["instance"],
                            "session": session_["id"], "mode": nxt_["mode"], "by": b["by"]})
                    raw_after_ = next(x for x in stepped_jobs.derive(c["chores"], read_events()) if x["instance"] == raw_["instance"])
                    if stepped_jobs.current(raw_after_)["kind"] == "complete":
                        append({"type": "project_complete", "instance": raw_["instance"], "by": b["by"]})
                after_change()
                ilog({"src": "project", "action": p.rsplit("/", 1)[1], "chore": raw_["chore"],
                      "instance": raw_["instance"], "step": b.get("step"), "by": b.get("by") or b.get("kid")})
                return self.send(200, {"ok": True, "event": ev_, "state": derive()})
        if p in ("/api/start", "/api/report", "/api/team", "/api/pause") \
                and stepped_jobs.definition(chores.get(b.get("chore"))):
            return self.send(409, {"error": "Use this project's guided steps."})
        if p == "/api/paper_scan_start":
            kid_, kind_ = b.get("kid"), str(b.get("kind") or "")
            school_ = next((x for x in c["chores"] if x.get("document_review") and
                            (not x.get("only") or kid_ in x["only"])), None)
            if kid_ not in kids or kind_ not in ("school", "personal") or not school_:
                return self.send(400, {"error": "paper scanning is not available for this profile"})
            packing_gate_ = packing_chore_gate(st, kid_, school_["id"]) if kind_ == "school" else None
            if packing_gate_:
                return self.send(409, {"error": packing_gate_["message"],
                                       "packing_required": packing_gate_})
            scan_id_ = uuid.uuid4().hex[:12]
            PAPER_SCANS[scan_id_] = {"id": scan_id_, "kid": kid_, "kind": kind_, "chore": school_["id"],
                                     "ts": time.time(), "expires": time.time() + 20 * 60, "status": "waiting"}
            save_paper_scans()
            ilog({"src": "paper", "action": "waiting", "kid": kid_, "kind": kind_, "scan": scan_id_})
            return self.send(200, {"ok": True, "scan": scan_id_, "status": "waiting"})
        if p == "/api/paper_scan_status":
            row_ = PAPER_SCANS.get(str(b.get("scan") or ""))
            if not row_ or row_.get("kid") != b.get("kid"):
                return self.send(404, {"error": "scan not found"})
            if row_["status"] == "waiting" and row_["expires"] < time.time():
                row_["status"] = "expired"
                save_paper_scans()
            return self.send(200, {"ok": True, "status": row_["status"], "ref": row_.get("ref"),
                                   **({"state": derive()} if row_["status"] == "done" else {})})
        if p in ("/api/paper_ingest", "/api/document_host"):
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                return self.send(403, {"error": "loopback only"})
            pages_ = b.get("pages") if isinstance(b.get("pages"), list) else []
            if not pages_ or len(pages_) > 30 or any(not re.fullmatch(r"/local/chore-papers/[a-zA-Z0-9_-]+\.jpg", str(x)) for x in pages_):
                return self.send(400, {"error": "invalid hosted paper pages"})
            if p == "/api/document_host":
                ref_ = str(b.get("ref") or "")
                report_ = next((x for x in st["history"] if x.get("id") == ref_ and x.get("type") == "report"), None)
                if not report_:
                    return self.send(404, {"error": "report not found"})
                ev_ = append({"type": "document_host", "ref": ref_, "pages": pages_,
                              "title": str(b.get("title") or "School paper")[:100]})
                after_change()
                return self.send(200, {"ok": True, "event": ev_, "state": derive()})
            scan_ = PAPER_SCANS.get(str(b.get("scan") or ""))
            if not scan_ or scan_["status"] != "waiting" or scan_["expires"] < time.time():
                return self.send(409, {"error": "no kid is waiting for that scan"})
            label_ = "School paper" if scan_["kind"] == "school" else "Personal paper"
            stamp_ = time.strftime("%b %d, %I:%M %p", time.localtime()).replace(" 0", " ")
            if scan_["kind"] == "school":
                price_ = (st["board"].get(scan_["chore"]) or {}).get("price")
                ev_ = append({"type": "report", "kid": scan_["kid"], "chore": scan_["chore"], "feeling": "ok",
                              "points": price_, "document_pages": pages_, "document_title": label_ + " · " + stamp_})
                threading.Thread(target=notify_report, args=(ev_, derive()), daemon=True).start()
            else:
                ev_ = append({"type": "personal_paper", "kid": scan_["kid"], "document_pages": pages_,
                              "document_title": label_ + " · " + stamp_})
            scan_["status"], scan_["ref"] = "done", ev_["id"]
            save_paper_scans()
            after_change()
            ilog({"src": "paper", "action": "ingested", "kid": scan_["kid"], "kind": scan_["kind"],
                  "scan": scan_["id"], "pages": len(pages_)})
            return self.send(200, {"ok": True, "event": ev_, "state": derive()})
        if p == "/api/kid_unlock":
            kid_ = next((x for x in c["kids"] if x["id"] == b.get("kid")), None)
            if not kid_:
                return self.send(404, {"error": "unknown kid"})
            if not kid_.get("pin_hash"):
                evidence_ = record_kid_display_presence(c, kid_["id"], display_ip_, verified=False)
                session_ = issue_kid_session(kid_["id"], display_ip_)
                if evidence_:
                    after_change()
                return self.send(200, {"ok": True, "kid_session": session_,
                                       "state": state_for_display(display_ip_)})
            now_, ip_ = time.time(), display_ip_
            attempts_ = [x for x in GUARD.setdefault("kid_pin_failures", {}).get(ip_, []) if now_ - x < 60]
            if len(attempts_) >= 5:
                return self.send(429, {"error": "Too many tries. Wait one minute."})
            if not verify_kid_pin(kid_, b.get("pin")):
                GUARD["kid_pin_failures"][ip_] = attempts_ + [now_]
                ilog({"src": "kid_pin", "kid": kid_["id"], "ip": ip_, "ok": False})
                return self.send(403, {"error": "That PIN did not match."})
            GUARD["kid_pin_failures"].pop(ip_, None)
            ilog({"src": "kid_pin", "kid": kid_["id"], "ip": ip_, "ok": True})
            evidence_ = record_kid_display_presence(c, kid_["id"], ip_, verified=True)
            session_ = issue_kid_session(kid_["id"], ip_)
            if evidence_:
                after_change()
            return self.send(200, {"ok": True, "kid_session": session_, "state": state_for_display(ip_)})
        if p == "/api/kid_peer_reset_request":
            target_ = next((x for x in c["kids"] if x["id"] == b.get("kid") and x.get("pin_hash")), None)
            display_ = peer_reset_display(display_ip_)
            eligible_ = set(PEER_PIN_CFG.get("kids") or [x["id"] for x in c["kids"] if x.get("pin_hash")])
            if not PEER_PIN_CFG or not display_ or not target_ or target_["id"] not in eligible_:
                return self.send(409, {"error": "The two-Hub reset is not available here."})
            if not HUB_PLACEMENT.get("trusted") or stale_peer_hubs():
                return self.send(409, {"error": "The two Nest Hubs need a grown-up placement check first."})
            if int(st["balance"].get(target_["id"], 0)) < 1:
                return self.send(409, {"error": "You need 1 star to thank the kid who helps. Ask a grown-up instead."})
            with PEER_LOCK:
                old_nonce_, old_ = active_peer_pin_reset()
                if old_:
                    if old_.get("target") != target_["id"] or old_.get("target_ip") != display_ip_:
                        return self.send(409, {"error": "Another two-Hub reset is already happening."})
                    nonce_ = old_nonce_
                else:
                    nonce_ = uuid.uuid4().hex[:10]
                    PEER_PIN_RESETS[nonce_] = {"target": target_["id"], "target_ip": display_ip_,
                                               "target_display": display_["id"], "status": "waiting",
                                               "ts": time.time(), "until": time.time() + max(60, min(900, int(
                                                   PEER_PIN_CFG.get("valid_seconds") or 300))),
                                               "holding": False}
                    save_peer_pin_resets()
                    after_change()
            ilog({"src": "peer_pin_reset", "action": "requested", "kid": target_["id"],
                  "display": display_["id"], "nonce": nonce_})
            return self.send(200, {"ok": True, "nonce": nonce_,
                                   "peer_pin_reset": peer_pin_reset_public(display_ip_, nonce=nonce_)})
        if p == "/api/kid_peer_reset_help":
            nonce_, helper_ = str(b.get("nonce") or ""), str(b.get("kid") or "")
            display_ = peer_reset_display(display_ip_)
            eligible_ = set(PEER_PIN_CFG.get("kids") or [x["id"] for x in c["kids"] if x.get("pin_hash")])
            with PEER_LOCK:
                row_ = PEER_PIN_RESETS.get(nonce_)
                if not row_ or row_.get("status") not in ("waiting", "helping") or float(row_.get("until") or 0) <= time.time():
                    return self.send(409, {"error": "That reset is no longer waiting."})
                if not HUB_PLACEMENT.get("trusted") or stale_peer_hubs():
                    return self.send(409, {"error": "The two Nest Hubs need a grown-up placement check first."})
                if not display_ or display_["id"] == row_.get("target_display") or helper_ == row_.get("target") or helper_ not in eligible_:
                    return self.send(403, {"error": "A different kid must help from the other Nest Hub."})
                if not valid_kid_session(b.get("kid_session"), helper_, display_ip_):
                    return self.send(403, {"error": "The helper must enter their own PIN on this Hub."})
                if row_.get("helper") not in (None, helper_):
                    return self.send(409, {"error": "Another kid is already helping."})
                row_.update(status="helping", helper=helper_, helper_ip=display_ip_,
                            helper_display=display_["id"], holding=False)
                save_peer_pin_resets()
            ilog({"src": "peer_pin_reset", "action": "helper_ready", "kid": row_["target"],
                  "helper": helper_, "display": display_["id"], "nonce": nonce_})
            after_change()
            return self.send(200, {"ok": True, "peer_pin_reset": peer_pin_reset_public(display_ip_, nonce=nonce_)})
        if p == "/api/kid_peer_reset_hold":
            nonce_, helper_ = str(b.get("nonce") or ""), str(b.get("kid") or "")
            with PEER_LOCK:
                row_ = PEER_PIN_RESETS.get(nonce_)
                if not row_ or row_.get("status") != "helping" or row_.get("helper") != helper_ \
                        or row_.get("helper_ip") != display_ip_ \
                        or not valid_kid_session(b.get("kid_session"), helper_, display_ip_):
                    return self.send(403, {"error": "The helper session is not active."})
                row_["holding"] = bool(b.get("holding"))
                row_["last_hold"] = time.time() if row_["holding"] else 0
                return self.send(200, {"ok": True, "holding": peer_helper_holding(row_)})
        if p == "/api/kid_peer_reset_status":
            public_ = peer_pin_reset_public(display_ip_, nonce=str(b.get("nonce") or ""))
            return self.send(200 if public_ else 404, {"ok": bool(public_), "peer_pin_reset": public_})
        if p == "/api/kid_peer_reset_finish":
            nonce_, target_ = str(b.get("nonce") or ""), str(b.get("kid") or "")
            with PEER_LOCK:
                row_ = PEER_PIN_RESETS.get(nonce_)
                if not row_ or row_.get("status") != "helping" or row_.get("target") != target_ \
                        or row_.get("target_ip") != display_ip_:
                    return self.send(409, {"error": "That two-Hub reset is not ready."})
                if not HUB_PLACEMENT.get("trusted") or stale_peer_hubs() or not peer_helper_holding(row_):
                    return self.send(409, {"error": "Your helper must stay at the other Hub and hold the big button."})
                if int(derive()["balance"].get(target_, 0)) < 1:
                    return self.send(409, {"error": "You no longer have the 1 star needed to thank your helper."})
                try:
                    new_hash_ = make_kid_pin_hash(b.get("new_pin"))
                except ValueError as ex:
                    return self.send(400, {"error": str(ex)})
                with LOCK:
                    config_ = cfg()
                    target_cfg_ = next((x for x in config_["kids"] if x["id"] == target_), None)
                    if not target_cfg_:
                        return self.send(404, {"error": "unknown kid"})
                    target_cfg_["pin_hash"] = new_hash_
                    save_cfg(config_)
                transfer_ = append({"type": "transfer", "from": target_, "to": row_["helper"], "points": 1,
                                    "reason": "Helped with a two-Hub PIN reset", "icon": "1f91d",
                                    "by": row_["helper"], "peer_reset": nonce_})
                row_.update(status="done", holding=False, completed_at=time.time(), transfer=transfer_["id"])
                save_peer_pin_resets()
            ilog({"src": "peer_pin_reset", "action": "completed", "kid": target_,
                  "helper": row_["helper"], "nonce": nonce_})
            after_change()
            session_ = issue_kid_session(target_, display_ip_)
            return self.send(200, {"ok": True, "helper": row_["helper"], "kid_session": session_,
                                   "state": state_for_display(display_ip_)})
        if p == "/api/hub_placement_attest":
            required_adult_ = str(PEER_PIN_CFG.get("adult") or LEAD_ADULT or "")
            if b.get("by") not in insp or b.get("by") != required_adult_:
                return self.send(403, {"error": "Only the designated grown-up can confirm the Hub locations."})
            ok_, error_ = attest_hub_placement(b.get("nonce"), b["by"])
            return self.send(200 if ok_ else 409, {"ok": ok_, "error": error_, "state": derive()})
        if p == "/api/kid_pin_reset_request":
            kid_ = next((x for x in c["kids"] if x["id"] == b.get("kid")), None)
            if not kid_:
                return self.send(404, {"error": "unknown kid"})
            first_pin_ = not kid_.get("pin_hash")
            approved_ = next((n for n, r in ADULT_REQ.items() if r.get("purpose") == "pin_reset"
                              and r.get("kid") == kid_["id"] and r.get("status") == "ok" and not r.get("used")
                              and bool(r.get("first_pin")) == first_pin_
                              and float(r.get("until") or 0) > time.time()), None)
            if approved_:
                return self.send(200, {"ok": True, "nonce": approved_, "state": derive()})
            old_ = next((n for n, r in ADULT_REQ.items() if r.get("purpose") == "pin_reset"
                         and r.get("kid") == kid_["id"] and r.get("status") == "pending"
                         and bool(r.get("first_pin")) == first_pin_
                         and time.time() - r["ts"] < ADULT_REQUEST_TTL_S), None)
            nonce_ = old_ or uuid.uuid4().hex[:8]
            if not old_:
                target_ = available_request_adult(st) or LEAD_ADULT
                if not target_:
                    return self.send(409, {"error": "No grown-up reset contact is configured."})
                ADULT_REQ[nonce_] = {"who": target_, "ip": display_ip_, "ts": time.time(),
                                     "status": "pending", "purpose": "pin_reset", "kid": kid_["id"],
                                     "first_pin": first_pin_}
                save_adult_requests()
                threading.Thread(target=send_adult_request, args=(nonce_, target_, "chore screen"), daemon=True).start()
                after_change()
            ilog({"src": "kid_pin", "kid": kid_["id"], "ip": display_ip_,
                  "action": "first_pin_requested" if first_pin_ else "reset_requested"})
            return self.send(200, {"ok": True, "nonce": nonce_, "state": derive()})
        if p == "/api/kid_pin_change":
            kid_ = next((x for x in c["kids"] if x["id"] == b.get("kid")), None)
            if not kid_:
                return self.send(404, {"error": "unknown kid"})
            first_pin_ = not kid_.get("pin_hash")
            reset_ = next((r for r in ADULT_REQ.values()
                if r.get("purpose") == "pin_reset" and r.get("kid") == kid_["id"] and r.get("status") == "ok"
                and bool(r.get("first_pin")) == first_pin_ and not r.get("used")
                and float(r.get("until") or 0) > time.time()), None)
            if first_pin_:
                if not reset_:
                    return self.send(403, {"error": "A grown-up must approve setting the first PIN."})
            elif not reset_ and not verify_kid_pin(kid_, b.get("current_pin")):
                return self.send(403, {"error": "The current PIN did not match."})
            try:
                new_hash_ = make_kid_pin_hash(b.get("new_pin"))
            except ValueError as ex:
                return self.send(400, {"error": str(ex)})
            with LOCK:
                c_ = cfg()
                target_ = next((x for x in c_["kids"] if x["id"] == kid_["id"]), None)
                if not target_:
                    return self.send(404, {"error": "unknown kid"})
                target_["pin_hash"] = new_hash_
                save_cfg(c_)
            if reset_:
                for row_ in ADULT_REQ.values():
                    if row_.get("purpose") == "pin_reset" and row_.get("kid") == kid_["id"] \
                            and row_.get("status") == "ok":
                        row_["used"] = True
                save_adult_requests()
            ilog({"src": "kid_pin", "kid": kid_["id"], "ip": display_ip_,
                  "action": "set_first" if first_pin_ else "changed_after_reset" if reset_ else "changed"})
            after_change()
            session_ = issue_kid_session(kid_["id"], display_ip_)
            return self.send(200, {"ok": True, "kid_session": session_, "state": state_for_display(display_ip_)})
        if p in ("/api/start", "/api/report", "/api/claim") and b.get("kid") and b.get("chore") in chores \
                and not is_here(chores[b["chore"]], c, b["kid"]):
            return self.send(409, {"error": "that one is at the other house"})
        if p == "/api/robot_seen":   # a grown-up says where a robot really is: the locator's best evidence (and a lesson)
            robots_ = list(((HOUSE.get("robot_locator") or {}).get("robots") or {}))
            vac_ = b.get("vacuum") or (robots_[0] if len(robots_) == 1 else None)
            room_ = str(b.get("room") or "").strip()[:40]
            if b.get("by") not in insp or vac_ not in robots_ or not room_:
                return self.send(400, {"error": "need by (a grown-up), vacuum and room"})
            globals().setdefault("LOCATOR_TRUTH", []).append({"vacuum": vac_, "room": room_, "at": time.time()})
            ilog({"src": "locator", "action": "robot_seen", "vacuum": vac_, "room": room_, "by": b["by"]})
            return self.send(200, {"ok": True})
        if p == "/api/room_eye/label":   # a grown-up names a spot the camera found, and says if it's a vacuum hazard
            import room_eye
            if b.get("by") not in insp or not room_eye.EYE:
                return self.send(400, {"error": "unknown grown-up or cameras off"})
            ok_ = room_eye.EYE.label(str(b.get("id") or ""), b.get("name"), bool(b.get("hazard")), b["by"])
            return self.send(200 if ok_ else 404, {"ok": ok_})
        if p == "/api/envelope_cancel":   # "start over" after a jam: drop the stuck job so it can't print twice
            envelopes.cancel(str(b.get("job") or ""))
            ilog({"src": "envelope", "kid": b.get("kid"), "cancel": b.get("job")})
            return self.send(200, {"ok": True})
        if p == "/api/envelope_already":   # recover a letter workflow when the physical envelope already exists
            if b.get("kid") not in kids or not envelopes.enabled():
                return self.send(400, {"error": "no envelopes here"})
            act_ = st["active"].get(b["kid"]) or {}
            team_ = set(act_.get("team") or [b["kid"]]) & kids
            envelopes.mark_printed(team_ or {b["kid"]}, used=True)
            ilog({"src": "envelope", "kid": b["kid"], "action": "already_printed"})
            return self.send(200, {"ok": True, "state": derive()})
        if p == "/api/envelope":   # a kid prints the envelope for their letter (not a ledger event)
            if b.get("kid") not in kids or not envelopes.enabled():
                return self.send(400, {"error": "no envelopes here"})
            try:
                job = envelopes.print_envelope(str(b.get("from") or ""), str(b.get("to") or ""))
            except Exception as ex:  # noqa: BLE001
                ilog({"src": "envelope", "kid": b.get("kid"), "from": b.get("from"), "to": b.get("to"), "error": str(ex)})
                return self.send(409, {"error": str(ex)})
            ilog({"src": "envelope", "kid": b.get("kid"), "from": b.get("from"), "to": b.get("to"), "job": job})
            act_ = st["active"].get(b["kid"]) or {}
            envelopes.mark_printed(set([b["kid"], str(b.get("from") or "")] + list(act_.get("team") or [])) & kids,
                                   {"from": b.get("from"), "to": b.get("to"), "job": job, "by": b["kid"]})
            return self.send(200, {"ok": True, "job": job})
        if p == "/api/envelope_delivered":   # the letter is in its envelope and handed over: the grown-up can inspect
            r_ = next((r for r in st["history"] if r.get("type") == "report" and r.get("status") == "pending"
                       and not r.get("delivered") and r.get("chore") == b.get("chore")
                       and b.get("kid") in (r.get("team") or [r["kid"]])), None)
            if not r_:
                return self.send(409, {"error": "nothing waiting"})
            ev = append({"type": "delivered", "ref": r_["id"], "kid": b["kid"]})
            after_change()
            ilog({"src": "api", "ip": display_ip_, "path": p, "event": ev})
            return self.send(200, {"ok": True, "state": derive()})
        if p == "/api/spend" and b.get("kid") and b.get("reward") in rewards and not is_here(rewards[b["reward"]], c, b["kid"]):
            return self.send(409, {"error": "that reward is at the other house"})
        if p in ("/api/spend", "/api/reward_consent_request") and b.get("kid") in kids and b.get("reward") in rewards:
            reward_block_ = reward_block_state(fresh=True)
            if reward_block_["on"]:
                return self.send(409, {"error": reward_block_["items"][0]["message"],
                                       "reward_block": reward_block_})
            packing_gate_ = packing_reward_gate(st, [b.get("kid")] + list(b.get("with") or []))
            if packing_gate_:
                prefix_ = "" if packing_gate_["kid"] == b.get("kid") else packing_gate_["name"] + ": "
                return self.send(409, {"error": prefix_ + packing_gate_["message"],
                                       "packing_required": packing_gate_})
        if p in ("/api/start", "/api/report", "/api/claim", "/api/team", "/api/resume"):
            kid_ = b.get("kid")
            gate_ = (st.get("must_do_first") or {}).get(kid_) or {}
            chore_ = b.get("chore") or (st.get("paused", {}).get(kid_) or {}).get("chore")
            packing_kids_ = [kid_]
            if p == "/api/team":
                packing_kids_ += list(b.get("partners") or [])
            elif p == "/api/report":
                packing_kids_ += list((st.get("active", {}).get(kid_) or {}).get("team") or [])
            elif p == "/api/resume":
                packing_kids_ += list((st.get("paused", {}).get(kid_) or {}).get("team") or [])
            packing_gate_ = next((gate_ for gate_ in
                                  (packing_chore_gate(st, who_, chore_) for who_ in dict.fromkeys(packing_kids_))
                                  if gate_), None)
            if packing_gate_:
                prefix_ = "" if packing_gate_["kid"] == kid_ else packing_gate_["name"] + ": "
                return self.send(409, {"error": prefix_ + packing_gate_["message"],
                                       "packing_required": packing_gate_})
            if gate_.get("on") and chore_ != gate_.get("chore") and not (chores.get(chore_) or {}).get("bedtime_ok"):
                return self.send(409, {"error": "%s first! Other chores come back at %s." % (gate_.get("name") or "Priority chore", gate_.get("until") or "the evening routine")})
            cleanup_gate_ = (st.get("chores_first") or {}).get(kid_) or {}
            cleanup_chore_ = cleanup_gate_.get("chore") or ("cleanup_" + str(kid_))
            if cleanup_gate_.get("on") and chore_ != cleanup_chore_ \
                    and not (chores.get(chore_) or {}).get("bedtime_ok"):
                return self.send(409, {"error": "%s first! Other chores come back when it passes inspection." %
                                                (cleanup_gate_.get("name") or "Mandatory room")})
            routine_ = ((st.get("evening_routine") or {}).get(kid_) or {})
            if (st.get("evening_routine_window") or {}).get("on") and routine_.get("due") and chore_ not in routine_.get("items", []):
                names_ = ["take a shower" if x == "shower" else "brush your teeth" for x in routine_["items"]]
                return self.send(409, {"error": "First " + " and ".join(names_) + ". Then chores come back until bedtime."})
            ast_ = ((st.get("adult_first") or {}).get(chore_) or {})
            ak_ = (ast_.get("kids") or {}).get(kid_) or {}
            if (chores.get(chore_) or {}).get("adult_first") and not ak_.get("trained"):
                if ast_.get("snoozed_until", 0) > time.time():
                    return self.send(409, {"error": "Every grown-up is busy. This will come back when one is available."})
                if not ak_.get("approved"):
                    return self.send(409, {"error": "A grown-up needs to accept and help the first time."})
        if p in ("/api/start", "/api/report", "/api/spend", "/api/claim", "/api/team", "/api/resume") and st["bedtime"]["on"] \
                and not (chores.get(b.get("chore") or (st["paused"].get(b.get("kid")) or {}).get("chore")) or {}).get("bedtime_ok"):
            # after bedtime only the configured evening routine; jobs and rewards wait for morning (6 AM)
            return self.send(409, {"error": "bedtime"})
        if p in ("/api/start", "/api/report", "/api/spend", "/api/claim", "/api/team", "/api/resume") and b.get("chore") != "get_dressed" \
                and (st.get("dress") or {}).get("on") and (st["dress"]["kids"].get(b.get("kid")) or "ready") != "ready":
            return self.send(409, {"error": "Put on your clothes first!"})
        if p == "/api/spend" and reward_closed(rewards.get(b.get("reward"))):
            return self.send(409, {"error": "%s is not available after %s." %
                                   (rewards[b["reward"]]["name"], rewards[b["reward"]]["before"])})
        if p == "/api/spend" and any(((st.get("must_do_first") or {}).get(k_) or {}).get("rewards") for k_ in [b.get("kid")] + list(b.get("with") or [])):
            k_ = next(k_ for k_ in [b.get("kid")] + list(b.get("with") or []) if ((st.get("must_do_first") or {}).get(k_) or {}).get("rewards"))
            gate_ = st["must_do_first"][k_]
            nm_ = next((x["name"] for x in c["kids"] if x["id"] == k_), str(k_))
            return self.send(409, {"error": ("" if k_ == b.get("kid") else nm_ + ": ") + (gate_.get("name") or "Priority chore") + " first! Rewards come back when it's done."})
        if p == "/api/spend" and any(((st.get("chores_first") or {}).get(k_) or {}).get("on") for k_ in [b.get("kid")] + list(b.get("with") or [])):
            k_ = next(k_ for k_ in [b.get("kid")] + list(b.get("with") or []) if ((st.get("chores_first") or {}).get(k_) or {}).get("on"))
            nm_ = next((x["name"] for x in c["kids"] if x["id"] == k_), str(k_))
            first_ = st["chores_first"][k_]
            msg_ = ((first_.get("name") or "Mandatory room") + " first! Other chores and rewards come back when it passes inspection.") \
                if first_.get("chore") else "Chores first! Tidy up, then rewards come back."
            return self.send(409, {"error": ("" if k_ == b.get("kid") else nm_ + ": ") + msg_})
        if p == "/api/log":
            evs = b if isinstance(b, list) else [b]
            opened = {}
            for e in evs[:50]:
                if isinstance(e, dict):
                    ilog({"src": "ui", "ip": display_ip_, **{k: e[k] for k in list(e)[:20]}})
                    if e.get("ev") in ("open_kid", "tap") and (e.get("to") or e.get("kid")):
                        opened[e.get("to") or e.get("kid")] = time.time()   # last time this kid used a screen
            if opened:
                try:
                    ko = json.loads(KID_OPEN.read_text())
                except (OSError, ValueError):
                    ko = {}
                ko.update(opened)
                KID_OPEN.write_text(json.dumps(ko))
            return self.send(200, {"ok": True})
        if p == "/api/ann_seen":
            try:
                seen = json.loads(NEWS_SEEN.read_text())
            except (OSError, ValueError):
                seen = {}
            k = ("ann:kid:" + b["kid"]) if b.get("kid") in kids else ("ann:" + display_ip_)
            seen[k] = (seen.get(k, []) + [str(i) for i in (b.get("ids") or [])])[-200:]
            NEWS_SEEN.write_text(json.dumps(seen))
            ilog({"src": "ui", "ip": display_ip_, "ev": "announce_dismissed", "ids": b.get("ids")})
            return self.send(200, {"ok": True})
        if p == "/api/assist_request":
            kid_, cid_ = b.get("kid"), b.get("chore")
            ch_ = chores.get(cid_) or {}
            ast_ = ((st.get("adult_first") or {}).get(cid_) or {})
            ak_ = (ast_.get("kids") or {}).get(kid_) or {}
            if kid_ not in kids or not ch_.get("adult_first"):
                return self.send(400, {"error": "unknown first-time job"})
            if ak_.get("trained") or ak_.get("approved"):
                return self.send(200, {"ok": True, "approved": True, "state": st})
            if ast_.get("snoozed_until", 0) > time.time():
                return self.send(409, {"error": "Every grown-up is busy. Adult-help requests come back when one is available.",
                                       "until": ast_["snoozed_until"]})
            # Reuse the pending invitation from a double tap instead of buzzing twice.
            old_ = next((n for n, r in ADULT_REQ.items() if r.get("purpose") == "assist"
                         and r.get("status") == "pending" and r.get("kid") == kid_
                         and r.get("chore") == cid_ and time.time() - r["ts"] < ADULT_REQUEST_TTL_S), None)
            nonce = old_ or uuid.uuid4().hex[:8]
            if not old_:
                target_ = available_request_adult(st)
                if not target_:
                    return self.send(409, {"error": "Every grown-up is busy right now.",
                                           "until": st.get("adult_busy_until", 0)})
                ADULT_REQ[nonce] = {"who": target_, "ip": display_ip_, "ts": time.time(),
                                    "status": "pending", "purpose": "assist", "kid": kid_, "chore": cid_,
                                    "last_notify": time.time()}
                save_adult_requests()
                threading.Thread(target=send_adult_request, args=(nonce, target_, "chore screen"), daemon=True).start()
                ilog({"src": "adult", "action": "assist_request", "kid": kid_, "chore": cid_})
            elif time.time() - ADULT_REQ[nonce].get("last_notify", 0) > 10:
                ADULT_REQ[nonce]["last_notify"] = time.time()
                save_adult_requests()
                threading.Thread(target=send_adult_request, args=(nonce, ADULT_REQ[nonce]["who"], "chore screen"), daemon=True).start()
            after_change()
            return self.send(200, {"ok": True, "nonce": nonce})
        if p == "/api/reward_consent_request":
            kid_, rid_ = b.get("kid"), b.get("reward")
            rw_ = rewards.get(rid_) or {}
            if kid_ not in kids or not rw_.get("needs_ok"):
                return self.send(400, {"error": "unknown adult-required reward"})
            if not is_here(rw_, c, kid_):
                return self.send(409, {"error": "that reward is at the other house"})
            if st["bedtime"]["on"]:
                return self.send(409, {"error": "bedtime"})
            if (st.get("dress") or {}).get("on") and (st["dress"]["kids"].get(kid_) or "ready") != "ready":
                return self.send(409, {"error": "Put on your clothes first!"})
            if reward_closed(rw_):
                return self.send(409, {"error": "%s is not available after %s." % (rw_["name"], rw_["before"])})
            gate_ = (st.get("must_do_first") or {}).get(kid_) or {}
            if gate_.get("rewards"):
                return self.send(409, {"error": (gate_.get("name") or "Priority chore") + " first! Rewards come back when it's done."})
            if ((st.get("chores_first") or {}).get(kid_) or {}).get("on"):
                first_ = st["chores_first"][kid_]
                return self.send(409, {"error": ((first_.get("name") or "Mandatory room") +
                                                  " first! Other chores and rewards come back when it passes inspection.")
                                                 if first_.get("chore") else
                                                 "Chores first! Tidy up, then rewards come back."})
            if st["balance"].get(kid_, 0) < reward_cost(rw_):
                return self.send(409, {"error": "not enough points"})
            ready_ = (st.get("reward_ready", {}).get(kid_) or {}).get(rid_, 0)
            if ready_ > time.time():
                return self.send(409, {"error": "needs a break first", "ready_at": ready_})
            busy_until_ = st.get("adult_busy_until", 0)
            if busy_until_ > time.time():
                return self.send(409, {"error": "Every grown-up is busy. Adult-help requests come back when one is available.",
                                       "until": busy_until_})
            # A page reload must not throw away a recent yes. Return the same
            # unused approval so the child can finish the purchase.
            approved_ = next((n for n, r in ADULT_REQ.items() if r.get("purpose") == "reward"
                              and r.get("status") == "ok" and not r.get("used") and r.get("kid") == kid_
                              and r.get("reward") == rid_ and time.time() - r["ts"] < 300), None)
            if approved_:
                return self.send(200, {"ok": True, "nonce": approved_})
            old_ = next((n for n, r in ADULT_REQ.items() if r.get("purpose") == "reward"
                         and r.get("status") == "pending" and r.get("kid") == kid_
                         and r.get("reward") == rid_ and time.time() - r["ts"] < ADULT_REQUEST_TTL_S), None)
            nonce = old_ or uuid.uuid4().hex[:8]
            if not old_:
                target_ = available_request_adult(st)
                if not target_:
                    return self.send(409, {"error": "Every grown-up is busy right now.",
                                           "until": st.get("adult_busy_until", 0)})
                ADULT_REQ[nonce] = {"who": target_, "ip": display_ip_, "ts": time.time(),
                                    "status": "pending", "purpose": "reward", "kid": kid_, "reward": rid_,
                                    "last_notify": time.time()}
                save_adult_requests()
                threading.Thread(target=send_adult_request, args=(nonce, target_, "chore screen"), daemon=True).start()
                ilog({"src": "adult", "action": "reward_request", "kid": kid_, "reward": rid_})
            elif time.time() - ADULT_REQ[nonce].get("last_notify", 0) > 10:
                ADULT_REQ[nonce]["last_notify"] = time.time()
                save_adult_requests()
                threading.Thread(target=send_adult_request, args=(nonce, ADULT_REQ[nonce]["who"], "chore screen"), daemon=True).start()
            after_change()
            return self.send(200, {"ok": True, "nonce": nonce})
        if p == "/api/adult_request_cancel":
            nonce_ = str(b.get("nonce") or "")
            r_ = ADULT_REQ.get(nonce_)
            if not r_ or r_.get("purpose") not in ("assist", "reward") or r_.get("status") != "pending" \
                    or r_.get("kid") != b.get("kid"):
                return self.send(409, {"error": "that request is no longer waiting"})
            r_["status"] = "canceled"
            save_adult_requests()
            after_change()
            threading.Thread(target=clear_adult_request, args=(nonce_, r_["who"]), daemon=True).start()
            ilog({"src": "adult", "action": "canceled", "purpose": r_["purpose"], "kid": r_["kid"]})
            return self.send(200, {"ok": True, "state": derive()})
        if p == "/api/assist_answer":
            if b.get("by") not in insp:
                return self.send(400, {"error": "unknown grown-up"})
            r_ = resolve_assist_request(str(b.get("nonce") or ""), bool(b.get("ok")), b["by"])
            if not r_:
                return self.send(409, {"error": "that invitation is no longer waiting"})
            return self.send(200, {"ok": True, "status": r_["status"], "until": r_.get("until"), "state": derive()})
        if p == "/api/adult_request":
            who = b.get("who") or LEAD_ADULT
            if who not in ADULT_NOTIFY:
                return self.send(400, {"error": "unknown grown-up"})
            nonce = uuid.uuid4().hex[:8]
            where = next((h.get("name") or k_.replace("_", " ").title() + " Hub" for k_, h in HUBS.items()
                          if h.get("ip") == display_ip_), "chore screen")
            if b.get("purpose") == "lock":
                where += " (to lock a TV)"
            ADULT_REQ[nonce] = {"who": who, "ip": display_ip_, "ts": time.time(), "status": "pending",
                                "purpose": "lock" if b.get("purpose") == "lock" else "login"}
            save_adult_requests()
            ilog({"src": "adult", "action": "request", "who": who, "ip": display_ip_})
            threading.Thread(target=send_adult_request, args=(nonce, who, where), daemon=True).start()
            return self.send(200, {"ok": True, "nonce": nonce})
        if p == "/api/adult_status":
            r = ADULT_REQ.get(b.get("nonce"))
            if not r:
                return self.send(404, {"error": "unknown request"})
            timed_out_ = False
            ttl_ = ADULT_REQUEST_TTL_S if r.get("purpose") in ("assist", "reward", "pin_reset") else 120
            if r["status"] == "pending" and time.time() - r["ts"] > ttl_:
                r["status"] = "timeout"
                timed_out_ = True
            if r["status"] == "ok" and r.get("purpose") in ("login", "lock"):
                r["used"] = True
            save_adult_requests()
            if timed_out_:
                after_change()
            if r["status"] != "pending":
                threading.Thread(target=clear_adult_request, args=(b["nonce"], r["who"]), daemon=True).start()
            return self.send(200, {"ok": True, "status": r["status"],
                                   **({"state": derive(), "until": r.get("until")}
                                      if r.get("purpose") in ("assist", "reward", "pin_reset") else {})})
        if p == "/api/news_seen":
            try:
                seen = json.loads(NEWS_SEEN.read_text())
            except (OSError, ValueError):
                seen = {}
            seen[("kid:" + b["kid"]) if b.get("kid") in kids else display_ip_] = str(b.get("v"))
            NEWS_SEEN.write_text(json.dumps(seen))
            ilog({"src": "ui", "ip": display_ip_, "ev": "news_dismissed", "v": b.get("v")})
            return self.send(200, {"ok": True})
        if p == "/api/tv/lock_now":
            GUARD["grace"] = {}
            GUARD["last_cast"] = {}
            return self.send(200, {"ok": True})
        if p == "/api/exit":
            ip = display_ip_
            hid_ = next((k for k, h in HUBS.items() if h["ip"] == ip), None)
            if hid_ or ip in [h.get("ip") for h in (HOUSE.get("kiosks") or {}).values()]:   # only screens we cast to
                if hid_:   # back to normal Google Home: the keeper leaves this Hub alone for a while
                    GUARD.setdefault("hub_exit", {})[hid_] = time.time()
                ilog({"src": "hub", "hub": hid_ or ip, "action": "kid_exit"})
                threading.Thread(target=lambda: subprocess.run([CATT, "-d", ip, "stop"], timeout=30,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL), daemon=True).start()
            return self.send(200, {"ok": True})
        if p == "/api/refund":
            evs_ = read_events()
            undone_ = {e["ref"] for e in evs_ if e["type"] == "undo"}
            q_ = next((e for e in evs_ if e["type"] == "spend" and e["id"] == b.get("ref") and e["id"] not in undone_), None)
            if not q_ or q_.get("kid") != b.get("kid") or q_.get("with_buyer"):
                return self.send(400, {"error": "nothing of yours to refund"})
            if time.time() - q_["ts"] > 120:
                return self.send(409, {"error": "the 2-minute refund time has ended"})
            group_ = q_.get("group")
            purchases_ = [e for e in evs_ if e["type"] == "spend" and e["id"] not in undone_ and
                          (e["id"] == q_["id"] or (group_ and e.get("group") == group_))]
            for purchase_ in purchases_:
                ev = append({"type": "undo", "ref": purchase_["id"], "by": "kid:" + b["kid"]})
            after_change()
            ilog({"src": "api", "ip": display_ip_, "path": p, "event": ev, "refund_count": len(purchases_)})
            return self.send(200, {"ok": True, "event": ev, "state": derive()})
        if p == "/api/report":
            if b.get("kid") not in kids or b.get("chore") not in chores:
                return self.send(400, {"error": "unknown kid/chore"})
            bd = st["board"][b["chore"]]
            ch_ = chores[b["chore"]]
            if b.get("partial"):
                act_ = st["active"].get(b["kid"]) or {}
                pz_ = st["paused"].get(b["kid"]) or {}
                started_ = act_.get("ts") if act_.get("chore") == b["chore"] and not act_.get("notimer") else None
                if act_.get("chore") == b["chore"]:
                    append({"type": "stop", "kid": b["kid"], "chore": b["chore"]})
                if pz_.get("chore") == b["chore"]:
                    append({"type": "release", "kid": b["kid"], "by": "kid"})
                ttl_ = (act_ if act_.get("chore") == b["chore"] else pz_ if pz_.get("chore") == b["chore"] else {}).get("title")
                ev = append({"type": "report", "kid": b["kid"], "chore": b["chore"], "points": None, "partial": True,
                             "feeling": b.get("feeling"), "full_price": bd.get("price"), **({"note": ttl_} if ttl_ else {}),
                             **({"worked": time.time() - started_} if started_ else {})})
                after_change()
                ilog({"src": "api", "ip": display_ip_, "path": p, "event": ev})
                threading.Thread(target=notify_report, args=(ev, derive()), daemon=True).start()
                return self.send(200, {"ok": True, "event": ev, "state": derive()})
            if ch_.get("only") and b["kid"] not in ch_["only"]:
                return self.send(400, {"error": "not your chore"})
            if ch_.get("each_daily") and b["kid"] in (bd.get("done_today") or []):
                return self.send(409, {"error": "already done today"})
            if (ch_.get("after") or ch_.get("before")) and chore_closed(ch_, st["board"]) in ("later", "over"):
                return self.send(409, {"error": "not now: %s-%s" % (ch_.get("after") or "", ch_.get("before") or "")})
            if ch_.get("slot"):
                if bd.get("claim_done"):
                    return self.send(409, {"error": "already done this week"})
                if bd.get("claimed_by") and bd["claimed_by"] != b["kid"]:
                    return self.send(409, {"error": "%s claimed it" % bd["claimed_by"]})
                if time.time() < bd["slot"] - 1800:
                    return self.send(409, {"error": "not until shortly before the slot"})
            if ch_.get("bundle"):
                if bd["pending_by"]:
                    return self.send(409, {"error": "already waiting for inspection"})
                if not bd["covers"]:
                    return self.send(409, {"error": "already tidy"})
            if bd["clean_until"]:
                return self.send(409, {"error": "already tidy"})
            if bd["pending_by"] and chores[b["chore"]].get("room"):
                return self.send(409, {"error": "already waiting for inspection"})
            if b["chore"] != "other" and b["kid"] in (bd.get("pending_by") or []) and not ch_.get("multi_pending"):
                # Most chores can wait only once. Document intake deliberately allows one inspection row per paper.
                return self.send(409, {"error": "already waiting for inspection"})
            if ch_.get("cleanup_for"):
                required = ((st.get("chores_first") or {}).get(b["kid"]) or {}).get("items") or []
                checked = b.get("checklist") if isinstance(b.get("checklist"), list) else []
                if required and checked != required:
                    return self.send(409, {"error": "check each clean-up item first"})
            extra = {}
            if b["chore"] == "toy_swap":
                kc = next(k for k in c["kids"] if k["id"] == b["kid"])
                if not kc.get("toy_swap"):
                    return self.send(400, {"error": "no toy swap for this kid"})
                extra = {"toy": "surprise", "old_toy": str(b.get("old_toy") or "other")[:20],
                         "old_toy_name": str(b.get("old_toy_name") or "")[:40]}
            if b.get("note"):
                extra["note"] = " ".join(str(b["note"]).split())[:40]
            if ch_.get("document_review") and b.get("document_url"):
                parsed_ = urllib.parse.urlparse(str(b["document_url"]))
                if parsed_.scheme == "https" and parsed_.netloc == "drive.google.com" and parsed_.path.startswith("/file/d/"):
                    extra["document_url"] = urllib.parse.urlunparse((parsed_.scheme, parsed_.netloc, parsed_.path, "", "", ""))[:300]
                    extra["document_title"] = " ".join(str(b.get("document_title") or b.get("note") or "Scanned paper").split())[:100]
            named = (st["active"].get(b["kid"]) or {}) if (st["active"].get(b["kid"]) or {}).get("chore") == b["chore"] else \
                (st["paused"].get(b["kid"]) or {}) if (st["paused"].get(b["kid"]) or {}).get("chore") == b["chore"] else {}
            if named.get("title") and not extra.get("note"):
                extra["note"] = named["title"]   # the kid's name for it ("organizing rocks") goes to the grown-up
            if b.get("delivered"):
                extra["delivered"] = time.time()   # a letter already handed over when it was reported
            if b.get("ask") is not None and (b["chore"] == "toy_out" or ch_.get("propose")):
                extra["ask"] = max(0, min(20, int(b.get("ask") or 0)))
            if b["chore"] == "toy_out":
                extra["old_toy"] = str(b.get("old_toy") or "other")[:20]
            if ch_.get("bundle"):
                extra["covers"] = bd["covers"]
            if ch_.get("help") == "rescue":   # only a main-brush rescue may include separately inspected room tidying
                op = next((e for e in reversed(read_events()) if e["type"] == "reopen" and e.get("chore") == b["chore"]), {})
                sp = [x for x in HOUSE.get("tidy_spaces") or [] if x["id"] in (op.get("spaces") or [])]
                if op.get("tidy_credit") and sp:
                    extra["partial_of"] = vacuum_help.partial_of(sp)
                    extra["rescue_base"] = bd["price"]
            if b["chore"] != "other" and not ch_.get("shared") and (any(a["chore"] == b["chore"] and b["kid"] not in (a.get("team") or [k])
                                              for k, a in st["active"].items()) or
                                          any(z["chore"] == b["chore"] and b["kid"] not in z["team"] for z in st["paused"].values())):
                return self.send(409, {"error": "someone else is doing it"})
            act = st["active"].get(b["kid"])
            if not act or act["chore"] != b["chore"]:   # moved on to another job but still on this one's team (a shared letter)
                act = next((a for a in st["active"].values() if a["chore"] == b["chore"] and b["kid"] in (a.get("team") or [])), act)
            if not act or act["chore"] != b["chore"]:   # finished a team job that was put off for later: the team still shares it
                pz = next((z for z in st["paused"].values() if z["chore"] == b["chore"] and b["kid"] in (z.get("team") or [])), None)
                if pz:
                    act = dict(pz, notimer=True)
            if act and act["chore"] == b["chore"]:
                if act.get("round"):
                    extra["round"] = act["round"]
                    extra["prev"] = (act.get("rework") or {}).get("ref")
                if not act.get("notimer"):
                    extra["started"] = act["ts"]
                if len(act.get("team") or []) > 1:
                    extra["team"] = [b["kid"]] + [m for m in act["team"] if m != b["kid"]]
                    if act.get("split"):
                        extra["split"] = act["split"]
            if b["chore"] == "get_dressed" and kid_uses_timer(c, b["kid"], b["chore"]) \
                    and (st.get("dress") or {}).get("on") and not st["dress"].get("school"):
                # A manually enabled dress race is elapsed time. School mornings are judged by the report's absolute
                # finish time, so they deliberately do not manufacture a race start.
                extra["started"] = st["dress"]["since"]
            if ch_.get("together") and not set(ch_.get("only") or []) <= set(extra.get("team") or [b["kid"]]):
                return self.send(409, {"error": "do it together: team up first"})
            opened_ = next((event_ for event_ in reversed(read_events()) if event_.get("type") == "reopen"
                            and event_.get("chore") == b["chore"]), {})
            physical_ = vacuum_help.report_verification(
                HOUSE.get("vacuum_help") or {}, b["chore"],
                extra.get("started") or (act or {}).get("ts") or opened_.get("ts") or time.time(),
                lambda entity_: ha_get(entity_, ha_token()) or {})
            if physical_ and physical_.get("error"):
                return self.send(409, {"error": physical_["error"]})
            if physical_:
                extra["auto_verified"] = physical_["reason"]
            ev = append({"type": "report", "kid": b["kid"], "chore": b["chore"],
                         "feeling": b.get("feeling"), "points": bd["price"], **extra})
            if physical_:
                append({"type": "inspect", "ts": physical_["at"], "ref": ev["id"], "by": "vacuum",
                        "ok": True, "points": int(bd["price"] or 0), "label": "", "trusted": True})
            elif ch_.get("trust") and not extra.get("partial"):   # a trusted job: the kid's word is the inspection
                append({"type": "inspect", "ref": ev["id"], "by": "vacuum" if ch_.get("help_robot") else LEAD_ADULT,
                        "ok": True, "points": int(bd["price"] or 0), "label": "", "trusted": True})
        elif p == "/api/spend":
            if b.get("kid") not in kids or b.get("reward") not in rewards:
                return self.send(400, {"error": "unknown kid/reward"})
            # "Watching together": everyone watching pays, and they share one clock
            with_ = [k for k in dict.fromkeys(b.get("with") or []) if k in kids and k != b["kid"]] \
                if b["reward"] in ("show", "movie", "games") else []
            participants_ = [b["kid"]] + with_
            if with_ and not valid_shared_reward_sessions(b, participants_, display_ip_):
                return self.send(403, {"error": "Everyone watching or playing together must enter their own PIN."})
            if with_ and any(not is_here(rewards[b["reward"]], c, wk) for wk in participants_):
                return self.send(409, {"error": "Everyone sharing this reward must be at this house."})
            if with_ and st.get("present_kids") and any(wk not in st["present_kids"] for wk in participants_):
                return self.send(409, {"error": "Everyone sharing this reward must be home."})
            game_mode_ = str(b.get("game_mode") or "") if b["reward"] == "games" and with_ else ""
            game_player_ = str(b.get("game_player") or "") if game_mode_ == "player_watch" else ""
            try:
                costs_ = shared_reward_costs(rewards[b["reward"]], participants_, game_mode_, game_player_)
            except ValueError as ex:
                return self.send(400, {"error": str(ex)})
            kname = {k["id"]: k["name"] for k in c["kids"]}
            for wk in [b["kid"]] + with_:
                who_ = "" if wk == b["kid"] else kname.get(wk, wk) + ": "
                if st["balance"][wk] < costs_[wk]:
                    return self.send(409, {"error": who_ + "not enough points"})
                ready = st["reward_ready"].get(wk, {}).get(b["reward"], 0)
                if ready > time.time():
                    return self.send(409, {"error": who_ + ("screen break" if b["reward"] in ("show", "movie", "games") and st["screen_break"].get(wk) else "needs a break first"), "ready_at": ready})
            if rewards[b["reward"]].get("needs_ok"):   # e.g. VR: adult consent first; accepted purchase starts its timer
                consent_ = ADULT_REQ.get(str(b.get("consent") or ""))
                if not consent_ or consent_.get("purpose") != "reward" or consent_.get("kid") != b["kid"] \
                        or consent_.get("reward") != b["reward"] or consent_.get("status") != "ok" or consent_.get("used"):
                    return self.send(409, {"error": "A grown-up needs to allow this first."})
                consent_["used"] = True
                save_adult_requests()
            tvx = {}
            setup_tv_ = rewards[b["reward"]].get("setup_tv")
            if setup_tv_ in TVS:
                setup_min_ = max(1, min(60, int(rewards[b["reward"]].get("setup_tv_minutes") or 15)))
                # The countdown describes how long the display and TV stay
                # available; the grown-up handles the actual VR-time call.
                tvx = {"tv": setup_tv_, "until": time.time() + setup_min_ * 60,
                       "display_minutes": setup_min_}
            if b["reward"] in UNLOCKS and (b.get("tv") in TVS or b.get("tv") == "*"):
                tm = rewards[b["reward"]].get("tv_minutes") or UNLOCKS[b["reward"]][0]
                tvx = {"tv": b["tv"], "until": time.time() + tm * 60, "minutes": tm}
            selected_tv_ = tvx.get("tv") or b.get("tv")
            queued = bool(tvx) and any(u["what"] != "vr" for tv_ in ([selected_tv_] if selected_tv_ in TVS else list(TVS))
                                       for u in active_unlocks().get(tv_, []))
            grp = {"group": uuid.uuid4().hex[:12], "together": participants_,
                   **({"game_mode": game_mode_, "game_player": game_player_ or None} if game_mode_ else {})} if with_ else {}
            ev = append({"type": "spend", "kid": b["kid"], "reward": b["reward"], "cost": costs_[b["kid"]], **tvx, **grp,
                         "reward_name": rewards[b["reward"]]["name"],
                         "reward_icon": rewards[b["reward"]]["icon"]})
            for wk in with_:
                append({"type": "spend", "kid": wk, "reward": b["reward"], "cost": costs_[wk], **tvx, **grp, "with_buyer": b["kid"],
                        "reward_name": rewards[b["reward"]]["name"], "reward_icon": rewards[b["reward"]]["icon"]})
            ev = dict(ev, queued=queued, cost_by_kid=costs_)
        elif p == "/api/reward_join":
            joiner_ = b.get("kid")
            if joiner_ not in kids:
                return self.send(400, {"error": "unknown kid"})
            with SHARED_REWARD_LOCK:
                # Re-read inside the join lock so two simultaneous taps cannot both replace the same session.
                raw_ = read_events()
                live_ = [(tid_, u_) for tid_, rows_ in active_unlocks_raw(raw_).items() for u_ in rows_
                         if (u_.get("group") or u_["id"]) == b.get("session_id")]
                if not live_:
                    return self.send(409, {"error": "That reward is no longer playing."})
                tids_ = {tid_ for tid_, _ in live_}
                reward_ids_ = {u_["what"] for _, u_ in live_}
                if len(tids_) != 1 or len(reward_ids_) != 1 or not reward_ids_ <= {"show", "movie", "games"}:
                    return self.send(409, {"error": "That reward cannot be joined."})
                if any(u_.get("armed") for _, u_ in live_):
                    return self.send(409, {"error": "Wait until it starts playing, then join."})
                current_ = list(dict.fromkeys(u_["kid"] for _, u_ in live_))
                if joiner_ in current_:
                    return self.send(409, {"error": "You are already sharing this reward."})
                participants_ = current_ + [joiner_]
                if not valid_shared_reward_sessions(b, participants_, display_ip_):
                    return self.send(403, {"error": "Everyone sharing must enter their own PIN."})
                reward_id_ = next(iter(reward_ids_))
                reward_ = rewards.get(reward_id_)
                fresh_ = derive()
                if not reward_ or not is_here(reward_, c, joiner_):
                    return self.send(409, {"error": "That reward is at the other house."})
                if fresh_.get("present_kids") and joiner_ not in fresh_["present_kids"]:
                    return self.send(409, {"error": "You must be home to join."})
                reward_block_ = reward_block_state(fresh=True)
                if reward_block_["on"]:
                    return self.send(409, {"error": reward_block_["items"][0]["message"]})
                packing_gate_ = packing_reward_gate(fresh_, [joiner_])
                if packing_gate_:
                    return self.send(409, {"error": "Finish packing before joining. You can still do other chores."})
                if fresh_["bedtime"]["on"] or reward_closed(reward_):
                    return self.send(409, {"error": "That reward is not available now."})
                if (fresh_.get("dress") or {}).get("on") and (fresh_["dress"]["kids"].get(joiner_) or "ready") != "ready":
                    return self.send(409, {"error": "Put on your clothes first!"})
                if ((fresh_.get("must_do_first") or {}).get(joiner_) or {}).get("rewards"):
                    return self.send(409, {"error": "Finish your mandatory chore before joining."})
                if ((fresh_.get("chores_first") or {}).get(joiner_) or {}).get("on"):
                    return self.send(409, {"error": "Finish your chores-first job before joining."})
                ready_ = (fresh_.get("reward_ready") or {}).get(joiner_, {}).get(reward_id_, 0)
                if ready_ > time.time():
                    return self.send(409, {"error": "You need a screen break before joining.", "ready_at": ready_})

                spend_by_id_ = {e_["id"]: e_ for e_ in raw_ if e_.get("type") == "spend"}
                old_spends_ = [spend_by_id_[u_["id"]] for _, u_ in live_ if u_["id"] in spend_by_id_]
                old_spends_ = list({e_["id"]: e_ for e_ in old_spends_}.values())
                if len(old_spends_) != len(current_):
                    return self.send(409, {"error": "That reward changed. Refresh and try again."})
                old_costs_ = {e_["kid"]: int(e_["cost"]) for e_ in old_spends_}
                started_at_ = min(u_.get("start") or u_["ts"] for _, u_ in live_)
                until_ = min(u_["until"] for _, u_ in live_)
                tv_ = next(iter(tids_))
                if reward_id_ == "games" and reward_.get("watcher_cost") is not None:
                    try:
                        join_cost_ = int(reward_["watcher_cost"])
                    except (TypeError, ValueError):
                        return self.send(409, {"error": "Watcher pricing is unavailable."})
                    if join_cost_ < 0 or fresh_["balance"].get(joiner_, 0) < join_cost_:
                        return self.send(409, {"error": "You do not have enough points to watch."})
                    game_mode_ = next((e_.get("game_mode") for e_ in old_spends_ if e_.get("game_mode")), None)
                    game_player_ = next((e_.get("game_player") for e_ in old_spends_ if e_.get("game_player")), None)
                    new_group_ = str(b.get("session_id") or "")
                    primary_ = current_[0]
                    ev = append({"type": "spend", "kid": joiner_, "reward": reward_id_, "cost": join_cost_,
                                 "tv": tv_, "until": until_, "minutes": reward_.get("tv_minutes"),
                                 "started_at": started_at_, "group": new_group_, "together": participants_,
                                 "by": "share_join", "with_buyer": primary_, "game_mode": game_mode_,
                                 "game_player": game_player_, "game_join_role": "watcher",
                                 "reward_name": reward_["name"], "reward_icon": reward_["icon"]})
                    costs_now_ = dict(old_costs_, **{joiner_: join_cost_})
                    after_change()
                    ilog({"src": "api", "ip": display_ip_, "path": p, "event": ev,
                          "session": b.get("session_id"), "participants": participants_,
                          "join_cost": join_cost_, "price_source": "game_watcher"})
                    return self.send(200, {"ok": True, "event": ev, "state": derive(),
                                           "refunds": {wk_: 0 for wk_ in current_},
                                           "join_cost": join_cost_, "cost_by_kid": costs_now_,
                                           "price_source": "game_watcher", "valid_until": until_,
                                           "participants": participants_})
                price_ = reward_join_price(reward_, len(participants_), started_at_, until_, tv_,
                                           str(b.get("session_id") or ""))
                shared_cost_ = price_["full_cost"]
                target_costs_ = {wk_: min(old_costs_[wk_], shared_cost_) for wk_ in current_}
                target_costs_[joiner_] = price_["cost"]
                for wk_ in participants_:
                    adjustment_ = target_costs_[wk_] - old_costs_.get(wk_, 0)
                    if fresh_["balance"].get(wk_, 0) < adjustment_:
                        name_ = next((x_["name"] for x_ in c["kids"] if x_["id"] == wk_), wk_)
                        return self.send(409, {"error": name_ + " does not have enough points to share."})

                new_group_ = str(b.get("session_id") or "")
                primary_ = current_[0]
                for old_ in old_spends_:
                    append({"type": "undo", "ref": old_["id"], "by": "share_join"})
                made_ = []
                for wk_ in participants_:
                    made_.append(append({"type": "spend", "kid": wk_, "reward": reward_id_, "cost": target_costs_[wk_],
                                         "tv": tv_, "until": until_, "minutes": reward_.get("tv_minutes"),
                                         "started_at": started_at_, "group": new_group_, "together": participants_,
                                         "by": "share_join", **({"with_buyer": primary_} if wk_ != primary_ else {}),
                                         **({"join_prorated": True, "price_fraction": price_["fraction"],
                                             "price_source": price_["source"]} if wk_ == joiner_ else {}),
                                         "reward_name": reward_["name"], "reward_icon": reward_["icon"]}))
                refunds_ = {wk_: max(0, old_costs_.get(wk_, 0) - target_costs_[wk_]) for wk_ in current_}
                ev = made_[-1]
                after_change()
                ilog({"src": "api", "ip": display_ip_, "path": p, "event": ev,
                      "session": b.get("session_id"), "participants": participants_,
                      "join_cost": price_["cost"], "price_source": price_["source"]})
                return self.send(200, {"ok": True, "event": ev, "state": derive(), "refunds": refunds_,
                                       "cost_each": shared_cost_, "join_cost": price_["cost"],
                                       "cost_by_kid": target_costs_, "price_source": price_["source"],
                                       "remaining_fraction": price_["fraction"],
                                       "valid_until": until_, "participants": participants_})
        elif p == "/api/inspect":
            reps = {r["id"]: r for r in st["history"] if r.get("type") == "report"}
            r = reps.get(b.get("ref"))
            if not r or b.get("by") not in insp:
                return self.send(400, {"error": "unknown report/inspector"})
            if r["status"] != "pending":
                return self.send(409, {"error": "already inspected"})
            ok = bool(b.get("ok", True)) and not b.get("prank")
            if b.get("prank"):   # asked for an inspection with nothing done: no, it's not handed back, and it costs 1 star
                b["feedback"] = str(b.get("feedback") or "Adding fake chores is against the rules! Nothing was done, so it costs 1 star.")
            pts = b.get("points", r.get("points"))
            if ok and isinstance(b.get("award"), dict):   # a grown-up decides who did it and what each kid gets
                award = {k: max(0, min(50, int(v or 0))) for k, v in b["award"].items() if k in kids}
                order = [r["kid"]] + [k["id"] for k in c["kids"] if k["id"] != r["kid"]]   # the reporter first
                team = [k for k in order if award.get(k)] or [r["kid"]]
                append({"type": "reteam", "ref": r["id"], "team": team, "by": b["by"]})
                append({"type": "split", "ref": r["id"], "split": {k: award.get(k, 0) for k in team}, "by": b["by"]})
                pts = sum(award.get(k, 0) for k in team)
            if ok and (pts is None or int(pts) < 0 or int(pts) > 50):
                return self.send(400, {"error": "points required"})
            extra = {}
            if r["chore"] == "toy_swap" and ok:
                left = st["toys"].get(r["kid"], {}).get("left", [])
                if not left:
                    return self.send(409, {"error": "no presents left"})
                import random
                extra = {"present": random.choice(left)}
                pts = 0
            if ok and b.get("leftover_points") is not None:
                extra["partial_pass"] = True
                extra["leftover_note"] = str(b.get("leftover_note") or "")[:600]
            ev = append({"type": "inspect", "ref": r["id"], "by": b["by"], "ok": ok,
                         "points": int(pts) if ok else 0, "label": str(b.get("label") or "")[:60], **extra,
                         **({"feedback": str(b.get("feedback") or "")[:600]} if not ok else {}),
                         **({"prank": True} if b.get("prank") else {}),
                         **({"keep": True} if not ok and b.get("keep") else {})})
            if r["chore"] == "get_dressed" and ok:
                d_ = derive().get("dress") or {}
                if d_.get("on") and all(v == "ready" for v in d_["kids"].values()):
                    append({"type": "dress", "on": False, "kids": [], "by": "auto", "why": "everyone dressed"})
            if ok and b.get("leftover_points") is not None and r["chore"] not in ("toy_swap", "other"):
                append({"type": "price", "chore": r["chore"], "points": max(0, min(50, int(b["leftover_points"]))),
                        "rest": True, "note": str(b.get("leftover_note") or "")[:600], "by": b["by"], "ref": r["id"]})
            if b.get("prank"):
                append({"type": "adjust", "kid": r["kid"], "points": -1, "by": b["by"], "icon": "1f6ab", "ref": r["id"],
                        "reason": "Inspection for nothing: " + str(r.get("chore_name") or (chores.get(r["chore"]) or {}).get("name", r["chore"]))})
            if not ok and not b.get("prank") and not r.get("partial") and r["chore"] not in ("toy_swap", "other", "get_dressed"):
                # hand the chore back: the kid's timer resumes from the work time so far (waiting/inspection excluded)
                worked = (r["ts"] - r["started"]) if r.get("started") else 0
                append({"type": "rework", "ref": r["id"], "chore": r["chore"], "team": r.get("team") or [r["kid"]],
                        "worked": worked, "timed": bool(r.get("started")), "feedback": str(b.get("feedback") or "")[:600],
                        "by": b["by"], "round": int(r.get("round") or 1) + (0 if b.get("keep") else 1), "split": r.get("split"),
                        **({"keep": True} if b.get("keep") else {}),
                        **({"title": r["note"]} if r.get("note") and r["chore"] != "other" else {})})
        elif p == "/api/reaward":   # change who got what for a job already passed (within a day), e.g. 2 and 2
            r = next((h for h in st["history"] if h.get("id") == b.get("ref") and h.get("type") == "report"), None)
            if not r or b.get("by") not in insp or not isinstance(b.get("award"), dict):
                return self.send(400, {"error": "unknown report/inspector"})
            if r["status"] != "inspected" or time.time() - (r.get("inspected_ts") or 0) > 86400:
                return self.send(409, {"error": "only a job passed in the last day"})
            award = {k: max(0, min(50, int(v or 0))) for k, v in b["award"].items() if k in kids}
            order = [r["kid"]] + [k["id"] for k in c["kids"] if k["id"] != r["kid"]]
            team = [k for k in order if award.get(k)] or [r["kid"]]
            ev = append({"type": "reaward", "ref": r["id"], "team": team, "award": {k: award.get(k, 0) for k in team}, "by": b["by"]})
        elif p == "/api/adjust":
            if b.get("kid") not in kids or b.get("by") not in insp:
                return self.send(400, {"error": "unknown kid/inspector"})
            ev = append({"type": "adjust", "kid": b["kid"], "points": int(b["points"]),
                         "reason": str(b.get("reason", ""))[:120], "by": b["by"],
                         "icon": str(b.get("icon") or "")[:20]})
        elif p == "/api/chore":
            if b.get("by") not in insp:
                return self.send(400, {"error": "unknown inspector"})
            ch = b.get("chore") or {}
            name = str(ch.get("name", "")).strip()[:40]
            if not name:
                return self.send(400, {"error": "name required"})
            pts = ch.get("points")
            pts = None if pts in (None, "", "?") else max(0, min(50, int(pts)))
            cid = ch.get("id") or "c_" + uuid.uuid4().hex[:6]
            with LOCK:
                c = cfg()
                cur = [x for x in c["chores"] if x["id"] == cid]
                more = chore_fields(ch, c)
                if b.get("remove"):
                    c["chores"] = [x for x in c["chores"] if x["id"] != cid]
                elif cur:
                    cur[0].update({"name": name, "points": pts, "icon": ch.get("icon") or cur[0]["icon"]})
                    apply_chore_fields(cur[0], more)
                else:
                    c["chores"].insert(-1 if c["chores"] and c["chores"][-1]["id"] == "other" else len(c["chores"]),
                                       apply_chore_fields({"id": cid, "name": name, "icon": ch.get("icon") or "2b50", "points": pts}, more))
                save_cfg(c)
            try:   # Grocy is the master list: mirror the edit there (a removed chore just leaves the kids' screens)
                cur_ = cur[0] if cur else next((x for x in cfg()["chores"] if x["id"] == cid), {})
                gid = cur_.get("grocy_id")
                if b.get("remove"):
                    if gid:
                        gcall("PUT", "/userfields/chores/%d" % gid, {"kids": "0"})
                elif gid:
                    gcall("PUT", "/objects/chores/%d" % gid, {"name": name, **({"description": cur_.get("desc", "")} if "desc" in more else {}),
                                                              **grocy_period(cur_, more)})
                    gcall("PUT", "/userfields/chores/%d" % gid, {"kid_points": "" if pts is None else str(pts), "kid_icon": cur_.get("icon", ""),
                                                                 **grocy_chore_userfields(gid, cur_, more)})
                elif not is_system(cid):
                    gid = int(gcall("POST", "/objects/chores", {"name": name, "description": cur_.get("desc", ""), "period_type": "manually", "track_date_only": 0, "rollover": 0, "start_date": "2026-11-01 00:00:00",
                                                                **grocy_period(cur_, more)})["created_object_id"])
                    gcall("PUT", "/userfields/chores/%d" % gid, {"kids": "1", "kid_key": cid, "kid_points": "" if pts is None else str(pts),
                                                                  "kid_icon": cur_.get("icon", "2b50"), **grocy_chore_userfields(gid, cur_, more, new=True)})
                    with LOCK:   # remember the Grocy id now, so the next pull matches it instead of hiding the local copy
                        c = cfg()
                        for x in c["chores"]:
                            if x["id"] == cid:
                                x["grocy_id"] = gid
                        save_cfg(c)
            except Exception as ex:  # noqa: BLE001
                print("grocy chart push failed", ex, flush=True)
            ev = append({"type": "chart", "by": b["by"], "chore": cid, "name": name, "points": pts,
                         "remove": bool(b.get("remove"))})
        elif p in ("/api/cancel", "/api/priority", "/api/refeel"):
            reps = {r["id"]: r for r in st["history"] if r.get("type") == "report"}
            r = reps.get(b.get("ref"))
            if not r or r["kid"] != b.get("kid"):
                return self.send(400, {"error": "not your job"})
            if r["status"] != "pending":
                return self.send(409, {"error": "already inspected"})
            if p == "/api/cancel":
                ev = append({"type": "undo", "ref": r["id"], "by": "kid:" + r["kid"]})
            elif p == "/api/priority":
                ev = append({"type": "priority", "ref": r["id"], "kid": r["kid"]})
            else:
                ev = append({"type": "refeel", "ref": r["id"], "kid": r["kid"], "feeling": str(b.get("feeling"))[:10]})
        elif p == "/api/team":
            mates = [k for k in (b.get("partners") or []) if k in kids and k != b.get("kid")]
            if b.get("kid") not in kids or not mates:
                return self.send(400, {"error": "pick a helper"})
            act = st["active"].get(b["kid"])
            cid_ = b.get("chore") or (act or {}).get("chore")
            if solo_job(chores.get(cid_)):
                return self.send(409, {"error": "everyone does this one on their own"})
            owner = next((k for k, a in st["active"].items() if a["chore"] == cid_ and a.get("team") and a["team"][0] == k), None)
            if owner and owner != b["kid"]:
                return self.send(409, {"error": "only %s can add helpers" % owner})
            if b.get("chore") in chores and (not act or act["chore"] != b["chore"]):
                append({"type": "start", "kid": b["kid"], "chore": b["chore"]})
                act = {"chore": b["chore"], "team": [b["kid"]]}
            if not act:
                return self.send(400, {"error": "start a chore first"})
            members = list(dict.fromkeys((act.get("team") or [b["kid"]]) + mates))
            ev = append({"type": "team", "chore": act["chore"], "members": members, "by": b["kid"]})
        elif p == "/api/rework":
            reps = {r["id"]: r for r in st["history"] if r.get("type") == "report"}
            r = reps.get(b.get("ref"))
            if not r or b.get("by") not in insp:
                return self.send(400, {"error": "unknown report"})
            worked = (r["ts"] - r["started"]) if r.get("started") else 0
            ev = append({"type": "rework", "ref": r["id"], "chore": r["chore"], "team": r.get("team") or [r["kid"]],
                         "worked": worked, "timed": bool(r.get("started")), "feedback": str(b.get("feedback") or r.get("feedback") or "")[:600],
                         "by": b["by"], "round": int(r.get("round") or 1) + 1, "split": r.get("split")})
        elif p == "/api/inspect_start":
            reps = {r["id"]: r for r in st["history"] if r.get("type") == "report"}
            r = reps.get(b.get("ref"))
            if not r or b.get("by") not in insp or r["status"] != "pending":
                return self.send(400, {"error": "nothing to inspect"})
            if (r.get("inspecting") or {}).get("by") == b["by"]:   # resuming on another device: no second announcement
                return self.send(200, {"ok": True, "resumed": True, "state": st})
            ev = append({"type": "inspect_start", "ref": r["id"], "by": b["by"]})
            threading.Thread(target=notify_inspecting, args=(r, b["by"]), daemon=True).start()
        elif p in ("/api/team_split", "/api/split"):
            sp = {k: max(0, int(v)) for k, v in (b.get("split") or {}).items() if k in kids}
            if p == "/api/team_split":
                act = st["active"].get(b.get("kid"))
                if not act or (act.get("team") or [b["kid"]])[0] != b["kid"] and b.get("by") not in insp:
                    return self.send(409, {"error": "only the kid who started it can share"})
                ev = append({"type": "team_split", "kid": b["kid"], "split": sp, "by": b.get("by") or b["kid"]})
            else:
                if b.get("by") not in insp:
                    return self.send(400, {"error": "grown-ups only"})
                ev = append({"type": "split", "ref": b.get("ref"), "split": sp, "by": b["by"]})
        elif p == "/api/pause":
            act = st["active"].get(b.get("kid"))
            if not act:
                return self.send(400, {"error": "not doing a chore"})
            ev = append({"type": "pause", "kid": b["kid"], "chore": act["chore"], "elapsed": time.time() - act["ts"],
                         "team": act.get("team") or [b["kid"]], "notimer": bool(act.get("notimer")),
                         "hold": "indefinite"})
        elif p == "/api/title":   # a kid names the job they're on (or one they saved for later)
            t = " ".join(str(b.get("title") or "").split())[:40]
            act = st["active"].get(b.get("kid")) or st["paused"].get(b.get("kid"))
            if b.get("kid") not in kids or not act:
                return self.send(400, {"error": "not doing a chore"})
            ev = append({"type": "title", "kid": b["kid"], "chore": act["chore"], "title": t})
        elif p == "/api/keep":   # a grown-up keeps a kid's saved job longer
            saved_ = next((z for z in st.get("paused_all") or [] if z["kid"] == b.get("kid")
                           and (not b.get("chore") or z["chore"] == b["chore"])), None)
            if b.get("by") not in insp or not saved_:
                return self.send(400, {"error": "unknown inspector / nothing saved"})
            if saved_.get("indefinite"):
                return self.send(200, {"ok": True, "already": True, "state": st})
            ev = append({"type": "keep", "kid": b["kid"], "chore": saved_["chore"],
                         "until": float(b.get("until") or time.time() + PAUSE_DAYS * 86400), "by": b["by"]})
        elif p == "/api/resume":
            pz = next((z for z in reversed(st.get("paused_all") or []) if z["kid"] == b.get("kid")
                       and (not b.get("chore") or z["chore"] == b["chore"])
                       and (not b.get("hold_id") or z.get("id") == b["hold_id"])), None)
            if not pz:
                return self.send(400, {"error": "nothing saved"})
            ev = append({"type": "resume", "kid": b["kid"], "chore": pz["chore"], "hold_id": pz.get("id")})
        elif p == "/api/abandon":   # an active job, or (paused: true, or nothing active) the one saved for later
            a_ = None if b.get("paused") else st["active"].get(b.get("kid"))
            if a_ and b.get("chore") and a_["chore"] != b["chore"]:
                a_ = None
            z_ = None if a_ else next((z for z in reversed(st.get("paused_all") or []) if z["kid"] == b.get("kid")
                                       and (not b.get("chore") or z["chore"] == b["chore"])
                                       and (not b.get("hold_id") or z.get("id") == b["hold_id"])), None)
            if b.get("by") not in insp or not (a_ or z_):
                return self.send(400, {"error": "unknown inspector / no job to stop"})
            j_ = a_ or z_
            ev = append({"type": "abandon", "kid": b["kid"], "chore": j_["chore"], "team": j_.get("team") or [b["kid"]],
                         "by": b["by"], **({"paused": True} if z_ else {}),
                         "minutes": round((time.time() - a_["ts"]) / 60 if a_ else z_.get("elapsed", 0) / 60)})
        elif p == "/api/release":
            z_ = next((z for z in reversed(st.get("paused_all") or []) if z["kid"] == b.get("kid")
                       and (not b.get("chore") or z["chore"] == b["chore"])
                       and (not b.get("hold_id") or z.get("id") == b["hold_id"])), None)
            if b.get("by") not in insp or not z_:
                return self.send(400, {"error": "unknown inspector / nothing saved"})
            ev = append({"type": "release", "kid": b["kid"], "chore": z_["chore"], "by": b["by"]})
        elif p == "/api/timer_pref":
            act = st["active"].get(b.get("kid"))
            if not act:
                return self.send(400, {"error": "not doing a chore"})
            ev = append({"type": "timer_pref", "kid": b["kid"], "on": bool(b.get("on"))})
        elif p == "/api/leave_team":
            if b.get("kid") not in kids:
                return self.send(400, {"error": "unknown kid"})
            ev = append({"type": "leave_team", "kid": b["kid"]})
        elif p == "/api/certify":   # a grown-up confirms (or not) that the kid who claimed it did it, e.g. read at church
            ch_ = chores.get(b.get("chore")) or {}
            bd = st["board"].get(b.get("chore")) or {}
            if b.get("by") not in insp or not bd.get("certify_at"):
                return self.send(400, {"error": "unknown inspector / nothing to certify"})
            if time.time() < bd["certify_at"]:
                return self.send(409, {"error": "not until after it happens"})
            if not bd.get("claimed_by") or bd.get("certified") is not None:
                return self.send(409, {"error": "nothing waiting"})
            ok = bool(b.get("ok"))
            ev = append({"type": "certify", "chore": b["chore"], "slot": bd["slot"], "kid": bd["claimed_by"], "ok": ok, "by": b["by"]})
            if ok:
                pts = int(b["points"]) if b.get("points") is not None else bd.get("price")
                rep = append({"type": "report", "kid": bd["claimed_by"], "chore": b["chore"], "feeling": None,
                              "points": pts, "certified": b["by"]})
                append({"type": "inspect", "ref": rep["id"], "by": b["by"], "ok": True, "points": pts, "label": ""})
        elif p == "/api/claim":
            ch_ = chores.get(b.get("chore")) or {}
            if b.get("kid") not in kids or not ch_.get("slot"):
                return self.send(400, {"error": "nothing to claim"})
            if ch_.get("only") and b["kid"] not in ch_["only"]:
                return self.send(400, {"error": "not your chore"})
            bd = st["board"][b["chore"]]
            if bd.get("claim_done"):
                return self.send(409, {"error": "already done this week"})
            if bd.get("claimed_by") and bd["claimed_by"] != b["kid"]:
                return self.send(409, {"error": "%s claimed it" % bd["claimed_by"]})
            if not b.get("release") and too_early(ch_, bd, b["kid"]):
                return self.send(409, {"error": "too early: claim it %s days ahead or less" % ch_["slot"]["claim_days"]})
            ev = append({"type": "claim", "kid": b["kid"], "chore": b["chore"], "slot": bd["slot"],
                         **({"release": True} if b.get("release") else {})})
        elif p in ("/api/start", "/api/stop"):
            if b.get("kid") not in kids or (p == "/api/start" and b.get("chore") not in chores):
                return self.send(400, {"error": "unknown kid/chore"})
            withdraw_pending_ = []
            if p == "/api/start" and any(project_.get("active_session") and
                    b["kid"] in (project_["active_session"].get("team") or []) for project_ in st.get("projects") or []):
                return self.send(409, {"error": "End your ongoing project session before starting another chore."})
            if p == "/api/start":
                pending_ = [r for r in st.get("history") or [] if r.get("type") == "report"
                            and r.get("status") == "pending" and not r.get("partial")
                            and r.get("chore") == b["chore"]
                            and b["kid"] in (r.get("team") or [r.get("kid")])]
                if pending_ and not chores[b["chore"]].get("multi_pending"):
                    resumable_ = b["kid"] in (st["board"].get(b["chore"]) or {}).get("resume_pending_by", [])
                    if not resumable_:
                        return self.send(409, {"error": "already waiting for inspection"})
                    withdraw_pending_ = pending_
            if p == "/api/start" and (st["active"].get(b["kid"]) or {}).get("chore") == b["chore"]:
                return self.send(200, {"ok": True, "already": True, "state": st})   # a second tap keeps the clock running
            if p == "/api/start" and chores[b["chore"]].get("each_daily") and \
                    b["kid"] in (st["board"].get(b["chore"]) or {}).get("done_today", []):
                return self.send(409, {"error": "already done today"})
            if p == "/api/start" and chores[b["chore"]].get("slot"):
                return self.send(409, {"error": "claim it instead"})
            if p == "/api/start" and chore_closed(chores[b["chore"]], st["board"]):
                return self.send(409, {"error": {"dark": "only in daylight", "done": "already done"}.get(
                    chore_closed(chores[b["chore"]], st["board"]), "not today")})
            if p == "/api/start" and b["chore"] != "other" and not chores[b["chore"]].get("shared"):   # shared = each kid does their own (organizing)
                busy_ = [k for k, a in st["active"].items() if a["chore"] == b["chore"] and k != b["kid"]] + \
                        [k for k, z in st["paused"].items() if z["chore"] == b["chore"] and b["kid"] not in z["team"]]
                if busy_:
                    return self.send(409, {"error": "%s is doing it" % busy_[0]})
            for report_ in withdraw_pending_:   # only after every start gate passes: rejection must keep the turn-in
                append({"type": "withdraw", "ref": report_["id"], "by": "kid:" + b["kid"],
                        "why": "resumed work after stopping"})
            ev = append({"type": p.rsplit("/", 1)[1], "kid": b["kid"], "chore": b.get("chore"),
                         **({"notimer": True} if p == "/api/start" and
                            not kid_uses_timer(c, b["kid"], b["chore"]) else {})})
        elif p == "/api/awards_seen":
            if b.get("kid") not in kids:
                return self.send(400, {"error": "unknown kid"})
            ev = append({"type": "awards_seen", "kid": b["kid"]})
        elif p == "/api/goal":
            if b.get("kid") not in kids or (b.get("reward") is not None and b.get("reward") not in rewards):
                return self.send(400, {"error": "unknown kid/reward"})
            ev = append({"type": "goal", "kid": b["kid"], "reward": b.get("reward")})
        elif p == "/api/tv/move":
            g = tv_grants(read_events())
            mine = [x for xs in g.values() for x in xs if x["id"] == b.get("ref") and x["kid"] == b.get("kid")]
            if not mine or b.get("tv") not in TVS:
                return self.send(400, {"error": "no running TV time to move"})
            ev = append({"type": "tv_move", "ref": b["ref"], "tv": b["tv"], "kid": b["kid"]})
        elif p == "/api/grocy_request":
            who = GROCY_REQUESTERS.get(self.headers.get("X-HA-User") or "")
            if not who:
                return self.send(403, {"error": "only listed grown-ups can request groceries"})
            text = " ".join(str(b.get("text") or "").split())[:200]
            if not text and not b.get("product_id"):
                return self.send(400, {"error": "say what you need"})
            new_id = grocy_request(text or "we're out", b.get("product_id"), who)
            ev = append({"type": "grocy_request", "text": text, "product_id": b.get("product_id"), "by": who, "grocy_id": new_id})
            threading.Thread(target=push_grocy, daemon=True).start()
        elif p == "/api/chore_toggle":
            if b.get("by") not in insp:
                return self.send(400, {"error": "unknown inspector"})
            m = GUARD.get("mode") or {}
            if m.get("override"):
                try:
                    CT_OVERRIDE.unlink()
                except OSError:
                    pass
                ev = append({"type": "chore_mode", "override": False, "by": b["by"]})
            else:
                CT_OVERRIDE.write_text(json.dumps({"state": not m.get("on"), "base_auto": bool(m.get("auto")), "by": b["by"], "ts": time.time()}))
                ev = append({"type": "chore_mode", "override": True, "state": not m.get("on"), "by": b["by"]})
            GUARD["mode_dirty"] = True
        elif p == "/api/house_done":   # a grown-up ticks off a house job (Grocy chore, task or battery)
            if b.get("by") not in insp:
                return self.send(400, {"error": "grown-ups only"})
            kind, iid = b.get("kind"), int(b.get("id") or 0)
            if not GROCY or kind not in ("chore", "task", "battery") or not iid:
                return self.send(400, {"error": "bad job"})
            from datetime import datetime
            path = {"task": "/tasks/%d/complete", "chore": "/chores/%d/execute", "battery": "/batteries/%d/charge"}[kind] % iid
            try:
                gcall("POST", path, {"tracked_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")} if kind == "chore" else {})
            except Exception as ex:  # noqa: BLE001
                return self.send(502, {"error": "Grocy: " + str(ex)[:80]})
            gone = lambda i: not (i.get("kind") == kind and i.get("id") == iid)
            GUARD["house_tasks"] = [i for i in GUARD.get("house_tasks") or [] if gone(i)]
            GUARD["house_tasks_displays"] = {d: [i for i in v if gone(i)] for d, v in (GUARD.get("house_tasks_displays") or {}).items()}
            ilog({"src": "house", "action": "done", "kind": kind, "id": iid, "by": b["by"]})
            return self.send(200, {"ok": True})
        elif p == "/api/house_add":   # a grown-up adds a one-off house job (e.g. typed or spoken into a synced list)
            if b.get("by") not in insp:
                return self.send(400, {"error": "grown-ups only"})
            name = " ".join(str(b.get("name") or "").split())[:200]
            if not GROCY or not name:
                return self.send(400, {"error": "bad job"})
            try:
                r = gcall("POST", "/objects/tasks", {"name": name, "description": str(b.get("note") or "")[:500]})
            except Exception as ex:  # noqa: BLE001
                return self.send(502, {"error": "Grocy: " + str(ex)[:80]})
            tid = int((r or {}).get("created_object_id") or 0)
            item = {"kind": "task", "id": tid, "name": name, "due": None, "overdue": False, "rooms": [], "visible": True}
            GUARD["house_tasks"] = (GUARD.get("house_tasks") or []) + [item]
            ilog({"src": "house", "action": "added", "id": tid, "name": name, "by": b["by"]})
            return self.send(200, {"ok": True, "id": tid})
        elif p == "/api/bedtime":
            if b.get("by") not in insp:
                return self.send(400, {"error": "grown-ups only"})
            ev = append({"type": "bedtime", "on": bool(b.get("on", True)), "by": b["by"]})
        elif p == "/api/chore_pause":
            if b.get("by") not in insp:
                return self.send(400, {"error": "unknown inspector"})
            from datetime import datetime, timedelta
            if b.get("resume"):
                try:
                    CT_PAUSE.unlink()
                except OSError:
                    pass
                ev = append({"type": "chore_pause", "resume": True, "by": b["by"]})
            else:
                until = datetime.combine(datetime.now().date() + timedelta(days=1), datetime.min.time()).replace(hour=AUTO_START_H).timestamp()
                CT_PAUSE.write_text(json.dumps({"until": until, "by": b["by"]}))
                ev = append({"type": "chore_pause", "until": until, "by": b["by"]})
        elif p == "/api/chore_time":
            if b.get("by") not in insp:
                return self.send(400, {"error": "unknown inspector"})
            tok = ha_token()
            if b.get("on") and b.get("today"):
                from datetime import datetime
                until = datetime.now().replace(hour=21, minute=0, second=0, microsecond=0).timestamp()
                if until <= time.time() + 300:
                    return self.send(409, {"error": "it's already past 9 PM"})
                CT_UNTIL.write_text(json.dumps({"until": until, "set_at": time.time()}))
                ha_call("input_boolean.turn_off", {"entity_id": CT_SWITCH}, tok)
                time.sleep(1)
                ha_call("input_boolean.turn_on", {"entity_id": CT_SWITCH}, tok)
                ev = append({"type": "chore_time", "on": True, "until": until, "by": b["by"]})
                after_change()
                return self.send(200, {"ok": True, "event": ev, "state": derive()})
            if b.get("on"):
                try:
                    CT_UNTIL.unlink()
                except OSError:
                    pass
                ha_call("input_number.set_value", {"entity_id": CT_HOURS,
                                                   "value": max(0.5, min(8, float(b.get("hours") or 2)))}, tok)
                ha_call("input_boolean.turn_off", {"entity_id": CT_SWITCH}, tok)  # restart the clock
                time.sleep(1)
                ha_call("input_boolean.turn_on", {"entity_id": CT_SWITCH}, tok)
            else:
                ha_call("input_boolean.turn_off", {"entity_id": CT_SWITCH}, tok)
            ev = append({"type": "chore_time", "on": bool(b.get("on")), "hours": b.get("hours"), "by": b["by"]})
        elif p == "/api/tv/adjust":
            if b.get("by") not in insp or (b.get("tv") not in TVS and b.get("tv") != "*"):
                return self.send(400, {"error": "unknown inspector/tv"})
            g = tv_grants(read_events())
            cur = max([x["until"] for t, xs in g.items() if b["tv"] in (t, "*") for x in xs] or [0])
            now = time.time()
            act = b.get("action")
            if act == "five":
                until = now + 300
            elif act == "lock":
                until = now
            elif act in ("plus", "minus"):
                if not cur:
                    return self.send(409, {"error": "TV is not open"})
                until = max(now, cur + (600 if act == "plus" else -600))
            else:
                return self.send(400, {"error": "unknown action"})
            if act in ("five", "lock"):     # a parent's lock is a lock: no "finish your show"
                for tid_ in (list(TVS) if b["tv"] == "*" else [b["tv"]]):
                    GUARD.setdefault("hard_lock", {})[tid_] = until
                    GUARD.setdefault("finish", {}).pop(tid_, None)
                    if act == "lock":
                        GUARD.setdefault("grace", {}).pop(tid_, None)
            ev = append({"type": "tv_set_until", "tv": b["tv"], "until": until, "by": b["by"], "action": act})
        elif p == "/api/timer_done":   # a kid may end their own turn; grown-ups may clear any timer
            tr_ = {t["ref"]: t for t in timed_rewards()}
            timer_ = tr_.get(b.get("ref"))
            adult_ = b.get("by") in insp
            own_ = b.get("kid") in kids and timer_ and timer_["kid"] == b["kid"]
            if not timer_ or not (adult_ or own_):
                return self.send(400, {"error": "not your timer / no timer"})
            ev = append({"type": "timer_done", "ref": b["ref"], "by": b.get("by") or b["kid"]})
        elif p == "/api/timer_restart":
            tr = {t["ref"]: t for t in timed_rewards()}
            if b.get("ref") not in tr:
                return self.send(400, {"error": "no running timer"})
            if tr[b["ref"]]["restarts"] >= 3 and b.get("by") not in insp:
                return self.send(409, {"error": "too many restarts"})
            ev = append({"type": "timer_restart", "ref": b["ref"], "by": b.get("by") or "hub"})
        elif p == "/api/tv/unlock":
            if b.get("by") not in insp:
                return self.send(400, {"error": "unknown inspector"})
            mins = max(0, min(240, int(b.get("minutes") or 0)))
            tvid = b.get("tv") or "*"
            what = b.get("what")
            kinds = sorted(UNLOCKS[what][1]) if what in UNLOCKS else ["stream", "cast", "hdmi"]
            until = time.time() + mins * 60
            if not mins:    # "lock again now" is a parent's lock: no finishing the show
                for tid_ in (list(TVS) if tvid == "*" else [tvid]):
                    GUARD.setdefault("hard_lock", {})[tid_] = time.time()
                    GUARD.setdefault("finish", {}).pop(tid_, None)
                    GUARD.setdefault("grace", {}).pop(tid_, None)
            if b.get("charge") and b.get("kid") in kids and what in rewards and mins:
                cost = rewards[what]["cost"]
                if st["balance"][b["kid"]] < cost:
                    return self.send(409, {"error": "not enough points"})
                ev = append({"type": "spend", "kid": b["kid"], "reward": what, "cost": cost, "tv": tvid,
                             "until": until, "minutes": mins, "by": b["by"],
                             "reward_name": rewards[what]["name"], "reward_icon": rewards[what]["icon"]})
            else:
                ev = append({"type": "tv_unlock", "tv": tvid, "by": b["by"], "minutes": mins, "until": until,
                             "kinds": kinds, "kid": b.get("kid"), "what": what})
        elif p == "/api/swap_step":
            if b.get("by") not in insp or b.get("step") not in ("old_out", "opened"):
                return self.send(400, {"error": "unknown inspector/step"})
            ev = append({"type": "swap_step", "ref": b.get("ref"), "step": b["step"], "by": b["by"],
                         "undo": bool(b.get("undo"))})
        elif p == "/api/toys":
            if b.get("by") not in insp or b.get("kid") not in kids:
                return self.send(400, {"error": "unknown inspector/kid"})
            if b.get("old_toy_add") or b.get("old_toy_remove"):
                with LOCK:
                    c = cfg()
                    kc = next(k for k in c["kids"] if k["id"] == b["kid"])
                    ot = kc.setdefault("old_toys", [])
                    if b.get("old_toy_add"):
                        ot.append({"id": "o_" + uuid.uuid4().hex[:6], "name": str(b["old_toy_add"])[:40],
                                   "icon": b.get("icon") or "1f9f8"})
                    else:
                        kc["old_toys"] = [t for t in ot if t["id"] != b["old_toy_remove"]]
                    save_cfg(c)
                ev = append({"type": "toys", "kid": b["kid"], "by": b["by"], "old_toys": True})
            elif b.get("presents") is not None:
                with LOCK:
                    c = cfg()
                    next(k for k in c["kids"] if k["id"] == b["kid"])["presents"] = max(0, min(50, int(b["presents"])))
                    save_cfg(c)
                ev = append({"type": "toys", "kid": b["kid"], "by": b["by"], "presents": int(b["presents"])})
            else:
                ev = append({"type": "present_named", "kid": b["kid"], "by": b["by"],
                             "present": int(b.get("present") or 0), "name": str(b.get("name") or "")[:40]})
        elif p == "/api/close_od":   # an on-demand job (laundry) toggled off again without anyone doing it
            if b.get("by") not in insp or b.get("chore") not in chores:
                return self.send(400, {"error": "unknown inspector/chore"})
            ev = append({"type": "close_od", "chore": b["chore"], "by": b["by"]})
        elif p == "/api/dress":   # "Put on clothes!": blocks other jobs until each kid is inspected dressed
            if b.get("by") not in insp:
                return self.send(400, {"error": "grown-ups only"})
            on = bool(b.get("on"))
            who = [k for k in (b.get("kids") or []) if k in kids] or dress_kids(c)
            if on and not who:
                return self.send(409, {"error": "no kids home"})
            ev = append({"type": "dress", "on": on, "kids": who if on else [], "by": b["by"]})
            if on:
                threading.Thread(target=dress_announce, args=(True,), daemon=True).start()
        elif p == "/api/request_answer":   # a grown-up starts a bought reward's timer (VR, once set up) or refunds it
            q = next((x for x in st["requests"] if x["id"] == b.get("ref")), None)
            if b.get("by") not in insp or not q:
                return self.send(400, {"error": "unknown inspector / nothing waiting"})
            rw_ = rewards.get(q["reward"]) or {}
            if b.get("ok"):
                ev = append({"type": "reward_start", "ref": q["id"], "kid": q["kid"], "by": b["by"]})
            else:
                ev = append({"type": "undo", "ref": q["id"], "by": b["by"]})
                append({"type": "kid_note", "kid": q["kid"], "by": b["by"], "icon": rw_.get("icon", "1f4ac"),
                        "text": (b.get("text") or "Not right now for %s. Your stars are back!" % rw_.get("name", q["reward"]))[:200]})
        elif p == "/api/kid_note":   # a grown-up's message, shown once on the kid's screen
            if b.get("by") not in insp or b.get("kid") not in kids or not str(b.get("text") or "").strip():
                return self.send(400, {"error": "grown-up, kid and text needed"})
            ev = append({"type": "kid_note", "kid": b["kid"], "text": str(b["text"]).strip()[:200], "icon": b.get("icon") or "1f4ac", "by": b["by"]})
        elif p == "/api/kid_note_seen":
            if b.get("kid") not in kids or not any(n["id"] == b.get("ref") for n in st["kid_notes"].get(b["kid"], [])):
                return self.send(400, {"error": "no such note"})
            ev = append({"type": "kid_note_seen", "kid": b["kid"], "ref": b["ref"]})
        elif p == "/api/chores_first":   # grown-ups: no rewards until they turn it off
            if b.get("by") not in insp or b.get("kid") not in kids:
                return self.send(400, {"error": "grown-ups only, for one kid"})
            if "clear_room" in b and type(b.get("clear_room")) is not bool:
                return self.send(400, {"error": "clear room must be true or false"})
            items = [str(x).strip()[:120] for x in (b.get("items") or []) if str(x).strip()][:20]
            ev = append({"type": "chores_first", "kid": b["kid"], "on": bool(b.get("on")), "items": items, "by": b["by"],
                         **({"chore": None, "name": None} if b.get("clear_room") else {}),
                         **({"done": True} if not b.get("on") and b.get("done") else {})})
        elif p == "/api/music_block":   # HA enforces this only while required-work gates are outstanding
            if b.get("by") not in insp:
                return self.send(400, {"error": "grown-ups only"})
            ev = append({"type": "music_block", "on": bool(b.get("on")), "by": b["by"]})
        elif p == "/api/spotify_volume_cap":   # HA owns enforcement; only its signed-in grown-up relay may save it
            if not verified_ha_adult(self.headers, b.get("by")):
                return self.send(403, {"error": "Use the signed-in grown-up Chore dashboard."})
            cap_ = b.get("cap")
            if type(cap_) is not int or not 0 <= cap_ <= 100:
                return self.send(400, {"error": "Spotify volume must be a whole number from 0 to 100."})
            ev = append({"type": "spotify_volume_cap", "cap": cap_, "by": b["by"]})
        elif p == "/api/space_tidy":
            space = next((x for x in HOUSE.get("tidy_spaces", []) if x["id"] == b.get("space")), None)
            if b.get("by") not in insp or space is None or type(b.get("tidy")) is not bool:
                return self.send(400, {"error": "known grown-up, room and tidy state required"})
            jobs = space.get("chores", [])
            if any(job not in chores for job in jobs):
                return self.send(409, {"error": "room chore configuration needs attention"})
            requested_kids = b.get("block_kids") or []
            if not isinstance(requested_kids, list):
                return self.send(400, {"error": "blocking kids must be a list"})
            block_kids = list(dict.fromkeys(x for x in requested_kids if x in kids))
            if requested_kids and (b["tidy"] or len(block_kids) != len(set(requested_kids))):
                return self.send(400, {"error": "blocking kids must be known and the room must be untidy"})
            if "items" in b and (not block_kids or not isinstance(b.get("items"), list)):
                return self.send(400, {"error": "room blocking items require selected kids and a list"})
            block_items = [str(x).strip()[:120] for x in (b.get("items") or []) if str(x).strip()][:20]
            present_ = set(GUARD.get("home_now") or [])
            if any(k not in present_ for k in block_kids):
                return self.send(409, {"error": "only kids currently home can be given this mandatory room"})
            assigned = {}
            for kid_ in block_kids:
                eligible_ = [job for job in jobs if not (chores[job].get("only") or []) or kid_ in chores[job]["only"]]
                if not eligible_:
                    return self.send(409, {"error": "this room has no chore available to one of the selected kids"})
                assigned[kid_] = eligible_[0]
            if any(a.get("chore") in jobs for a in list(st["active"].values()) + list(st["paused"].values())) or any(st["board"].get(job, {}).get("pending_by") for job in jobs):
                return self.send(409, {"error": "finish or inspect the room's existing job first"})
            if jobs:
                for job in jobs:
                    ev = append({"type": "mark_clean" if b["tidy"] else "reopen", "chore": job, "by": b["by"]})
            else:
                ev = append({"type": "space_tidy", "space": space["id"], "tidy": b["tidy"], "by": b["by"]})
            for kid_, job_ in assigned.items():
                ev = append({"type": "chores_first", "kid": kid_, "on": True,
                             "items": block_items or ["Tidy " + space["name"]],
                             "chore": job_, "name": chores[job_].get("name") or space["name"], "by": b["by"]})
        elif p == "/api/mark_clean":
            if b.get("by") not in insp or b.get("chore") not in chores:
                return self.send(400, {"error": "unknown inspector/chore"})
            if any(a.get("chore") == b["chore"] for a in list(st["active"].values()) + list(st["paused"].values())) or st["board"].get(b["chore"], {}).get("pending_by"):
                return self.send(409, {"error": "finish or inspect the existing job first"})
            ev = append({"type": "mark_clean", "chore": b["chore"], "by": b["by"]})
        elif p == "/api/tour_seen":
            if b.get("by") not in insp or b.get("chore") not in chores:
                return self.send(400, {"error": "unknown inspector/chore"})
            ev = append({"type": "tour_seen", "chore": b["chore"], "by": b["by"]})
        elif p == "/api/note":
            if b.get("by") not in insp or b.get("chore") not in chores:
                return self.send(400, {"error": "unknown inspector/chore"})
            ev = append({"type": "note", "chore": b["chore"], "by": b["by"], "note": str(b.get("note") or "").strip()[:600]})
        elif p in ("/api/price", "/api/reopen"):
            if b.get("by") not in insp or b.get("chore") not in chores:
                return self.send(400, {"error": "unknown inspector/chore"})
            note = str(b.get("note") or "").strip()[:600]
            if p == "/api/price":
                pts = b.get("points")
                pts = None if pts in (None, "", "?") else max(0, min(50, int(pts)))
                ev = append({"type": "price", "chore": b["chore"], "points": pts, "by": b["by"], **({"note": note} if note else {})})
            else:
                ev = append({"type": "reopen", "chore": b["chore"], "by": b["by"], **({"note": note} if note else {})})
        elif p == "/api/undo":
            if b.get("by") not in insp:
                return self.send(400, {"error": "unknown inspector"})
            ev = append({"type": "undo", "ref": b.get("ref"), "by": b["by"]})
        else:
            return self.send(404, {"error": "not found"})
        after_change()
        ilog({"src": "api", "ip": display_ip_, "path": p, "event": ev})
        if p == "/api/spend" and ev.get("type") == "spend":
            threading.Thread(target=notify_parents, args=(ev,), daemon=True).start()
        if p == "/api/start" and ev.get("type") == "start" and ev.get("chore") != "get_dressed":
            threading.Thread(target=notify_started, args=(ev["kid"], ev["chore"]), daemon=True).start()
        if p == "/api/team" and ev.get("type") == "team":
            threading.Thread(target=notify_started, args=(ev["members"][0], ev["chore"], ev["members"], "team"), daemon=True).start()
        if p == "/api/pause" and ev.get("type") == "pause":
            threading.Thread(target=notify_started, args=(ev["kid"], ev["chore"], ev.get("team"), "paused"), daemon=True).start()
        if p == "/api/report" and ev.get("type") == "report" and not ev.get("auto_verified") \
                and not (chores.get(ev.get("chore")) or {}).get("trust"):
            threading.Thread(target=notify_report, args=(ev, derive()), daemon=True).start()
        if ev.get("type") in ("inspect", "undo") and ev.get("ref"):
            threading.Thread(target=clear_report_notice, args=(ev["ref"],), daemon=True).start()
        if p == "/api/request_answer" and ev.get("ref"):
            threading.Thread(target=clear_reward_request_notice, args=(ev["ref"],), daemon=True).start()
        self.send(200, {"ok": True, "event": ev, "state": derive()})


# ---- launchers: HA input_button press -> CATT cast of the kid UI to a Hub ----
import subprocess
LAUNCHERS = {k: (v["ip"], v.get("path", "/")) for k, v in (HOUSE.get("launchers") or {}).items()}   # HA button -> cast a page
SELF_URL = os.environ.get("CHORE_SELF_URL", HOUSE.get("public_url") or "http://localhost:%d" % PORT)   # how Hubs/TVs reach us
CATT = str(house_path((HOUSE.get("casting") or {}).get("catt")) or "catt")
HUB_VOLUME = int(os.environ.get("CHORE_HUB_VOLUME", (HOUSE.get("casting") or {}).get("hub_volume", 40)))


def cast(ip, path):
    try:
        AUTHORITY.require_effect()
    except FencedError as ex:
        print("cast suppressed by failover fence:", ex, flush=True)
        return False
    # a fresh query string forces DashCast to reload instead of keeping the old page
    url = SELF_URL + path + ("&" if "?" in path else "?") + "v=%d" % time.time()
    print("cast", ip, url, flush=True)
    try:
        if hub_showing_chores(ip) or ip in [t["ip"] for t in TVS.values()]:
            subprocess.run([CATT, "-d", ip, "stop"], timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
        subprocess.run([CATT, "-d", ip, "cast_site", url], timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if path.startswith("/") and not path.startswith("/tv"):
            # The server mute also protects stale/cached kid pages which have not received the state flag yet.
            subprocess.run([CATT, "-d", ip, "volume", str(HUB_VOLUME if sound_enabled() else 0)], timeout=20,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as ex:  # noqa: BLE001
        print("cast failed", ip, ex, flush=True)


def pause_and_lock_tv_reward(tvid, playback, tok=None):
    """Synchronous physical path, split out so a fixture can verify it without touching a household TV."""
    t, tok = TVS[tvid], tok or ha_token()
    targets = [playback.get("player")] if playback and playback.get("player") else list(t.get("players") or [])
    for player in targets:
        ha_call("media_player.media_pause", {"entity_id": player}, tok)
    GUARD.setdefault("last_cast", {})[t["ip"]] = time.time()
    cast(t["ip"], "/tv?paused_reward=1&room=" + urllib.parse.quote(tvid, safe=""))


def queue_tv_reward_pause(tvid, playback):
    """Pause the receiver first, then replace it with the durable child-facing lock page."""
    threading.Thread(target=pause_and_lock_tv_reward, args=(tvid, playback),
                     name="tv-reward-pause-" + tvid, daemon=True).start()


def queue_tv_reward_resume(tvid, playback):
    """Release the lock page and restore the captured position only if the same player still supports it."""
    t, tok = TVS[tvid], ha_token()

    def go():
        try:
            subprocess.run([CATT, "-d", t["ip"], "stop"], timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as ex:  # noqa: BLE001
            print("TV reward unlock failed", tvid, ex, flush=True)
        if not playback or not playback.get("player") or playback.get("pos") is None:
            return
        state = ha_get(playback["player"], tok)
        attrs = state.get("attributes") or {}
        same = ((playback.get("content_id") and attrs.get("media_content_id") == playback["content_id"]) or
                (playback.get("title") and attrs.get("media_title") == playback["title"]))
        if not same:
            return   # the service/app can still offer its own Continue Watching position
        ha_call("media_player.media_seek", {"entity_id": playback["player"],
                                            "seek_position": max(0, float(playback["pos"]))}, tok)
        ha_call("media_player.media_play", {"entity_id": playback["player"]}, tok)

    threading.Thread(target=go, name="tv-reward-resume-" + tvid, daemon=True).start()


def watch_launchers():
    try:
        tok = ha_token()
    except OSError:
        return
    last = {}
    while True:
        for ent, (ip, path) in LAUNCHERS.items():
            try:
                req = urllib.request.Request(f"{HA_URL}/api/states/{ent}",
                                             headers={"Authorization": f"Bearer {tok}"})
                st = json.loads(urllib.request.urlopen(req, timeout=5).read())["state"]
            except Exception:  # noqa: BLE001
                continue
            if ent in last and st != last[ent] and st not in ("unknown", "unavailable"):
                # on demand (Spotify -> chores, tapping 3x while the cast loads): one cast per 10 s,
                # extra taps are ignored so back-to-back recasts can't wedge the Hub
                lc = GUARD.setdefault("launch_ts", {})
                if time.time() - lc.get(ip, 0) < 10:
                    ilog({"src": "launcher", "button": ent, "action": "debounced"})
                else:
                    lc[ip] = time.time()
                    ilog({"src": "launcher", "button": ent})
                    GUARD.setdefault("hub_exit", {}).update({k: 0 for k, h in HUBS.items() if h["ip"] == ip})
                    threading.Thread(target=cast, args=(ip, path), daemon=True).start()
            last[ent] = st
        time.sleep(1.5)


# ---- TV guard: during "Chore time", streaming/HDMI on the kid TVs needs a paid unlock ----
TVS = HOUSE.get("tvs") or {}   # tv id -> {name, ip, kind (sony|vizio|...), television, players, remote, ...}
HUBS = HOUSE.get("hubs") or {}   # hub id -> {ip, entity, from_h}: the kid screens (e.g. Nest Hub Max)
HUB_RECHECK_S = 10
HUB_EXIT_S = 3 * 60     # a kid tapped Exit: this long to start Spotify etc.; once something plays, we wait for it to stop
HUB_PAUSE_GRACE_S = 120


def hub_showing_chores(ip):
    """The kid page polls /api/state every 5 s, so a recent poll from that IP proves it is on screen.
    (catt status never names the app for DashCast; trusting it recast every 45 s and crashed a Hub.)"""
    seen = GUARD["seen"].get(ip)
    return bool(seen and time.time() - seen[0] < 15)


def keep_hubs_on_chores(tok):
    now = time.time()
    if (GUARD.get("mode") or {}).get("chill") and not GUARD.get("kid_workflow_open"):
        return
    from datetime import datetime
    for hid, h in HUBS.items():
        if datetime.now().hour < h.get("from_h", 0):
            continue   # the kids-room Hub is in their bedroom: don't light it up at 5 AM
        if active_spotify_interruption(h.get("room") or hid):
            continue   # its already-open Chore page is showing the silent block; never cast over it
        last = GUARD.setdefault("hub_check", {}).get(hid, 0)
        if now - last < HUB_RECHECK_S:
            continue
        GUARD["hub_check"][hid] = now
        # hold off while Spotify (or anything) plays on the Hub; cast again as soon as it stops.
        # A pause gets HUB_PAUSE_GRACE_S. After a kid's Exit: HUB_EXIT_S to start something, else straight back.
        hs = ha_get(h["entity"], tok)
        busy = GUARD.setdefault("hub_busy", {})
        if hs["state"] in ("playing", "buffering") and hs["attributes"].get("app_name") != "DashCast":
            GUARD.setdefault("hub_casts", {})[hid] = []   # our cast stuck; they chose music: no back-off on the way back
            if now - busy.get(hid, 0) > 60:
                ilog({"src": "hub", "hub": hid, "action": "hold_playing", "app": hs["attributes"].get("app_name")})
            busy[hid] = now
            continue
        if hs["state"] == "paused" and now - busy.get(hid, 0) < HUB_PAUSE_GRACE_S:
            continue
        ex = GUARD.setdefault("hub_exit", {}).get(hid, 0)
        if now - ex < HUB_EXIT_S and busy.get(hid, 0) < ex:
            continue   # just exited and nothing has played yet
        hc = GUARD.setdefault("hub_casts", {}).setdefault(hid, [])
        hc[:] = [t for t in hc if now - t < 600]
        # During an active job or packing trip, recover an accidentally dismissed screen after one missed
        # polling window. The explicit Exit grace and real music/playback holds above still take priority.
        gap = 15 if GUARD.get("kid_workflow_open") else \
            (300 if len(hc) >= 3 and not hub_showing_chores(h["ip"]) else 60)
        if not hub_showing_chores(h["ip"]) and (not hc or now - hc[-1] > gap) and \
                now - GUARD.setdefault("launch_ts", {}).get(h["ip"], 0) > 15:   # a launcher cast is already loading
            hc.append(now)
            print("chore time: hub back to chores", hid, flush=True)
            ilog({"src": "hub", "hub": hid, "action": "auto_chore_screen"})
            cast(h["ip"], "/")


ANNOUNCE_SPEAKER = os.environ.get("CHORE_SPEAKER", (HA.get("announce") or {}).get("speaker"))
ANNOUNCE_TTS = (HA.get("announce") or {}).get("tts")


ANNOUNCE_MEDIA_BASE = (HA.get("announce") or {}).get("media_base")   # e.g. HA's LAN address


def announce(msg, tok, speaker=None, repeat=1):
    """Say msg on the speaker(s), `repeat` times in a row (VR: twice, so a kid in a headset catches it).
    announce.media_base: when the speaker can't fetch HA's own TTS links (e.g. Music Assistant can't resolve
    HA's internal hostname, so only its announcement bell plays), the audio is made here and played from that base."""
    if not sound_enabled() or not ANNOUNCE_TTS or not (speaker or ANNOUNCE_SPEAKER):
        return
    text = " ".join([msg] * max(1, int(repeat)))
    ilog({"src": "announce", "msg": msg, "repeat": repeat, "speaker": speaker or ANNOUNCE_SPEAKER})
    print("announce:", text, flush=True)
    if ANNOUNCE_MEDIA_BASE:
        try:
            req = urllib.request.Request(f"{HA_URL}/api/tts_get_url", method="POST",
                                         data=json.dumps({"engine_id": ANNOUNCE_TTS, "message": text, "cache": True}).encode(),
                                         headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
            path = json.loads(urllib.request.urlopen(req, timeout=20).read())["path"]
            ha_call("media_player.play_media", {"entity_id": speaker or ANNOUNCE_SPEAKER, "announce": True,
                                                "media_content_id": ANNOUNCE_MEDIA_BASE.rstrip("/") + path,
                                                "media_content_type": "music"}, tok)
            return
        except Exception as ex:  # noqa: BLE001
            ilog({"src": "announce", "action": "media_base_failed", "error": str(ex)[:200]})
    ha_call("tts.speak", {"entity_id": ANNOUNCE_TTS, "media_player_entity_id": speaker or ANNOUNCE_SPEAKER,
                          "message": text, "cache": True}, tok)


def timed_rewards(evs=None, now=None):
    """Running timed rewards (VR): start = newest restart or the purchase; kept until 2 min after the end."""
    evs = evs if evs is not None else read_events()
    now = now or time.time()
    c = cfg()
    rw = {r["id"]: r for r in c["rewards"]}
    undone = {e["ref"] for e in evs if e["type"] == "undo"}
    out = {}
    waiting = {e["id"]: e for e in evs if e["type"] == "spend" and e.get("await_ok")}
    for e in evs:
        if e["type"] == "spend" and e.get("await_ok"):
            continue   # waits for a grown-up's start (below)
        if e["type"] == "reward_start" and e.get("ref") in waiting:
            e = dict(waiting.pop(e["ref"]), ts=e["ts"])
        if e["type"] == "spend" and e["id"] not in undone and rw.get(e.get("reward"), {}).get("minutes"):
            mins = e.get("display_minutes") or e.get("minutes") or rw[e["reward"]]["minutes"]
            out[e["id"]] = {"ref": e["id"], "kid": e["kid"], "reward": e["reward"], "start": e["ts"], "minutes": mins,
                            "restarts": 0}
        elif e["type"] == "timer_restart" and e.get("ref") in out:
            out[e["ref"]]["start"] = e["ts"]
            out[e["ref"]]["restarts"] += 1
        elif e["type"] == "timer_done" and e.get("ref") in out:   # a grown-up cleared it (headset off)
            out.pop(e["ref"])
    return [t for t in out.values() if t["start"] + t["minutes"] * 60 + 120 > now]


def vr_timers(tok):
    """Refresh HA when a visible VR availability countdown expires.

    The grown-up calls actual VR time; never announce countdown warnings on
    Google speakers.
    """
    live = sorted(t["ref"] for t in timed_rewards())
    if live != GUARD.get("vr_live"):   # a timer ran out (dropped 2 min after time's up): screens that only see the
        if GUARD.get("vr_live") is not None:   # HA sensor need a push, or "time is up" stays on them
            push_state_sensor()
        GUARD["vr_live"] = live


UNLOCKS = {  # reward id -> (minutes, what it allows, longest single playback allowed in s or None)
    "show": (40, {"stream", "cast"}, 3600),
    "movie": (180, {"stream", "cast"}, None),
    "vr": (10, {"cast"}, None),   # 5 min VR + slack; casting only, on every TV
    "_adult": (0, {"stream", "cast", "hdmi"}, None),  # grown-up override from the inspection page
    "games": (60, {"hdmi"}, None),
}
SONY_IDLE = {"com.google.android.apps.tv.dreamx", "com.google.android.apps.tv.launcherx",
             "com.android.systemui"}   # the input picker/overlay: a kid got locked out switching to the console's HDMI
DASHCAST_APP_ID = "84912283"
HA_CAST_APP_ID = "A078F6B0"
TRAIN_AMBIENT_ENTITY = "sensor.train_ambient_display"
GUARD = {"last_cast": {}, "status": {}, "grace": {}, "seen": {}}
GRACE_FALLBACK_MIN = 30   # "finish your episode" when the TV can't tell us how much is left
GRACE_EXTRA_S = 300   # cut off at the predicted end of the show + 5 min


def remaining_s(t, tok):
    """Seconds left in what's playing, if any player reports position+duration."""
    from datetime import datetime
    best = None
    for p in t["players"]:
        a = ha_get(p, tok)["attributes"]
        dur, pos, upd = a.get("media_duration"), a.get("media_position"), a.get("media_position_updated_at")
        if dur and pos is not None:
            try:
                pos = pos + max(0, time.time() - datetime.fromisoformat(upd).timestamp()) if upd else pos
            except ValueError:
                pass
            left = max(0, dur - pos)
            best = left if best is None else min(best, left)
    return best


FINISH_CAP_S = 3600   # an hour into one title = a movie, not a show -> lock


def playing_item(t, tok):
    """What's playing, only when a player reliably reports it: {title, pos, dur, left} or None (=> lock on the timer)."""
    from datetime import datetime
    for p in t["players"]:
        s = ha_get(p, tok)
        a = s.get("attributes") or {}
        dur, pos, upd = a.get("media_duration"), a.get("media_position"), a.get("media_position_updated_at")
        if s.get("state") != "playing" or not dur or pos is None:
            continue
        try:
            pos = pos + max(0, time.time() - datetime.fromisoformat(upd).timestamp()) if upd else pos
        except ValueError:
            pass
        return {"title": a.get("media_title") or a.get("media_content_id") or "?", "series": a.get("media_series_title"),
                "content_id": a.get("media_content_id"), "pos": pos, "dur": dur,
                "left": max(0, dur - pos), "player": p}
    return None


def app_of(t, tok):
    if t["kind"] == "sony":
        return ha_get(t["remote"], tok)["attributes"].get("current_activity")
    a = ha_get(t["television"], tok)["attributes"]
    return a.get("app_name") or a.get("source")


def ha_get(ent, tok):
    if not HA_URL:   # no Home Assistant configured
        return {"state": "unavailable", "attributes": {}}
    req = urllib.request.Request(f"{HA_URL}/api/states/{ent}", headers={"Authorization": f"Bearer {tok}"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=5).read())
    except Exception:  # noqa: BLE001
        return {"state": "unavailable", "attributes": {}}


SPOTIFY_INTERRUPTION = "sensor.spotify_chore_interruption"


def normalize_spotify_interruptions(raw, now=None):
    """HA's fresh positive-Spotify receipts; concurrent rooms retain separate expiries."""
    now = time.time() if now is None else now
    attrs = (raw or {}).get("attributes") or {}
    if (raw or {}).get("state") in (None, "unknown", "unavailable"):
        return []
    rows = attrs.get("recent") if isinstance(attrs.get("recent"), list) else [attrs]
    out = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        try:
            until = float(item.get("valid_until") or 0)
            attempted = float(item.get("attempted_at") or 0)
        except (TypeError, ValueError):
            continue
        rooms = item.get("rooms") if isinstance(item.get("rooms"), list) else []
        if until <= now or not item.get("receiver") or not rooms or not item.get("attempt_id"):
            continue
        out.append({"receiver": item["receiver"], "rooms": [str(x) for x in rooms], "attempted_at": attempted,
                    "valid_until": until, "attempt_id": str(item["attempt_id"])})
    return sorted(out, key=lambda x: x["attempted_at"], reverse=True)


def refresh_spotify_interruption(tok):
    GUARD["music_interruptions"] = normalize_spotify_interruptions(ha_get(SPOTIFY_INTERRUPTION, tok))


def display_for_ip(ip):
    def found(kind, display_id, cfg_):
        row = {"kind": kind, "id": display_id, "room": cfg_.get("room") or display_id}
        if cfg_.get("household"):
            row["household"] = cfg_["household"]
        return row
    for display_id, cfg_ in HUBS.items():
        if cfg_.get("ip") == ip:
            return found("hub", display_id, cfg_)
    for display_id, cfg_ in TVS.items():
        if cfg_.get("ip") == ip:
            return found("tv", display_id, cfg_)
    for display_id, cfg_ in (HOUSE.get("display_aliases") or {}).items():
        if cfg_.get("ip") == ip:
            return found(cfg_.get("kind") or "display", display_id, cfg_)
    return None


def trusted_display_client_ip(peer_ip, headers):
    """Recover only a configured display IP from the dedicated LAN proxy.

    The raw TCP peer remains authoritative for loopback/admin authorization.
    Proxy identity is deliberately narrower: it can identify a configured
    Hub/TV for its session and UI behavior, but it cannot turn a request into
    localhost or confer any grown-up privilege.
    """
    peer_ip = str(peer_ip or "")
    if display_for_ip(peer_ip):
        return peer_ip
    config = HOUSE.get("trusted_display_proxy") or {}
    token_path = house_path(config.get("token_file"))
    networks = config.get("peer_cidrs") or []
    if not token_path or not networks:
        return peer_ip
    try:
        peer = ipaddress.ip_address(peer_ip)
        if not any(peer in ipaddress.ip_network(str(network), strict=False) for network in networks):
            return peer_ip
        if stat.S_IMODE(token_path.stat().st_mode) != 0o600:
            return peer_ip
        expected = token_path.read_text().strip()
    except (OSError, ValueError):
        return peer_ip
    supplied = str(headers.get(config.get("token_header") or "X-Chore-Proxy-Token") or "")
    if not expected or not hmac.compare_digest(supplied, expected):
        return peer_ip
    forwarded = str(headers.get(config.get("forwarded_header") or "X-Forwarded-For") or "")
    candidates = [part.strip() for part in forwarded.split(",") if part.strip()]
    if not candidates:
        return peer_ip
    # The rightmost address is the peer Traefik observed. A forged address
    # prepended by a client therefore never wins over its actual LAN address.
    candidate = candidates[-1]
    try:
        candidate = str(ipaddress.ip_address(candidate))
    except ValueError:
        return peer_ip
    return candidate if display_for_ip(candidate) else peer_ip


def coin_flip_state(c, present, display, location_state=None, now=None, settings=None):
    """Choose the eligible pool for the login-screen coin flip.

    Room filtering is all-or-nothing: only fresh evidence that unambiguously
    locates every child currently home may narrow the pool to this display's
    room. Uncertain evidence falls back to every child home.
    """
    settings = settings if settings is not None else (HOUSE.get("coin_flip") or {})
    now = time.time() if now is None else float(now)
    configured = [k["id"] for k in c.get("kids") or []]
    home_set = set(present or [])
    home = [k for k in configured if k in home_set]
    out = {"kids": home, "scope": "home"}
    if not home or not display or not display.get("room") or not settings:
        return out
    attrs = (location_state or {}).get("attributes") or {}
    try:
        observed = float(attrs.get("observed_at"))
        valid_until = float(attrs.get("valid_until"))
    except (TypeError, ValueError):
        return out
    max_age = max(5, min(600, int(settings.get("max_age_seconds") or 120)))
    if observed > now + 5 or now - observed > max_age or valid_until <= now:
        return out
    aliases = {str(k): str(v) for k, v in (settings.get("room_aliases") or {}).items()}
    canon = lambda room: aliases.get(str(room), str(room))
    threshold = max(0.5, min(1.0, float(settings.get("min_probability") or 0.8)))
    located = {}
    people = attrs.get("people") if isinstance(attrs.get("people"), list) else []
    for kid in home:
        rows = [p for p in people if isinstance(p, dict) and p.get("person_key") == kid
                and p.get("confirmed") is True and p.get("located") is True]
        if len(rows) != 1:
            return out
        rooms = []
        for room in rows[0].get("rooms") or []:
            try:
                if float(room.get("probability") or 0) >= threshold and float(room.get("valid_until") or 0) > now:
                    rooms.append(canon(room.get("room")))
            except (AttributeError, TypeError, ValueError):
                continue
        rooms = list(dict.fromkeys(rooms))
        if len(rooms) != 1:
            return out
        located[kid] = rooms[0]
    display_room = canon(display["room"])
    in_room = [kid for kid in home if located.get(kid) == display_room]
    if in_room:
        return {"kids": in_room, "scope": "room", "room": display["room"]}
    return out


def active_spotify_interruption(room):
    room = spotify_room_key(room)
    exempt = {spotify_room_key(item) for item in spotify_exempt_rooms()}
    if not room or room in exempt:
        return None
    now = time.time()
    return next((item for item in GUARD.get("music_interruptions") or []
                 if item.get("valid_until", 0) > now
                 and any(spotify_room_key(receipt_room) == room and spotify_room_key(receipt_room) not in exempt
                         for receipt_room in item.get("rooms", []))), None)


def ha_post_state(ent, state, attrs, tok):
    if not HA_URL:   # no Home Assistant configured
        return None
    try:
        AUTHORITY.require_effect()
    except FencedError:
        return None
    body = json.dumps({"state": state, "attributes": attrs}, ensure_ascii=False,
                      separators=(",", ":")).encode()
    req = urllib.request.Request(f"{HA_URL}/api/states/{ent}", data=body,
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=10).read()


def ha_fire_event(event, data, tok):
    """Publish a bounded observation for HA consumers; failure must never delay or reject the kid's login."""
    if not HA_URL:
        return None
    try:
        AUTHORITY.require_effect()
    except FencedError:
        return None
    req = urllib.request.Request(f"{HA_URL}/api/events/{event}", data=json.dumps(data).encode(),
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                                 method="POST")
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as ex:  # noqa: BLE001
        print("HA event failed", event, ex, flush=True)
        return False


def ha_call(svc, data, tok):
    if not HA_URL:   # no Home Assistant configured
        return None
    try:
        AUTHORITY.require_effect()
    except FencedError:
        return None
    dom, name = svc.split(".")
    req = urllib.request.Request(f"{HA_URL}/api/services/{dom}/{name}", data=json.dumps(data).encode(),
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                                 method="POST")
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as ex:  # noqa: BLE001
        print("HA call failed", svc, ex, flush=True)
        return False


def external_display_active(t, players, tok):
    """Honor an explicit display while its receiver loads or is visible.

    Optional per-TV config: external_display_entity and external_display_app_id.
    Stale state, an overlong lease or a native app in front never exempts playback.
    """
    entity=t.get("external_display_entity")
    if not entity or not t.get("external_display_app_id"):
        return False
    st=ha_get(entity,tok)
    try:
        valid=(st.get("state") in ("loading","active","restoring") and
               0 < float(st.get("attributes",{}).get("lease_until",0))-time.time() <= 90)
    except (TypeError,ValueError):
        return False
    if not valid:
        return False
    if t.get("kind")=="sony":
        activity=(ha_get(t["remote"],tok).get("attributes") or {}).get("current_activity")
        allowed = {"com.google.android.apps.mediashell"}
        if st.get("state") == "loading":
            # Cast startup passes through the screensaver/launcher. A fresh
            # explicit reservation must survive that gap, or the idle-screen
            # guard launches Chores over the in-flight receiver (Nov15).
            allowed.update({"com.google.android.apps.tv.dreamx",
                            "com.google.android.apps.tv.launcherx"})
        if activity not in allowed:
            return False
    # HA Cast metadata can lag launch. A fresh loading lease protects that
    # handoff; the display controller withdraws it after its 75-second deadline.
    if st.get("state")=="loading":
        return True
    # Render confirmation can reach HA before its Cast integration catches up.
    # The explicit controller reads the receiver directly; its fresh lease and
    # matching live receiver are sufficient even while players still say Off
    # or DashCast. Native input and lease-expiry gates above still take priority.
    attrs=st.get("attributes",{})
    if attrs.get("receiver_connected") is True and attrs.get("receiver_app")==t["external_display_app_id"]:
        return True
    return any(p.get("attributes",{}).get("app_id")==t["external_display_app_id"] for p in players)


def train_ambient_lease(t, tok, now=None):
    """Validate HA's short, room-specific request to replace only our idle /tv page."""
    room = t.get("room")
    view = {"family_room": "train-family-room", "kitchen": "train-kitchen"}.get(room)
    if not view:
        return {"allowed": False, "reason": "room"}
    raw = ha_get(TRAIN_AMBIENT_ENTITY, tok) or {}
    attrs = raw.get("attributes") or {}
    expected_states = {"rotate-" + room, "map-" + room}
    checks = (
        (raw.get("state") in expected_states, "state"),
        (attrs.get("room") == room, "room"),
        (attrs.get("dashboard") == "downstairs-faults", "dashboard"),
        (attrs.get("view") == view, "view"),
        (attrs.get("receiver_app") == HA_CAST_APP_ID, "receiver"),
    )
    failed = next((reason for ok, reason in checks if not ok), None)
    if failed:
        return {"allowed": False, "reason": failed}
    try:
        current = time.time() if now is None else float(now)
        observed, valid = float(attrs["observed_at"]), float(attrs["valid_until"])
    except (KeyError, TypeError, ValueError):
        return {"allowed": False, "reason": "time"}
    if not observed <= current < valid <= observed + 30.001:
        return {"allowed": False, "reason": "stale"}
    return {"allowed": True, "reason": "fresh_train_ambient", "valid_until": valid,
            "state": raw.get("state"), "view": view}


def tv_status(t, tok):
    """-> (what, longest_duration_s): what in off|idle|lock|stream|hdmi"""
    players = [ha_get(p, tok) for p in t["players"]]
    if external_display_active(t,players,tok):
        return "external_display",None
    if t["kind"] == "sony":   # the TV's own input wins over a lock page still loaded on its Chromecast
        act0 = (ha_get(t["remote"], tok).get("attributes") or {}).get("current_activity") or ""
        if act0 == "com.google.android.apps.tv.dreamx" and ha_get(t["remote"], tok).get("state") == "on":
            return "idle", None
        if any(x in act0.lower() for x in ("tvinput", "tv.input", ".inputs", "dtv")) and \
                ha_get(t["television"], tok).get("state") not in ("off", "standby"):
            return "hdmi", max([p["attributes"].get("media_duration") or 0 for p in players] or [0]) or None
    if any(p["attributes"].get("app_id") == DASHCAST_APP_ID or p["attributes"].get("app_name") == "DashCast"
           for p in players):
        return "lock", None
    # HA's own fault dashboards (ambient_displays) and Music Assistant audio are normal TV life, not watching
    if any(p["state"] not in ("off", "unavailable", "idle") and (
           p["attributes"].get("app_id") in ("A078F6B0", "music_assistant")
           or p["attributes"].get("app_name") == "Home Assistant Lovelace") for p in players):
        return "idle", None
    dur = max([p["attributes"].get("media_duration") or 0 for p in players] or [0]) or None
    tv = ha_get(t["television"], tok)
    if t["kind"] == "sony":
        rem = ha_get(t["remote"], tok)
        act = rem["attributes"].get("current_activity")
        if rem["state"] in ("off", "unavailable", "unknown") or tv["state"] in ("off", "standby"):
            return "off", dur
        if act in SONY_IDLE:
            return "idle", dur
        src = str(tv["attributes"].get("source") or "").upper()
        if (not act and "HDMI" in src) or (act and any(x in act.lower() for x in ("tvinput", "tv.input", ".inputs", "dtv"))):
            # the TV itself says HDMI/console: this beats our lock page still polling in the background
            # (switched to HDMI 1 over the lock page, which kept polling -> read as "lock", never locked)
            return "hdmi", dur
        seen_ = GUARD["seen"].get(t["ip"])
        if seen_ and time.time() - seen_[0] < 15:     # our own page is polling from this TV: it's the lock screen
            return "lock", dur
        if act and "mediashell" in act:
            # Chromecast receiver: our own lock page (just cast, HA not caught up) or someone casting (VR, phone)
            return ("lock" if time.time() - GUARD["last_cast"].get(t["ip"], 0) < 30 else "cast"), dur
        return "stream", dur
    if tv["state"] in ("off", "standby", "unavailable", "unknown"):
        return "off", dur
    seen = GUARD["seen"].get(t["ip"])
    if seen and time.time() - seen[0] < 15 and time.time() - GUARD["last_cast"].get(t["ip"], 0) < 6 * 3600:
        return "lock", dur
    src = str(tv["attributes"].get("source") or "")
    app = str(tv["attributes"].get("app_name") or "")
    if (app in ("_UNKNOWN_APP", "_NO_APP_RUNNING", "", "SmartCast Home") or "HOME" in app.upper()) \
            and not src.upper().startswith(("HDMI", "COMP")):
        return "idle", dur   # SmartCast home / nothing launched
    if src.upper().startswith(("HDMI", "COMP")) or src == "TV":
        return "hdmi", dur
    if src.upper() in ("CAST", "SMARTCAST", "CHROMECAST", "GOOGLE CAST"):
        return ("lock" if time.time() - GUARD["last_cast"].get(t["ip"], 0) < 30 else "cast"), dur
    return "stream", dur


def tv_adult_presence(t, tok, now=None):
    """Validate a short, room-local adult-viewing lease from the house camera controller.

    Presence at home, motion, a stale face, or an identity from another room never
    substitutes for this purpose-limited contract. The guard calls this every pass,
    so expiry revokes the exemption even when Home Assistant sends no new event.
    """
    spec = t.get("adult_presence") or {}
    entity, room = spec.get("entity"), spec.get("room")
    privacy_away_field = spec.get("privacy_away_field")
    people = set(spec.get("people") or [])
    out = {"allowed": False, "reason": "not_configured"}
    if not entity or not room or not people or not privacy_away_field:
        return out
    raw = ha_get(entity, tok) or {}
    attrs = raw.get("attributes") or {}
    if raw.get("state") != "present":
        return {"allowed": False, "reason": "not_present"}
    checks = (
        (attrs.get("schema_version") == 1, "schema"),
        (attrs.get("authority") == "chore_tv_only", "authority"),
        (attrs.get("room") == room, "room"),
        (attrs.get("person_key") in people, "person"),
        (attrs.get("confidence") == "high", "confidence"),
        (isinstance(attrs.get("kid_home"), list) and bool(attrs.get("kid_home")), "no_kid_home"),
        (attrs.get(privacy_away_field) is True, "privacy_presence"),
        (attrs.get("privacy_clear") is True, "privacy_hold"),
        (attrs.get("continuous_occupancy") is True, "room_not_continuously_occupied"),
    )
    failed = next((reason for ok, reason in checks if not ok), None)
    if failed:
        return {"allowed": False, "reason": failed}
    try:
        continuous = float(attrs["continuous_since"])
        face = float(attrs["face_at"])
        observed = float(attrs["observed_at"])
        valid = float(attrs["valid_until"])
        current = time.time() if now is None else float(now)
    except (KeyError, TypeError, ValueError):
        return {"allowed": False, "reason": "bad_time"}
    if not (continuous < face <= observed <= current < valid <= observed + 15.001):
        return {"allowed": False, "reason": "stale_or_inconsistent"}
    return {"allowed": True, "reason": "fresh_adult_in_room", "valid_until": valid,
            "observed_at": observed}


def adult_idle_screen(t, tok, now=None):
    """Use ordinary ambient content when only a named adult is placed at this TV."""
    room = t.get("room")
    adults = (t.get("adult_presence") or {}).get("people") or []
    if not room or not adults or t.get("kind") != "sony":
        return False
    current = time.time() if now is None else now

    def fresh(attrs):
        seen, until = attrs.get("observed_at"), attrs.get("valid_until")
        return (attrs.get("schema_version") == 1 and type(seen) in (int, float)
                and type(until) in (int, float) and seen <= current < until <= seen + 120)

    panel = t.get("external_display_entity")
    if panel:
        selection = ha_get(panel, tok)
        if (selection.get("state") in ("active", "loading", "restoring")
                and current < ((selection.get("attributes") or {}).get("lease_until") or 0)):
            return False
    occupancy = ha_get("sensor.room_retained_occupancy", tok)
    attrs = occupancy.get("attributes") or {}
    visit = (attrs.get("rooms") or {}).get(room) or {}
    if (occupancy.get("state") != "ok" or not fresh(attrs) or not attrs.get("roster_complete")
            or visit.get("state") not in ("occupied", "still") or visit.get("anonymous_group")
            or visit.get("state") == "still" and visit.get("display_state") not in ("remains", "roster_placed")
            or (visit.get("occupancy_bounds") or {}).get("minimum", 0) > 1
            or not current < (visit.get("valid_until") or 0) or len(visit.get("people") or []) != 1):
        return False
    tracks = ha_get("sensor.occupant_tracks", tok).get("attributes") or {}
    if not fresh(tracks):
        return False
    people = tracks.get("people") or []
    if (len(people) != attrs.get("expected") or attrs.get("unexplained", 0)
            or attrs.get("people_min") != len(people) or attrs.get("people_max") != len(people)):
        return False
    here = [p for p in people if room in (p.get("possible_rooms") or [])]
    unplaced = [p for p in people if not p.get("possible_rooms") or not p.get("located")]
    if unplaced:
        # The tracker can retain a named bedroom group across a restart even
        # when its interchangeable tracks temporarily have no individual room.
        groups = [v for k,v in attrs["rooms"].items() if k != room and v.get("group_members")
                  and v.get("display_state") in ("remains", "roster_placed")
                  and current < (v.get("valid_until") or 0)]
        if len(groups) != 1:
            return False
        members = set(groups[0]["group_members"])
        if (len(unplaced) != len(members) or len(people) != len(unplaced) + 1
                or any(p.get("possible_rooms") or p.get("display_name") and p["display_name"] not in members
                       for p in unplaced) or visit["people"][0] in members):
            return False
    return (len(here) == 1 and here[0].get("person_key") in adults
            and here[0].get("confirmed") is True and here[0].get("located") is True
            and here[0].get("possible_rooms") == [room]
            and here[0].get("display_name") == visit["people"][0])


def yield_chore_screen(t, tok, now):
    # Only leave our own polling /tv on the foreground receiver. Other DashCast
    # pages, native playback and explicit floorplan panel leases have their own owners.
    ip = t["ip"]
    seen, referer = GUARD["seen"].get(ip, (0, ""))
    if (not 0 <= now-seen < 15 or urllib.parse.urlsplit(referer).path != "/tv"
            or now - GUARD.setdefault("ambient_release", {}).get(ip, 0) < 30
            or app_of(t, tok) != "com.google.android.apps.mediashell"):
        return
    if tv_status(t, tok)[0] != "lock":
        return
    if ha_call("remote.send_command", {"entity_id": t["remote"], "command": ["HOME", "BACK", "BACK"],
                                       "delay_secs": 1}, tok):
        GUARD["ambient_release"][ip] = now
        ilog({"src": "tv", "action": "yield_chore_screen", "room": t.get("room")})


def yield_chore_screen_to_train(t, tok, now):
    """Stop only Chore's proven ordinary /tv DashCast so HA can take its fresh lease."""
    ip = t["ip"]
    seen, referer = GUARD["seen"].get(ip, (0, ""))
    parsed = urllib.parse.urlsplit(referer)
    query = urllib.parse.parse_qs(parsed.query)
    if (not 0 <= now-seen < 15 or parsed.path != "/tv"
            or any(key != "v" for key in query)
            or now - GUARD.setdefault("ambient_release", {}).get(ip, 0) < 30
            or tv_status(t, tok)[0] != "lock"):
        return False
    GUARD["ambient_release"][ip] = now
    ilog({"src": "tv", "action": "yield_chore_screen_to_train", "room": t.get("room")})
    threading.Thread(target=lambda: subprocess.run([CATT, "-d", ip, "stop"], timeout=30,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
                     name="train-ambient-yield", daemon=True).start()
    return True


def tv_reward_lifecycle(evs):
    """Newest durable pause/resume transition for each purchased reward session."""
    state = {}
    undone = {e["ref"] for e in evs if e["type"] == "undo"}
    for e in evs:
        if e["id"] not in undone and e["type"] in ("tv_reward_pause", "tv_reward_resume") and e.get("session"):
            state[e["session"]] = e
    return state


def _tv_grants(evs, now=None, reward_lifecycle=True):
    """Per TV: active unlocks [{id, until, kinds, kid, what, by, maxdur, ts}] after adult adjustments."""
    now = now or time.time()
    undone = {e["ref"] for e in evs if e["type"] == "undo"}
    g = {t: [] for t in TVS}
    started, mins_override, moved, adjustments = {}, {}, {}, []
    for e in evs:
        if e["type"] == "tv_move" and e["id"] not in {x["ref"] for x in evs if x["type"] == "undo"}:
            moved[e["ref"]] = e["tv"]
        if e["type"] == "tv_started" and e.get("ref") not in started:
            started[e["ref"]] = e.get("at") or e["ts"]   # "at": backdated start (watching began before we noticed)
        if e["type"] == "grant_minutes":
            mins_override[e["ref"]] = e["minutes"]
        if e["type"] == "tv_set_until" and e["id"] not in undone:
            adjustments.append(e)
    for e in evs:
        if e["id"] in undone:
            continue
        if e["type"] == "spend" and e.get("reward") in UNLOCKS:
            mins, kinds, maxdur = UNLOCKS[e["reward"]]
            until = e.get("until") or (e["ts"] + mins * 60 if e["reward"] == "vr" else 0)
            if not until:
                continue
            armed, m = False, round((until - e["ts"]) / 60)
            if e["reward"] != "vr" and e.get("by") in (None, e["kid"]):
                # bought on the Hub: the clock starts when the TV is first seen in use
                m = mins_override.get(e["id"]) or e.get("minutes") or round((until - e["ts"]) / 60)
                st = started.get(e["id"])
                if st:
                    until = st + m * 60
                else:
                    until, armed = e["ts"] + max(m, 90) * 60, True   # unused for 90 min -> expires (room to queue behind a sibling's show)
            tvsel = moved.get(e["id"], e.get("tv"))
            for tid in (list(TVS) if tvsel == "*" or (e["reward"] == "vr" and tvsel not in TVS) else [tvsel]):
                if tid in g:
                    g[tid].append({"id": e["id"], "ts": e["ts"], "until": until, "kinds": sorted(kinds), "kid": e["kid"],
                                   "start": started.get(e["id"]) or e.get("started_at") or e["ts"],
                                   "what": e["reward"], "by": e.get("by") or e["kid"], "armed": armed, "group": e.get("group"), "mins": m,
                                   "game_mode": e.get("game_mode"), "game_player": e.get("game_player"),
                                   "maxdur": None if e.get("minutes") else maxdur})
        elif e["type"] == "tv_unlock":
            for tid in (list(TVS) if e["tv"] == "*" else [e["tv"]]):
                if tid not in g:
                    continue
                if not e.get("minutes"):          # legacy "lock again now"
                    for x in g[tid]:
                        x["until"] = min(x["until"], e["ts"])
                    continue
                g[tid].append({"id": e["id"], "ts": e["ts"], "until": e["until"], "start": e["ts"],
                               "kinds": e.get("kinds") or ["stream", "cast", "hdmi"], "kid": e.get("kid"),
                               "what": e.get("what") or "other", "by": e["by"], "maxdur": None})
    if reward_lifecycle:
        lifecycle = tv_reward_lifecycle(evs)
        for tid, rows in g.items():
            kept = []
            for x in rows:
                transition = lifecycle.get(x.get("group") or x["id"])
                if transition and transition.get("tv") in (None, tid):
                    if transition["type"] == "tv_reward_pause":
                        continue
                    x["until"] = float(transition.get("until") or
                                       (transition["ts"] + float(transition.get("remaining_s") or 0)))
                    x["start"], x["armed"] = transition["ts"], False
                    x["_reward_transition_ts"] = transition["ts"]
                kept.append(x)
            g[tid] = kept
    # Adjustments are applied last so +10/-10/Lock after a resumed reward operate on its new clock.
    for e in adjustments:
        for tid in (list(TVS) if e["tv"] == "*" else [e["tv"]]):
            for x in g.get(tid, []):
                if x["ts"] < e["ts"] and e["ts"] > x.get("_reward_transition_ts", 0) and x["until"] > e["ts"]:
                    x["until"] = e["until"]
    for rows in g.values():
        for x in rows:
            x.pop("_reward_transition_ts", None)
    return {t: [x for x in xs if x["until"] > now] for t, xs in g.items()}


def tv_grants(evs, now=None):
    return _tv_grants(evs, now, reward_lifecycle=True)


def saved_tv_rewards(evs, now=None):
    """Paused purchased-time balances. They are ledger state, not media-player timelines."""
    now = time.time() if now is None else float(now)
    lifecycle = tv_reward_lifecycle(evs)
    original = _tv_grants(evs, now=0.000001, reward_lifecycle=False)
    by_session = {}
    for tid, rows in original.items():
        for row in rows:
            session = row.get("group") or row["id"]
            transition = lifecycle.get(session)
            if not transition or transition["type"] != "tv_reward_pause" or transition.get("tv") not in (None, tid):
                continue
            saved = by_session.setdefault(session, {"session_id": session, "tv": tid, "reward": row["what"],
                                                     "kids": [], "remaining_s": float(transition["remaining_s"]),
                                                     "paused_at": transition["ts"], "paused_by": transition.get("by"),
                                                     "kinds": list(row.get("kinds") or []),
                                                     "playback": transition.get("playback")})
            if row.get("kid") and row["kid"] not in saved["kids"]:
                saved["kids"].append(row["kid"])
    return sorted(by_session.values(), key=lambda x: x["paused_at"], reverse=True)


def active_unlocks(now=None):
    return tv_grants(read_events(), now)


def active_unlocks_raw(evs, now=None):
    return tv_grants(evs, now)


CT_UNTIL = DATA / "chore_time_until.json"   # explicit end ("rest of today until 9 PM"), tied to one switch-on
CT_PAUSE = DATA / "chore_pause.json"          # (retired) parent pause
CT_OVERRIDE = DATA / "chore_override.json"    # parent flipped chore mode until the next natural change point
AUTO_START_H = int((HOUSE.get("hours") or {}).get("chore_start", 5))   # chore time: the kids' waking hours
AUTO_END_H = int((HOUSE.get("hours") or {}).get("chore_end", 21))
CHILL_JOBS, CHILL_POINTS = 3, 6               # in the last 3 h -> "chill": no nagging screens (locks still apply)
IDLE_SCREEN_MAX_S = 30 * 60                   # brain screen idle this long with no watch attempt -> back to standby


def auto_chore(tok):
    """Auto chore mode: 5 AM-9 PM whenever any kid's HA home switch is on, unless a parent paused it.
    Returns (end_ts or None, info)."""
    from datetime import datetime
    now = datetime.now()
    home = [k["name"] for k in cfg()["kids"] if k.get("home_entity") and ha_get(k["home_entity"], tok).get("state") == "on"]
    end = now.replace(hour=AUTO_END_H, minute=0, second=0, microsecond=0).timestamp()
    in_hours = AUTO_START_H <= now.hour < AUTO_END_H
    paused = 0
    try:
        paused = json.loads(CT_PAUSE.read_text()).get("until", 0)
    except (OSError, ValueError):
        pass
    info = {"home": home, "in_hours": in_hours, "paused_until": paused if paused > time.time() else 0}
    if in_hours and home and not info["paused_until"]:
        return end, info
    return None, info


def chore_time(tok):
    st = ha_get(CT_SWITCH, tok)
    if st["state"] != "on":
        return None
    from datetime import datetime
    start = datetime.fromisoformat(st["last_changed"]).timestamp()
    try:
        o = json.loads(CT_UNTIL.read_text())
        if abs(o.get("switched_on", 0) - start) < 120 or o.get("set_at", 0) >= start - 5:
            return o["until"]
    except (OSError, ValueError, KeyError):
        pass
    hrs = float(ha_get(CT_HOURS, tok)["state"] or 2)
    return start + hrs * 3600


def start_armed_clocks(tok, unl):
    """Chore time OFF: TVs are free, but a show a kid PAID for still starts its clock when the TV
    is first in use, so the time left (login screen, her screen) is real. Mirrors the chore-time loop below."""
    for tid, t in TVS.items():
        us = [u for u in unl.get(tid, []) if u["what"] != "vr"]
        if not any(u.get("armed") for u in us):
            continue
        what, _ = tv_status(t, tok)
        running = [v for v in us if not v.get("armed") and v["until"] > time.time()]
        started_g = set()
        for u in sorted(us, key=lambda v: v["ts"]):
            g_ = u.get("group") or u["id"]
            if u.get("armed") and what in set(u["kinds"]) and not any((v.get("group") or v["id"]) != g_ for v in running) \
                    and (not started_g or g_ in started_g):
                started_g.add(g_)
                append({"type": "tv_started", "ref": u["id"], "tv": tid, "what": what, "chore_time": False})
                ilog({"src": "tv", "tv": tid, "action": "clock_started", "ref": u["id"], "chore_time": False})
                threading.Thread(target=after_change, daemon=True).start()


def watch_tvs():
    try:
        tok = ha_token()
    except OSError:
        return
    was_on = bool(chore_time(tok))  # a restart mid chore-time must not hand out a fresh grace
    GUARD["tv_said"] = set()
    for tid, us in active_unlocks().items():
        tv_us = [u for u in us if u["what"] != "vr"]
        if tv_us:
            end = max(u["until"] for u in tv_us)
            if end - time.time() <= 300:
                GUARD["tv_said"].add((tid, round(end)))
    while True:
        try:
            refresh_spotify_interruption(tok)
            try:
                vr_timers(tok)
            except Exception as ex:  # noqa: BLE001
                print("vr timer error", ex, flush=True)
            aend, ainfo = auto_chore(tok)
            auto_on = bool(aend)
            ov = None
            try:
                ov = json.loads(CT_OVERRIDE.read_text())
            except (OSError, ValueError):
                pass
            if ov and ov.get("base_auto") != auto_on:     # the natural change point arrived: back to automatic
                try:
                    CT_OVERRIDE.unlink()
                except OSError:
                    pass
                ilog({"src": "mode", "action": "override_ended", "auto": auto_on})
                ov = None
            on = ov["state"] if ov else auto_on
            from datetime import datetime, timedelta
            n_ = datetime.now()
            nxt = n_.replace(hour=AUTO_END_H, minute=0, second=0, microsecond=0) if AUTO_START_H <= n_.hour < AUTO_END_H else \
                (datetime.combine(n_.date() + (timedelta(days=1) if n_.hour >= AUTO_END_H else timedelta()), datetime.min.time()).replace(hour=AUTO_START_H))
            # chill: lots done recently -> stop nagging (no persistent chore screens), locks still apply
            st_ = derive()
            GUARD["kid_workflow_open"] = kid_workflow_open(st_)
            recent = [r for r in st_["history"] if r.get("type") == "report" and r.get("status") == "inspected" and not r.get("partial")
                      and (r.get("inspected_ts") or 0) > time.time() - 3 * 3600]
            chill = len(recent) >= CHILL_JOBS or sum(int(r.get("awarded") or 0) for r in recent) >= CHILL_POINTS
            GUARD["mode"] = {"on": on, "auto": auto_on, "override": bool(ov), "by": (ov or {}).get("by"), "next_change": nxt.timestamp(),
                             "chill": chill, "recent_jobs": len(recent), **ainfo}
            end = (aend if (on and auto_on) else nxt.timestamp()) if on else None
            dress_on = bool((st_.get("dress") or {}).get("on"))   # "Put on clothes!" takes every TV in use, no grace
            if dress_on and not end:
                end = time.time() + 60
            if dress_on:
                GUARD["grace"], GUARD["finish"] = {}, {}
            GUARD["lock_until"] = end or 0
            want = "on" if on else "off"   # HA's chore_time switch mirrors the effective state (not the dressing race)
            if ha_get(CT_SWITCH, tok).get("state") != want:
                ha_call("input_boolean.turn_" + want, {"entity_id": CT_SWITCH}, tok)
            if not end:
                # a parent switching chore time OFF during the kids' hours frees the TVs only: the Hubs stay on the
                # chore screens so the kids can still do jobs and spend points. The natural end gives them back.
                hubs_stay = auto_on or GUARD.get("kid_workflow_open")
                try:
                    start_armed_clocks(tok, active_unlocks())
                except Exception as ex:  # noqa: BLE001
                    print("armed clock error", ex, flush=True)
                if hubs_stay:
                    try:
                        keep_hubs_on_chores(tok)
                    except Exception as ex:  # noqa: BLE001
                        print("hub keeper error", ex, flush=True)
                if was_on:  # chore time just ended: take the resting screen down, and give the Hubs back
                    for hid, h in HUBS.items():
                        if hubs_stay:
                            break
                        if hub_showing_chores(h["ip"]):
                            ilog({"src": "hub", "hub": hid, "action": "chore_mode_off_stop"})
                            subprocess.run([CATT, "-d", h["ip"], "stop"], timeout=30,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    for t in TVS.values():
                        if tv_status(t, tok)[0] == "lock":
                            subprocess.run([CATT, "-d", t["ip"], "stop"], timeout=30,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                was_on = False
                time.sleep(3)
                continue
            if not was_on and not dress_on:  # just switched on: let anything already playing finish its episode
                GUARD["grace"] = {}
                for tid, t in TVS.items():
                    what, _ = tv_status(t, tok)
                    if what in ("stream", "cast"):
                        left = remaining_s(t, tok)
                        if not left:   # no reliable end of show (Netflix) => no grace, it locks on the timer
                            continue
                        secs = left + GRACE_EXTRA_S
                        GUARD["grace"][tid] = {"until": time.time() + min(secs, FINISH_CAP_S), "app": app_of(t, tok)}
                        print("TV guard: grace", tid, int(secs), "s", flush=True)
            was_on = True
            try:
                keep_hubs_on_chores(tok)
            except Exception as ex:  # noqa: BLE001
                print("hub keeper error", ex, flush=True)
            unl = active_unlocks()
            said = GUARD.setdefault("tv_said", set())
            kn = {k["id"]: k["name"] for k in cfg()["kids"]}
            for tid, us in unl.items():
                tv_us = [u for u in us if u["what"] != "vr"]
                if not tv_us:
                    continue
                end = max(u["until"] for u in tv_us)
                key = (tid, round(end))
                if 0 < end - time.time() <= 300 and key not in said and GUARD["status"].get(tid, {}).get("what") in ("stream", "hdmi", "cast"):
                    said.add(key)
                    who = next((kn.get(u["kid"]) for u in tv_us if u.get("kid")), None)
                    announce((f"{who}, " if who else "") + f"five minutes of TV time left on the {TVS[tid]['name'].replace(' TV', '')} TV.", tok)
                    if parent_available(tok):
                        for svc in PARENT_NOTIFY:
                            data = phone_notice_data(LEAD_ADULT, {"tag": "chore-tv-" + tid, "group": "chores",
                                                     "priority": "high", "ttl": 0,
                                                     "actions": [{"action": "CHORE_TVPLUS_" + tid, "title": "+10 min"},
                                                                 {"action": "CHORE_TVLOCK_" + tid, "title": "Lock now"}]}, tok)
                            if data is not None:
                                ha_call(svc, {"title": "Chores: 5 min of TV left",
                                              "message": f"{TVS[tid]['name']}" + (f" ({who})" if who else "") + " locks in 5 minutes.",
                                              "data": data}, tok)
            for tid, t in TVS.items():
                if active_spotify_interruption(t.get("room") or tid):
                    GUARD["status"][tid] = {"what": "music_block", "dur": 0, "at": time.time()}
                    continue   # HA paused Spotify; do not cast, lock, announce, or change volume over its explanation
                what, dur = tv_status(t, tok)
                adult_presence = tv_adult_presence(t, tok)
                ambient = train_ambient_lease(t, tok)
                if (ambient["allowed"] and not dress_on and not unl.get(tid)
                        and not GUARD.setdefault("grace", {}).get(tid)
                        and not GUARD.setdefault("finish", {}).get(tid)):
                    GUARD["status"][tid] = {"what": what, "allowed": True, "duration": dur,
                                            "ambient_reason": ambient["reason"], "ambient": ambient}
                    if what == "lock":
                        yield_chore_screen_to_train(t, tok, time.time())
                    continue
                allowed = False
                if dress_on:   # no TV time for anyone until they're dressed: straight to the clothes screen
                    unl = {}
                # separate purchases on one TV run back to back; a "together" group shares one clock
                running = [v for v in unl.get(tid, []) if not v.get("armed") and v["what"] != "vr" and v["until"] > time.time()]
                if running:
                    GUARD.setdefault("tv_last_end", {})[tid] = max(v["until"] for v in running)
                started_g = set()
                for u in sorted(unl.get(tid, []), key=lambda v: v["ts"]):
                    kinds, maxdur = set(u["kinds"]), u.get("maxdur")
                    g_ = u.get("group") or u["id"]
                    if u.get("armed") and what in kinds and not any((v.get("group") or v["id"]) != g_ for v in running) \
                            and (not started_g or g_ in started_g):
                        started_g.add(g_)
                        append({"type": "tv_started", "ref": u["id"], "tv": tid, "what": what})
                        ilog({"src": "tv", "tv": tid, "action": "clock_started", "ref": u["id"], "group": u.get("group")})
                        print("TV guard: clock started", tid, u["id"], flush=True)
                        threading.Thread(target=after_change, daemon=True).start()
                        if time.time() - GUARD.setdefault("tv_last_end", {}).get(tid, 0) < 180 and u["kid"] and \
                                GUARD.setdefault("turn_said", {}).get(tid) != g_:
                            GUARD["turn_said"][tid] = g_
                            nm = " and ".join(kn.get(x["kid"], x["kid"]) for x in unl[tid] if (x.get("group") or x["id"]) == g_)
                            announce(f"{nm}'s turn on the {TVS[tid]['name'].replace(' TV', '')} TV. Your time starts now.", tok)
                    if what in kinds and not (maxdur and dur and dur > maxdur):
                        allowed = True
                g = GUARD["grace"].get(tid)
                if g:
                    # grace ends at its deadline, when the TV stops/idles, or when they switch apps
                    if time.time() > g["until"] or what in ("off", "idle", "hdmi") or app_of(t, tok) != g["app"]:
                        GUARD["grace"].pop(tid, None)
                    elif what in ("stream", "cast"):
                        allowed = True
                if adult_presence["allowed"] and what in ("stream", "cast", "hdmi"):
                    allowed = True
                # finish the show: when the time runs out mid-episode on a TV that reliably reports
                # position+length, keep it open to the end of THAT title (never past an hour into it); a new
                # title (autoplay), pause, app switch or a parent's Lock now ends it. Unreliable TVs lock on the timer.
                fin = GUARD.setdefault("finish", {})
                if t.get("finish_show"):
                    if allowed and what in ("stream", "cast"):
                        GUARD.setdefault("allowed_at", {})[tid] = time.time()
                    f = fin.get(tid)
                    if not allowed and not f and what in ("stream", "cast") and \
                            time.time() - GUARD.setdefault("allowed_at", {}).get(tid, 0) < 30 and \
                            time.time() - GUARD.setdefault("hard_lock", {}).get(tid, 0) > 90 and \
                            time.time() - GUARD.setdefault("finish_done", {}).get(tid, 0) > 600:
                        it = playing_item(t, tok)
                        if it:
                            secs = min(it["left"] + GRACE_EXTRA_S, FINISH_CAP_S - it["pos"])
                            ilog({"src": "tv", "tv": tid, "action": "finish_show" if secs > 30 else "finish_refused_movie",
                                  "title": it["title"], "left": round(it["left"]), "pos": round(it["pos"]), "secs": round(secs)})
                            if secs > 30:
                                f = fin[tid] = {"until": time.time() + secs, "title": it["title"], "app": app_of(t, tok)}
                                who = next((kn.get(u["kid"]) for u in GUARD.get("last_grants", {}).get(tid, []) if u.get("kid")), None)
                                announce((f"{who}, " if who else "") + "time's up. You can finish this episode, then the TV locks.", tok)
                    if f:
                        it = playing_item(t, tok)
                        if time.time() > f["until"] or what not in ("stream", "cast") or not it or it["title"] != f["title"] \
                                or app_of(t, tok) != f["app"]:
                            fin.pop(tid, None)
                            GUARD.setdefault("finish_done", {})[tid] = time.time()
                            ilog({"src": "tv", "tv": tid, "action": "finish_show_end", "title": f["title"],
                                  "why": "time" if time.time() > f["until"] else ("stopped" if not it else "changed")})
                        else:
                            allowed = True
                if unl.get(tid):
                    GUARD.setdefault("last_grants", {})[tid] = unl[tid]
                prev = GUARD["status"].get(tid, {})
                if prev.get("what") != what or prev.get("allowed") != allowed \
                        or (prev.get("adult_presence") or {}).get("allowed") != adult_presence["allowed"]:
                    ilog({"src": "tv", "tv": tid, "what": what, "allowed": allowed,
                          "adult_present": adult_presence["allowed"], "app": app_of(t, tok)})
                GUARD["status"][tid] = {"what": what, "allowed": allowed, "duration": dur,
                                        "grace_until": (GUARD["grace"].get(tid) or {}).get("until"),
                                        "finish": fin.get(tid), "adult_presence": adult_presence}
                if what not in ("stream", "hdmi", "cast") or allowed:
                    GUARD.setdefault("fails", {})[tid] = 0
                prev_what = (GUARD.setdefault("prev", {})).get(tid)
                GUARD["prev"][tid] = what
                if what in ("lock", "idle") and not unl.get(tid) and adult_idle_screen(t, tok):
                    GUARD["status"][tid]["ambient_reason"] = "adult_in_room"
                    GUARD.setdefault("lock_since", {}).pop(tid, None)
                    if what == "lock":
                        yield_chore_screen(t, tok, time.time())
                    continue
                if (t["kind"] == "sony" and what == "idle" and prev_what == "lock" and not allowed
                        and not (GUARD.get("mode") or {}).get("chill") and not GUARD.setdefault("standby", {}).get(tid)
                        and app_of(t, tok) == "com.google.android.apps.tv.launcherx"):
                    print("TV guard: lock escaped to home screen, back to brain screen", tid, flush=True)
                    ilog({"src": "tv", "tv": tid, "action": "relock_home"})
                    GUARD["last_cast"][t["ip"]] = time.time()
                    threading.Thread(target=cast, args=(t["ip"], "/tv"), daemon=True).start()
                    continue
                # Chore time: a TV with a kid screen shows it whenever it's on and idle (persistent)
                if (t["kind"] == "sony" and what == "idle" and not allowed and not unl.get(tid)
                        and not (GUARD.get("mode") or {}).get("chill") and not GUARD.setdefault("standby", {}).get(tid)
                        and time.time() - GUARD["last_cast"].get(t["ip"], 0) > 60):
                    print("TV guard: idle during chore time -> chore screen", tid, flush=True)
                    ilog({"src": "tv", "tv": tid, "action": "persist_cast"})
                    GUARD["last_cast"][t["ip"]] = time.time()
                    threading.Thread(target=cast, args=(t["ip"], "/tv"), daemon=True).start()
                    continue
                ls = GUARD.setdefault("lock_since", {})
                if what == "lock":
                    ls.setdefault(tid, time.time())
                    if time.time() - ls[tid] > IDLE_SCREEN_MAX_S and not GUARD.setdefault("attempt", {}).get(tid, 0) > ls[tid]:
                        print("TV guard: brain screen idle 30 min -> standby", tid, flush=True)
                        ilog({"src": "tv", "tv": tid, "action": "screen_timeout"})
                        GUARD.setdefault("standby", {})[tid] = True   # was a KeyError that crashed every guard pass
                        ls.pop(tid, None)
                        threading.Thread(target=lambda ip=t["ip"]: subprocess.run([CATT, "-d", ip, "stop"], timeout=30,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL), daemon=True).start()
                        continue
                else:
                    ls.pop(tid, None)
                if what in ("stream", "hdmi", "cast") and not allowed:
                    GUARD.setdefault("attempt", {})[tid] = time.time()
                    GUARD.setdefault("standby", {})[tid] = False
                    f = GUARD.setdefault("fails", {})
                    if t.get("home_first") and time.time() - GUARD["last_cast"].get(t["ip"], 0) > 25:
                        GUARD["last_cast"][t["ip"]] = time.time()
                        if f.get(tid, 0) >= 2:
                            print("TV guard: lock screen not loading, turning off", tid, flush=True)
                            ilog({"src": "tv", "tv": tid, "action": "turn_off", "what": what})
                            ha_call("media_player.turn_off", {"entity_id": t["television"]}, tok)
                            f[tid] = 0
                        else:
                            f[tid] = f.get(tid, 0) + 1
                            print("TV guard: home + lock", tid, what, flush=True)
                            ilog({"src": "tv", "tv": tid, "action": "home_then_cast", "what": what})

                            def go(t=t):
                                ha_call("remote.send_command", {"entity_id": t["home_first"], "command": "HOME"}, tok)
                                time.sleep(4)
                                cast(t["ip"], "/tv")
                            threading.Thread(target=go, daemon=True).start()
                    elif t.get("off_only") and time.time() - GUARD["last_cast"].get(t["ip"], 0) > 20:
                        print("TV guard: turning off", tid, what, flush=True)
                        ilog({"src": "tv", "tv": tid, "action": "turn_off", "what": what})
                        ha_call("media_player.turn_off", {"entity_id": t["television"]}, tok)
                        GUARD["last_cast"][t["ip"]] = time.time()
                    elif t.get("off_only"):
                        pass
                    elif f.get(tid, 0) >= 3:  # the lock screen will not stick on this TV: switch it off instead
                        print("TV guard: cast not sticking, turning off", tid, flush=True)
                        ha_call("media_player.turn_off", {"entity_id": t["television"]}, tok)
                        f[tid] = 0
                        GUARD["last_cast"][t["ip"]] = time.time()
                    elif time.time() - GUARD["last_cast"].get(t["ip"], 0) > 20:
                        f[tid] = f.get(tid, 0) + 1
                        GUARD["last_cast"][t["ip"]] = time.time()
                        print("TV guard: locking", tid, what, dur, flush=True)
                        ilog({"src": "tv", "tv": tid, "action": "lock_cast", "what": what})
                        threading.Thread(target=cast, args=(t["ip"], "/tv"), daemon=True).start()
        except Exception as ex:  # noqa: BLE001
            print("TV guard error", ex, flush=True)
        time.sleep(2)


HOME_DAYS = DATA / "home_days.json"   # {kid: [ "YYYY-MM-DD", ... ]} days the kid's HA home switch was on at some point
HOME_ARRIVALS = DATA / "home_arrivals.json"   # {kid: {"YYYY-MM-DD": "HH:MM"}} when a kid got home after a day away


def load_home_days():
    try:
        return {k: set(v) for k, v in json.loads(HOME_DAYS.read_text()).items()}
    except (OSError, ValueError):
        return {}


def save_home_days(hd):
    HOME_DAYS.write_text(json.dumps({k: sorted(v) for k, v in hd.items()}))


def load_home_arrivals():
    """{kid: {date: "HH:MM"}}: when a kid got home on a day after a day away"""
    try:
        return json.loads(HOME_ARRIVALS.read_text())
    except (OSError, ValueError):
        return {}


def save_home_arrivals(ar):
    HOME_ARRIVALS.write_text(json.dumps(ar, sort_keys=True))


def watch_home_days():
    """Record which days each kid was home (streaks skip days away). Backfill from HA history once (~10 days kept)."""
    from datetime import datetime, date, timedelta
    tok = ha_token()
    hd = load_home_days()
    ar = load_home_arrivals()
    kids = cfg()["kids"]
    try:
        start = (datetime.now() - timedelta(days=12)).strftime("%Y-%m-%dT00:00:00")
        ents = ",".join(k["home_entity"] for k in kids if k.get("home_entity"))
        req = urllib.request.Request(f"{HA_URL}/api/history/period/{start}?filter_entity_id={ents}",
                                     headers={"Authorization": f"Bearer {tok}"})
        hist = json.loads(urllib.request.urlopen(req, timeout=60).read())
        for rows in hist:
            if not rows:
                continue
            k = next((x["id"] for x in kids if x.get("home_entity") == rows[0]["entity_id"]), None)
            if not k:
                continue
            for i, r in enumerate(rows):   # a day counts if the switch was on at any moment of it
                if r["state"] != "on":
                    continue
                a = datetime.fromisoformat(r["last_changed"]).astimezone()
                b = datetime.fromisoformat(rows[i + 1]["last_changed"]).astimezone() if i + 1 < len(rows) else datetime.now().astimezone()
                d = a.date()
                if (d - timedelta(days=1)).isoformat() not in hd.get(k, set()) and d.isoformat() not in hd.get(k, set()):
                    ar.setdefault(k, {}).setdefault(d.isoformat(), a.strftime("%H:%M"))   # home after a day away
                while d <= b.date():
                    hd.setdefault(k, set()).add(d.isoformat())
                    d += timedelta(days=1)
        save_home_days(hd)
        save_home_arrivals(ar)
    except Exception as ex:  # noqa: BLE001
        print("home-days backfill failed", ex, flush=True)
    while True:
        try:
            changed = False
            home_now = set()
            for k in cfg()["kids"]:
                if k.get("home_entity") and ha_get(k["home_entity"], tok).get("state") == "on":
                    home_now.add(k["id"])
                    t = date.today().isoformat()
                    if t not in hd.setdefault(k["id"], set()):
                        if (date.today() - timedelta(days=1)).isoformat() not in hd[k["id"]]:
                            ar.setdefault(k["id"], {})[t] = datetime.now().strftime("%H:%M")   # home after a day away
                            save_home_arrivals(ar)
                        hd[k["id"]].add(t)
                        changed = True
            if changed:
                save_home_days(hd)
            GUARD["home_days"], GUARD["home_arrivals"], GUARD["home_now"] = hd, ar, home_now
        except Exception as ex:  # noqa: BLE001
            print("home-days error", ex, flush=True)
        time.sleep(30)


_MIRROR = HOUSE.get("dashboard_mirror") or {}   # optional: keep a grown-up's own HA dashboard in step
MAE_DASH = _MIRROR.get("dashboard")
MAE_ORDER = [tuple(v) for v in (_MIRROR.get("views") or [])]


def sync_mae_dashboard():
    """A grown-up's own HA dashboard (house.json dashboard_mirror): listed views are mirrored
    from another dashboard whenever they change, so they never drift."""
    import asyncio, hashlib
    import websockets
    tok = ha_token()
    async def run():
        async with websockets.connect(HA_URL.replace("http", "ws") + "/api/websocket", max_size=None) as ws:
            await ws.recv(); await ws.send(json.dumps({"type": "auth", "access_token": tok})); await ws.recv()
            n = [0]
            async def call(m):
                n[0] += 1; m["id"] = n[0]; await ws.send(json.dumps(m))
                while True:
                    r = json.loads(await ws.recv())
                    if r.get("id") == n[0]:
                        return r
            dl = (await call({"type": "lovelace/dashboards/list"}))["result"]
            if not any(d["url_path"] == MAE_DASH for d in dl):
                await call({"type": "lovelace/dashboards/create", "url_path": MAE_DASH, "title": _MIRROR.get("title", "Home"), "mode": "storage",
                            "show_in_sidebar": True, "require_admin": False, "icon": "mdi:home-heart"})
            ov = (await call({"type": "lovelace/config"}))["result"]
            byp = {v.get("path"): v for v in ov["views"]}
            byt = {v.get("title"): v for v in ov["views"]}
            views, missing = [], []
            for path, title in MAE_ORDER:
                v = dict(byp.get(path) or byt.get(title) or {})   # a renamed path is still found by its title
                if not v:
                    missing.append(path)
                    continue
                v.pop("visible", None)
                v["title"] = title
                if path == "chores":
                    v["icon"] = "mdi:broom"
                views.append(v)
            if missing:   # never shrink someone's dashboard because a source view moved; say so and keep theirs
                print("dashboard mirror: source views not found, not syncing:", missing, flush=True)
                return
            cfg = {"title": _MIRROR.get("title", "Home"), "views": views}
            sig = hashlib.md5(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
            if GUARD.get("mae_sig") != sig:
                r = await call({"type": "lovelace/config/save", "url_path": MAE_DASH, "config": cfg})
                GUARD["mae_sig"] = sig
                print("dashboard mirror synced", r.get("success"), [v["title"] for v in views], flush=True)
    asyncio.run(run())


def watch_mae_dashboard():
    while True:
        try:
            sync_mae_dashboard()
        except Exception as ex:  # noqa: BLE001
            print("dashboard mirror failed", ex, flush=True)
        time.sleep(600)


# Bright rooms while a chore runs: room light mode -> White, shades open (daytime); restored when it ends.
_LIGHTS = HOUSE.get("lights") or {}
# chore -> (room light-mode selects to turn bright white, shades to open in daylight). A bundle chore with no entry
# of its own uses its rooms' entries.
CHORE_ROOMS = {k: (v.get("modes") or [], v.get("covers") or []) for k, v in (_LIGHTS.get("chore_rooms") or {}).items()}


def chore_rooms_for(cid):
    if cid in CHORE_ROOMS:
        return CHORE_ROOMS[cid]
    ch = next((x for x in cfg()["chores"] if x["id"] == cid), {})
    if ch.get("bundle"):
        parts = [CHORE_ROOMS[x] for x in ch["bundle"] if x in CHORE_ROOMS]
        return sorted({e for p_ in parts for e in p_[0]}), sorted({e for p_ in parts for e in p_[1]})
    return [], []


# room (light-mode name) -> colour bulbs that show the working kid's colour:
# {"bulbs": [always], "pick": N, "pool": [N picked at random per job], "ref": light to copy back on release}
SIGNAL_BULBS = _LIGHTS.get("signal_bulbs") or {}
BRIGHT = DATA / "brightened.json"
KID_OPEN = DATA / "kid_open.json"   # kid -> last time they used a screen (for "you left a job" reminders)


def hex_rgb(h):
    h = (h or "#ffffff").lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)]


def near_rgb(attrs, rgb):
    a = attrs.get("rgb_color")
    if not a:
        return False
    peak = max(rgb) or 1
    want = [c * 255 / peak for c in rgb]
    return sum((x - y) ** 2 for x, y in zip(a, want)) ** .5 <= 60


SIGNAL_STATE = DATA / "signal_bulbs.json"   # random picks + held bulbs survive a restart (no jumping, no stuck colour)


def apply_signal_bulbs(rooms, tok):
    if "signal_held" not in GUARD:
        try:
            sv = json.loads(SIGNAL_STATE.read_text())
            GUARD["signal_held"], GUARD["signal_pick"] = sv.get("held", {}), sv.get("pick", {})
        except (OSError, ValueError):
            pass
    before = json.dumps([GUARD.get("signal_held"), GUARD.get("signal_pick")], sort_keys=True)
    try:
        _apply_signal_bulbs(rooms, tok)
    finally:
        after = json.dumps([GUARD.get("signal_held"), GUARD.get("signal_pick")], sort_keys=True)
        if after != before:
            SIGNAL_STATE.write_text(json.dumps({"held": GUARD.get("signal_held", {}), "pick": GUARD.get("signal_pick", {})}))


def _apply_signal_bulbs(rooms, tok):
    held = GUARD.setdefault("signal_held", {})          # bulb -> (room, ref)
    want = {}
    kcfg = {k["id"]: k for k in cfg()["kids"]}
    picks = GUARD.setdefault("signal_pick", {})       # room -> random pool bulbs, kept for the whole job
    for room in [r for r in picks if r not in rooms]:
        picks.pop(room)
    for room, info in rooms.items():
        cfgb = SIGNAL_BULBS.get(room)
        if not cfgb or not info.get("colors"):
            continue
        if cfgb.get("pick") and room not in picks:
            picks[room] = random.sample(cfgb["pool"], cfgb["pick"])
        bulbs = cfgb["bulbs"] + picks.get(room, [])
        refs = [cfgb["ref"]] if cfgb.get("ref") else [x for x in cfgb.get("pool", []) if x not in bulbs]
        # a kid's wish: working alone, some bulbs show their 'family' colour beside their own
        # (2+ bulbs: one purple; 5+ bulbs like the living room: a couple, the sconce pair). His setting only.
        solo = info.get("kids") or []
        fam = kcfg.get(solo[0], {}).get("family_color") if len(solo) == 1 else None
        nfam = (2 if len(bulbs) >= 5 else 1) if fam and len(bulbs) >= 2 else 0
        for i, b in enumerate(bulbs):
            want[b] = (hex_rgb(fam if i < nfam else info["colors"][i % len(info["colors"])]), refs)
    GUARD["signal_want"] = {}
    for room_, info_ in rooms.items():   # published in sensor.chore_lights so the room apps skip these bulbs (no white flash)
        cfgb_ = SIGNAL_BULBS.get(room_) or {}
        bl_ = cfgb_.get("bulbs", []) + picks.get(room_, [])
        GUARD["signal_want"][room_] = {b: want[b][0] for b in bl_ if b in want}
    now = time.time()
    for b, (rgb, ref) in want.items():
        st = ha_get(b, tok)
        if st.get("state") in (None, "unavailable", "unknown"):
            continue
        if st.get("state") != "on" or not near_rgb(st.get("attributes") or {}, rgb) or st.get("attributes", {}).get("brightness", 0) < 250:
            if now - GUARD.setdefault("signal_ts", {}).get(b, 0) > 20:
                ha_call("light.turn_on", {"entity_id": b, "rgb_color": rgb, "brightness": 255, "transition": 1}, tok)
                GUARD["signal_ts"][b] = now
                ilog({"src": "bright", "action": "signal", "entity": b, "rgb": rgb})
        held[b] = ref
    for b in [x for x in list(held) if x not in want]:
        refs = held.pop(b)
        refs = [refs] if isinstance(refs, str) else refs
        ref = next((r for r in refs if r not in held and r not in want), refs[0] if refs else b)
        rs = ha_get(ref, tok)
        ra = rs.get("attributes") or {}
        if rs.get("state") == "on":
            data = {"entity_id": b, "brightness": ra.get("brightness", 255), "transition": 2}
            if ra.get("color_mode") == "color_temp" and ra.get("color_temp_kelvin"):
                data["color_temp_kelvin"] = ra["color_temp_kelvin"]
            elif ra.get("xy_color"):
                data["xy_color"] = ra["xy_color"]
            ha_call("light.turn_on", data, tok)
        else:
            ha_call("light.turn_off", {"entity_id": b, "transition": 2}, tok)
        ilog({"src": "bright", "action": "signal_release", "entity": b, "copied": ref})   # {mode_entity: previous_option} that we switched to white


def default_inspector(tok):
    """who will inspect: the one grown-up who's home; the lead grown-up when several, none or unknown"""
    c_ = GUARD.get("insp_cache")
    if c_ and time.time() - c_[0] < 60:
        return c_[1]
    home = [k for k in ADULTS if adult_home(k, tok)]
    who = home[0] if len(home) == 1 else LEAD_ADULT
    GUARD["insp_cache"] = (time.time(), who)
    return who


def watch_bright_rooms():
    tok = ha_token()
    while True:
        try:
            try:
                saved = json.loads(BRIGHT.read_text())
            except (OSError, ValueError):
                saved = {}
            if time.time() - GUARD.get("sun_ts", 0) > 60:   # daylight-only chores (clean out the car)
                sun_state_ = ha_get(SUN, tok).get("state")
                GUARD["sun_up"] = True if sun_state_ == "above_horizon" else False if sun_state_ == "below_horizon" else None
                GUARD["sun_ts"] = time.time()
            st0 = derive()
            # a room reported done (waiting for inspection) stays bright too, in the inspecting adult's colour
            pend = [r for r in st0["history"] if r.get("type") == "report" and r.get("status") == "pending" and not r.get("partial")]
            active = {a["chore"] for a in st0["active"].values()} | {r["chore"] for r in pend}
            want_modes, want_covers = set(), set()
            for cid in active:
                m, c = chore_rooms_for(cid)
                want_modes.update(m); want_covers.update(c)
            changed = False
            for ent in sorted(want_modes):
                if ent not in saved:
                    cur = ha_get(ent, tok).get("state")
                    if cur in (None, "unavailable", "unknown"):
                        continue
                    saved[ent] = cur
                    changed = True
                    if cur != "white":
                        ha_call("input_select.select_option", {"entity_id": ent, "option": "white"}, tok)
                        ilog({"src": "bright", "action": "white", "entity": ent, "was": cur})
            for ent in [e for e in list(saved) if e not in want_modes]:
                prev = saved.pop(ent)
                changed = True
                if ha_get(ent, tok).get("state") == "white":   # back to auto when the job's done (unless someone changed it since)
                    ha_call("input_select.select_option", {"entity_id": ent, "option": "auto"}, tok)
                    ilog({"src": "bright", "action": "restore", "entity": ent, "to": "auto", "was_before": prev})
            if want_covers and ha_get(SUN, tok).get("state") == "above_horizon":
                for cv in want_covers:
                    if ha_get(cv, tok).get("state") == "closed" and cv not in GUARD.setdefault("opened_covers", set()):
                        ha_call("cover.open_cover", {"entity_id": cv}, tok)
                        GUARD["opened_covers"].add(cv)
                        ilog({"src": "bright", "action": "open_cover", "entity": cv})
            if not want_covers:
                GUARD["opened_covers"] = set()
            if changed:
                BRIGHT.write_text(json.dumps(saved))
            st = derive()
            kids = {k["id"]: k for k in cfg()["kids"]}
            rooms = {}
            for a in st["active"].values():
                for ent in chore_rooms_for(a["chore"])[0]:
                    room = ent.split(".", 1)[1].rsplit("_lights_mode", 1)[0]
                    for kid in a.get("team") or []:
                        r = rooms.setdefault(room, {"kids": [], "colors": []})
                        if kid not in r["kids"] and kid in kids:
                            r["kids"].append(kid); r["colors"].append(kids[kid].get("color"))
            insp = {i["id"]: i for i in cfg().get("inspectors", [])}
            for r in pend:
                who = (r.get("inspecting") or {}).get("by") or default_inspector(tok)
                for ent in chore_rooms_for(r["chore"])[0]:
                    room = ent.split(".", 1)[1].rsplit("_lights_mode", 1)[0]
                    if room not in rooms and insp.get(who, {}).get("color"):   # a kid still working there keeps their colour
                        rooms[room] = {"kids": [], "colors": [insp[who]["color"]], "inspector": who}
            apply_signal_bulbs(rooms, tok)
            for rm, info in rooms.items():
                info["bulbs"] = (GUARD.get("signal_want") or {}).get(rm, {})
            sig = json.dumps(rooms, sort_keys=True)
            if sig != GUARD.get("chore_lights_sig") or time.time() - GUARD.get("chore_lights_ts", 0) > 300:
                ha_post_state("sensor.chore_lights", str(len(rooms)),
                              {"friendly_name": "Chore lights", "icon": "mdi:broom", "rooms": rooms}, tok)
                GUARD["chore_lights_sig"] = sig; GUARD["chore_lights_ts"] = time.time()
        except Exception as ex:  # noqa: BLE001
            print("bright rooms error", ex, flush=True)
        time.sleep(5)


# ---------------- Grocy = master chore list (optional) ----------------
# Grocy Chores hold the list + schedule + history. Chores with userfield kids=1 (or a kid_key) become Chore Time chores;
# house jobs (kids=0, no key) stay Grocy-only. Chore Time keeps points/inspection/teams/TV. System entries (other,
# toy_swap, toy_out) and bundles stay local. Passed jobs / "it's clean" -> Grocy execution; "it's dirty" -> reschedule now;
# undo -> undo the execution; a chore ticked done in Grocy itself -> "marked clean" here.
GROCY_SYNC = DATA / "grocy_sync.json"
ENVELOPE_GROCY_SYNC = DATA / "envelope_grocy_sync.json"
SYSTEM_CHORES = {"other", "toy_swap", "toy_out", "get_dressed"}
GROCY = HOUSE.get("grocy") or {}   # optional: Grocy is the master chore list (kids' chores tagged with userfields)
GROCY_START = float(GROCY.get("ledger_seeded_before", 0))   # ledger events before this were imported, never pushed


def gcall(method, path, body=None):
    AUTHORITY.require_effect()
    key = GROCY_KEY_FILE.read_text().strip()
    req = urllib.request.Request(GROCY_API + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"GROCY-API-KEY": key, "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        t = r.read()
        return json.loads(t) if t else None


def grocy_push_envelope_inventory(call=None, events=None, sync_file=None, uses=None):
    """Consume envelopes when used and handoff supplies when delivered, retrying every item independently."""
    products = envelopes.CFG.get("grocy_products") or {}
    envelope_product = envelopes.CFG.get("grocy_envelope_product")
    if not products and not envelope_product:
        return 0
    call = call or gcall
    events = read_events() if events is None else events
    sync_file = Path(sync_file or ENVELOPE_GROCY_SYNC)
    try:
        done = set(json.loads(sync_file.read_text()).get("done", []))
    except (OSError, ValueError):
        done = set()
    cutoff = float(envelopes.CFG.get("grocy_track_after") or 0)
    changed = 0
    for use in (envelopes.USES if uses is None else uses):
        if not envelope_product or float(use.get("ts") or 0) < cutoff:
            continue
        token = "use:%s:%s" % (use["id"], envelope_product)
        if token in done:
            continue
        call("POST", "/stock/products/%d/consume" % int(envelope_product),
             {"amount": 1, "transaction_type": "consume", "spoiled": False})
        done.add(token)
        changed += 1
        sync_file.write_text(json.dumps({"done": sorted(done)[-3000:]}))
        ilog({"src": "grocy", "action": "envelope_supply_used", "ref": use["id"],
              "supply": "Letter envelope", "product_id": int(envelope_product)})
    envelope_chores = set(envelopes.CFG.get("chores") or [])
    reports = {e["id"]: e for e in events if e.get("type") == "report" and e.get("chore") in envelope_chores}
    ready = {rid: float(r["delivered"]) for rid, r in reports.items() if r.get("delivered")}
    for e in events:
        if e.get("type") == "delivered" and e.get("ref") in reports:
            ready[e["ref"]] = min(float(e.get("ts") or 0), ready.get(e["ref"], float("inf")))
    ready = {ref: completed for ref, completed in ready.items() if completed >= cutoff}
    for ref in sorted(ready, key=ready.get):
        for label, product_id in products.items():
            token = "%s:%s" % (ref, product_id)
            if token in done:
                continue
            call("POST", "/stock/products/%d/consume" % int(product_id),
                 {"amount": 1, "transaction_type": "consume", "spoiled": False})
            done.add(token)
            changed += 1
            sync_file.write_text(json.dumps({"done": sorted(done)[-3000:]}))
            ilog({"src": "grocy", "action": "envelope_supply_used", "ref": ref,
                  "supply": str(label)[:80], "product_id": int(product_id)})
    return changed


def _strip_html(t):
    import html, re
    t = re.sub(r"<br\s*/?>|</p>", "\n", t or "")
    return html.unescape(re.sub(r"<[^>]+>", "", t)).strip()


def grocy_chores_pull():
    rows = gcall("GET", "/objects/chores")
    with LOCK:
        c = cfg()
        local = {x["id"]: (i, x) for i, x in enumerate(c["chores"])}
        out = []
        seen = set()
        for g in rows:
            uf = g.get("userfields") or {}
            key = (uf.get("kid_key") or "").strip()
            on = str(uf.get("kids")) == "1"
            if not key and not on:
                continue   # a house job, not for the kids
            if not key:    # a grown-up added a kids' chore in Grocy: give it a stable key
                import re
                key = (re.sub(r"[^a-z0-9]+", "_", g["name"].lower()).strip("_")[:24] or "chore") + "_g%d" % g["id"]
                gcall("PUT", "/userfields/chores/%d" % g["id"], {"kid_key": key})
            try:
                extra = json.loads(uf.get("kid_extra") or "{}")
            except ValueError:
                extra = {}
            ch = {"id": key, "name": g["name"], "icon": (uf.get("kid_icon") or "").strip() or "2b50", "grocy_id": g["id"]}
            pts = (uf.get("kid_points") or "").strip() if isinstance(uf.get("kid_points"), str) else uf.get("kid_points")
            ch["points"] = int(float(pts)) if pts not in (None, "") else None
            pt = g.get("period_type")
            if pt == "daily":
                ch["daily"] = True
            elif pt == "weekly":
                dows = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
                due = next((dows.index(d) for d in str(g.get("period_config") or "").split(",") if d in dows), 4)
                ch["weekly"] = dict(extra.get("weekly") or {"open_dow": 0}, due_dow=due)
            elif pt == "dynamic-regular" and g.get("period_days"):
                ch["cooldown_h"] = int(g["period_days"]) * 24
                ch["room"] = extra.get("room", True)
            else:
                ch["cooldown_h"] = 0
                if extra.get("room"):
                    ch["room"] = True
            if ch.get("room") is False:
                ch.pop("room")
            for k_, f_ in (("floor", "kid_floor"), ("where", "kid_where")):
                if (uf.get(f_) or "").strip():
                    ch[k_] = uf[f_].strip()
            if (uf.get("kid_only") or "").strip():
                ch["only"] = [x.strip().lower() for x in uf["kid_only"].split(",") if x.strip()]
            if (uf.get("kid_map") or "").strip():
                ch["map_rooms"] = [x.strip() for x in uf["kid_map"].split(",") if x.strip()]
            d_ = _strip_html(g.get("description"))
            if d_ and "Google Tasks" not in d_:
                ch["desc"] = d_
            if not on or str(g.get("active", 1)) == "0":
                ch["hidden"] = True
            for k_, v_ in extra.items():
                if k_ not in ("order", "room", "weekly"):
                    ch.setdefault(k_, v_)
            ch["_order"] = extra.get("order", 500 + g["id"])
            out.append(ch)
            seen.add(key)
        for cid, (i, x) in local.items():   # keep system chores, bundles, and anything removed from Grocy (hidden)
            if cid in seen:
                continue
            if is_system(cid) or x.get("bundle"):
                out.append(dict(x, _order=i))
            elif x.get("grocy_id") or x.get("_was_grocy"):
                out.append(dict(x, hidden=True, _order=i))
            else:
                out.append(dict(x, _order=i))
        out.sort(key=lambda x: x["_order"])
        for x in out:
            x.pop("_order", None)
        changed = out != c["chores"]
        if changed:
            c["chores"] = out
            save_cfg(c)
    if changed:   # ilog takes LOCK itself: never call it while holding LOCK (this once deadlocked the whole app)
        ilog({"src": "grocy", "action": "chores_pulled", "n": len(out)})
    return changed


def grocy_push_events():
    """Ledger -> Grocy: executions for passed jobs / marked clean, reschedule for reopen, undo for undo."""
    try:
        sync = json.loads(GROCY_SYNC.read_text())
    except (OSError, ValueError):
        sync = {}
    done, logmap = set(sync.get("done", [])), sync.get("logmap", {})
    chores = {x["id"]: x for x in cfg()["chores"]}
    evs = read_events()
    reports = {e["id"]: e for e in evs if e["type"] == "report"}
    from datetime import datetime
    tstr = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
    changed = False
    for e in evs:
        if e["ts"] < GROCY_START or e["id"] in done:
            continue
        try:
            if e["type"] == "inspect" and e.get("ok") and not e.get("partial_pass"):
                r = reports.get(e.get("ref"))
                for cid in (r.get("covers") or [r["chore"]]) if r else []:
                    gid = chores.get(cid, {}).get("grocy_id")
                    if gid:
                        res = gcall("POST", "/chores/%d/execute" % gid, {"tracked_time": tstr(e["ts"]), "done_by": 1})
                        logmap.setdefault(e["id"], []).append(res.get("id") if isinstance(res, dict) else None)
            elif e["type"] == "mark_clean" and not e.get("grocy_log"):
                gid = chores.get(e.get("chore"), {}).get("grocy_id")
                if gid:
                    res = gcall("POST", "/chores/%d/execute" % gid, {"tracked_time": tstr(e["ts"]), "done_by": 1})
                    logmap.setdefault(e["id"], []).append(res.get("id") if isinstance(res, dict) else None)
            elif e["type"] == "reopen":
                gid = chores.get(e.get("chore"), {}).get("grocy_id")
                if gid:
                    gcall("PUT", "/objects/chores/%d" % gid, {"rescheduled_date": tstr(e["ts"])})
            elif e["type"] == "undo" and e.get("ref") in logmap:
                for lid in logmap.pop(e["ref"]):
                    if lid:
                        gcall("POST", "/chores/executions/%d/undo" % int(lid))
            done.add(e["id"])
            changed = True
        except Exception as ex:  # noqa: BLE001  (Grocy down: retry next round)
            print("grocy push failed", e["id"], ex, flush=True)
            break
    if changed:
        sync.update(done=sorted(done)[-3000:], logmap=logmap)
        ours = set(sync.get("ours", []))
        for v in logmap.values():
            ours.update(x for x in v if x)
        sync["ours"] = sorted(ours)[-3000:]
        GROCY_SYNC.write_text(json.dumps(sync))


def grocy_pull_executions():
    """A kids' chore ticked done in Grocy by a grown-up -> 'marked clean' here (no points to anyone)."""
    try:
        sync = json.loads(GROCY_SYNC.read_text())
    except (OSError, ValueError):
        sync = {}
    seen = int(sync.get("log_seen", 0))
    rows = gcall("GET", "/objects/chores_log?query%5B%5D=id%3E" + str(seen))
    if not rows:
        return
    ours = set(sync.get("ours", []))
    bygid = {x.get("grocy_id"): x["id"] for x in cfg()["chores"] if x.get("grocy_id")}
    from datetime import datetime
    for row in sorted(rows, key=lambda r: int(r["id"])):
        seen = max(seen, int(row["id"]))
        if int(row["id"]) in ours or str(row.get("undone")) == "1" or str(row.get("skipped")) == "1":
            continue
        cid = bygid.get(int(row["chore_id"]))
        if cid and sync.get("log_seen") is not None:   # first run only sets the watermark
            append({"type": "mark_clean", "chore": cid, "by": "grocy", "grocy_log": int(row["id"]),
                    "done_at": datetime.fromisoformat(row["tracked_time"]).timestamp()})
            ilog({"src": "grocy", "action": "done_in_grocy", "chore": cid, "log": row["id"]})
            after_change()
    sync["log_seen"] = seen
    GROCY_SYNC.write_text(json.dumps(sync))


def watch_grocy_chores():
    n = 0
    while True:
        try:
            if n % 2 == 0 and grocy_chores_pull():
                after_change()
            grocy_push_events()
            grocy_push_envelope_inventory()
            grocy_pull_executions()
        except Exception as ex:  # noqa: BLE001
            print("grocy chore sync error", ex, flush=True)
        n += 1
        time.sleep(30)


GROCY_API = (GROCY.get("url") or "").rstrip("/") + "/api"
GROCY_KEY_FILE = house_path(GROCY.get("key_file"))
GROCY_CACHE = {}
GROCY_REQUESTERS = GROCY.get("requesters") or {}   # HA user id -> name allowed to add shopping requests


def grocy_request(text, product_id, who):
    """Add a request to Grocy shopping list 4 ('Requests') ONLY — never Walmart (1) / Albertsons (2)."""
    key = GROCY_KEY_FILE.read_text().strip()
    body = {"shopping_list_id": 4, "note": text[:200], "amount": 1}
    if product_id:
        body["product_id"] = int(product_id)
    req = urllib.request.Request(GROCY_API + "/objects/shopping_list", data=json.dumps(body).encode(),
                                 headers={"GROCY-API-KEY": key, "Content-Type": "application/json"}, method="POST")
    new_id = json.loads(urllib.request.urlopen(req, timeout=20).read()).get("created_object_id")
    if new_id:
        req = urllib.request.Request(GROCY_API + "/userfields/shopping_list/%s" % new_id, data=json.dumps({"requested_by": who}).encode(),
                                     headers={"GROCY-API-KEY": key, "Content-Type": "application/json"}, method="PUT")
        urllib.request.urlopen(req, timeout=20).read()
    return new_id


def grocy_summary():
    """Read-only kitchen summary for an HA Kitchen tab -> sensor.grocy_kitchen (the Grocy key never leaves this server)."""
    from datetime import date, timedelta
    key = GROCY_KEY_FILE.read_text().strip()
    def g(path):
        req = urllib.request.Request(GROCY_API + path, headers={"GROCY-API-KEY": key, "Accept": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=20).read())
    prod_objs = g("/objects/products")
    products = {p["id"]: p["name"] for p in prod_objs}
    # frozen meat must thaw first: userfield products.needs_thaw; stock in the
    # Freezer (location 3) doesn't make a recipe "Ready now"
    thaw_ids = {p["id"] for p in prod_objs if str((p.get("userfields") or {}).get("needs_thaw") or "").lower() in ("1", "true")}
    FREEZER = int(GROCY.get("freezer_location", 3))   # Grocy location id of the freezer
    recipes = {r["id"]: r["name"] for r in g("/objects/recipes")}
    sections = {x["id"]: x["name"] for x in g("/objects/meal_plan_sections") if x.get("name")}
    days = [(date.today() + timedelta(days=i)).isoformat() for i in range(7)]
    meals = {}
    for m in g("/objects/meal_plan"):
        if m["day"] not in days:
            continue
        what = (recipes.get(m.get("recipe_id")) if m["type"] == "recipe" else products.get(m.get("product_id")) if m["type"] == "product" else None) \
            or (m.get("note") or "").split(" - ")[0][:90]
        if not what:
            continue
        who = (m.get("userfields") or {}).get("who")
        meals.setdefault(m["day"], []).append({"section": sections.get(m.get("section_id"), "Other"), "what": what,
                                               "who": who, "done": bool(m.get("done"))})
    order = {"Breakfast": 0, "Lunch": 1, "Dinner": 2}
    for d in meals:
        meals[d].sort(key=lambda x: order.get(x["section"], 3))
    lists = {x["id"]: x["name"] for x in g("/objects/shopping_lists")}
    shop = {}
    for it in g("/objects/shopping_list"):
        if it.get("done"):
            continue
        nm = products.get(it.get("product_id")) or (it.get("note") or "").strip()
        if nm:
            shop.setdefault(lists.get(it.get("shopping_list_id"), "List"), []).append(
                {"name": nm, "amount": it.get("amount"), "note": (it.get("note") or "")[:60] if it.get("product_id") else ""})
    # --- ready to make
    ready = []
    pos_by = {}   # ONE heavy recipes_pos_resolved call for every recipe (per-recipe calls can take 10+ s each on a busy Grocy)
    try:
        for x in g("/objects/recipes_pos_resolved"):
            pos_by.setdefault(x.get("recipe_id"), []).append(x)
    except Exception:  # noqa: BLE001
        pos_by = None
    for f in g("/recipes/fulfillment"):
        if int(f.get("recipe_id") or 0) < 0 or f["recipe_id"] not in recipes:
            continue
        miss = []
        if not f.get("need_fulfilled"):
            miss = [products.get(x.get("product_id"), "?") for x in (pos_by or {}).get(f["recipe_id"], [])
                    if not int(x.get("need_fulfilled") or 0)]
        thaw = []
        if f.get("need_fulfilled") and thaw_ids:
            try:
                for x in (pos_by or {}).get(f["recipe_id"], []):
                    if x.get("product_id") in thaw_ids:
                        out_ = sum(float(e.get("amount") or 0) for e in g("/stock/products/%d/entries" % x["product_id"])
                                   if int(e.get("location_id") or 0) != FREEZER)
                        if out_ < float(x.get("recipe_amount") or 0):
                            thaw.append(products.get(x["product_id"], "?"))
            except Exception:  # noqa: BLE001
                thaw = []
        ready.append({"name": recipes[f["recipe_id"]], "ready": bool(f.get("need_fulfilled")) and not thaw,
                      "thaw": sorted(set(thaw)), "missing": sorted(set(miss)),
                      "missing_n": int(f.get("missing_products_count") or len(set(miss)))})
    ready.sort(key=lambda r: (not r["ready"], not r.get("thaw"), r["missing_n"], r["name"]))
    # --- what's in the house, by food group (userfield products.food_group, comma list; cached 1 h)
    prods = {p["id"]: p for p in g("/objects/products")}
    parents = {p.get("parent_product_id") for p in prods.values() if p.get("parent_product_id")}
    qus = {q["id"]: q for q in g("/objects/quantity_units")}
    fg = GROCY_CACHE.get("food_group") if time.time() - GROCY_CACHE.get("fg_ts", 0) < 3600 else None
    if fg is None:
        fg = {}
        for pid in prods:
            try:
                fg[pid] = (g("/userfields/products/%d" % pid) or {}).get("food_group") or ""
            except Exception:  # noqa: BLE001
                fg[pid] = ""
        GROCY_CACHE["food_group"], GROCY_CACHE["fg_ts"] = fg, time.time()
    groups = {}
    for st_ in g("/stock"):
        pid = st_["product_id"]
        if pid in parents:          # e.g. K-cups: show the per-flavour children, not the parent
            continue
        amt = float(st_.get("amount") or 0)
        if amt <= 0:
            continue
        p_ = prods.get(pid) or st_.get("product") or {}
        qu = qus.get(p_.get("qu_id_stock")) or {}
        unit = qu.get("name") if amt == 1 else (qu.get("name_plural") or qu.get("name"))
        for grp in [x.strip() for x in (fg.get(pid) or "").split(",") if x.strip()]:
            groups.setdefault(grp, []).append({"id": pid, "name": p_.get("name"), "amount": round(amt, 2), "unit": unit or ""})
    for v in groups.values():
        v.sort(key=lambda x: x["name"] or "")
    # --- requests (list 4)
    reqs = []
    for it in g("/objects/shopping_list?query[]=shopping_list_id=4"):
        if it.get("done"):
            continue
        try:
            by = (g("/userfields/shopping_list/%d" % it["id"]) or {}).get("requested_by")
        except Exception:  # noqa: BLE001
            by = None
        reqs.append({"id": it["id"], "name": products.get(it.get("product_id")) or "", "note": it.get("note") or "",
                     "by": by, "ts": it.get("row_created_timestamp")})
    vol = g("/stock/volatile?due_soon_days=4")
    def short(xs):
        return [{"name": x["product"]["name"], "date": x.get("best_before_date"), "amount": x.get("amount")} for x in xs]
    shop.pop("Requests", None)
    return {"meals": meals, "days": days, "shopping": shop, "ready": ready, "groups": groups, "requests": reqs,
            "due": short(vol.get("due_products", [])), "overdue": short(vol.get("overdue_products", [])),
            "expired": short(vol.get("expired_products", [])), "updated": time.time()}


def push_grocy():
    try:
        tok = ha_token()
        summ = grocy_summary()
    except Exception as ex:  # noqa: BLE001
        print("grocy summary failed", ex, flush=True)
        return
    body = json.dumps({"state": str(int(time.time())), "attributes": {"friendly_name": "Grocy kitchen", **summ}}).encode()
    req = urllib.request.Request(f"{HA_URL}/api/states/sensor.grocy_kitchen", data=body,
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as ex:  # noqa: BLE001
        print("grocy push failed", ex, flush=True)


def watch_grocy():
    while True:
        push_grocy()
        time.sleep(600)


RELAY_PASS = ("nonce", "status")   # small answers an HA-served page needs back (the grown-up watch check)


def watch_ha_actions():
    """HA-served inspection page writes {"a":..,"n":nonce,...} into input_text.chore_action."""
    try:
        tok = ha_token()
    except OSError:
        return
    last = None
    beat = 0
    while True:
        try:
            st = ha_get(RELAY_TEXT, tok)
            val = st.get("state")
            if last is not None and val != last and val not in ("unknown", "unavailable", ""):
                try:
                    msg = json.loads(val)
                    a = msg.pop("a")
                    n = msg.pop("n", None)
                    user = (st.get("context") or {}).get("user_id")
                    if a == "_part":   # long action split by the HA page; each piece is acked, the last one runs it
                        parts = GUARD.setdefault("ha_parts", {})
                        key = (msg.get("id"), user)
                        parts.setdefault(key, {})[int(msg["i"])] = msg["d"]
                        if len(parts[key]) < int(msg["of"]):
                            GLOBAL_LAST["r"] = {"n": n, "ok": True, "error": None}
                            push_state_sensor()
                            last = val
                            continue
                        got = parts.pop(key)
                        whole = "".join(got[i] for i in range(int(msg["of"])))
                        msg = json.loads(whole)
                        a = msg.pop("a")
                        msg.pop("n", None)
                    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/{a}", data=json.dumps(msg).encode(),
                                                 headers={"Content-Type": "application/json", "X-HA-User": str(user)},
                                                 method="POST")
                    try:
                        res = json.loads(urllib.request.urlopen(req, timeout=10).read())
                        GLOBAL_LAST["r"] = {"n": n, "ok": bool(res.get("ok")), "error": res.get("error"),
                                            **{k: res[k] for k in RELAY_PASS if k in res}}
                    except urllib.error.HTTPError as e:
                        GLOBAL_LAST["r"] = {"n": n, "ok": False, "error": json.loads(e.read() or b"{}").get("error", str(e.code))}
                    ilog({"src": "ha_page", "action": a, "body": msg, "ha_user": user, "result": GLOBAL_LAST["r"]})
                except (ValueError, KeyError) as ex:
                    GLOBAL_LAST["r"] = {"n": None, "ok": False, "error": "bad action"}
                    print("bad HA action", val, ex, flush=True)
                push_state_sensor()
            last = val
            beat += 1
            if beat % 7 == 0:  # ~10 s: the grown-up got home -> send the jobs still waiting for inspection
                expire_adult_requests()
                home = parent_available(tok)
                if home and GUARD.get("ben_was_home") is False:
                    st_ = derive()
                    waiting = [r for r in st_["history"] if r.get("type") == "report" and r.get("status") == "pending"]
                    ilog({"src": "notify", "action": "welcome_home", "n": len(waiting)})
                    for r in waiting:
                        notify_report(r, st_, force=True)
                    GUARD["held_reports"] = set()
                GUARD["ben_was_home"] = home
            if beat % 13 == 0:  # refresh every ~20 s: TV/chore-time status + survives HA restarts
                push_state_sensor()
        except Exception as ex:  # noqa: BLE001
            print("HA action watcher error", ex, flush=True)
        time.sleep(1.5)


def demo_chore_time():
    """CHORE_DEMO=1 (tools/demo.py): pretend chore time is on, so the pages show what they look like in real use."""
    if os.environ.get("CHORE_DEMO"):
        GUARD["lock_until"] = time.time() + 4 * 3600
        GUARD["mode"] = {"on": True, "auto": True, "override": False, "by": None, "next_change": time.time() + 4 * 3600,
                         "chill": False, "recent_jobs": 0, "home": [], "in_hours": True, "paused_until": 0}


def start_modules():
    """Start what house.json turns on. Core alone = chores, points, rewards and every page, no Home Assistant."""
    on = []

    def go(name, fn, *args):
        threading.Thread(target=fn, args=args, daemon=True, name=name).start()
        on.append(name)
    if AUTHORITY.enabled:
        status = AUTHORITY.status()
        if AUTHORITY.rehearsal:
            return ["failover-rehearsal"]
        if not status["writable"]:
            return ["failover-standby"]

        def watch_authority():
            # Stop already-running workers promptly when a partition lets the
            # coordinator lease expire. A service restart comes up read-only.
            while True:
                if not AUTHORITY.status()["writable"]:
                    print("Failover authority lost; exiting before further household effects.", flush=True)
                    os._exit(75)
                time.sleep(.1)
        go("failover-authority", watch_authority)
    if (HOUSE.get("envelopes") or {}).get("desktop_summary_after"):
        def watch_envelope_summary():
            while True:
                envelopes.desktop_summary()
                time.sleep(60)
        go("envelope-summary", watch_envelope_summary)
    if (HOUSE.get("kid_calendar") or {}).get("ics_url"):
        go("kid-calendar", schedule.watch)        # which weekends the kids are here (church-style slot chores)
    if HA_URL:
        push_ha(derive())
        go("ha-sensors", push_state_sensor)
        if HUBS:
            go("hub-child-sessions", watch_hub_child_sessions)  # trusted foreground child per physical Hub
        go("ha-actions", watch_ha_actions)        # parent actions from HA dashboards (input_text relay)
        go("ha-events", watch_ha_events)          # phone/watch notification buttons
        go("home-days", watch_home_days)          # streaks skip days a kid was away
        if any(k.get("where") for k in cfg()["kids"]):
            go("kid-where", watch_kid_where)      # which household each kid is at (two-household families)
            go("when-down", watch_when_down)      # "plug it back in" jobs appear while a device is unavailable
            go("dress", watch_dress)              # "Put on clothes!" spoken reminders every 30 s while it's on
            if SCHOOL:
                go("school-mornings", watch_school_mornings)   # "Put on clothes!" by itself on school mornings
            if any((h_ or {}).get("motion") for h_ in (HOUSE.get("hubs") or {}).values()):
                go("hub-motion", watch_hub_motion)   # someone walks up to a Hub: it chimes for kids with something waiting
        if PEER_PIN_CFG:
            go("hub-placement", watch_hub_placement)   # an unplugged reset Hub revokes the separation trust
        if TVS or HUBS:
            go("tv-guard", watch_tvs)             # chore time, TV locks, Hub keeper, bedtime holds
        if LAUNCHERS:
            go("launchers", watch_launchers)
        if CHORE_ROOMS or SIGNAL_BULBS:
            go("room-lights", watch_bright_rooms)
        if any((stepped_jobs.definition(ch) or {}).get("contexts") for ch in cfg()["chores"]):
            go("project-context", watch_project_context)   # outdoor safety independent of the room-light worker
        if MAE_DASH:
            go("dashboard-mirror", watch_mae_dashboard)
        if (HOUSE.get("mail_delivery") or {}).get("entity"):
            import mail_delivery
            go("mail-delivery", mail_delivery.run, globals())   # doorbell caption -> reopen the Sort mail chore
        if HOUSE.get("vacuum"):
            import vacuum_auto
            go("vacuum", vacuum_auto.run, globals())
        for key in HOUSE.get("vacuum_more") or []:   # more robots (e.g. upstairs): one independent autopilot copy each
            import importlib.util
            spec_ = importlib.util.spec_from_file_location("vacuum_auto_" + key, Path(__file__).with_name("vacuum_auto.py"))
            mod_ = importlib.util.module_from_spec(spec_)
            spec_.loader.exec_module(mod_)
            go("vacuum-" + key, mod_.run, globals(), key)
        if GROCY.get("url"):
            go("grocy-summary", watch_grocy)
        if (HOUSE.get("robot_locator") or {}).get("robots"):
            import locator
            go("robot-locator", locator.run, globals())   # which room each robot is in, even off-cleaning
        if (HOUSE.get("room_eye") or {}).get("capture_url"):
            import room_eye
            go("room-eye", room_eye.run, globals())   # cameras learn tidy rooms, mark messy ones, spot the robots
            for camera, cc in HOUSE["room_eye"].get("cameras",{}).items():
                if cc.get("visual_robot"):
                    import robot_camera
                    go("robot-camera-" + camera, robot_camera.run, globals(), camera, cc["visual_robot"])
        if (HOUSE.get("vacuum_help") or {}).get("robots"):
            go("vacuum-help", vacuum_help.run, globals())   # hero jobs: rescue a stuck robot, change its water
    if GROCY.get("url"):
        go("grocy-chores", watch_grocy_chores)
        import house_tasks
        go("house-tasks", house_tasks.run, globals())   # the grown-ups' side: due chores, tasks, batteries, context
    go("bedtime", watch_bedtime)                 # core: jobs in progress go on hold at bedtime
    return on


if __name__ == "__main__":
    demo_chore_time()
    mods = start_modules()
    print(f"Chore Time on :{PORT}, data {DATA}, modules: {', '.join(mods) or 'core only'}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
