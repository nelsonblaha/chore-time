#!/usr/bin/env python3
"""Chore app: kid touch UI + inspection API. Append-only JSONL ledger; balances derived.

Events (one JSON object per line):
  report   {kid, chore, feeling, points|None}            -> pending until inspected
  inspect  {ref, by, points, ok:bool}                    -> ok=True credits points
  spend    {kid, reward, cost}
  adjust   {kid, points(+/-), reason, by}
  undo     {ref, by}                                     -> voids a report/spend/adjust/inspect
"""
import json
import random, os, threading, time, uuid, urllib.request
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
import envelopes  # noqa: E402
envelopes.configure(HOUSE.get("envelopes"), str(DATA / "envelopes_printed.json"))
import schedule  # noqa: E402
schedule.configure(HOUSE.get("kid_calendar"), str(DATA / "kid_calendar.json"))
HA = HOUSE.get("home_assistant") or {}   # empty = no Home Assistant: TV locks, Hubs, lights, vacuum, watch login all off


def house_path(p):
    return Path(str(p)).expanduser() if p else None


def ha_token():
    try:
        return HA_TOKEN_FILE.read_text().strip()
    except (OSError, NameError):
        return ""
LEDGER = DATA / "ledger.jsonl"
ILOG = DATA / "interactions.jsonl"
NEWS_SEEN = DATA / "news_seen.json"   # whats-new version acknowledged, per device IP   # every UI tap/screen + TV guard decision, for later study


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
CT_SWITCH = HA.get("chore_time_switch", "input_boolean.chore_time")      # mirrors chore time in HA
CT_HOURS = HA.get("chore_time_hours", "input_number.chore_time_hours")
RELAY_TEXT = HA.get("relay_text", "input_text.chore_action")              # HA dashboards post actions here
SUN = HA.get("sun", "sun.sun")
CONFIG = DATA / "chores.json"
DATA.mkdir(parents=True, exist_ok=True)
if not CONFIG.exists():
    CONFIG.write_text(SEED.read_text())


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
        def away(d):   # a day the kid wasn't home never breaks the streak; unknown history = home
            return hd is not None and d.isoformat() not in hd and d < today
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
                if mine:
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
        add(k, "first:" + cid, ch.get("icon", "1f31f"), "First " + ch.get("name", cid) + "!", "Your first time", ts)
        if ch.get("trophy"):   # a job that pays in trophies, every time (e.g. fixing something that got unplugged)
            t_ = ch["trophy"] if isinstance(ch["trophy"], dict) else {}
            took_ = r["ts"] - r["started"] if r.get("started") and 0 <= r["ts"] - r["started"] < 4 * 3600 else None
            add(k, "trophy:%s:%d" % (cid, int(ts)), t_.get("icon", "1f3c6"), t_.get("title", ch.get("name", cid) + "!"),
                t_.get("sub") or ("in " + fmt_s(took_) if took_ is not None else datetime.fromtimestamp(ts).strftime("%b %-d")), ts)
        if ch.get("bundle"):
            add(k, "bundle:" + cid, "1f3e0", ch.get("name", cid) + "!", "The big job", ts)
        if r.get("started") and r.get("_team"):   # team records: per chore AND team size
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
        if r.get("started") and not r.get("_team"):
            sec = r["ts"] - r["started"]
            if 30 <= sec <= 4 * 3600:
                best = records.get(cid)
                if not best or sec < best["sec"]:
                    records[cid] = {"kid": k, "sec": sec, "ts": ts, "prev": best}
                    add(k, "record:%s:%d" % (cid, int(ts)), "1f947", "Fastest " + ch.get("name", cid) + " ever!",
                        fmt_s(sec) + ("" if not best else " (beat " + fmt_s(best["sec"]) + ")"), ts)
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
    return {"awards": {k: sorted(v, key=lambda a: a["ts"]) for k, v in aw.items()}, "records": recs,
            "awards_seen": seen, "next_awards": nxt}


GET_DRESSED = {"id": "get_dressed", "name": "Get dressed", "icon": "1f455", "points": 0, "cooldown_h": 0, "shared": True, "est_minutes": 5,
               "hidden": True, "desc": "Clothes, socks and shoes on. Then show a grown-up.",
               "trophy": {"icon": "1f3c3", "title": "Dressed!"}}   # the "Put on clothes!" job: a timed race, a trophy each time


def cfg():
    c = json.loads(CONFIG.read_text())
    if not any(x.get("id") == "get_dressed" for x in c.get("chores") or []):
        c.setdefault("chores", []).append(dict(GET_DRESSED))   # built in; never saved unless the config is edited
    return c


def save_cfg(c):
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, indent=2))
    tmp.replace(CONFIG)


def read_events():
    if not LEDGER.exists():
        return []
    out = []
    for line in LEDGER.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def append(ev):
    ev = {"id": uuid.uuid4().hex[:12], "ts": time.time(), **ev}
    DATA.mkdir(parents=True, exist_ok=True)
    with LOCK:
        # guard: never glue onto a line without a trailing newline
        if LEDGER.exists() and LEDGER.stat().st_size:
            with open(LEDGER, "rb") as f:
                f.seek(-1, 2)
                if f.read(1) != b"\n":
                    with open(LEDGER, "a") as g:
                        g.write("\n")
        with open(LEDGER, "a") as f:
            f.write(json.dumps(ev) + "\n")
    return ev


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


PAUSE_DAYS = 7   # "finish later" keeps a job this many days (kids may be at the other house in between)


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
CHORE_FLAGS = ("once", "each_daily", "on_demand", "daylight")


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


GROCY_EXTRA_KEYS = ("once", "each_daily", "on_demand", "daylight", "household", "from", "until", "after", "before", "est_minutes")


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
            tok, changed = ha_token(), False
            ents = {e for ch in cfg()["chores"] for e in ch.get("when_down") or []}
            for e in ents:
                st_ = (ha_get(e, tok) or {}).get("state")
                if st_ in (None, "unavailable", "unknown"):
                    if e not in DOWN:
                        DOWN[e] = time.time()
                        ilog({"src": "when_down", "entity": e, "state": "down"})
                    changed = changed or any(time.time() - DOWN[e] >= float(ch.get("down_min", 10)) * 60 - 60
                                             for ch in cfg()["chores"] if e in (ch.get("when_down") or []))
                elif e in DOWN:
                    DOWN.pop(e)
                    ilog({"src": "when_down", "entity": e, "state": "back"})
                    changed = True
            for e in [x for x in DOWN if x not in ents]:
                DOWN.pop(e)
            if changed:
                after_change()
        except Exception as ex:  # noqa: BLE001
            print("when_down error", ex, flush=True)
        time.sleep(60)


# "Put on clothes!" (a grown-up's switch on the parents' page): the listed kids can't do other jobs or spend until a
# grown-up passes their "Get dressed" (clothes, socks, shoes); the switch turns itself off when all are ready.
# Spoken reminders every 30 s on house.json dress.speakers (plain speakers: a Hub would drop its kids' screen).
DRESS = HOUSE.get("dress") or {}


