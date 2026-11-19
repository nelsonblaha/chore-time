"""Print an addressed envelope from a kid's screen (e.g. after writing a letter).

house.json "envelopes" (all optional except the addresses):
  printer        CUPS queue name
  lp_user        optional CUPS job owner (keeps automated envelope jobs out of the desktop user's print notifications)
  desktop_summary_after local time for one quiet next-day desktop summary (for example "08:00")
  grocy_envelope_product product id consumed for every accepted print and "already printed" recovery
  grocy_products {label: product id}; one of each remaining supply (for example a stamp) is consumed at handoff
  grocy_track_after epoch timestamp for the physical-count baseline (older handoffs are not deducted retroactively)
  lp_options     {"PageSize": "Env10", "InputSlot": "Manual", ...} passed to lp as -o key=value
  size_pt        [width, height] of the envelope in points, landscape (US #10 = [684, 297])
  chores         chore ids whose "I'm done" goes through the envelope steps
  return_address lines under the sender's name (top left)
  senders        {kid id: "Name as printed"}; only these kids can be the "from"
  recipients     [{"id", "label" (on the button), "name" (printed), "icon", "address": [lines] (optional)}]
  to_address     lines under the recipient's name when a recipient has no address of its own
  load_photo     an image in the data folder showing how to put the envelope in the printer
  handoff        the last instruction once the envelope is printed (default: give it to a grown-up)
  handoff_done   the kid's button once they've done it (default: "I gave it to a grown-up!")
  printer_uri    the printer's own IPP address (e.g. ipp://192.168.0.20/ipp/print): lets the kids' screen see a jam
A one-page PDF is written with the built-in Helvetica font (no extra libraries) and sent to lp.
"""
import json
import re
import subprocess
import tempfile
import time
import uuid

CFG = {}
LAST = {"ts": 0.0}
JOBS = set()     # print jobs this app sent (only these can be checked or cancelled)
PRINTED = {}     # kid -> when an envelope was last printed for them (so coming back skips straight to "deliver it")
PRINTS = []      # successful automated prints, retained briefly for the next-day desktop summary
USES = []        # physical envelopes used by prints/reprints or recovered through "already printed"
SUMMARY = {}     # {date: YYYY-MM-DD}; prevents duplicate summaries after service restarts
MIN_GAP_S = 20   # a double tap must not print two envelopes


STATE = {"file": None}


def configure(cfg, state_file=None):
    CFG.clear()
    CFG.update(cfg or {})
    STATE["file"] = state_file
    PRINTED.clear()
    LASTPRINT.clear()
    PRINTS.clear()
    USES.clear()
    SUMMARY.clear()
    try:
        saved = json.loads(open(state_file).read()) if state_file else {}
    except (OSError, ValueError):
        saved = {}
    LASTPRINT.update(saved.pop("_last", None) or {})
    PRINTS.extend(x for x in (saved.pop("_prints", None) or []) if isinstance(x, dict) and isinstance(x.get("ts"), (int, float)))
    USES.extend(x for x in (saved.pop("_uses", None) or []) if isinstance(x, dict) and x.get("id") and isinstance(x.get("ts"), (int, float)))
    summary_date = saved.pop("_summary_date", None)
    if summary_date:
        SUMMARY["date"] = summary_date
    PRINTED.update({k: v for k, v in saved.items() if isinstance(v, (int, float))})


LASTPRINT = {}   # the newest envelope: {"ts", "from", "to", "job"} (for the grown-ups' checklist)


def _save():
    if STATE["file"]:
        try:
            with open(STATE["file"], "w") as f:
                json.dump(dict(PRINTED, _last=LASTPRINT, _prints=PRINTS[-100:], _uses=USES[-1000:],
                               _summary_date=SUMMARY.get("date")), f)
        except OSError:
            pass


