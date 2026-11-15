"""Chore Time regression tests: each case comes from something that went wrong in real family use.

Runs the real server module against a throwaway data dir, with Home Assistant / Grocy / notifications faked.
    python3 -m unittest discover -s tests -v        (from the repo root)
Nothing here talks to the network or the live ledger.
"""
import importlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def iso(t):
    return datetime.fromtimestamp(t).astimezone().isoformat()


def base_config():
    """A small, fixed house: the shapes the live config uses, none of the live data."""
    return {
        "bedtime": "off",   # tests drive bedtime with the parent button, never the wall clock
        "kids": [{"id": "ava", "name": "Ava", "icon": "1f430", "color": "#d0103a"},
                 {"id": "max", "name": "Max", "icon": "1f428", "color": "#1e88e5", "family_color": "#8e24aa"},
                 {"id": "zoe", "name": "Zoe", "icon": "1f984", "color": "#43a047"}],
        "inspectors": [{"id": "jordan", "name": "Jordan", "icon": "2708", "color": "#00e676"},
                       {"id": "casey", "name": "Casey", "icon": "1f436", "color": "#e91e63"}],
        "rewards": [{"id": "show", "name": "TV show", "icon": "1f4fa", "cost": 5, "tv_minutes": 30},
                    {"id": "movie", "name": "Movie", "icon": "1f3ac", "cost": 15, "tv_minutes": 100},
                    {"id": "games", "name": "Video games", "icon": "1f3ae", "cost": 10, "tv_minutes": 60},
                    {"id": "vr", "name": "VR", "icon": "1f97d", "cost": 2, "minutes": 5, "break_min": 30}],
        "chores": [
            {"id": "kitchen", "name": "Kitchen", "icon": "1f373", "points": 4, "room": True, "cooldown_h": 72},
            {"id": "family_room", "name": "Sun room", "icon": "1f6cb", "points": 3, "room": True, "cooldown_h": 72},
            {"id": "living_room", "name": "Living room", "icon": "1f6cb", "points": 3, "room": True, "cooldown_h": 72},
            {"id": "front_hall", "name": "Front hall", "icon": "1f6aa", "points": 2, "room": True, "cooldown_h": 72},
            {"id": "down_bath", "name": "Bathroom", "icon": "1f6bd", "points": 2, "room": True, "cooldown_h": 72},
            {"id": "whole_downstairs", "name": "Whole downstairs", "icon": "2b07", "points": None,
             "bundle": ["family_room", "kitchen", "down_bath", "living_room", "front_hall"], "boost": {"pct": 25, "min": 3}},
            {"id": "organize", "name": "Organizing", "icon": "1f4e6", "points": None, "cooldown_h": 0, "shared": True, "propose": True},
            {"id": "other", "name": "Something else", "icon": "2728", "points": None, "shared": True},
            {"id": "car", "name": "Clean out the car", "icon": "1f697", "points": 4, "room": True, "cooldown_h": 168, "daylight": True},
            {"id": "book_pick", "name": "Pick a book", "icon": "1f4da", "points": 2, "cooldown_h": 0, "only": ["ava"], "once": True},
            {"id": "brush_teeth", "name": "Brush teeth", "icon": "1faa5", "points": 1, "cooldown_h": 0, "shared": True,
             "each_daily": True, "after": "00:00", "bedtime_ok": True, "desc_for": {"ava": "Brush your teeth and clean your earring holes."}},
            {"id": "shower", "name": "Shower", "icon": "1f6bf", "points": 2, "cooldown_h": 0, "shared": True,
             "each_daily": True, "each_days": 2, "after": "00:00", "before": "23:59"},
            {"id": "laundry", "name": "Put away laundry", "icon": "1f455", "points": 3, "room": True, "cooldown_h": 72, "on_demand": True},
            {"id": "piano", "name": "Practice piano", "icon": "1f4d6", "points": 3, "cooldown_h": 0,
             "slot": {"dow": 6, "time": "11:00"}, "price_dates": {}},
        ],
    }


class App:
    """One isolated Chore Time: temp DATA dir, fake HA states, HTTP server on a random port."""

    def __init__(self, config=None):
        self.dir = Path(tempfile.mkdtemp(prefix="chore-test-"))
        (self.dir / "chores.json").write_text(json.dumps(config or base_config()))
        house = json.loads((REPO / "example-config" / "house.json").read_text())
        house["home_assistant"]["url"] = "http://127.0.0.1:9"   # never reachable: every HA call is faked below
        (self.dir / "house.json").write_text(json.dumps(house))
        os.environ["CHORE_DATA"] = str(self.dir)
        os.environ.pop("CHORE_HOUSE", None)
        sys.path.insert(0, str(REPO))
        sys.modules.pop("server", None)
        self.s = importlib.import_module("server")
        self.states, self.calls = {}, []
        s = self.s
        s.ha_get = lambda e, tok: self.states.get(e, {"state": "unavailable", "attributes": {}})
        s.ha_call = lambda svc, data, tok: self.calls.append((svc, data))
        s.ha_post_state = lambda *a, **k: None
        self.orig = {n: getattr(s, n) for n in ("notify_report", "notify_inspecting") if hasattr(s, n)}
        for n in ("announce", "notify_report", "notify_inspecting", "notify_started", "send_adult_request",
                  "clear_adult_request", "push_state_sensor", "push_ha", "after_change"):
            if hasattr(s, n):
                setattr(s, n, lambda *a, **k: None)
        s.gcall = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no Grocy in tests"))
        s.HA_TOKEN_FILE = self.dir / "token"
        s.HA_TOKEN_FILE.write_text("test")
        s.H.log_message = lambda *a, **k: None
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), s.H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def close(self):
        self.httpd.shutdown()
        shutil.rmtree(self.dir, ignore_errors=True)

    def set(self, ent, state, ago=0, **attrs):
        self.states[ent] = {"state": state, "attributes": attrs, "last_changed": iso(time.time() - ago)}

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            r = urllib.request.urlopen(req, timeout=10)
            return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def state(self):
        return json.loads(urllib.request.urlopen(self.base + "/api/state", timeout=10).read())

    def event(self, **ev):
        return self.s.append(ev)

    def pass_report(self, kid, chore, points=3, **extra):
        code, r = self.post("/api/report", {"kid": kid, "chore": chore, "feeling": "ok", **extra})
        assert code == 200, (code, r)
        code, r2 = self.post("/api/inspect", {"ref": r["event"]["id"], "by": "jordan", "ok": True, "points": points})
        assert code == 200, (code, r2)
        return r["event"]


