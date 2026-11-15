"""Which days the kids are at this house, from a shared calendar, for chores tied to a weekly event (e.g. church).

house.json "kid_calendar" (all optional):
  ics_url   the calendar's private iCal address (e.g. Google Calendar > settings > "Secret address in iCal format")
  with      regex an all-day event's title must match to mean "the kids are here" (default "^Home")
  every_hours  how often to re-read it (default 6)
Days the calendar covers are cached in the data folder (kid_calendar.json: {"days": {"YYYY-MM-DD": true|false}}),
so a slot chore can follow the real schedule (holidays, swaps) and fall back to its every-N-weeks rule elsewhere.
Only all-day events are read; recurring ones (RRULE with FREQ=DAILY/WEEKLY, INTERVAL, BYDAY, UNTIL, COUNT),
EXDATE and moved instances (RECURRENCE-ID) are handled. Standard library only.
"""
import json
import re
import time
import urllib.request
from datetime import date, datetime, timedelta

CFG = {}
CACHE = {"file": None, "days": {}, "fetched": 0}
DOW = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def configure(cfg, cache_file):
    CFG.clear()
    CFG.update(cfg or {})
    CACHE["file"] = cache_file
    try:
        saved = json.loads(open(cache_file).read())
    except (OSError, ValueError, TypeError):
        saved = {}
    CACHE["days"] = dict(saved.get("days") or {})
    CACHE["fetched"] = saved.get("fetched") or 0


def _unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text).splitlines()


def _d(v):
    """an iCal DATE or DATE-TIME value -> date"""
    v = v.strip()[:8]
    return date(int(v[:4]), int(v[4:6]), int(v[6:8]))


def parse(text):
    """VEVENTs as dicts: summary, start (date), end (date, exclusive), allday, rrule (dict), exdates (set), rid (date|None), uid"""
    out, ev = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            ev = {"exdates": set(), "rid": None, "allday": False, "summary": "", "rrule": None, "uid": ""}
        elif line == "END:VEVENT" and ev is not None:
            if "start" in ev:
                ev.setdefault("end", ev["start"] + timedelta(days=1))
                out.append(ev)
            ev = None
        elif ev is not None and ":" in line:
            k, v = line.split(":", 1)
            name, params = k.split(";")[0], k.upper()
            if name == "DTSTART":
                ev["start"] = _d(v)
                ev["allday"] = "VALUE=DATE" in params or len(v.strip()) == 8
            elif name == "DTEND":
                ev["end"] = _d(v)
            elif name == "SUMMARY":
                ev["summary"] = v.replace("\\,", ",").replace("\\;", ";").strip()
            elif name == "UID":
                ev["uid"] = v.strip()
            elif name == "RRULE":
                ev["rrule"] = dict(p.split("=", 1) for p in v.split(";") if "=" in p)
            elif name == "EXDATE":
                ev["exdates"] |= {_d(x) for x in v.split(",") if x.strip()}
            elif name == "RECURRENCE-ID":
                ev["rid"] = _d(v)
    return out


def occurrences(ev, until):
    """start dates of an event's instances up to `until` (inclusive)"""
    r = ev.get("rrule")
    if not r:
        return [ev["start"]] if ev["start"] <= until else []
    freq, step = r.get("FREQ"), int(r.get("INTERVAL") or 1)
    stop = _d(r["UNTIL"]) if r.get("UNTIL") else until
    stop = min(stop, until)
    count = int(r["COUNT"]) if r.get("COUNT") else None
    out = []
    if freq == "DAILY":
        d = ev["start"]
        while d <= stop and (count is None or len(out) < count):
            out.append(d)
            d += timedelta(days=step)
    elif freq == "WEEKLY":
        days = [DOW.index(x[-2:]) for x in (r.get("BYDAY") or DOW[ev["start"].weekday()]).split(",") if x[-2:] in DOW]
        wk = ev["start"] - timedelta(days=ev["start"].weekday())
        while wk <= stop and (count is None or len(out) < count):
            for wd in sorted(days):
                d = wk + timedelta(days=wd)
                if ev["start"] <= d <= stop and (count is None or len(out) < count):
                    out.append(d)
            wk += timedelta(weeks=step)
    else:
        return [ev["start"]] if ev["start"] <= until else []
    return [d for d in out if d not in ev["exdates"]]


def days_here(text, pattern="^Home", start=None, horizon_days=120):
    """{"YYYY-MM-DD": True|False} for each day from `start` for horizon_days: True if an all-day event matching
    `pattern` covers it. Moved instances (RECURRENCE-ID) replace the instance they move."""
    start = start or date.today() - timedelta(days=7)
    until = start + timedelta(days=horizon_days)
    evs = [e for e in parse(text) if e["allday"]]
    moved = {(e["uid"], e["rid"]) for e in evs if e["rid"]}
    spans = []
    for e in evs:
        n = (e["end"] - e["start"]).days or 1
        if e["rid"]:
            spans.append((e["start"], e["start"] + timedelta(days=n), e["summary"]))
            continue
        for s in occurrences(e, until):
            if (e["uid"], s) not in moved:
                spans.append((s, s + timedelta(days=n), e["summary"]))
    rx = re.compile(pattern, re.I)
    out, d = {}, start
    while d <= until:
        out[d.isoformat()] = any(a <= d < b and rx.search(t) for a, b, t in spans)
        d += timedelta(days=1)
    return out


def refresh(fetch=None):
    """re-read the calendar (if configured); keeps the old cache when the read fails"""
    url = CFG.get("ics_url")
    if not url:
        return False
    try:
        text = fetch(url) if fetch else urllib.request.urlopen(url, timeout=30).read().decode("utf-8", "replace")
        days = days_here(text, CFG.get("with") or "^Home")
    except Exception:  # noqa: BLE001
        return False
    CACHE["days"].update(days)
    CACHE["fetched"] = time.time()
    if CACHE["file"]:
        try:
            with open(CACHE["file"], "w") as f:
                json.dump({"days": CACHE["days"], "fetched": CACHE["fetched"]}, f)
        except OSError:
            pass
    return True


def here_on(d):
    """True / False from the calendar, or None when the calendar says nothing about that day"""
    v = CACHE["days"].get(d.isoformat() if isinstance(d, (date, datetime)) else str(d))
    return None if v is None else bool(v)


def watch():
    while True:
        refresh()
        time.sleep(float(CFG.get("every_hours") or 6) * 3600)
