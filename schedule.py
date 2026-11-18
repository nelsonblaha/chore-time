"""Which days the kids are at this house, from a shared calendar, for chores tied to a weekly event (e.g. church).

house.json "kid_calendar" (all optional):
  ics_url   the calendar's private iCal address (e.g. Google Calendar > settings > "Secret address in iCal format")
  with      regex an all-day event's title must match to mean "the kids are here" (default "^Home")
  every_hours  how often to re-read it (default 6)
  no_school    regex: an all-day event whose title matches means no school that day (default: holiday, break,
               intersession, "no school", "off school", vacation); naming a kid makes it that kid's day off only
  school_anyway  regex that wins over no_school (default: "last day", "resumes", "first day")
  agenda      optional kids-screen agenda: days, max_events, base {name/include/exclude}, and extra_sources
              [{name, ics_url, include, exclude}]. Only titles and times are retained; descriptions, locations,
              links and guest lists are discarded. Keep every private iCal address in the private house.json.
Days the calendar covers are cached in the data folder (kid_calendar.json: {"days": {"YYYY-MM-DD": true|false}}),
so a slot chore can follow the real schedule (holidays, swaps) and fall back to its every-N-weeks rule elsewhere.
Only all-day events are read; recurring ones (RRULE with FREQ=DAILY/WEEKLY, INTERVAL, BYDAY, UNTIL, COUNT),
EXDATE and moved instances (RECURRENCE-ID) are handled. Standard library only.
"""
import json
import re
import time
import urllib.request
from datetime import date, datetime, time as day_time, timedelta, timezone
from zoneinfo import ZoneInfo

CFG = {}
CACHE = {"file": None, "days": {}, "off": {}, "agenda_sources": {}, "fetched": 0}
NO_SCHOOL = r"holiday|break|intersession|no (regular )?school|off school|vacation"
SCHOOL_ANYWAY = r"last (school )?day|resumes|first day"
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
    CACHE["off"] = dict(saved.get("off") or {})
    CACHE["agenda_sources"] = dict(saved.get("agenda_sources") or {})
    CACHE["fetched"] = saved.get("fetched") or 0


def _unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text).splitlines()


def _d(v):
    """an iCal DATE or DATE-TIME value -> date"""
    v = v.strip()[:8]
    return date(int(v[:4]), int(v[4:6]), int(v[6:8]))


def _dt(v, params=""):
    """An iCal DATE-TIME, normalized to the configured display timezone."""
    v = v.strip()
    zone = ZoneInfo(CFG.get("timezone") or "America/Denver")
    if v.endswith("Z"):
        return datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(zone)
    out = datetime.strptime(v[:15], "%Y%m%dT%H%M%S")
    hit = re.search(r"(?:^|;)TZID=([^;:]+)", params)
    try:
        return out.replace(tzinfo=ZoneInfo(hit.group(1)) if hit else zone).astimezone(zone)
    except (KeyError, ValueError):
        return out.replace(tzinfo=zone)


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
                if not ev["allday"]:
                    ev["start_dt"] = _dt(v, k)
            elif name == "DTEND":
                ev["end"] = _d(v)
                if not ev.get("allday") and "T" in v:
                    ev["end_dt"] = _dt(v, k)
            elif name == "SUMMARY":
                ev["summary"] = v.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").strip()
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


def _spans(text, until):
    """(start, end exclusive, title) for every all-day instance up to `until`. Moved instances (RECURRENCE-ID)
    replace the instance they move."""
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
    return spans


def days_here(text, pattern="^Home", start=None, horizon_days=120):
    """{"YYYY-MM-DD": True|False} for each day from `start` for horizon_days: True if an all-day event matching
    `pattern` covers it."""
    start = start or date.today() - timedelta(days=7)
    until = start + timedelta(days=horizon_days)
    spans, rx = _spans(text, until), re.compile(pattern, re.I)
    out, d = {}, start
    while d <= until:
        out[d.isoformat()] = any(a <= d < b and rx.search(t) for a, b, t in spans)
        d += timedelta(days=1)
    return out


def days_off(text, start=None, horizon_days=120, no_school=None, school_anyway=None):
    """{"YYYY-MM-DD": [titles]} for the days an all-day event says there's no school ("District: Winter Break",
    "No school - teacher development"); titles matching school_anyway ("last school day before break") don't count."""
    start = start or date.today() - timedelta(days=7)
    until = start + timedelta(days=horizon_days)
    off_rx, yes_rx = re.compile(no_school or NO_SCHOOL, re.I), re.compile(school_anyway or SCHOOL_ANYWAY, re.I)
    spans = [(a, b, t) for a, b, t in _spans(text, until) if off_rx.search(t) and not yes_rx.search(t)]
    out, d = {}, start
    while d <= until:
        hit = sorted({t for a, b, t in spans if a <= d < b})
        if hit:
            out[d.isoformat()] = hit
        d += timedelta(days=1)
    return out