class Case(unittest.TestCase):
    config = None

    def setUp(self):
        self.app = App(self.config)
        self.s = self.app.s

    def tearDown(self):
        self.app.close()


# ---------------------------------------------------------------- who may do what
class SharedVsExclusive(Case):
    def test_paused_organizing_does_not_block_another_kid(self):
        # Jordan: "Ava organizing ... aren't exclusive and don't need releasing for someone else"
        self.assertEqual(self.app.post("/api/start", {"kid": "ava", "chore": "organize"})[0], 200)
        self.assertEqual(self.app.post("/api/pause", {"kid": "ava"})[0], 200)
        self.assertEqual(self.app.post("/api/start", {"kid": "max", "chore": "organize"})[0], 200)

    def test_something_else_pending_for_one_kid_stays_open_for_others(self):
        # Ava couldn't find "Something else" while Max's juggling show waited for inspection
        self.assertEqual(self.app.post("/api/report", {"kid": "max", "chore": "other", "note": "JUGGLING SHOW"})[0], 200)
        st = self.app.state()
        self.assertTrue(next(c for c in st["config"]["chores"] if c["id"] == "other").get("shared"))
        self.assertEqual(self.app.post("/api/start", {"kid": "ava", "chore": "other"})[0], 200)
        self.assertEqual(self.app.post("/api/report", {"kid": "ava", "chore": "other", "note": "SOCKS"})[0], 200)

    def test_a_room_is_exclusive(self):
        self.assertEqual(self.app.post("/api/start", {"kid": "max", "chore": "kitchen"})[0], 200)
        code, r = self.app.post("/api/start", {"kid": "ava", "chore": "kitchen"})
        self.assertEqual(code, 409, r)


# ---------------------------------------------------------------- prices
class Prices(Case):
    def test_whole_downstairs_boost_rounds_half_up_at_three_rooms(self):
        # Max's idea: +25% at 3+ rooms, "rounded to the nearest point"
        for room in ("family_room", "down_bath"):   # leave kitchen 4 + living_room 3 + front_hall 2 = 9 -> 11.25 -> 11
            self.app.pass_report("zoe", room)
        b = self.app.state()["board"]["whole_downstairs"]
        self.assertEqual(sorted(b["covers"]), ["front_hall", "kitchen", "living_room"])
        self.assertEqual((b["boost_from"], b["price"], b["boost_pct"]), (9, 11, 25))
        self.app.pass_report("zoe", "front_hall")  # 4 + 3 = 7 over two rooms: no boost
        b = self.app.state()["board"]["whole_downstairs"]
        self.assertEqual(b["price"], 7)
        self.assertIsNone(b.get("boost_pct"))

    def test_half_rounds_up_not_to_even(self):
        # Python round(12.5) == 12; Jordan asked for nearest point, kids expect 13
        cfg = json.loads((self.app.dir / "chores.json").read_text())
        for c in cfg["chores"]:
            if c["id"] == "front_hall":
                c["points"] = 3          # 4 + 3 + 3 = 10 -> 12.5 -> 13
        (self.app.dir / "chores.json").write_text(json.dumps(cfg))
        for room in ("family_room", "down_bath"):
            self.app.pass_report("zoe", room)
        self.assertEqual(self.app.state()["board"]["whole_downstairs"]["price"], 13)

    def test_price_for_one_date(self):
        # a job worth more on one special day: 3 normally, 8 today
        cfg = json.loads((self.app.dir / "chores.json").read_text())
        next(c for c in cfg["chores"] if c["id"] == "piano")["price_dates"] = {date.today().isoformat(): 8}
        (self.app.dir / "chores.json").write_text(json.dumps(cfg))
        b = self.app.state()["board"]["piano"]
        self.assertEqual((b["price"], b["bonus"]), (8, True))


