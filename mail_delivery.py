"""Turn a doorbell's mail-delivery captions into a persistent chore reopen."""
from datetime import datetime
import time


def delivery_time(event):
    try:
        return datetime.fromisoformat(str(event.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0


def is_mail_delivery(event):
    """Require postal evidence, not merely a person/package at the door."""
    words = " ".join(str(event.get(k) or "") for k in ("caption", "description")).casefold()
    carrier = any(x in words for x in ("mail carrier", "postal carrier", "postal worker", "usps"))
    delivered = any(x in words for x in ("deliver", "mail", "letter"))
    return carrier and delivered


def check(g, config, now=None):
    """Reopen once per new camera event when it is newer than the last sorting."""
    now = now or time.time()
    chore = config.get("chore", "mail")
    state = g["ha_get"](config["entity"], g["ha_token"]()) or {}
    events = (state.get("attributes") or {}).get("events") or []
    ledger = g["read_events"]()
    seen = {e.get("source_event") for e in ledger if e.get("source") == "mail_delivery"}
    last_clean = ((g["derive"]().get("board") or {}).get(chore) or {}).get("last_clean") or 0
    max_age = float(config.get("max_age_h", 72)) * 3600
    found = []
    for event in events:
        at = delivery_time(event)
        event_id = str(event.get("event_id") or "")
        if event_id and event_id not in seen and is_mail_delivery(event) and at > last_clean and -60 <= now - at <= max_age:
            found.append((at, event_id))
    for at, event_id in sorted(found):
        when = datetime.fromtimestamp(at).strftime("%-I:%M %p")
        g["append"]({"type": "reopen", "chore": chore, "by": "mail detector",
                     "note": "Mail was delivered at %s." % when,
                     "source": "mail_delivery", "source_event": event_id, "delivery_ts": at})
        g["ilog"]({"src": "mail_delivery", "action": "reopen", "chore": chore,
                   "source_event": event_id, "delivery_ts": at})
    if found:
        g["after_change"]()
    return len(found)


def run(g):
    config = g["HOUSE"].get("mail_delivery") or {}
    while True:
        try:
            check(g, config)
        except Exception as ex:  # noqa: BLE001 (HA may be restarting; retry without killing the service)
            print("mail delivery watcher error", ex, flush=True)
        time.sleep(float(config.get("poll_s", 30)))