def dress_state(evs, reports):
    last = next((e for e in reversed(evs) if e["type"] == "dress"), None)
    if not last or not last.get("on"):
        return {"on": False, "off_ts": last["ts"] if last else None, "off_by": last.get("by") if last else None}
    out = {}
    for k in last.get("kids") or []:
        mine = [r for r in reports.values() if r["chore"] == "get_dressed" and r["kid"] == k and r["ts"] >= last["ts"]]
        out[k] = "ready" if any(r["status"] == "inspected" for r in mine) else \
            "waiting" if any(r["status"] == "pending" for r in mine) else "todo"
    return {"on": True, "since": last["ts"], "by": last.get("by"), "kids": out}


def dress_kids(c):
    """who gets dressed by default: kids at this house whose home switch is on"""
    tok = ha_token()
    return [k["id"] for k in c["kids"] if kid_where(c, k["id"]) == home_of(c)
            and (not k.get("home_entity") or ha_get(k["home_entity"], tok).get("state") == "on")]


def dress_announce(first=False):
    d_ = derive().get("dress") or {}
    todo = [k for k, v in (d_.get("kids") or {}).items() if v == "todo"]
    if not d_.get("on") or not todo:
        return
    names = " and ".join(next((x["name"] for x in cfg()["kids"] if x["id"] == k), k) for k in todo)
    msg = (DRESS.get("message") or "{names}: put on your clothes, socks and shoes right now! Then show a grown-up.").format(names=names)
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
            on = bool((derive().get("dress") or {}).get("on"))
            if on != was or (on and n % 12 == 0):   # on/off, and once a minute while on (someone may have stopped it)
                dress_displays(on, started)
            was = on
            if on and n % max(1, int(DRESS.get("every_s", 30)) // 5) == 0:
                dress_announce()
        except Exception as ex:  # noqa: BLE001
            print("dress error", ex, flush=True)


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


def live_cfg(c, board):
    out = []
    for ch in c["chores"]:
        why = None if ch.get("hidden") else chore_closed(ch, board)
        out.append(dict(ch, hidden=True, closed=why) if why else ch)
    return dict(c, chores=out)


def home_of(c):
    """Households (optional, chores.json "households"): the first is the default home of every job and reward."""
    hh = c.get("households") or []
    return hh[0]["id"] if hh else None


def kid_where(c, kid):
    """Which household a kid is at now: kids[].where = {"entity", "on": household, "off": household} (Home Assistant),
    refreshed by watch_kid_where; without it, the default household."""
    return ((GUARD.get("kid_where") or {}) if "GUARD" in globals() else {}).get(kid) or home_of(c)


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
        except Exception as ex:  # noqa: BLE001
            print("kid where error", ex, flush=True)
        time.sleep(30)


BED_END_H = int((HOUSE.get("hours") or {}).get("bed_end", 6))   # bedtime holds until then; jobs held at bedtime resume


def bedtime_hold(st):
    """At bedtime every job in progress goes on hold, resumable from 6 AM. One per call; teams pause together."""
    chs = {x["id"]: x for x in st["config"]["chores"]}
    hold = [(k, a) for k, a in st["active"].items() if not (chs.get(a["chore"]) or {}).get("bedtime_ok")]   # brushing teeth goes on
    if not (st.get("bedtime") or {}).get("on") or not hold:
        return False
    from datetime import datetime as _d, timedelta as _td
    kid_, act_ = hold[0]
    eod_ = _d.combine((_d.now() + _td(days=1)).date(), _d.max.time()).timestamp()
    append({"type": "pause", "kid": kid_, "chore": act_["chore"], "elapsed": time.time() - act_["ts"],
            "team": act_.get("team") or [kid_], "notimer": bool(act_.get("notimer")), "until": eod_, "why": "bedtime"})
    ilog({"src": "bedtime", "action": "hold", "kid": kid_, "chore": act_["chore"]})
    after_change()
    return True


def watch_bedtime():
    while True:
        try:
            while bedtime_hold(derive()):
                pass
        except Exception as ex:  # noqa: BLE001
            print("bedtime hold error", ex, flush=True)
        time.sleep(10)


def bedtime_state(c, evs):
    """Bedtime: from c["bedtime"] (default 20:30) until 6 AM, or when a parent presses
    Bedtime; a parent's latest press since 5 AM wins over the clock."""
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
    evs = read_events()
    undone = {e["ref"] for e in evs if e["type"] == "undo"}
    evs = [e for e in evs if e["id"] not in undone]
    chores = {x["id"]: x for x in c["chores"]}
    reports = {e["id"]: dict(e, status="pending") for e in evs if e["type"] == "report"}
    for e in evs:
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
            if e.get("label"):
                r["label"] = e["label"]
            if e.get("present"):
                r["present"] = e["present"]
            if e.get("feedback"):
                r["feedback"] = e["feedback"]
            if e.get("partial_pass"):
                r["partial"] = True
                r["leftover_note"] = e.get("leftover_note")
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

    def unshelve(k, chore=None):   # drop a kid's saved job: the newest, or the one for this chore
        st_ = shelf.get(k) or []
        hit = next((z for z in reversed(st_) if chore is None or z["chore"] == chore), None)
        if hit:
            st_.remove(hit)
        return hit
    for e in evs:
        if e["type"] == "start":
            unshelve(e["kid"], e["chore"])
            active[e["kid"]] = {"chore": e["chore"], "ts": e["ts"], "team": [e["kid"]]}
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
                 "notimer": e.get("notimer"), "ts": e["ts"], "until": e["until"],
                 **{k: gone[k] for k in ("split", "round", "rework", "title") if gone.get(k)}})   # a sent-back team job keeps its deal
        elif e["type"] == "resume" and shelf.get(e["kid"]):
            pz = unshelve(e["kid"])
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
    shelf = {k: [z for z in v if z["until"] > time.time()] for k, v in shelf.items()}
    paused = {k: v[-1] for k, v in shelf.items() if v}   # the one a kid's screen shows; older ones come back after it
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
            earned[e["kid"]] += int(e["points"])
    for r in reports.values():
        ch = chores.get(r["chore"], {})
        r["chore_name"] = r.get("label") or (r.get("note") and r["chore"] == "other" and r["note"].capitalize()) \
            or ch.get("name", r["chore"])
        if r.get("partial"):
            r["chore_name"] += " (partly)"
        if r["chore"] == "toy_out":
            r["chore_name"] = "Toy goodbye: " + (r.get("label") or (r.get("note") or "a toy").capitalize())
        if r.get("covers") and ch.get("bundle"):
            r["chore_name"] += " (" + ", ".join(chores.get(x, {}).get("name", x) for x in r["covers"]) + ")"
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
        pend_by = list(dict.fromkeys(m for r in reports.values() if cid in covers(r) and r["status"] == "pending" and not r.get("partial")
                                     for m in (r.get("team") or [r["kid"]])))
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
                      "last_clean": last_ok, "pending_by": pend_by, "opened": reopened}
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
            if rw.get("break_min"):
                t = e["ts"] + (rw.get("minutes", 0) + rw["break_min"]) * 60
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
            st0 = started_.get(e["id"]) or e["ts"]
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
        list(reports.values()) + [e for e in evs if e["type"] in ("spend", "adjust")],
        key=lambda e: e["ts"], reverse=True)
    tvs = {k: {"name": v["name"], "short": v.get("short") or v["name"].replace(" TV", ""), "icon": v.get("icon", "1f4fa"), **GUARD["status"].get(k, {})} for k, v in TVS.items()} if "TVS" in globals() else {}
    if "TVS" in globals():
        try:
            for tid, us in active_unlocks_raw(evs).items():
                if tid in tvs and us:
                    tvs[tid]["unlocked_until"] = max(u["until"] for u in us)
                    tvs[tid]["unlock_why"] = sorted({u["what"] for u in us})
                    tvs[tid]["grants"] = [{k: u.get(k) for k in ("id", "until", "start", "kid", "what", "by", "kinds", "armed", "mins")} for u in us]
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
    return {"version": ver, "ver_tv": ver_tv, "lead_adult": LEAD_ADULT if "LEAD_ADULT" in globals() else None, "kid_open": kid_open, "whatsnew": news[-1] if news else None, "whatsnew_all": news[-8:],
            "chore_mode": GUARD.get("mode") if "GUARD" in globals() else None, "toys": toys, "reward_ready": reward_ready,
            "timers": timed_rewards(read_events()), "screen_break": screen_break, "bedtime": bedtime_state(c, evs),
            "config": live_cfg(c, board), "balance": bal, "earned": earned, "goals": goals, "goal_since": goal_since, "favorite": favorite, "active": active, "paused": paused, "typical": typical,
            **(lambda A: {**A, **dict(zip(("achievements", "streak"), achievements(c, reports, earned, A["awards"])))})(compute_awards(c, reports, evs, earned, typical)),
            **dict(zip(("expected", "personal_best"), expected_times(c, reports, typical))), "pending_points": pend, "board": board,
            "tv": {"lock_until": GUARD.get("lock_until", 0) if "GUARD" in globals() else 0, "tvs": tvs},
            "history": history[:200], "now": time.time(),
            "households": c.get("households") or [], "kid_where": {k["id"]: kid_where(c, k["id"]) for k in c["kids"]},
            "dress": dress_state(evs, reports),
            "room_choices": (HOUSE.get("house_tasks") or {}).get("floors") or {}, "envelope": envelopes.public(),
            "next_step": {m: next_step(chores.get(a["chore"]) or {}, a.get("team") or [k], a) for k, a in active.items()
                          if (chores.get(a["chore"]) or {}).get("remind") for m in (a.get("team") or [k])}}