# ---------------------------------------------------------------- availability
class Availability(Case):
    def closed(self, cid):
        return next(c for c in self.app.state()["config"]["chores"] if c["id"] == cid).get("closed")

    def test_car_only_in_daylight(self):
        self.s.GUARD["sun_up"] = False
        self.assertEqual(self.closed("car"), "dark")
        self.assertEqual(self.app.post("/api/start", {"kid": "max", "chore": "car"})[0], 409)
        self.s.GUARD["sun_up"] = True
        self.assertIsNone(self.closed("car"))

    def test_on_demand_job_only_after_a_grownup_opens_it(self):
        # laundry: there's only laundry when the machines ran; a grown-up opens it from the parents page
        self.assertEqual(self.closed("laundry"), "on_demand")
        self.assertEqual(self.app.post("/api/start", {"kid": "max", "chore": "laundry"})[0], 409)
        self.assertEqual(self.app.post("/api/reopen", {"chore": "laundry", "by": "jordan"})[0], 200)
        self.assertIsNone(self.closed("laundry"))
        self.app.pass_report("max", "laundry", points=3)
        self.assertEqual(self.closed("laundry"), "on_demand")                       # done: gone until next time

    def test_one_time_chore_closes_after_it_passes(self):
        self.assertIsNone(self.closed("book_pick"))
        self.app.pass_report("ava", "book_pick", points=2)
        self.assertEqual(self.closed("book_pick"), "done")

    def test_lesson_slot_one_kid_per_week(self):
        a = self.app
        self.assertEqual(a.post("/api/claim", {"kid": "ava", "chore": "piano"})[0], 200)
        code, r = a.post("/api/claim", {"kid": "max", "chore": "piano"})
        self.assertEqual(code, 409)
        self.assertIn("ava", r["error"])
        self.assertEqual(a.post("/api/start", {"kid": "ava", "chore": "piano"})[0], 409)   # claimed, never "started"
        b = a.state()["board"]["piano"]
        if time.time() < b["slot"] - 1800:   # not service time yet: no reporting
            self.assertEqual(a.post("/api/report", {"kid": "ava", "chore": "piano"})[0], 409)
        self.assertEqual(a.post("/api/claim", {"kid": "ava", "chore": "piano", "release": True})[0], 200)
        self.assertEqual(a.post("/api/claim", {"kid": "max", "chore": "piano"})[0], 200)


# ---------------------------------------------------------------- evening routine
class Bedtime(Case):
    def bed(self, on=True):
        self.assertEqual(self.app.post("/api/bedtime", {"by": "jordan", "on": on})[0], 200)

    def test_brush_teeth_once_a_day_per_kid(self):
        a = self.app
        self.assertEqual(a.post("/api/report", {"kid": "max", "chore": "brush_teeth"})[0], 200)
        self.assertEqual(a.post("/api/report", {"kid": "ava", "chore": "brush_teeth"})[0], 200)   # her own teeth
        self.assertEqual(a.post("/api/report", {"kid": "max", "chore": "brush_teeth"})[0], 409)   # not twice today
        self.assertEqual(sorted(a.state()["board"]["brush_teeth"]["done_today"]), ["ava", "max"])

    def test_shower_every_other_night_per_kid(self):
        a, s = self.app, self.s
        a.event(type="report", kid="max", chore="shower", feeling="ok", points=2, ts=time.time() - 86400)   # last night
        st = a.state()
        self.assertIn("max", st["board"]["shower"]["done_today"])
        self.assertEqual(a.post("/api/report", {"kid": "max", "chore": "shower"})[0], 409)
        self.assertEqual(a.post("/api/report", {"kid": "zoe", "chore": "shower"})[0], 200)

    def test_shower_not_at_bedtime(self):
        self.bed()
        self.assertEqual(self.app.post("/api/report", {"kid": "zoe", "chore": "shower"})[1].get("error"), "bedtime")

    def test_brush_teeth_not_before_its_time(self):
        cfg = json.loads((self.app.dir / "chores.json").read_text())
        next(c for c in cfg["chores"] if c["id"] == "brush_teeth")["after"] = "23:59"
        (self.app.dir / "chores.json").write_text(json.dumps(cfg))
        if datetime.now().strftime("%H:%M") < "23:59":
            self.assertEqual(self.app.post("/api/report", {"kid": "zoe", "chore": "brush_teeth"})[0], 409)

    def test_bedtime_allows_only_tooth_brushing(self):
        a = self.app
        a.event(type="adjust", kid="max", points=50, by="jordan", reason="test")
        self.bed()
        self.assertTrue(a.state()["bedtime"]["on"])
        self.assertEqual(a.post("/api/start", {"kid": "max", "chore": "kitchen"})[1].get("error"), "bedtime")
        self.assertEqual(a.post("/api/spend", {"kid": "max", "reward": "show", "tv": "livingroom"})[1].get("error"), "bedtime")
        self.assertEqual(a.post("/api/report", {"kid": "max", "chore": "brush_teeth"})[0], 200)
        self.bed(False)
        self.assertEqual(a.post("/api/start", {"kid": "max", "chore": "kitchen"})[0], 200)

    def test_jobs_in_progress_go_on_hold(self):
        a = self.app
        self.assertEqual(a.post("/api/start", {"kid": "zoe", "chore": "kitchen"})[0], 200)
        self.bed()
        self.assertTrue(self.s.bedtime_hold(self.s.derive()))
        st = a.state()
        self.assertNotIn("zoe", st["active"])
        self.assertEqual(st["paused"]["zoe"]["chore"], "kitchen")
        self.assertEqual(a.post("/api/resume", {"kid": "zoe"})[1].get("error"), "bedtime")   # from 6 AM

    def test_brushing_teeth_is_never_put_on_hold(self):
        # 8:32 PM: Max was mid-brush when bedtime started and got paused
        a = self.app
        self.assertEqual(a.post("/api/start", {"kid": "max", "chore": "brush_teeth"})[0], 200)
        self.bed()
        self.assertFalse(self.s.bedtime_hold(self.s.derive()))
        self.assertEqual(a.state()["active"]["max"]["chore"], "brush_teeth")