def agenda_events(text, source="Calendar", start=None, horizon_days=45, include=None, exclude=None):
    """Small, display-safe agenda records. Descriptions, locations, guests and links never leave the feed parser."""
    zone = ZoneInfo(CFG.get("timezone") or "America/Denver")
    start = start or datetime.now(zone)
    if isinstance(start, date) and not isinstance(start, datetime):
        start = datetime.combine(start, day_time.min, zone)
    until = start + timedelta(days=horizon_days)
    yes = re.compile(include, re.I) if include else None
    no = re.compile(exclude, re.I) if exclude else None
    events = parse(text)
    moved = {(e["uid"], e["rid"]) for e in events if e.get("rid")}
    out = []
    for ev in events:
        title = re.sub(r"\s+", " ", ev.get("summary") or "").strip()
        if not title or (yes and not yes.search(title)) or (no and no.search(title)):
            continue
        starts = [ev["start"]] if ev.get("rid") else occurrences(ev, until.date())
        for d in starts:
            if not ev.get("rid") and (ev.get("uid"), d) in moved:
                continue
            if ev.get("allday"):
                days = max(1, (ev.get("end", ev["start"] + timedelta(days=1)) - ev["start"]).days)
                end_d = d + timedelta(days=days)
                if end_d <= start.date() or d > until.date():
                    continue
                rec = {"title": title, "start": d.isoformat(), "end": end_d.isoformat(), "all_day": True, "source": source}
            else:
                base = ev.get("start_dt") or datetime.combine(ev["start"], day_time.min, zone)
                end_base = ev.get("end_dt") or (base + timedelta(hours=1))
                begins = base.replace(year=d.year, month=d.month, day=d.day)
                ends = begins + max(timedelta(minutes=1), end_base - base)
                if ends <= start or begins > until:
                    continue
                rec = {"title": title, "start": begins.timestamp(), "end": ends.timestamp(), "all_day": False, "source": source}
            out.append(rec)
    return out


def _agenda_cfg(source, default_name):
    return {"source": source.get("name") or default_name, "include": source.get("include"), "exclude": source.get("exclude")}


def agenda(now=None):
    """Merged, de-duplicated upcoming agenda for the kids' menu."""
    ac = CFG.get("agenda") or {}
    if not ac:
        return []
    zone = ZoneInfo(CFG.get("timezone") or "America/Denver")
    now = now or datetime.now(zone)
    if not isinstance(now, datetime):
        now = datetime.fromtimestamp(float(now), zone)
    until = now + timedelta(days=float(ac.get("days") or 14))
    rows, seen = [], set()
    for items in CACHE.get("agenda_sources", {}).values():
        for e in items:
            if e.get("all_day"):
                visible = e.get("end", "") > now.date().isoformat() and e.get("start", "") <= until.date().isoformat()
            else:
                visible = float(e.get("end") or 0) > now.timestamp() and float(e.get("start") or 0) <= until.timestamp()
            key = (re.sub(r"\W+", "", str(e.get("title") or "").lower()), e.get("start"), e.get("end"))
            if visible and key not in seen:
                seen.add(key)
                rows.append(e)
    rows.sort(key=lambda e: (e["start"] if not e.get("all_day") else datetime.fromisoformat(e["start"]).replace(tzinfo=zone).timestamp(), e["title"]))
    return rows[:int(ac.get("max_events") or 24)]


def refresh(fetch=None):
    """re-read the calendar (if configured); keeps the old cache when the read fails"""
    url = CFG.get("ics_url")
    if not url:
        return False
    try:
        text = fetch(url) if fetch else urllib.request.urlopen(url, timeout=30).read().decode("utf-8", "replace")
        days = days_here(text, CFG.get("with") or "^Home")
        off = days_off(text, no_school=CFG.get("no_school"), school_anyway=CFG.get("school_anyway"))
    except Exception:  # noqa: BLE001
        return False
    CACHE["days"].update(days)
    for d in days:            # the days just read are replaced (a holiday taken off the calendar is gone)
        CACHE["off"].pop(d, None)
    CACHE["off"].update(off)
    ac = CFG.get("agenda") or {}
    if ac:
        base = _agenda_cfg(ac.get("base") or {}, "Kids")
        CACHE["agenda_sources"]["base"] = agenda_events(text, **base)
        wanted = {"base"} | {"extra:%d" % i for i, source in enumerate(ac.get("extra_sources") or []) if source.get("ics_url")}
        for key in list(CACHE["agenda_sources"]):
            if key not in wanted:
                CACHE["agenda_sources"].pop(key, None)
        for i, source in enumerate(ac.get("extra_sources") or []):
            if not source.get("ics_url"):
                continue
            try:
                extra = fetch(source["ics_url"]) if fetch else urllib.request.urlopen(source["ics_url"], timeout=30).read().decode("utf-8", "replace")
                CACHE["agenda_sources"]["extra:%d" % i] = agenda_events(extra, **_agenda_cfg(source, "Family"))
            except Exception:  # noqa: BLE001
                pass   # keep that source's last good agenda
    CACHE["fetched"] = time.time()
    if CACHE["file"]:
        try:
            with open(CACHE["file"], "w") as f:
                json.dump({"days": CACHE["days"], "off": CACHE["off"], "agenda_sources": CACHE["agenda_sources"],
                           "fetched": CACHE["fetched"]}, f)
        except OSError:
            pass
    return True


def here_on(d):
    """True / False from the calendar, or None when the calendar says nothing about that day"""
    v = CACHE["days"].get(d.isoformat() if isinstance(d, (date, datetime)) else str(d))
    return None if v is None else bool(v)


def off_on(d):
    """titles of the no-school events on that day ([] = a school day as far as the calendar says), or None when
    the calendar says nothing about that day"""
    k = d.isoformat() if isinstance(d, (date, datetime)) else str(d)
    if k not in CACHE["days"]:
        return None
    return list(CACHE["off"].get(k) or [])


def watch():
    while True:
        refresh()
        time.sleep(float(CFG.get("every_minutes") or float(CFG.get("every_hours") or 6) * 60) * 60)