def mark_printed(kids_, info=None, used=False):
    now = time.time()
    for k in kids_:
        PRINTED[k] = now
    if info:
        LASTPRINT.clear()
        LASTPRINT.update(info, ts=now)
        PRINTS.append(dict(info, ts=LASTPRINT["ts"]))
        USES.append({"id": "print:" + str(info.get("job") or uuid.uuid4().hex), "ts": now, "kind": "print"})
        PRINTS[:] = [x for x in PRINTS if now - x["ts"] < 14 * 86400]
    elif used:
        USES.append({"id": "already:" + uuid.uuid4().hex, "ts": now, "kind": "already_printed"})
    _save()


def desktop_summary(now=None, run=subprocess.run):
    """Send at most one low-priority summary for yesterday's prints; return its text, or None."""
    after = CFG.get("desktop_summary_after")
    if not after:
        return None
    from datetime import datetime, timedelta, time as dtime
    now = time.time() if now is None else now
    dt = datetime.fromtimestamp(now)
    try:
        hh, mm = map(int, str(after).split(":", 1))
        threshold = dtime(hh, mm)
    except (TypeError, ValueError):
        return None
    today = dt.date()
    if SUMMARY.get("date") == today.isoformat() or dt.time() < threshold:
        return None
    prior = today - timedelta(days=1)
    lo = datetime.combine(prior, dtime.min).timestamp()
    hi = datetime.combine(today, dtime.min).timestamp()
    count = sum(lo <= x["ts"] < hi for x in PRINTS)
    SUMMARY["date"] = today.isoformat()
    _save()
    if not count:
        return None
    text = "Kids printed %d envelope%s yesterday." % (count, "" if count == 1 else "s")
    try:
        run(["notify-send", "-a", "Chore Time", "-u", "low", "Kids’ envelopes", text],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass
    return text


def enabled():
    return bool(CFG.get("senders") and CFG.get("recipients"))


def public():
    """what the kids' screens need (names and labels only)"""
    if not enabled():
        return None
    return {"chores": list(CFG.get("chores") or []), "senders": list(CFG["senders"]),
            "recipients": [{"id": r["id"], "label": r.get("label") or r["name"], "icon": r.get("icon") or "1f48c"}
                           for r in CFG["recipients"]],
            "photo": bool(CFG.get("load_photo")),
            "handoff": CFG.get("handoff") or "Fold your letter and put it in the envelope, but don't seal it! Then give it to a grown-up.",
            "handoff_done": CFG.get("handoff_done") or "I gave it to a grown-up!",
            "printed": {k: t for k, t in PRINTED.items() if time.time() - t < 12 * 3600},
            "last": ({"ts": LASTPRINT["ts"], "from": LASTPRINT.get("from"), "job": LASTPRINT.get("job"),
                      "to": next((r.get("label") or r["name"] for r in CFG["recipients"] if r["id"] == LASTPRINT.get("to")), LASTPRINT.get("to"))}
                     if LASTPRINT.get("ts") and time.time() - LASTPRINT["ts"] < 12 * 3600 else None)}


def _esc(t):
    t = str(t).encode("latin-1", "replace").decode("latin-1")
    return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def pdf(sender_lines, to_lines, size=(684, 297)):
    """a one-page PDF: return address top left (10 pt), recipient in the middle right (15 pt)"""
    w, h = size
    ops = ["BT /F1 10 Tf 12 TL 30 %d Td" % (h - 33)]
    for i, line in enumerate(sender_lines):
        ops.append(("(%s) Tj" if i == 0 else "T* (%s) Tj") % _esc(line))
    ops.append("ET")
    ops.append("BT /F1 15 Tf 21 TL %d %d Td" % (int(w * 0.42), int(h - 139)))
    for i, line in enumerate(to_lines):
        ops.append(("(%s) Tj" if i == 0 else "T* (%s) Tj") % _esc(line))
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>" % (w, h)).encode(),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"]
    out, offs = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for o in offs:
        out += b"%010d 00000 n \n" % o
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def build(sender, recipient):
    """(pdf bytes, title) or raises ValueError"""
    if sender not in (CFG.get("senders") or {}):
        raise ValueError("unknown sender")
    rec = next((r for r in CFG.get("recipients") or [] if r["id"] == recipient), None)
    if not rec:
        raise ValueError("unknown recipient")
    frm = [CFG["senders"][sender]] + list(CFG.get("return_address") or [])
    to = [rec["name"]] + list(rec.get("address") or CFG.get("to_address") or [])
    return pdf(frm, to, tuple(CFG.get("size_pt") or (684, 297))), "%s to %s - envelope" % (sender, rec.get("label") or rec["name"])