# ---------------------------------------------------------------- TV clocks
class TvClocks(Case):
    def spend(self, kid, reward, tv="livingroom"):
        a = self.app
        a.event(type="adjust", kid=kid, points=50, by="jordan", reason="test")
        code, r = a.post("/api/spend", {"kid": kid, "reward": reward, "tv": tv})
        self.assertEqual(code, 200, r)
        return r["event"]

    def grant(self, tv="livingroom"):
        return self.app.state()["tv"]["tvs"][tv]["grants"]

    def test_purchase_waits_unstarted_then_backdated_start_counts(self):
        # Ava's Netflix: clock missed while chore time was off, backdated to when playback began
        ev = self.spend("ava", "show")
        g = self.grant()[0]
        self.assertTrue(g["armed"])
        began = time.time() - 600
        self.app.event(type="tv_started", ref=ev["id"], tv="livingroom", what="stream", at=began)
        g = self.grant()[0]
        self.assertFalse(g["armed"])
        self.assertAlmostEqual(g["until"], began + 30 * 60, delta=2)

    def test_queued_purchases_lock_back_to_back(self):
        e1 = self.spend("max", "show")
        self.app.event(type="tv_started", ref=e1["id"], tv="livingroom", what="stream")
        self.spend("zoe", "show")
        tv = self.app.state()["tv"]["tvs"]["livingroom"]
        running = [g for g in tv["grants"] if not g["armed"]][0]
        self.assertAlmostEqual(tv["locks_at"], running["until"] + 30 * 60, delta=2)

    def test_games_break_comes_after_the_games(self):
        # "screen break until 8:54" showed before Max had played at all
        ev = self.spend("max", "games")
        started = time.time() - 60
        self.app.event(type="tv_started", ref=ev["id"], tv="livingroom", what="hdmi", at=started)
        brk = self.app.state()["screen_break"]["max"]
        self.assertAlmostEqual(brk, started + 60 * 60 + 3600, delta=2)


class SonyInputs(Case):
    """tv_status on the Sun room Sony: the TV's own input report wins (Max's console never locked, 8 PM)."""

    def setUp(self):
        super().setUp()
        t = self.s.TVS["livingroom"]
        self.t = t
        a = self.app
        a.set(t["television"], "on", source="HDMI 1")
        for p in t["players"]:
            a.set(p, "idle")

    def act(self, activity):
        self.app.set(self.t["remote"], "on", current_activity=activity)
        return self.s.tv_status(self.t, "x")[0]

    def test_hdmi_beats_a_lock_page_still_loaded_on_the_chromecast(self):
        self.app.set(self.t["players"][0], "idle", app_name="DashCast", app_id=self.s.DASHCAST_APP_ID)
        self.s.GUARD["seen"][self.t["ip"]] = (time.time(), "")
        self.assertEqual(self.act("com.sony.dtv.tvx"), "hdmi")

    def test_input_picker_is_idle_not_streaming(self):
        # Max got the lock screen while switching inputs to his console
        self.assertEqual(self.act("com.android.systemui"), "idle")

    def test_our_page_on_screen_is_the_lock(self):
        self.s.GUARD["seen"][self.t["ip"]] = (time.time(), "")
        self.assertEqual(self.act("com.google.android.apps.mediashell"), "lock")

    def test_netflix_is_streaming(self):
        self.assertEqual(self.act("com.netflix.ninja"), "stream")


class FinishTheShow(Case):
    def test_netflix_reports_no_end_so_no_finishing(self):
        t = self.s.TVS["livingroom"]
        self.app.set(t["players"][0], "playing", app_name="Netflix")
        self.assertIsNone(self.s.playing_item(t, "x"))

    def test_plex_reports_its_end(self):
        t = self.s.TVS["livingroom"]
        self.app.set(t["players"][0], "playing", media_title="Bluey", media_duration=420, media_position=120,
                     media_position_updated_at=iso(time.time()))
        it = self.s.playing_item(t, "x")
        self.assertAlmostEqual(it["left"], 300, delta=3)
        self.assertEqual(self.s.GRACE_EXTRA_S, 300)          # Jordan: predicted end + 5 min
        self.assertEqual(self.s.FINISH_CAP_S, 3600)          # an hour into one title = a movie


# ---------------------------------------------------------------- grown-up login on a Hub
class AdultLogin(Case):
    def test_approval_survives_a_page_reload(self):
        # the Hub reloaded for an update between "check your watch" and "It's me"
        code, r = self.app.post("/api/adult_request", {"who": "jordan"})
        self.s.ADULT_REQ[r["nonce"]]["status"] = "ok"
        self.assertEqual(self.app.state().get("adult_go"), "jordan")
        self.assertIsNone(self.app.state().get("adult_go"))           # delivered once

    def test_lock_confirmation_never_opens_the_parent_screen(self):
        code, r = self.app.post("/api/adult_request", {"who": "jordan", "purpose": "lock"})
        self.s.ADULT_REQ[r["nonce"]]["status"] = "ok"
        self.assertIsNone(self.app.state().get("adult_go"))


# ---------------------------------------------------------------- watch buttons
class WatchButtons(Case):
    def test_something_else_has_a_one_tap_zero(self):
        # kids spam "something else"; one tap on the watch settles it at 0
        sent = []
        self.s.ben_home = lambda tok: True
        self.s.ha_call = lambda svc, d, tok: sent.append(d)
        code, r = self.app.post("/api/report", {"kid": "max", "chore": "other", "note": "SPAM"})
        self.app.orig["notify_report"](r["event"], self.s.derive(), force=True)
        acts = [a["action"].split("_")[1] for a in sent[-1]["data"]["actions"]]
        self.assertEqual(acts[0], "ZERO")


