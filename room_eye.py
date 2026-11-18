"""Room eye: cameras that learn what a tidy room looks like, mark messy rooms untidy, and help find the robots.

Only while a camera is already on (the house's own camera policy decides that, e.g. "the floor is empty"):
  * Every few hours (every_h, default 4) per camera: turn the room's lights full white for a moment (snapshot, white,
    picture, restore the snapshot), take one picture,
    compare it with the room's TIDY pictures, and if enough of it changed, mark the room untidy (live).
  * A grown-up's "tidy" / "untidy" click on a room this camera sees (the map's Mark tidy, a reopened or marked-clean
    room job, a passed inspection) adds a labelled picture: tidy ones become the references, untidy ones calibrate how
    much change counts as mess. If the camera is off at the click, the picture waits for the room's next camera window
    (the newest click wins; dropped after a day).
  * Trust the map while a camera is new: its first few pictures of each kind are labelled by the map's own tidy /
    untidy state for its rooms.
  * What changed goes to a label queue (a crop per spot) for a grown-up to name and mark "vacuum hazard" or not. A spot
    labelled "not a hazard" at the same place is ignored from then on.
  * Robot watch: while a floor is empty, motion in a camera's view while that floor's robot is out means the robot is in
    that camera's room; that teaches the robot locator (position -> room) and gives it a fresh fix.

Only the grown-ups' labels are ever used as evidence; the camera's own verdicts never become training data.

house.json:
  "room_eye": {
    "capture_url": "http://127.0.0.1:8791",       # GET /cameras, POST /capture {camera, purpose} -> image/jpeg
    "every_h": 4, "settle_s": 5,
    "cameras": {"sun_room": {"spaces": ["sun_room"], "lights": ["light.sun_room"],
                             "motion": "binary_sensor.sun_room_camera_motion",
                             "robots": {"vacuum.downstairs_robot": "Sun Room"}}}
  }
Needs numpy and Pillow.
"""
import io
import json
import time
import urllib.request
import uuid
from pathlib import Path

GRID = (20, 15)          # blocks across, down
WIDTH = 320              # frames are compared at this width
REFS = 8                 # newest tidy pictures kept as references per camera
DEFAULT_THRESHOLD = 0.01  # fraction of blocks changed (3 of 300: about a sock) that means mess, until calibrated
MIN_BLOCK = 12.0         # a block must differ at least this much (0-255, after lighting normalisation)
NOISE_X = 1.6            # ... and this many times more than the tidy pictures differ among themselves there
LABEL_TTL_S = 86400
QUEUE_MAX = 40
TEACH_QUIET_S = 30
WHITE_K = 5000           # "full white": daylight white at full brightness
BOOT = 3                 # trust the map's tidy/untidy state for a camera's first pictures of each kind       # no robot teaching right after our own light flash (the light change looks like motion)
ADULT_BY_SKIP = {"vacuum", "dinner", "grocy", "camera", "auto"}


# ---------------------------------------------------------------- image maths (pure, tested)
def prep(jpeg):
    """JPEG bytes -> (normalised grey float array HxW, colour PIL image at compare size)."""
    import numpy as np
    from PIL import Image, ImageFilter
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    h = int(im.height * WIDTH / im.width)
    im = im.resize((WIDTH, h))
    g = im.convert("L")
    a = np.asarray(g, dtype=np.float32)
    bg = np.asarray(g.filter(ImageFilter.GaussianBlur(24)), dtype=np.float32)
    a = a - bg                                   # remove slow lighting gradients
    a = (a - a.mean()) / (a.std() + 1e-6) * 40 + 128
    return a, im


def blocks(a):
    import numpy as np
    gw, gh = GRID
    h, w = a.shape
    bh, bw = h // gh, w // gw
    a = a[:bh * gh, :bw * gw]
    return a.reshape(gh, bh, gw, bw).transpose(0, 2, 1, 3).reshape(gh, gw, bh * bw)


def block_diff(a, b):
    """mean absolute difference per block"""
    import numpy as np
    return np.abs(blocks(a) - blocks(b)).mean(axis=2)