def push_ha(state):
    """Mirror balances into HA as sensor.chore_points_<kid> (best effort)."""
    if not HA_URL:   # no Home Assistant configured
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


def push_state_sensor(state=None, last_result=None):
    """sensor.chore_state: everything the HA-served inspection page needs (kept well under 16 KB)."""
    if not HA_URL:   # no Home Assistant configured
        return None
    try:
        tok = ha_token()
    except OSError:
        return
    state = state or derive()
    keep = ("id", "ts", "type", "kid", "chore", "chore_name", "chore_icon", "status", "points", "awarded",
            "feeling", "inspected_by", "inspect_id", "reason", "by", "reward", "reward_name", "reward_icon", "cost", "tv",
            "toy", "toy_name", "label", "present", "old_name", "old_out", "opened", "priority", "note", "started", "team", "partial", "full_price", "worked", "split", "inspecting", "round", "feedback", "leftover_note", "ask", "delivered", "inspected_ts", "certified")
    hist = [{k: h[k] for k in keep if k in h} for h in state["history"][:40]]
    attrs = {"friendly_name": "Chore state", "config": state["config"], "balance": state["balance"],
             "earned": state.get("earned"), "active": state.get("active"), "typical": state.get("typical"),
             "records": state.get("records"), "paused": state.get("paused"),
             "expected": state.get("expected"), "personal_best": state.get("personal_best"),
             "achievements": state.get("achievements"), "streak": state.get("streak"), "chore_mode": state.get("chore_mode"),
             "pending_points": state["pending_points"], "board": state["board"], "history": hist,
             "now": time.time(), "tv": state.get("tv"), "toys": state.get("toys"),
             "reward_ready": state.get("reward_ready"), "timers": state.get("timers"),
             # for the kids' screen inside HA (a child at another home, signed in by their parent's HA login)
             "awards": state.get("awards"), "goals": state.get("goals"), "goal_since": state.get("goal_since"), "favorite": state.get("favorite"),
             "screen_break": state.get("screen_break"), "bedtime": state.get("bedtime"), "version": state.get("version"),
             "households": state.get("households"), "kid_where": state.get("kid_where"),
             "dress": state.get("dress"), "envelope": state.get("envelope"), "next_step": state.get("next_step")}
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
    body = json.dumps({"state": str(int(time.time())), "attributes": attrs}).encode()
    req = urllib.request.Request(f"{HA_URL}/api/states/sensor.chore_state", data=body,
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                                 method="POST")
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as ex:  # noqa: BLE001
        print("HA state push failed", ex, flush=True)


GLOBAL_LAST = {}


ADULTS = HOUSE.get("adults") or {}   # grown-up id -> {presence, home_state, notify: [HA notify services]}
ADULT_NOTIFY = {k: v.get("notify") or [] for k, v in ADULTS.items() if v.get("notify")}
LEAD_ADULT = next(iter(ADULT_NOTIFY), next(iter(ADULTS), None))   # gets the phone/watch pings (first adult with notify)
PARENT_NOTIFY = ADULT_NOTIFY.get(LEAD_ADULT, [])


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
        ha_call(svc, {"title": "Chores: TV time", "message": msg,
                      "data": {"clickAction": "/lovelace/chores", "tag": "chore-tv-" + str(ev.get("tv")), "group": "chores",
                               "priority": "high", "ttl": 0, "actions": acts}}, tok)


# Phone only: Android mirrors it to the Pixel Watch WITH its buttons (taps handled by the phone app).
# The watch's own HA app shows notifications without buttons (tested), so don't send there.
ADULT_REQ = {}   # nonce -> {who, ip, ts, status}