# ---------------------------------------------------------------- Grocy sync
class GrocySync(Case):
    def test_pull_never_deadlocks(self):
        # ilog() (takes LOCK) inside `with LOCK` hung the whole app; Max's input wasn't accepted
        rows = [{"id": 7, "name": "Toy table", "period_type": "dynamic-regular", "period_days": 3, "active": 1,
                 "description": None, "userfields": {"kids": "1", "kid_points": "2", "kid_icon": "1f9f8", "kid_key": "toy_table",
                                                     "kid_extra": json.dumps({"shared": False, "order": 3})}}]
        self.s.gcall = lambda m, p, b=None: rows if m == "GET" else {}
        done = threading.Event()
        threading.Thread(target=lambda: (self.s.grocy_chores_pull(), done.set()), daemon=True).start()
        self.assertTrue(done.wait(5), "grocy_chores_pull hung (lock re-entry?)")
        self.assertTrue(self.s.LOCK.acquire(timeout=1))
        self.s.LOCK.release()
        ids = [c["id"] for c in self.s.cfg()["chores"]]
        self.assertIn("toy_table", ids)
        self.assertIn("whole_downstairs", ids)   # bundles and system chores stay local
        self.assertIn("other", ids)


# ---------------------------------------------------------------- lights
class SignalLights(Case):
    def test_maxs_family_purple(self):
        sent = {}
        s = self.s
        s._apply_signal_bulbs.__globals__["ha_call"] = lambda svc, d, tok: sent.__setitem__(d["entity_id"], d.get("rgb_color"))
        for b in s.SIGNAL_BULBS["living_room"]["bulbs"] + s.SIGNAL_BULBS["living_room"]["pool"]:
            self.app.set(b, "on", brightness=255, rgb_color=[255, 255, 255])
        s.apply_signal_bulbs({"living_room": {"kids": ["max"], "colors": ["#1e88e5"]}}, "x")
        purple = [b for b, rgb in sent.items() if rgb == [142, 36, 170]]
        blue = [b for b, rgb in sent.items() if rgb == [30, 136, 229]]
        self.assertEqual(sorted(purple), sorted(s.SIGNAL_BULBS["living_room"]["bulbs"]))   # the sconce pair
        self.assertEqual(len(blue), s.SIGNAL_BULBS["living_room"]["pick"])

    def test_inspector_is_who_is_home_else_the_lead_grownup(self):
        s, a = self.s, self.app
        for lead, other, want in (("home", "home", "jordan"), ("not_home", "home", "casey"), ("not_home", "not_home", "jordan"), ("home", "not_home", "jordan")):
            s.GUARD.pop("insp_cache", None)
            a.set("person.jordan", lead)
            a.set("person.casey", other)
            self.assertEqual(s.default_inspector("x"), want, (lead, other))