def print_envelope(sender, recipient, run=subprocess.run):
    """send it to the printer; returns the lp output (job id line)"""
    if time.time() - LAST["ts"] < MIN_GAP_S:
        raise ValueError("already printing")
    data, title = build(sender, recipient)
    if not CFG.get("printer"):
        raise ValueError("no printer set")
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data)
        f.flush()
        cmd = ["lp"]
        if CFG.get("lp_user"):
            cmd += ["-U", CFG["lp_user"]]
        cmd += ["-d", CFG["printer"], "-t", title]
        for k, v in (CFG.get("lp_options") or {}).items():
            cmd += ["-o", "%s=%s" % (k, v)]
        r = run(cmd + [f.name], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise ValueError((r.stderr or "print failed").strip()[:200])
    LAST["ts"] = time.time()
    m = re.search(r"request id is (\S+)", r.stdout or "")
    if m:
        JOBS.add(m.group(1))
    return m.group(1) if m else (r.stdout or "").strip()


# printer-state-reasons -> what the kids' screen says
PROBLEMS = (("jam", "jam"), ("media-empty", "empty"), ("media-needed", "empty"), ("input-tray-missing", "empty"),
            ("door-open", "open"), ("cover-open", "open"), ("toner-empty", "toner"), ("offline", "offline"))


def printer_problem(run=subprocess.run):
    uri = CFG.get("printer_uri")
    if not uri:
        return None
    try:
        r = run(["ipptool", "-tv", uri, "get-printer-attributes.test"], capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001
        return None
    reasons = " ".join(l.split("=", 1)[1] for l in (r.stdout or "").splitlines() if "printer-state-reasons" in l and "=" in l)
    return next((p for k, p in PROBLEMS if k in reasons), None)


JOB_TEST = """{
OPERATION Get-Job-Attributes
GROUP operation-attributes-tag
ATTR charset attributes-charset utf-8
ATTR naturalLanguage attributes-natural-language en
ATTR uri printer-uri $uri
ATTR integer job-id $job
ATTR name requesting-user-name $user
ATTR keyword requested-attributes job-state,job-state-reasons
}
"""


def job_state(job, run=subprocess.run):
    """CUPS job-state: pending, processing, processing-stopped, canceled, aborted, completed (or None)"""
    import getpass
    num = job.rsplit("-", 1)[-1]
    with tempfile.NamedTemporaryFile("w", suffix=".test") as f:
        f.write(JOB_TEST)
        f.flush()
        r = run(["ipptool", "-tv", "-d", "job=" + num, "-d", "user=" + getpass.getuser(),
                 "ipp://localhost/printers/" + CFG.get("printer", ""), f.name], capture_output=True, text=True, timeout=10)
    m = re.search(r"job-state \(enum\) = (\S+)", r.stdout or "")
    return m.group(1) if m else None


def status(job, run=subprocess.run):
    """{"state": printing|done|stopped|gone, "problem": jam|empty|open|toner|offline|None} for a job this app sent"""
    if job not in JOBS:
        return {"state": "gone", "problem": None}
    st = job_state(job, run)
    if st in ("pending", "pending-held", "processing", "processing-stopped"):
        return {"state": "printing", "problem": printer_problem(run)}
    if st == "completed":
        return {"state": "done", "problem": None}
    return {"state": "stopped" if st in ("canceled", "aborted") else "gone", "problem": printer_problem(run)}


def cancel(job, run=subprocess.run):
    if job in JOBS:
        run(["cancel", job], capture_output=True, text=True, timeout=10)
        LAST["ts"] = 0.0   # starting over may print again right away