def send_adult_request(nonce, who, where):
    try:
        tok = ha_token()
    except OSError:
        return
    for svc in ADULT_NOTIFY.get(who, []):
        ha_call(svc, {"title": "Chores: is this you?",
                      "message": f"Someone at the {where} wants grown-up mode as {who.title()}.",
                      "data": {"tag": "chore-adult-" + nonce, "priority": "high", "ttl": 0,
                               "actions": [{"action": "CHORE_OK_" + nonce, "title": "It's me"},
                                           {"action": "CHORE_NO_" + nonce, "title": "Not me"}]}}, tok)


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
        ha_call(svc, {"title": "Chores: ready to inspect", "message": msg,
                      "data": {"tag": "chore-insp-" + ev["id"], "group": "chores", "priority": "high", "ttl": 0,
                               "channel": "Chores", "importance": "high",
                               "clickAction": "/lovelace/chores", "actions": actions}}, tok)
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
        ha_call(svc, {"title": "Chores", "message": msg,
                      "data": {"tag": "chore-doing-" + kid, "group": "chores", "clickAction": "/lovelace/chores"}}, tok)


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
        ha_call(svc, {"title": "Chores: inspecting", "message": f"🔍 Inspecting {who}: {r.get('chore_name')}",
                      "data": {"tag": "chore-insp-" + r["id"], "group": "chores", "priority": "high", "ttl": 0,
                               "chronometer": True, "when": int(time.time()), "actions": acts}}, tok)


def clear_report_notice(ref):
    try:
        tok = ha_token()
    except OSError:
        return
    for svc in PARENT_NOTIFY:
        ha_call(svc, {"message": "clear_notification", "data": {"tag": "chore-insp-" + ref}}, tok)


def clear_adult_request(nonce, who):
    try:
        tok = ha_token()
    except OSError:
        return
    for svc in ADULT_NOTIFY.get(who, []):
        ha_call(svc, {"message": "clear_notification", "data": {"tag": "chore-adult-" + nonce}}, tok)


def watch_ha_events():
    """Listen on HA's websocket for notification button presses (watch/phone)."""
    import asyncio
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
                    await ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "mobile_app_notification_action"}))
                    print("HA event socket: subscribed to notification actions", flush=True)
                    while True:
                        m = json.loads(await ws.recv())
                        if m.get("type") == "result":
                            print("HA event socket: subscribe result", m.get("success"), flush=True)
                            continue
                        data_ = ((m.get("event") or {}).get("data") or {})
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
                        if act.split("_")[1] in ("PASS", "NOTYET", "ADD1", "ADD2", "PTS", "ZERO") and act.startswith("CHORE_"):
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
                        if act.startswith("CHORE_OK_") or act.startswith("CHORE_NO_"):
                            nonce = act[9:]
                            r = ADULT_REQ.get(nonce)
                            if r and r["status"] == "pending":
                                r["status"] = "ok" if act.startswith("CHORE_OK_") else "no"
                                ilog({"src": "adult", "action": r["status"], "who": r["who"], "ip": r["ip"]})
                                print("grown-up request", nonce, r["status"], flush=True)
            except Exception as ex:  # noqa: BLE001
                print("HA event socket error", ex, flush=True)
                await asyncio.sleep(5)
    asyncio.run(run())