# ---------------------------------------------------------------- vacuum autopilot
class Vacuum(unittest.TestCase):
    """The downstairs autopilot state machine against a fake house (decisions Q1-Q8)."""

    def setUp(self):
        sys.path.insert(0, str(REPO))
        import vacuum_auto as va
        self.va = va
        house = json.loads((REPO / "example-config" / "house.json").read_text())
        va.configure(house["vacuum"])
        adults = house["adults"]
        self.now = [datetime(2026, 11, 15, 10, 0).timestamp()]
        va.time.time = lambda: self.now[0]
        self.H, self.calls = {}, []
        for e in va.SENSORS:
            self.S(e, "off", 3000)
        self.S(va.VAC, "docked", fan_speed="max")
        for e, v in ((va.ROOM_SENSOR, "Hall"), (va.IN_CLEAN, "off"), (va.BATTERY, "100"), (va.ERROR, "none"),
                     (va.MODE_SEL, "vac_and_mop"), (va.ENABLE, "on"), ("person.jordan", "home"),
                     ("binary_sensor.upstairs_motion_last_30_minutes", "on")):
            self.S(e, v)
        self.board = {"kitchen": {"clean_until": 1, "last_clean": self.now[0] - 3600},
                      "family_room": {"clean_until": 1, "last_clean": self.now[0] - 3600},
                      "living_room": {"clean_until": 0, "last_clean": 0},
                      "front_hall": {"clean_until": 0, "last_clean": 0}}
        self.tmp = Path(tempfile.mkdtemp())
        g = {"DATA": self.tmp, "ilog": lambda r: None, "ha_get": lambda e, t: self.H.get(e, {"state": "unavailable", "attributes": {}}),
             "ha_call": lambda svc, d, t: self.calls.append((svc, d)), "ha_post_state": lambda *a: None,
             "derive": lambda: {"board": self.board, "active": {}, "paused": {}}, "TVS": {}, "tv_status": lambda t, k: ("off", 0),
             "ADULTS": adults, "HOUSE": house, "ha_token": lambda: "x",
             "adult_home": lambda who, tok: self.H.get(adults[who]["presence"], {}).get("state") == adults[who].get("home_state", "home")}
        p = va.Pilot.__new__(va.Pilot)
        p.g, p.path, p.tok, p.last_pub = g, self.tmp / "v.json", "x", None
        p.D = {"visits": {"Kitchen": self.now[0] - 7200, "Family Room": self.now[0] - 7200}, "phase": "idle", "runs": []}
        self.p = p

    def tearDown(self):
        importlib.reload(self.va.time)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def S(self, e, st, ago=0, **attrs):
        self.H[e] = {"state": st, "attributes": attrs, "last_changed": iso(self.now[0] - ago)}

    def tick(self, dt=10):
        self.now[0] += dt
        self.calls.clear()
        self.p.tick()
        return [c[0] for c in self.calls]

    def at(self, h, m=0, day=15):
        self.now[0] = datetime(2026, 11, day, h, m).timestamp()

    def status(self):
        return self.p.segment_status()

    def test_every_room_at_least_every_three_days(self):
        self.p.D["visits"].update({"Living Room": self.now[0] - 4 * 86400, "Family Room": self.now[0] - 86400})
        self.board["family_room"]["last_clean"] = self.now[0] - 2 * 86400   # tidied before the robot came
        st = self.status()
        self.assertTrue(st["Living Room"]["eligible"])                 # 4 days
        self.assertFalse(st["Family Room"]["eligible"])                # 1 day, no fresh tidy
        self.assertTrue(st["Hall"]["eligible"])                        # never cleaned: due (no kids' job needed)

    def test_kitchen_every_night_after_seven(self):
        self.p.D["visits"]["Kitchen"] = datetime(2026, 11, 15, 9, 0).timestamp()
        self.board["kitchen"]["last_clean"] = 0                        # no tidy trigger
        self.at(14)
        self.assertFalse(self.status()["Kitchen"]["eligible"])         # daytime: not yet
        self.at(21)
        self.assertTrue(self.status()["Kitchen"]["eligible"])          # tonight
        self.p.D["visits"]["Kitchen"] = datetime(2026, 11, 15, 21, 30).timestamp()
        self.at(23)
        self.assertFalse(self.status()["Kitchen"]["eligible"])         # done tonight (a manual button run counts too)

    def test_dinner_is_one_long_sit_after_five_not_walk_throughs(self):
        # calibrated on real evenings: sitting at the table = one long stretch; passing through = short blips
        va, d = self.va, datetime(2026, 11, 12)
        T = lambda h, m: d.replace(hour=h, minute=m).timestamp()
        rows = lambda *iv: [r for a, b in iv for r in ({"state": "on", "last_changed": iso(a)}, {"state": "off", "last_changed": iso(b)})]
        args = (T(15, 0), T(21, 30), T(17, 0), T(20, 30), 180, 45 * 60)
        self.assertTrue(va.dinner_from_rows(rows((T(16, 36), T(18, 10))), *args))                     # 70 min after 5
        self.assertFalse(va.dinner_from_rows(rows((T(16, 1), T(16, 51))), *args))                     # too early
        self.assertFalse(va.dinner_from_rows(rows(*[(T(17, m), T(17, m + 4)) for m in range(0, 60, 10)]), *args))  # blips

    def test_a_detected_dinner_makes_the_table_dirty_once(self):
        va, appended = self.va, []
        va.DINNER = dict(va.DINNER, reopens=["dining_table"], **{"from": "17:00", "min_block_min": 45})
        self.p.g["append"] = appended.append
        self.board["dining_table"] = {"clean_until": 1, "last_clean": 1}
        self.p.dinner_eaten = lambda ref: True
        self.at(17, 30)
        self.p.dinner_reopens()
        self.assertEqual(appended, [])                                   # a 45-min dinner can't be over yet
        self.at(19, 0)
        self.p.dinner_reopens()
        self.p.dinner_reopens()
        self.assertEqual([e["chore"] for e in appended], ["dining_table"])   # once per evening

    def test_a_passed_tidy_still_triggers_a_clean(self):
        self.p.D["visits"]["Family Room"] = self.now[0] - 3600
        self.board["family_room"] = {"clean_until": 1, "last_clean": self.now[0] - 60}
        self.assertTrue(self.status()["Family Room"]["eligible"])

    def test_a_job_in_progress_blocks_its_room(self):
        self.board["living_room"]["pending_by"] = ["max"]
        self.assertFalse(self.status()["Living Room"]["eligible"])

    def test_waits_for_twenty_quiet_minutes(self):
        self.S("binary_sensor.kitchen_motion", "off", 600)
        self.assertNotIn("script.vacuum_clean_rooms", self.tick())
        self.S("binary_sensor.kitchen_motion", "off", 1300)
        self.assertIn("script.vacuum_clean_rooms", self.tick())

    def test_vacuum_rooms_before_mop_rooms_and_suction_by_who_is_home(self):
        self.tick()
        g = self.p.D["groups"]
        self.assertEqual([x["mode"] for x in g], ["vacuum", "vac_and_mop"])        # vacuum-only rooms first, then mop rooms
        self.assertTrue(set(g[1]["rooms"]) <= self.va.MOP and not set(g[0]["rooms"]) & self.va.MOP)
        self.assertEqual(self.p.D["fan"], "turbo")                     # people upstairs, daytime
        self.now[0] = datetime(2026, 11, 15, 21, 0).timestamp()
        self.assertEqual(self.p.tier(), "quiet")                      # 8 PM - 7 AM
        self.now[0] = datetime(2026, 11, 15, 13, 0).timestamp()
        self.S("person.jordan", "not_home")
        self.S("binary_sensor.upstairs_motion_last_30_minutes", "off")
        self.assertEqual(self.p.tier(), "max_plus")                   # nobody home

    def run_into_living_room(self):
        self.tick()
        self.S(self.va.VAC, "cleaning", fan_speed="turbo")
        self.S(self.va.IN_CLEAN, "on")
        self.S(self.va.ROOM_SENSOR, "Living Room")
        self.tick()

    def test_robot_tripping_its_own_room_does_not_pause(self):
        self.run_into_living_room()
        self.S("binary_sensor.living_room_motion", "on")
        self.assertNotIn("vacuum.pause", self.tick())

    def test_stairs_alone_is_someone_upstairs_not_a_person_coming_down(self):
        # the stairs sensor fired with the upstairs hall's: someone on the landing, nobody came down
        self.run_into_living_room()
        self.S("binary_sensor.stairs_motion", "on")
        self.assertNotIn("vacuum.pause", self.tick())

    def test_person_pauses_and_it_resumes_once_every_sensor_is_off(self):
        self.run_into_living_room()
        self.S("binary_sensor.stairs_motion", "on")
        self.tick()
        self.S("binary_sensor.kitchen_motion", "on")      # ...and then shows up downstairs
        self.assertIn("vacuum.pause", self.tick())
        self.S("binary_sensor.kitchen_motion", "off", 0)
        self.S(self.va.VAC, "paused")
        self.S("binary_sensor.stairs_motion", "off", 0)
        self.assertNotIn("vacuum.send_command", self.tick(10))   # sensors only just went off
        self.assertIn("vacuum.send_command", self.tick(15))      # all off (= ~4 min without movement) + debounce

    def test_a_lone_trip_elsewhere_is_the_robot_not_a_person(self):
        # the robot sets off random sensors one at a time; that must not pause it
        self.run_into_living_room()
        self.S("binary_sensor.kitchen_motion", "on")
        self.assertNotIn("vacuum.pause", self.tick())

    def test_walking_through_two_rooms_is_a_person(self):
        self.run_into_living_room()
        self.S("binary_sensor.kitchen_motion", "on")
        self.tick()
        self.S("binary_sensor.family_room_motion", "on")
        self.assertIn("vacuum.pause", self.tick())

    def test_a_door_opening_is_a_person(self):
        self.run_into_living_room()
        self.S("binary_sensor.front_door", "on")
        self.assertIn("vacuum.pause", self.tick())

    def test_person_who_stays_ten_minutes_sends_it_home(self):
        self.run_into_living_room()
        self.S("binary_sensor.front_door", "on")   # came in the front door
        self.tick()
        self.S(self.va.VAC, "paused")
        self.S("binary_sensor.entry_motion", "on")  # ...and stays in the entry hall
        self.assertIn("vacuum.return_to_base", self.tick(610))

    def test_paused_time_does_not_count_as_cleaning_a_room(self):
        self.run_into_living_room()
        self.S(self.va.VAC, "paused")
        for _ in range(60):
            self.tick(10)
        self.S(self.va.VAC, "returning")
        self.tick()
        self.assertNotIn("Living Room", self.p.D["visits"])