def noise(refs):
    """per block, how much tidy pictures differ among themselves (their normal variation)"""
    import numpy as np
    if len(refs) < 2:
        return np.full((GRID[1], GRID[0]), MIN_BLOCK / NOISE_X, dtype=np.float32)
    ds = [block_diff(refs[i], refs[j]) for i in range(len(refs)) for j in range(i + 1, len(refs))]
    return np.max(ds, axis=0)


def changed(a, refs, ignore=()):
    """bool grid of blocks that differ from EVERY tidy reference beyond normal variation, minus ignored blocks"""
    import numpy as np
    d = np.min([block_diff(a, r) for r in refs], axis=0)
    ch = (d > MIN_BLOCK) & (d > noise(refs) * NOISE_X)
    for (x, y) in ignore:
        if 0 <= y < ch.shape[0] and 0 <= x < ch.shape[1]:
            ch[y, x] = False
    return ch


def regions(ch):
    """connected groups of changed blocks -> [{"blocks": [(x, y)...], "box": [x0, y0, x1, y1] in blocks}]"""
    seen, out = set(), []
    gh, gw = ch.shape
    for y in range(gh):
        for x in range(gw):
            if ch[y, x] and (x, y) not in seen:
                stack, comp = [(x, y)], []
                seen.add((x, y))
                while stack:
                    cx, cy = stack.pop()
                    comp.append((cx, cy))
                    for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                        if 0 <= nx < gw and 0 <= ny < gh and ch[ny, nx] and (nx, ny) not in seen:
                            seen.add((nx, ny))
                            stack.append((nx, ny))
                xs, ys = [p[0] for p in comp], [p[1] for p in comp]
                out.append({"blocks": comp, "box": [min(xs), min(ys), max(xs) + 1, max(ys) + 1]})
    return out


def score(ch):
    return float(ch.mean())


def threshold(tidy_scores, untidy_scores):
    """between the messiest tidy picture and the tidiest untidy one; the default until both exist"""
    if tidy_scores and untidy_scores:
        lo, hi = max(tidy_scores), min(untidy_scores)
        if hi > lo:
            return (lo + hi) / 2
        return max(lo * 1.1, DEFAULT_THRESHOLD)
    if tidy_scores:
        return max(DEFAULT_THRESHOLD, max(tidy_scores) * 1.5)
    return DEFAULT_THRESHOLD


def crop(im, box, pad=1):
    gw, gh = GRID
    bw, bh = im.width / gw, im.height / gh
    x0, y0, x1, y1 = box
    c = im.crop((int(max(0, x0 - pad) * bw), int(max(0, y0 - pad) * bh), int(min(gw, x1 + pad) * bw), int(min(gh, y1 + pad) * bh)))
    out = io.BytesIO()
    c.save(out, "JPEG", quality=85)
    return out.getvalue()