def after_change():
    threading.Thread(target=lambda: (push_ha(derive()), push_state_sensor()), daemon=True).start()


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        print("%s %s" % (self.address_string(), fmt % a), flush=True)

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
        if p in ("/tv", "/api/state"):
            GUARD["seen"][self.client_address[0]] = (time.time(), self.headers.get("Referer") or "")
        if p in ("/", "/kids"):
            return self.send(200, (HERE / "static/kids.html").read_bytes(), "text/html; charset=utf-8")
        if p == "/tv":
            return self.send(200, (HERE / "static/tvlock.html").read_bytes(), "text/html; charset=utf-8")
        if p == "/now":   # grown-up "right now" board (e.g. a kitchen Hub Max)
            return self.send(200, (HERE / "static/now.html").read_bytes(), "text/html; charset=utf-8")
        if p == "/inspect":
            return self.send(200, (HERE / "static/inspect.html").read_bytes(), "text/html; charset=utf-8")
        if p == "/api/envelope_status":   # the kids' screen follows its print job (and sees a jam)
            job = (urllib.parse.parse_qs(self.path.partition("?")[2]).get("job") or [""])[0]
            try:
                return self.send(200, envelopes.status(job))
            except Exception as ex:  # noqa: BLE001
                return self.send(200, {"state": "unknown", "problem": None, "error": str(ex)[:120]})
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
            hub_ = next((h for h, x in (HOUSE.get("hubs") or {}).items() if x.get("ip") == self.client_address[0]), None)
            st["hub"], st["hub_motion"] = hub_, (GUARD.get("hub_motion") or {}).get(hub_)
            try:
                seen = json.loads(NEWS_SEEN.read_text())
            except (OSError, ValueError):
                seen = {}
            st["news_unseen"] = bool(st["whatsnew"]) and seen.get(self.client_address[0]) != st["whatsnew"]["v"]
            for r_ in ADULT_REQ.values():   # the grown-up said "It's me": this Hub opens their screen even if its page reloaded meanwhile
                if r_["ip"] == self.client_address[0] and r_["status"] == "ok" and not r_.get("used") and time.time() - r_["ts"] < 180 \
                        and r_.get("purpose", "login") == "login":
                    r_["used"] = True
                    st["adult_go"] = r_["who"]
            acked = set(seen.get("ann:" + self.client_address[0], []))
            st["news_seen_kids"] = {k["id"]: seen.get("kid:" + k["id"]) for k in st["config"]["kids"]}
            st["ann_seen_kids"] = {k["id"]: seen.get("ann:kid:" + k["id"], []) for k in st["config"]["kids"]}
            st["announce_all"] = [{"id": b["bonus_id"], "chore": cid, "price": b["price"], "base": b["base"]}
                                  for cid, b in st["board"].items() if b.get("bonus") and b.get("bonus_id") and not b["clean_until"]]
            st["announce"] = [{"id": b["bonus_id"], "chore": cid, "price": b["price"], "base": b["base"]}
                              for cid, b in st["board"].items()
                              if b["bonus"] and b["bonus_id"] and not b["clean_until"] and b["bonus_id"] not in acked]
            return self.send(200, st)
        if p == "/api/house_jobs":   # the grown-ups' house jobs (Grocy), ranked for one screen or overall
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            disp = (q.get("display") or [(HOUSE.get("house_tasks") or {}).get("board_display", "")])[0]
            shown = GUARD.get("house_tasks_displays", {}).get(disp) if disp and disp != "all" else None
            meta = GUARD.get("house_tasks_meta", {})
            return self.send(200, {"items": shown if shown is not None else (GUARD.get("house_tasks") or [])[:20],
                                   "private": meta.get("private", []), "to_review": meta.get("to_review", 0),
                                   "on": "house_tasks" in GUARD})
        if p == "/health":
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if self.headers.get("X-HA-User") and self.client_address[0] != "127.0.0.1":
            return self.send(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            b = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.send(400, {"error": "bad json"})
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
        if p in ("/api/start", "/api/report", "/api/claim") and b.get("kid") and b.get("chore") in chores \
                and not is_here(chores[b["chore"]], c, b["kid"]):
            return self.send(409, {"error": "that one is at the other house"})
        if p == "/api/envelope_cancel":   # "start over" after a jam: drop the stuck job so it can't print twice
            envelopes.cancel(str(b.get("job") or ""))
            ilog({"src": "envelope", "kid": b.get("kid"), "cancel": b.get("job")})
            return self.send(200, {"ok": True})
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
                       and r.get("chore") == b.get("chore") and b.get("kid") in (r.get("team") or [r["kid"]])), None)
            if not r_:
                return self.send(409, {"error": "nothing waiting"})
            ev = append({"type": "delivered", "ref": r_["id"], "kid": b["kid"]})
            after_change()
            ilog({"src": "api", "ip": self.client_address[0], "path": p, "event": ev})
            return self.send(200, {"ok": True, "state": derive()})
        if p == "/api/spend" and b.get("kid") and b.get("reward") in rewards and not is_here(rewards[b["reward"]], c, b["kid"]):
            return self.send(409, {"error": "that reward is at the other house"})
        if p in ("/api/start", "/api/report", "/api/spend", "/api/claim", "/api/team", "/api/resume") and st["bedtime"]["on"] \
                and not (chores.get(b.get("chore") or (st["paused"].get(b.get("kid")) or {}).get("chore")) or {}).get("bedtime_ok"):
            # after bedtime only tooth-brushing; jobs and rewards wait for the morning (6 AM)
            return self.send(409, {"error": "bedtime"})
        if p in ("/api/start", "/api/report", "/api/spend", "/api/claim", "/api/team", "/api/resume") and b.get("chore") != "get_dressed" \
                and (st.get("dress") or {}).get("on") and (st["dress"]["kids"].get(b.get("kid")) or "ready") != "ready":
            return self.send(409, {"error": "Put on your clothes first!"})
        if p == "/api/log":
            evs = b if isinstance(b, list) else [b]
            opened = {}
            for e in evs[:50]:
                if isinstance(e, dict):
                    ilog({"src": "ui", "ip": self.client_address[0], **{k: e[k] for k in list(e)[:20]}})
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
            k = ("ann:kid:" + b["kid"]) if b.get("kid") in kids else ("ann:" + self.client_address[0])
            seen[k] = (seen.get(k, []) + [str(i) for i in (b.get("ids") or [])])[-200:]
            NEWS_SEEN.write_text(json.dumps(seen))
            ilog({"src": "ui", "ip": self.client_address[0], "ev": "announce_dismissed", "ids": b.get("ids")})
            return self.send(200, {"ok": True})
        if p == "/api/adult_request":
            who = b.get("who") or LEAD_ADULT
            if who not in ADULT_NOTIFY:
                return self.send(400, {"error": "unknown grown-up"})
            nonce = uuid.uuid4().hex[:8]
            where = next((h.get("name") or k_.replace("_", " ").title() + " Hub" for k_, h in HUBS.items()
                          if h.get("ip") == self.client_address[0]), "chore screen")
            if b.get("purpose") == "lock":
                where += " (to lock a TV)"
            ADULT_REQ[nonce] = {"who": who, "ip": self.client_address[0], "ts": time.time(), "status": "pending",
                                "purpose": "lock" if b.get("purpose") == "lock" else "login"}
            ilog({"src": "adult", "action": "request", "who": who, "ip": self.client_address[0]})
            threading.Thread(target=send_adult_request, args=(nonce, who, where), daemon=True).start()
            return self.send(200, {"ok": True, "nonce": nonce})
        if p == "/api/adult_status":
            r = ADULT_REQ.get(b.get("nonce"))
            if not r:
                return self.send(404, {"error": "unknown request"})
            if r["status"] == "pending" and time.time() - r["ts"] > 120:
                r["status"] = "timeout"
            if r["status"] == "ok":
                r["used"] = True
            if r["status"] != "pending":
                threading.Thread(target=clear_adult_request, args=(b["nonce"], r["who"]), daemon=True).start()
            return self.send(200, {"ok": True, "status": r["status"]})
        if p == "/api/news_seen":
            try:
                seen = json.loads(NEWS_SEEN.read_text())
            except (OSError, ValueError):
                seen = {}
            seen[("kid:" + b["kid"]) if b.get("kid") in kids else self.client_address[0]] = str(b.get("v"))
            NEWS_SEEN.write_text(json.dumps(seen))
            ilog({"src": "ui", "ip": self.client_address[0], "ev": "news_dismissed", "v": b.get("v")})
            return self.send(200, {"ok": True})
        if p == "/api/tv/lock_now":
            GUARD["grace"] = {}
            GUARD["last_cast"] = {}
            return self.send(200, {"ok": True})
        if p == "/api/exit":
            ip = self.client_address[0]
            hid_ = next((k for k, h in HUBS.items() if h["ip"] == ip), None)
            if hid_ or ip in [h.get("ip") for h in (HOUSE.get("kiosks") or {}).values()]:   # only screens we cast to
                if hid_:   # back to normal Google Home: the keeper leaves this Hub alone for a while
                    GUARD.setdefault("hub_exit", {})[hid_] = time.time()
                ilog({"src": "hub", "hub": hid_ or ip, "action": "kid_exit"})
                threading.Thread(target=lambda: subprocess.run([CATT, "-d", ip, "stop"], timeout=30,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL), daemon=True).start()
            return self.send(200, {"ok": True})
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
                ilog({"src": "api", "ip": self.client_address[0], "path": p, "event": ev})
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
                    return self.send(409, {"error": "presumed clean"})
            if bd["clean_until"]:
                return self.send(409, {"error": "presumed clean"})
            if bd["pending_by"] and chores[b["chore"]].get("room"):
                return self.send(409, {"error": "already waiting for inspection"})
            if b["chore"] != "other" and b["kid"] in (bd.get("pending_by") or []):   # the same kid twice (a second tap on a waiting tile)
                return self.send(409, {"error": "already waiting for inspection"})
            extra = {}
            if b["chore"] == "toy_swap":
                kc = next(k for k in c["kids"] if k["id"] == b["kid"])
                if not kc.get("toy_swap"):
                    return self.send(400, {"error": "no toy swap for this kid"})
                extra = {"toy": "surprise", "old_toy": str(b.get("old_toy") or "other")[:20],
                         "old_toy_name": str(b.get("old_toy_name") or "")[:40]}
            if b.get("note"):
                extra["note"] = " ".join(str(b["note"]).split())[:40]
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
            if b["chore"] == "get_dressed" and (st.get("dress") or {}).get("on"):
                extra["started"] = st["dress"]["since"]   # the race clock starts when the grown-up flips the switch (always timed)
            if ch_.get("together") and not set(ch_.get("only") or []) <= set(extra.get("team") or [b["kid"]]):
                return self.send(409, {"error": "do it together: team up first"})
            ev = append({"type": "report", "kid": b["kid"], "chore": b["chore"],
                         "feeling": b.get("feeling"), "points": bd["price"], **extra})
        elif p == "/api/spend":
            if b.get("kid") not in kids or b.get("reward") not in rewards:
                return self.send(400, {"error": "unknown kid/reward"})
            cost = rewards[b["reward"]]["cost"]
            # "Watching together": everyone watching pays, and they share one clock
            with_ = [k for k in dict.fromkeys(b.get("with") or []) if k in kids and k != b["kid"]] \
                if b["reward"] in ("show", "movie", "games") else []
            kname = {k["id"]: k["name"] for k in c["kids"]}
            for wk in [b["kid"]] + with_:
                who_ = "" if wk == b["kid"] else kname.get(wk, wk) + ": "
                if st["balance"][wk] < cost:
                    return self.send(409, {"error": who_ + "not enough points"})
                ready = st["reward_ready"].get(wk, {}).get(b["reward"], 0)
                if ready > time.time():
                    return self.send(409, {"error": who_ + ("screen break" if b["reward"] in ("show", "movie", "games") and st["screen_break"].get(wk) else "needs a break first"), "ready_at": ready})
            tvx = {}
            if b["reward"] in UNLOCKS and (b.get("tv") in TVS or b.get("tv") == "*"):
                tm = rewards[b["reward"]].get("tv_minutes") or UNLOCKS[b["reward"]][0]
                tvx = {"tv": b["tv"], "until": time.time() + tm * 60, "minutes": tm}
            queued = bool(tvx) and any(u["what"] != "vr" for tv_ in ([b["tv"]] if b["tv"] in TVS else list(TVS))
                                       for u in active_unlocks().get(tv_, []))
            grp = {"group": uuid.uuid4().hex[:12], "together": [b["kid"]] + with_} if with_ else {}
            ev = append({"type": "spend", "kid": b["kid"], "reward": b["reward"], "cost": cost, **tvx, **grp,
                         "reward_name": rewards[b["reward"]]["name"],
                         "reward_icon": rewards[b["reward"]]["icon"]})
            for wk in with_:
                append({"type": "spend", "kid": wk, "reward": b["reward"], "cost": cost, **tvx, **grp, "with_buyer": b["kid"],
                        "reward_name": rewards[b["reward"]]["name"], "reward_icon": rewards[b["reward"]]["icon"]})
            ev = dict(ev, queued=queued)
        elif p == "/api/inspect":
            reps = {r["id"]: r for r in st["history"] if r.get("type") == "report"}
            r = reps.get(b.get("ref"))
            if not r or b.get("by") not in insp:
                return self.send(400, {"error": "unknown report/inspector"})
            if r["status"] != "pending":
                return self.send(409, {"error": "already inspected"})
            ok = bool(b.get("ok", True))
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
                         **({"keep": True} if not ok and b.get("keep") else {})})
            if r["chore"] == "get_dressed" and ok:
                d_ = derive().get("dress") or {}
                if d_.get("on") and all(v == "ready" for v in d_["kids"].values()):
                    append({"type": "dress", "on": False, "kids": [], "by": "auto", "why": "everyone dressed"})
            if ok and b.get("leftover_points") is not None and r["chore"] not in ("toy_swap", "other"):
                append({"type": "price", "chore": r["chore"], "points": max(0, min(50, int(b["leftover_points"]))),
                        "rest": True, "note": str(b.get("leftover_note") or "")[:600], "by": b["by"], "ref": r["id"]})
            if not ok and not r.get("partial") and r["chore"] not in ("toy_swap", "other", "get_dressed"):
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
                elif cid not in SYSTEM_CHORES:
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
            from datetime import datetime, timedelta
            eod = datetime.combine((datetime.now() + timedelta(days=PAUSE_DAYS)).date(), datetime.max.time()).timestamp()
            ev = append({"type": "pause", "kid": b["kid"], "chore": act["chore"], "elapsed": time.time() - act["ts"],
                         "team": act.get("team") or [b["kid"]], "notimer": bool(act.get("notimer")), "until": eod})
        elif p == "/api/title":   # a kid names the job they're on (or one they saved for later)
            t = " ".join(str(b.get("title") or "").split())[:40]
            act = st["active"].get(b.get("kid")) or st["paused"].get(b.get("kid"))
            if b.get("kid") not in kids or not act:
                return self.send(400, {"error": "not doing a chore"})
            ev = append({"type": "title", "kid": b["kid"], "chore": act["chore"], "title": t})
        elif p == "/api/keep":   # a grown-up keeps a kid's saved job longer
            if b.get("by") not in insp or b.get("kid") not in st["paused"]:
                return self.send(400, {"error": "unknown inspector / nothing saved"})
            ev = append({"type": "keep", "kid": b["kid"], "chore": b.get("chore") or st["paused"][b["kid"]]["chore"],
                         "until": float(b.get("until") or time.time() + PAUSE_DAYS * 86400), "by": b["by"]})
        elif p == "/api/resume":
            pz = st["paused"].get(b.get("kid"))
            if not pz:
                return self.send(400, {"error": "nothing saved"})
            ev = append({"type": "resume", "kid": b["kid"]})
        elif p == "/api/release":
            if b.get("by") not in insp or b.get("kid") not in st["paused"]:
                return self.send(400, {"error": "unknown inspector / nothing saved"})
            ev = append({"type": "release", "kid": b["kid"], "by": b["by"]})
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
            ev = append({"type": "claim", "kid": b["kid"], "chore": b["chore"], "slot": bd["slot"],
                         **({"release": True} if b.get("release") else {})})
        elif p in ("/api/start", "/api/stop"):
            if b.get("kid") not in kids or (p == "/api/start" and b.get("chore") not in chores):
                return self.send(400, {"error": "unknown kid/chore"})
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
            ev = append({"type": p.rsplit("/", 1)[1], "kid": b["kid"], "chore": b.get("chore")})
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
        elif p == "/api/mark_clean":
            if b.get("by") not in insp or b.get("chore") not in chores:
                return self.send(400, {"error": "unknown inspector/chore"})
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
        ilog({"src": "api", "ip": self.client_address[0], "path": p, "event": ev})
        if p == "/api/spend" and ev.get("type") == "spend":
            threading.Thread(target=notify_parents, args=(ev,), daemon=True).start()
        if p == "/api/start" and ev.get("type") == "start":
            threading.Thread(target=notify_started, args=(ev["kid"], ev["chore"]), daemon=True).start()
        if p == "/api/team" and ev.get("type") == "team":
            threading.Thread(target=notify_started, args=(ev["members"][0], ev["chore"], ev["members"], "team"), daemon=True).start()
        if p == "/api/pause" and ev.get("type") == "pause":
            threading.Thread(target=notify_started, args=(ev["kid"], ev["chore"], ev.get("team"), "paused"), daemon=True).start()
        if p == "/api/report" and ev.get("type") == "report":
            threading.Thread(target=notify_report, args=(ev, derive()), daemon=True).start()
        if ev.get("type") in ("inspect", "undo") and ev.get("ref"):
            threading.Thread(target=clear_report_notice, args=(ev["ref"],), daemon=True).start()
        self.send(200, {"ok": True, "event": ev, "state": derive()})