# ---------------------------------------------------------------- robot locator
class RobotLocator(unittest.TestCase):
    """HA's 'current room' freezes off-cleaning (it read 'Hall' while the robot wandered the family room, heading home)."""

    def setUp(self):
        sys.path.insert(0, str(REPO))
        import locator
        self.L = locator.Locator.__new__(locator.Locator)
        self.L.M, self.L.cfg = {}, {}

    def test_learns_while_cleaning_and_locates_later(self):
        L, v = self.L, "vacuum.robot"
        for x in range(20000, 23000, 100):
            L.learn(v, x, 25000, "Kitchen")
        for x in range(23000, 26000, 100):
            L.learn(v, x, 25000, "Family Room")
        self.assertEqual(L.classify(v, 21000, 25050)[0], "Kitchen")
        self.assertEqual(L.classify(v, 25500, 24900)[0], "Family Room")
        self.assertEqual(L.classify(v, 22990, 25600)[0], "Kitchen")             # a few cells off the learned path
        self.assertIsNone(L.classify(v, 40000, 40000)[0])                       # never been there: unknown, no guess

    def test_majority_wins_at_a_doorway(self):
        L, v = self.L, "vacuum.robot"
        for _ in range(5):
            L.learn(v, 24000, 25000, "Corridor")
        L.learn(v, 24010, 25010, "Kitchen")                                      # map refresh lag near a boundary
        self.assertEqual(L.classify(v, 24005, 25005)[0], "Corridor")