def overlap(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua else 0


# ---------------------------------------------------------------- the running module
class Eye:
    def __init__(self, g, cfg):
        self.g, self.cfg = g, cfg
        self.dir = Path(g["DATA"]) / "room_eye"
        (self.dir / "samples").mkdir(parents=True, exist_ok=True)
        (self.dir / "labels").mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "state.json"
        try:
            self.S = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.S = {}
        for k, v in (("samples", []), ("queue", []), ("pending", {}), ("last_check", {}), ("seen_ev", 0), ("checks", []),
                     ("shots", []), ("photos", [])):
            self.S.setdefault(k, v)
        self.tok = None
        self.flashed_at = {}
        self.motion_prev = {}

    # ---- plumbing (replaced in tests)
    def http(self, method, path, body=None, timeout=30):
        req = urllib.request.Request(self.cfg["capture_url"].rstrip("/") + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()

    def cameras_on(self):
        try:
            _, _, b = self.http("GET", "/cameras", timeout=10)
            return {k: bool((v or {}).get("on")) for k, v in json.loads(b).items()}
        except Exception:  # noqa: BLE001
            return {}

    def capture(self, cam, purpose, wake=False):
        """(jpeg, people) or (None, why). wake: the camera service may turn the camera on just for this picture."""
        try:
            code, hd, b = self.http("POST", "/capture", {"camera": cam, "purpose": purpose, **({"wake": True} if wake else {})})
        except urllib.error.HTTPError as e:
            try:
                return None, json.loads(e.read()).get("why") or str(e.code)
            except Exception:  # noqa: BLE001
                return None, str(e.code)
        except Exception as ex:  # noqa: BLE001
            return None, repr(ex)[:120]
        people = int({k.lower(): v for k, v in hd.items()}.get("x-people", -1))
        return b, people

    def sleep(self, s):
        time.sleep(s)

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.S))
        tmp.replace(self.path)

    # ---- lights
    def flash(self, cam, shades=False):
        """Snapshot the room's lights (and a group's bulbs), turn them full white for the picture; with shades, open the
        room's shades too (restored from the same snapshot). Never through the house's light-mode selects: a mode write
        there means a person's request (sticky White, a re-armed timer)."""
        c = self.cfg["cameras"][cam]
        g, lights, covers = self.g, c.get("lights") or [], (c.get("shades") or []) if shades else []
        if not lights and not covers:
            return None
        snap = list(covers)
        for ent in lights:
            snap += [ent] + list((g["ha_get"](ent, self.tok).get("attributes") or {}).get("entity_id") or [])
        scene = "room_eye_" + cam
        g["ha_call"]("scene.create", {"scene_id": scene, "snapshot_entities": list(dict.fromkeys(snap))}, self.tok)
        if lights:
            g["ha_call"]("light.turn_on", {"entity_id": lights, "brightness": 255, "color_temp_kelvin": WHITE_K, "transition": 0}, self.tok)
        if covers:
            g["ha_call"]("cover.open_cover", {"entity_id": covers}, self.tok)
        self.flashed_at[cam] = time.time()
        self.sleep(float(c.get("shades_s", 20)) if covers else float(self.cfg.get("settle_s", 5)))
        return "scene." + scene

    def unflash(self, cam, scene):
        if scene:
            self.g["ha_call"]("scene.turn_on", {"entity_id": scene, "transition": 0}, self.tok)
        self.flashed_at[cam] = time.time()

    def shoot(self, cam, purpose, wake=False, shades=False):
        was = self.flash(cam, shades)
        try:
            return self.capture(cam, purpose, wake)
        finally:
            self.unflash(cam, was)

    # ---- before and after pictures for an inspection (room_eye.inspect_shots)
    def shot_events(self, e, by_job):
        """a kid claims a room job -> a "before" picture; says it's done -> an "after" (each room with a camera)"""
        if not self.cfg.get("inspect_shots") or e["type"] not in ("start", "report") or e.get("partial"):
            return
        cams = list(dict.fromkeys(c for sid in by_job.get(e.get("chore"), []) for c in self.cam_of_space(sid)))
        for cam in cams:
            self.S["shots"].append({"cam": cam, "chore": e["chore"], "kid": e.get("kid"), "ts": e["ts"],
                                    "which": "before" if e["type"] == "start" else "after",
                                    "ref": e.get("id") if e["type"] == "report" else None})

    def shots_blocked(self):
        """a grown-up named in inspect_shots.unless_home is home: no pictures"""
        for who in (self.cfg.get("inspect_shots") or {}).get("unless_home") or []:
            try:
                if self.g["adult_home"](who, self.tok):
                    return "%s is home" % who
            except Exception:  # noqa: BLE001
                return "can't tell whether %s is home" % who
        return None

    def do_shots(self, now):
        todo, self.S["shots"] = self.S["shots"], []
        for sh in todo:
            if now - sh["ts"] > 600:      # too late to show the room as it was then
                continue
            why = self.shots_blocked()
            if why:
                self.g["ilog"]({"src": "room_eye", "action": "shot_skipped", "chore": sh["chore"], "which": sh["which"], "why": why})
                continue
            jpeg, people = self.shoot(sh["cam"], "inspection_" + sh["which"], wake=True, shades=True)
            if not jpeg:
                self.g["ilog"]({"src": "room_eye", "action": "shot_failed", "chore": sh["chore"], "which": sh["which"], "why": people})
                continue
            self.store_shot(sh, jpeg, now)

    def store_shot(self, sh, jpeg, now):
        rec = None
        if sh["which"] == "after":   # pair with this kid's "before" of the same job, within a few hours
            rec = next((p for p in reversed(self.S["photos"]) if p["chore"] == sh["chore"] and p["kid"] == sh["kid"]
                        and p["cam"] == sh["cam"] and not p.get("after") and now - p["ts"] < 4 * 3600), None)
        if not rec:
            rec = {"key": uuid.uuid4().hex[:16], "cam": sh["cam"], "chore": sh["chore"], "kid": sh["kid"], "ts": sh["ts"]}
            self.S["photos"].append(rec)
        d = self.dir / "inspect" / rec["key"]
        d.mkdir(parents=True, exist_ok=True)
        (d / (sh["which"] + ".jpg")).write_bytes(jpeg)
        rec[sh["which"]] = sh["ts"]
        if sh.get("ref"):
            rec["ref"] = sh["ref"]
        media = (self.cfg.get("inspect_shots") or {}).get("media_dir")
        if media:   # Home Assistant's own (signed-in) media folder, for the grown-ups' page inside HA
            m = Path(media) / rec["key"]
            m.mkdir(parents=True, exist_ok=True)
            (m / (sh["which"] + ".jpg")).write_bytes(jpeg)
        self.prune(now)

    def prune(self, now):
        keep_s = float((self.cfg.get("inspect_shots") or {}).get("keep_days", 14)) * 86400
        media = (self.cfg.get("inspect_shots") or {}).get("media_dir")
        old = [p for p in self.S["photos"] if now - p["ts"] > keep_s]
        for p in old:
            for root in [self.dir / "inspect"] + ([Path(media)] if media else []):
                d = root / p["key"]
                for f in d.glob("*.jpg"):
                    f.unlink()
                try:
                    d.rmdir()
                except OSError:
                    pass
        self.S["photos"] = [p for p in self.S["photos"] if p not in old]

    def photos_for(self, ref=None, chore=None, kid=None):
        return [p for p in self.S["photos"] if (ref and p.get("ref") == ref) or (not ref and p["chore"] == chore and p["kid"] == kid)]

    # ---- samples
    def keep(self, cam, jpeg, label, why):
        sid = uuid.uuid4().hex[:12]
        (self.dir / "samples" / (sid + ".jpg")).write_bytes(jpeg)
        self.S["samples"].append({"id": sid, "camera": cam, "label": label, "ts": time.time(), "why": why})
        self.g["ilog"]({"src": "room_eye", "action": "sample", "camera": cam, "label": label, "why": why})
        return sid

    def load(self, s):
        return prep((self.dir / "samples" / (s["id"] + ".jpg")).read_bytes())[0]

    def refs(self, cam):
        tidy = [s for s in self.S["samples"] if s["camera"] == cam and s["label"] == "tidy"][-REFS:]
        return tidy, [self.load(s) for s in tidy]

    def ignored(self, cam):
        out = set()
        for q in self.S["queue"]:
            if q["camera"] == cam and q.get("hazard") is False:
                out.update(tuple(b) for b in q.get("blocks") or [])
        return out

    def calib(self, cam):
        """scores of each labelled picture against the tidy references (each tidy one against the OTHERS)"""
        tidy, refs = self.refs(cam)
        ts, us, ig = [], [], self.ignored(cam)
        for i, s in enumerate(tidy):
            others = refs[:i] + refs[i + 1:]
            if others:
                ts.append(score(changed(refs[i], others, ig)))
        for s in [s for s in self.S["samples"] if s["camera"] == cam and s["label"] == "untidy"][-REFS:]:
            if refs:
                us.append(score(changed(self.load(s), refs, ig)))
        return ts, us

    # ---- the tidy check
    def check(self, cam, jpeg, now=None):
        """score one picture; live: mark the camera's rooms untidy when it looks messy. Returns the check record."""
        now = now or time.time()
        tidy, refs = self.refs(cam)
        rec = {"camera": cam, "ts": now, "refs": len(refs)}
        if not refs:
            rec["verdict"] = "learning"
            return rec
        a, im = prep(jpeg)
        ch = changed(a, refs, self.ignored(cam))
        sc = score(ch)
        thr = threshold(*self.calib(cam))
        rec.update(score=round(sc, 4), threshold=round(thr, 4), verdict="untidy" if sc > thr else "tidy")
        found = regions(ch)
        hazards = [q for q in self.S["queue"] if q["camera"] == cam and q.get("hazard")]
        for r in found:
            hit = next((q for q in hazards if overlap(q["box"], r["box"]) > 0.3), None)
            if hit:
                rec.setdefault("hazards", []).append(hit.get("name") or "hazard")
            if not any(q["camera"] == cam and q.get("status") == "open" and overlap(q["box"], r["box"]) > 0.5 for q in self.S["queue"]):
                self.enqueue(cam, im, r, now)
        if rec.get("hazards"):
            rec["verdict"] = "untidy"
        if rec["verdict"] == "untidy":
            self.mark_untidy(cam, rec)
        self.S["checks"] = (self.S["checks"] + [rec])[-200:]
        self.g["ilog"]({"src": "room_eye", "action": "check", **rec})
        return rec

    def enqueue(self, cam, im, r, now):
        qid = uuid.uuid4().hex[:12]
        (self.dir / "labels" / (qid + ".jpg")).write_bytes(crop(im, r["box"]))
        self.S["queue"].append({"id": qid, "camera": cam, "ts": now, "box": r["box"], "blocks": r["blocks"],
                                "status": "open", "name": None, "hazard": None})
        opened = [q for q in self.S["queue"] if q["status"] == "open"]
        for q in opened[:-QUEUE_MAX]:   # keep the newest open ones
            q["status"] = "expired"

    def mark_untidy(self, cam, rec):
        g = self.g
        st = g["derive"]()
        spaces = {s["id"]: s for s in g["HOUSE"].get("tidy_spaces") or []}
        note = "The camera spotted mess here" + (": " + ", ".join(rec["hazards"]) if rec.get("hazards") else "")
        for sid in self.cfg["cameras"][cam].get("spaces") or []:
            s = spaces.get(sid) or {}
            jobs = s.get("chores") or []
            for j in jobs:
                if (st["board"].get(j) or {}).get("clean_until"):
                    g["append"]({"type": "reopen", "chore": j, "by": "camera", "note": note})
            if s and not jobs and (st.get("tidy_spaces", {}).get(sid) or {}).get("state") == "tidy":
                g["append"]({"type": "space_tidy", "space": sid, "tidy": False, "by": "camera"})
        g["after_change"]()

    # ---- labels from the grown-ups' clicks
    def cam_of_space(self, sid):
        return [c for c, v in self.cfg["cameras"].items() if sid in (v.get("spaces") or [])]

    def clicks(self, evs, reports):
        """new tidy/untidy evidence from the ledger -> pending[camera] = {label, ts}"""
        spaces = self.g["HOUSE"].get("tidy_spaces") or []
        by_job = {}
        for s in spaces:
            for j in s.get("chores") or []:
                by_job.setdefault(j, []).append(s["id"])
        last = self.S["seen_ev"]
        for e in evs:
            if e["ts"] <= last:
                continue
            self.S["seen_ev"] = max(self.S["seen_ev"], e["ts"])
            self.shot_events(e, by_job)
            label, sids = None, []
            if e["type"] == "space_tidy" and e.get("by") not in ADULT_BY_SKIP:
                label, sids = ("tidy" if e.get("tidy") else "untidy"), [e.get("space")]
            elif e["type"] in ("mark_clean", "reopen") and e.get("by") not in ADULT_BY_SKIP:
                label, sids = ("tidy" if e["type"] == "mark_clean" else "untidy"), by_job.get(e.get("chore"), [])
            elif e["type"] == "inspect" and e.get("ok"):
                r = reports.get(e.get("ref")) or {}
                if not r.get("partial"):
                    label = "tidy"
                    sids = [sp for j in (r.get("covers") or [r.get("chore")]) for sp in by_job.get(j, [])] + list(r.get("spaces") or [])
            for sid in sids:
                for cam in self.cam_of_space(sid):
                    self.S["pending"][cam] = {"label": label, "ts": e["ts"], "space": sid}

    def map_label(self, cam, st):
        """the map's word for the camera's rooms: "tidy" (all tidy), "untidy" (any untidy) or None (unknown/busy)"""
        rows = [(st.get("tidy_spaces") or {}).get(sid) or {} for sid in self.cfg["cameras"][cam].get("spaces") or []]
        states = [r.get("state") for r in rows]
        if not states or any(x in (None, "unknown", "doing", "waiting", "paused") for x in states):
            return None
        return "untidy" if "untidy" in states else "tidy"

    def bootstrap(self, cam, st):
        """Trust the map while a camera is new: until it has BOOT pictures of a kind, the map's state labels it."""
        lab = self.map_label(cam, st)
        have = sum(1 for s in self.S["samples"] if s["camera"] == cam and s["label"] == lab)
        return lab if lab and have < BOOT else None

    # ---- robot watch
    def robots(self, cams_on, now):
        g = self.g
        for cam, cc in self.cfg["cameras"].items():
            ent = cc.get("motion")
            if not ent:
                continue
            m = g["ha_get"](ent, self.tok).get("state")
            rising = m == "on" and self.motion_prev.get(cam) != "on"
            self.motion_prev[cam] = m
            if not rising or not cams_on.get(cam) or now - self.flashed_at.get(cam, 0) < TEACH_QUIET_S:
                continue
            for vac, room in (cc.get("robots") or {}).items():
                vs = g["ha_get"](vac, self.tok).get("state")
                if vs in ("cleaning", "returning", "paused", "idle", "error"):
                    g.setdefault("LOCATOR_TEACH", []).append({"vacuum": vac, "room": room, "at": now, "camera": cam})
                    g["ilog"]({"src": "room_eye", "action": "robot_seen", "camera": cam, "vacuum": vac, "room": room})

    # ---- one pass
    def tick(self, now=None):
        now = now or time.time()
        g = self.g
        self.tok = g["ha_token"]()
        st = g["derive"]()
        if not self.S["seen_ev"]:
            self.S["seen_ev"] = time.time()   # first run: clicks from now on (the map's state stands in for the past)
        self.clicks(g["read_events"](), {r["id"]: r for r in st.get("history") or [] if r.get("id")})
        for cam, p in list(self.S["pending"].items()):
            if now - p["ts"] > LABEL_TTL_S:
                del self.S["pending"][cam]
        if self.S["shots"]:
            self.do_shots(now)
        on = self.cameras_on() if self.cfg["cameras"] else {}
        self.robots(on, now)
        for cam in self.cfg["cameras"]:
            if not on.get(cam):
                continue
            p = self.S["pending"].get(cam)
            if p:
                jpeg, people = self.shoot(cam, "tidy_label")
                if jpeg and people <= 0:
                    self.keep(cam, jpeg, p["label"], "click")
                    del self.S["pending"][cam]
                    self.S["last_check"][cam] = now
                continue
            if now - self.S["last_check"].get(cam, 0) >= float(self.cfg.get("every_h", 4)) * 3600:
                boot = self.bootstrap(cam, st)
                jpeg, people = self.shoot(cam, "tidy_label" if boot else "tidy_check")
                self.S["last_check"][cam] = now
                if jpeg and people <= 0:
                    if boot:
                        self.keep(cam, jpeg, boot, "map")
                    else:
                        self.check(cam, jpeg, now)
        self.save()

    # ---- the grown-ups' label queue
    def open_queue(self):
        return [{k: q[k] for k in ("id", "camera", "ts", "box")} for q in self.S["queue"] if q["status"] == "open"]

    def label(self, qid, name, hazard, by):
        q = next((x for x in self.S["queue"] if x["id"] == qid), None)
        if not q:
            return False
        q.update(status="labelled", name=(str(name or "").strip()[:40] or None), hazard=bool(hazard), by=by, labelled_at=time.time())
        self.save()
        self.g["ilog"]({"src": "room_eye", "action": "label", "id": qid, "name": q["name"], "hazard": q["hazard"], "by": by})
        return True


EYE = None


def run(g):
    global EYE
    cfg = g["HOUSE"].get("room_eye") or {}
    EYE = Eye(g, cfg)
    while True:
        try:
            EYE.tick()
        except Exception as ex:  # noqa: BLE001
            print("room eye error", repr(ex), flush=True)
            g["ilog"]({"src": "room_eye", "action": "error", "error": repr(ex)})
        time.sleep(30)