# ---- launchers: HA input_button press -> CATT cast of the kid UI to a Hub ----
import subprocess
LAUNCHERS = {k: (v["ip"], v.get("path", "/")) for k, v in (HOUSE.get("launchers") or {}).items()}   # HA button -> cast a page
SELF_URL = os.environ.get("CHORE_SELF_URL", HOUSE.get("public_url") or "http://localhost:%d" % PORT)   # how Hubs/TVs reach us
CATT = str(house_path((HOUSE.get("casting") or {}).get("catt")) or "catt")
HUB_VOLUME = int(os.environ.get("CHORE_HUB_VOLUME", (HOUSE.get("casting") or {}).get("hub_volume", 40)))


def cast(ip, path):
    # a fresh query string forces DashCast to reload instead of keeping the old page
    url = SELF_URL + path + ("&" if "?" in path else "?") + "v=%d" % time.time()
    print("cast", ip, url, flush=True)
    try:
        if hub_showing_chores(ip) or ip in [t["ip"] for t in TVS.values()]:
            subprocess.run([CATT, "-d", ip, "stop"], timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
        subprocess.run([CATT, "-d", ip, "cast_site", url], timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if path.startswith("/") and not path.startswith("/tv"):  # kid page on a Hub: make the chimes audible
            subprocess.run([CATT, "-d", ip, "volume", str(HUB_VOLUME)], timeout=20,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as ex:  # noqa: BLE001
        print("cast failed", ip, ex, flush=True)


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
    if (GUARD.get("mode") or {}).get("chill"):
        return
    from datetime import datetime
    for hid, h in HUBS.items():
        if datetime.now().hour < h.get("from_h", 0):
            continue   # the kids-room Hub is in their bedroom: don't light it up at 5 AM
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
        gap = 300 if len(hc) >= 3 and not hub_showing_chores(h["ip"]) else 60   # back off if casts aren't sticking
        if not hub_showing_chores(h["ip"]) and (not hc or now - hc[-1] > gap) and \
                now - GUARD.setdefault("launch_ts", {}).get(h["ip"], 0) > 15:   # a launcher cast is already loading
            hc.append(now)
            print("chore time: hub back to chores", hid, flush=True)
            ilog({"src": "hub", "hub": hid, "action": "auto_chore_screen"})
            cast(h["ip"], "/")


ANNOUNCE_SPEAKER = os.environ.get("CHORE_SPEAKER", (HA.get("announce") or {}).get("speaker"))
ANNOUNCE_TTS = (HA.get("announce") or {}).get("tts")


def announce(msg, tok, speaker=None):
    if not ANNOUNCE_TTS or not (speaker or ANNOUNCE_SPEAKER):   # no speaker configured: stay silent
        return
    ilog({"src": "announce", "msg": msg, "speaker": speaker or ANNOUNCE_SPEAKER})
    print("announce:", msg, flush=True)
    ha_call("tts.speak", {"entity_id": ANNOUNCE_TTS, "media_player_entity_id": speaker or ANNOUNCE_SPEAKER,
                          "message": msg, "cache": True}, tok)


def timed_rewards(evs=None, now=None):
    """Running timed rewards (VR): start = newest restart or the purchase; kept until 2 min after the end."""
    evs = evs if evs is not None else read_events()
    now = now or time.time()
    c = cfg()
    rw = {r["id"]: r for r in c["rewards"]}
    undone = {e["ref"] for e in evs if e["type"] == "undo"}
    out = {}
    for e in evs:
        if e["type"] == "spend" and e["id"] not in undone and rw.get(e.get("reward"), {}).get("minutes"):
            mins = e.get("minutes") or rw[e["reward"]]["minutes"]
            out[e["id"]] = {"ref": e["id"], "kid": e["kid"], "reward": e["reward"], "start": e["ts"], "minutes": mins,
                            "restarts": 0}
        elif e["type"] == "timer_restart" and e.get("ref") in out:
            out[e["ref"]]["start"] = e["ts"]
            out[e["ref"]]["restarts"] += 1
    return [t for t in out.values() if t["start"] + t["minutes"] * 60 + 120 > now]


def vr_timers(tok):
    """Spoken 1-minute warning and time's-up for every running timed reward (restart-aware)."""
    now = time.time()
    done = GUARD.setdefault("vr_said", set())
    kidname = {k["id"]: k["name"] for k in cfg()["kids"]}
    for t in timed_rewards():
        end = t["start"] + t["minutes"] * 60
        nm = kidname.get(t["kid"], "")
        k1, k0 = (t["ref"], round(t["start"]), 1), (t["ref"], round(t["start"]), 0)
        if end - 60 <= now < end and k1 not in done:
            done.add(k1)
            announce(f"{nm}, one minute of VR left.", tok)
        if end <= now < end + 90 and k0 not in done:
            done.add(k0)
            announce(f"{nm}, VR time is up. Please take off the headset.", tok)


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
                "pos": pos, "dur": dur, "left": max(0, dur - pos), "player": p}
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


def ha_post_state(ent, state, attrs, tok):
    if not HA_URL:   # no Home Assistant configured
        return None
    body = json.dumps({"state": state, "attributes": attrs}).encode()
    req = urllib.request.Request(f"{HA_URL}/api/states/{ent}", data=body,
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=10).read()


def ha_call(svc, data, tok):
    if not HA_URL:   # no Home Assistant configured
        return None
    dom, name = svc.split(".")
    req = urllib.request.Request(f"{HA_URL}/api/services/{dom}/{name}", data=json.dumps(data).encode(),
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                                 method="POST")
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as ex:  # noqa: BLE001
        print("HA call failed", svc, ex, flush=True)


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
        if activity != "com.google.android.apps.mediashell":
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


def tv_status(t, tok):
    """-> (what, longest_duration_s): what in off|idle|lock|stream|hdmi"""
    players = [ha_get(p, tok) for p in t["players"]]
    if external_display_active(t,players,tok):
        return "external_display",None
    if t["kind"] == "sony":   # the TV's own input wins over a lock page still loaded on its Chromecast
        act0 = (ha_get(t["remote"], tok).get("attributes") or {}).get("current_activity") or ""
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


def tv_grants(evs, now=None):
    """Per TV: active unlocks [{id, until, kinds, kid, what, by, maxdur, ts}] after adult adjustments."""
    now = now or time.time()
    undone = {e["ref"] for e in evs if e["type"] == "undo"}
    g = {t: [] for t in TVS}
    started, mins_override, moved = {}, {}, {}
    for e in evs:
        if e["type"] == "tv_move" and e["id"] not in {x["ref"] for x in evs if x["type"] == "undo"}:
            moved[e["ref"]] = e["tv"]
        if e["type"] == "tv_started" and e.get("ref") not in started:
            started[e["ref"]] = e.get("at") or e["ts"]   # "at": backdated start (watching began before we noticed)
        if e["type"] == "grant_minutes":
            mins_override[e["ref"]] = e["minutes"]
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
            for tid in (list(TVS) if e["reward"] == "vr" or tvsel == "*" else [tvsel]):
                if tid in g:
                    g[tid].append({"id": e["id"], "ts": e["ts"], "until": until, "kinds": sorted(kinds), "kid": e["kid"],
                                   "start": started.get(e["id"]) or e["ts"],
                                   "what": e["reward"], "by": e.get("by") or e["kid"], "armed": armed, "group": e.get("group"), "mins": m,
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
        elif e["type"] == "tv_set_until":
            for tid in (list(TVS) if e["tv"] == "*" else [e["tv"]]):
                for x in g.get(tid, []):
                    if x["ts"] < e["ts"] and x["until"] > e["ts"]:
                        x["until"] = e["until"]
    return {t: [x for x in xs if x["until"] > now] for t, xs in g.items()}


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
    # VR purchases already finished/announced before a restart must not be re-announced
    GUARD["tv_said"] = set()
    for tid, us in active_unlocks().items():
        tv_us = [u for u in us if u["what"] != "vr"]
        if tv_us:
            end = max(u["until"] for u in tv_us)
            if end - time.time() <= 300:
                GUARD["tv_said"].add((tid, round(end)))
    GUARD["vr_said"] = set()
    for t in timed_rewards():
        end = t["start"] + t["minutes"] * 60
        if time.time() >= end - 60:
            GUARD["vr_said"].add((t["ref"], round(t["start"]), 1))
        if time.time() >= end:
            GUARD["vr_said"].add((t["ref"], round(t["start"]), 0))
    while True:
        try:
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
                hubs_stay = auto_on
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
                            ha_call(svc, {"title": "Chores: 5 min of TV left", "message": f"{TVS[tid]['name']}" + (f" ({who})" if who else "") + " locks in 5 minutes.",
                                          "data": {"tag": "chore-tv-" + tid, "group": "chores", "priority": "high", "ttl": 0,
                                                   "actions": [{"action": "CHORE_TVPLUS_" + tid, "title": "+10 min"},
                                                               {"action": "CHORE_TVLOCK_" + tid, "title": "Lock now"}]}}, tok)
            for tid, t in TVS.items():
                what, dur = tv_status(t, tok)
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
                if prev.get("what") != what or prev.get("allowed") != allowed:
                    ilog({"src": "tv", "tv": tid, "what": what, "allowed": allowed, "app": app_of(t, tok)})
                GUARD["status"][tid] = {"what": what, "allowed": allowed, "duration": dur,
                                        "grace_until": (GUARD["grace"].get(tid) or {}).get("until"),
                                        "finish": fin.get(tid)}
                if what not in ("stream", "hdmi", "cast") or allowed:
                    GUARD.setdefault("fails", {})[tid] = 0
                prev_what = (GUARD.setdefault("prev", {})).get(tid)
                GUARD["prev"][tid] = what
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


def load_home_days():
    try:
        return {k: set(v) for k, v in json.loads(HOME_DAYS.read_text()).items()}
    except (OSError, ValueError):
        return {}


def save_home_days(hd):
    HOME_DAYS.write_text(json.dumps({k: sorted(v) for k, v in hd.items()}))


def watch_home_days():
    """Record which days each kid was home (streaks skip days away). Backfill from HA history once (~10 days kept)."""
    from datetime import datetime, date, timedelta
    tok = ha_token()
    hd = load_home_days()
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
                while d <= b.date():
                    hd.setdefault(k, set()).add(d.isoformat())
                    d += timedelta(days=1)
        save_home_days(hd)
    except Exception as ex:  # noqa: BLE001
        print("home-days backfill failed", ex, flush=True)
    while True:
        try:
            changed = False
            for k in cfg()["kids"]:
                if k.get("home_entity") and ha_get(k["home_entity"], tok).get("state") == "on":
                    t = date.today().isoformat()
                    if t not in hd.setdefault(k["id"], set()):
                        hd[k["id"]].add(t)
                        changed = True
            if changed:
                save_home_days(hd)
            GUARD["home_days"] = hd
        except Exception as ex:  # noqa: BLE001
            print("home-days error", ex, flush=True)
        time.sleep(300)


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
                GUARD["sun_up"] = ha_get(SUN, tok).get("state") != "below_horizon"
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
SYSTEM_CHORES = {"other", "toy_swap", "toy_out", "get_dressed"}
GROCY = HOUSE.get("grocy") or {}   # optional: Grocy is the master chore list (kids' chores tagged with userfields)
GROCY_START = float(GROCY.get("ledger_seeded_before", 0))   # ledger events before this were imported, never pushed


def gcall(method, path, body=None):
    key = GROCY_KEY_FILE.read_text().strip()
    req = urllib.request.Request(GROCY_API + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"GROCY-API-KEY": key, "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        t = r.read()
        return json.loads(t) if t else None


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
            if cid in SYSTEM_CHORES or x.get("bundle"):
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
    if (HOUSE.get("kid_calendar") or {}).get("ics_url"):
        go("kid-calendar", schedule.watch)        # which weekends the kids are here (church-style slot chores)
    if HA_URL:
        push_ha(derive())
        go("ha-sensors", push_state_sensor)
        go("ha-actions", watch_ha_actions)        # parent actions from HA dashboards (input_text relay)
        go("ha-events", watch_ha_events)          # phone/watch notification buttons
        go("home-days", watch_home_days)          # streaks skip days a kid was away
        if any(k.get("where") for k in cfg()["kids"]):
            go("kid-where", watch_kid_where)      # which household each kid is at (two-household families)
            go("when-down", watch_when_down)      # "plug it back in" jobs appear while a device is unavailable
            go("dress", watch_dress)              # "Put on clothes!" spoken reminders every 30 s while it's on
            if any((h_ or {}).get("motion") for h_ in (HOUSE.get("hubs") or {}).values()):
                go("hub-motion", watch_hub_motion)   # someone walks up to a Hub: it chimes for kids with something waiting
        if TVS or HUBS:
            go("tv-guard", watch_tvs)             # chore time, TV locks, Hub keeper, bedtime holds
        if LAUNCHERS:
            go("launchers", watch_launchers)
        if CHORE_ROOMS or SIGNAL_BULBS:
            go("room-lights", watch_bright_rooms)
        if MAE_DASH:
            go("dashboard-mirror", watch_mae_dashboard)
        if HOUSE.get("vacuum"):
            import vacuum_auto
            go("vacuum", vacuum_auto.run, globals())
        if GROCY.get("url"):
            go("grocy-summary", watch_grocy)
        if (HOUSE.get("robot_locator") or {}).get("robots"):
            import locator
            go("robot-locator", locator.run, globals())   # which room each robot is in, even off-cleaning
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