class HouseTasks(unittest.TestCase):
    """The grown-ups' side: Grocy chores/tasks/batteries with Home Assistant context, faked."""

    def setUp(self):
        sys.path.insert(0, str(REPO))
        import house_tasks
        self.H = house_tasks
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ha, self.calls, self.grocy = {}, [], {"/objects/chores": [], "/users": [], "/chores": [],
                                                  "/objects/tasks": [], "/objects/batteries": []}

        def gcall(method, path, body=None):
            self.calls.append((method, path))
            return self.grocy.get(path) if method == "GET" else {}
        self.g = {"HOUSE": {"grocy": {"review_category": "Inbox"}}, "ha_token": lambda: "t", "DATA": self.tmp, "gcall": gcall, "ilog": lambda e: None, "GUARD": {},
                  "ha_get": lambda e, tok: self.ha.get(e, {"state": "unavailable"}),
                  "ha_post_state": lambda ent, state, attrs, tok: self.ha.__setitem__(ent, {"state": state, "attributes": attrs})}

    def test_conditions(self):
        self.ha.update({"person.jordan": {"state": "home"}, "sensor.washer": {"state": "3.5"}})
        get = lambda e: self.ha.get(e, {"state": "unavailable"})
        self.assertTrue(self.H.check("person.jordan = home", get))
        self.assertTrue(self.H.check("person.jordan = Home and sensor.washer < 5", get))
        self.assertFalse(self.H.check("sensor.washer >= 10", get))
        self.assertIsNone(self.H.check("sensor.nothing = on", get))             # unknown: neither show nor auto-complete
        self.assertIsNone(self.H.check("person.jordan > home", get))            # no ordering on words

    def test_a_four_button_remote_reads_its_newest_battery_entity(self):
        """A quad remote showed 4 battery entities with different levels: the stale ones lag behind."""
        lvl = self.H.battery_level([{"state": "95", "last_reported": "2026-01-01T10:00:00+00:00"},
                                    {"state": "100", "last_reported": "2026-01-02T10:00:00+00:00"},
                                    {"state": "unknown"}])
        self.assertEqual(lvl, 100.0)
        self.assertIsNone(self.H.battery_level([{"state": "unavailable"}]))

    def test_low_battery_becomes_an_item_then_is_tracked_as_replaced(self):
        self.grocy["/objects/batteries"] = [{"id": 7, "name": "Hall sensor",
                                             "userfields": {"ha_entity": "sensor.a_battery,sensor.b_battery", "ha_low": 20}}]
        self.ha["sensor.a_battery"] = {"state": "12", "last_reported": "2026-01-02T10:00:00+00:00"}
        h = self.H.HouseTasks(self.g)
        h.tick()
        items = self.ha["sensor.house_tasks"]["attributes"]["items"]
        self.assertEqual([i["name"] for i in items], ["Replace the battery: Hall sensor"])
        self.ha["sensor.a_battery"] = {"state": "100", "last_reported": "2026-01-03T10:00:00+00:00"}
        h.tick()
        self.assertIn(("POST", "/batteries/7/charge"), self.calls)
        self.assertEqual(self.ha["sensor.house_tasks"]["state"], "0")

    def test_never_watched_with_a_negative_threshold(self):
        """Sensors on USB power read 0% forever: a negative threshold keeps them registered but quiet."""
        self.grocy["/objects/batteries"] = [{"id": 8, "name": "USB sensor", "userfields": {"ha_entity": "sensor.u", "ha_low": -1}}]
        self.ha["sensor.u"] = {"state": "0"}
        self.H.HouseTasks(self.g).tick()
        self.assertEqual(self.ha["sensor.house_tasks"]["state"], "0")

    def test_an_imported_inbox_is_counted_not_listed(self):
        """150 imported to-dos would bury the board: the review inbox is a number until they're sorted."""
        self.grocy["/objects/task_categories"] = [{"id": 2, "name": "inbox"}]
        self.grocy["/objects/tasks"] = [{"id": i, "name": "imported %d" % i, "done": 0, "category_id": 2} for i in range(30)] + [
            {"id": 99, "name": "Fix the gate", "done": 0, "category_id": None}]
        self.H.HouseTasks(self.g).tick()
        a = self.ha["sensor.house_tasks"]["attributes"]
        self.assertEqual(([i["name"] for i in a["items"]], a["to_review"]), (["Fix the gate"], 30))

    def test_private_tasks_are_counts_never_names(self):
        self.grocy["/users"] = [{"id": 2, "display_name": "Jordan"}]
        self.grocy["/tasks/private-summary"] = [{"owner_user_id": 2, "open": 3, "due_today": 1, "overdue": 1}]
        self.H.HouseTasks(self.g).tick()
        a = self.ha["sensor.house_tasks"]["attributes"]
        self.assertEqual(a["private"], [{"owner": "Jordan", "open": 3, "due_today": 1, "overdue": 1}])
        self.assertEqual(a["items"], [])

    def test_a_stock_grocy_without_private_tasks_still_works(self):
        gcall = self.g["gcall"]

        def stock(method, path, body=None):
            if path == "/tasks/private-summary":
                raise urllib.error.HTTPError(path, 400, "Bad Request", {}, None)
            return gcall(method, path, body)
        self.g["gcall"] = stock
        self.H.HouseTasks(self.g).tick()
        self.assertEqual(self.ha["sensor.house_tasks"]["attributes"]["private"], [])

    def test_done_by_itself_once(self):
        self.grocy["/objects/tasks"] = [{"id": 3, "name": "Move the wash to the dryer", "done": 0,
                                         "userfields": {"ctx_done_when": "sensor.dryer = running"}}]
        self.ha["sensor.dryer"] = {"state": "running"}
        h = self.H.HouseTasks(self.g)
        h.tick()
        h.tick()
        self.assertEqual(self.calls.count(("POST", "/tasks/3/complete")), 1)


# ---------------------------------------------------------------- pages
class Pages(unittest.TestCase):
    def test_no_unwrapped_string_plus_or(self):
        # tvlock: `html + f() || alt` == `(html + f()) || alt` dropped every card's closing </div>
        import re
        for f in (REPO / "static").glob("*.html"):
            for n, line in enumerate(f.read_text().splitlines(), 1):
                if re.search(r"'\s*\+\s*\(function\(\)\{.*\}\)\(\)\s*\|\|", line):
                    self.fail("%s:%d string + IIFE || alt (wrap the alternatives in parentheses)" % (f.name, n))


if __name__ == "__main__":
    unittest.main()
