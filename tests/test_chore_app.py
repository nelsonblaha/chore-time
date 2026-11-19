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
from datetime import date, datetime, timedelta, timezone
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
             "each_daily": True, "each_days": 2, "after": "00:00", "before": "23:59", "bedtime_ok": True},
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
        self.orig = {n: getattr(s, n) for n in ("notify_report", "notify_inspecting", "announce") if hasattr(s, n)}
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


class SoundSettings(Case):
    def test_house_can_mute_every_kids_interface_sound_effect(self):
        self.s.HOUSE["sound_effects"] = False
        self.assertIs(self.app.state()["sound_effects"], False)


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

    def test_due_shower_is_part_of_bedtime_routine(self):
        self.bed()
        self.assertEqual(self.app.post("/api/report", {"kid": "zoe", "chore": "shower"})[0], 200)

    def test_brush_teeth_not_before_its_time(self):
        cfg = json.loads((self.app.dir / "chores.json").read_text())
        next(c for c in cfg["chores"] if c["id"] == "brush_teeth")["after"] = "23:59"
        (self.app.dir / "chores.json").write_text(json.dumps(cfg))
        if datetime.now().strftime("%H:%M") < "23:59":
            self.assertEqual(self.app.post("/api/report", {"kid": "zoe", "chore": "brush_teeth"})[0], 409)

    def test_bedtime_allows_only_the_teeth_and_due_shower_routine(self):
        a = self.app
        a.event(type="adjust", kid="max", points=50, by="jordan", reason="test")
        self.bed()
        self.assertTrue(a.state()["bedtime"]["on"])
        self.assertEqual(a.post("/api/start", {"kid": "max", "chore": "kitchen"})[1].get("error"), "bedtime")
        self.assertEqual(a.post("/api/spend", {"kid": "max", "reward": "show", "tv": "livingroom"})[1].get("error"), "bedtime")
        self.assertEqual(a.post("/api/report", {"kid": "max", "chore": "brush_teeth"})[0], 200)
        self.assertEqual(a.post("/api/report", {"kid": "max", "chore": "shower"})[0], 200)
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


    def test_explicit_display_loading_can_precede_cast_metadata(self):
        self.t.update(external_display_entity='sensor.explicit_display',external_display_app_id='custom-display')
        self.app.set('sensor.explicit_display','loading',lease_until=time.time()+40)
        self.app.set(self.t['remote'],'on',current_activity='com.google.android.apps.mediashell')
        players=[{'state':'idle','attributes':{'app_id':self.s.DASHCAST_APP_ID}}]
        self.assertTrue(self.s.external_display_active(self.t,players,'x'))
        self.app.set(self.t['remote'],'on',current_activity='com.sony.dtv.tvx')
        self.assertFalse(self.s.external_display_active(self.t,players,'x'))
        self.app.set(self.t['remote'],'on',current_activity='com.google.android.apps.mediashell')
        self.app.set('sensor.explicit_display','loading',lease_until=time.time()-1)
        self.assertFalse(self.s.external_display_active(self.t,players,'x'))

    def test_loading_reservation_survives_screensaver_and_launcher_transition(self):
        self.t.update(external_display_entity='sensor.explicit_display', external_display_app_id='custom-display')
        self.app.set('sensor.explicit_display', 'loading', lease_until=time.time()+40)
        for activity in ('com.google.android.apps.tv.dreamx', 'com.google.android.apps.tv.launcherx'):
            self.assertEqual(self.act(activity), 'external_display', activity)
        for activity in ('com.sony.dtv.tvx', 'com.netflix.ninja', '', None, 'unknown'):
            self.app.set(self.t['remote'], 'on', current_activity=activity)
            self.assertFalse(self.s.external_display_active(self.t, [], 'x'), activity)
        self.app.set(self.t['remote'], 'on', current_activity='com.google.android.apps.tv.dreamx')
        for state, until in [('active', time.time()+40), ('loading', time.time()-1),
                             ('loading', time.time()+500), ('idle', time.time()+40)]:
            self.app.set('sensor.explicit_display', state, lease_until=until)
            self.assertFalse(self.s.external_display_active(self.t, [], 'x'), (state, until))

    def test_explicit_display_lease_expires_and_native_playback_wins(self):
        self.t.update(external_display_entity='sensor.explicit_display',external_display_app_id='custom-display')
        self.app.set('sensor.explicit_display','active',lease_until=time.time()+40)
        self.app.set(self.t['remote'],'on',current_activity='com.google.android.apps.mediashell')
        players=[{'state':'idle','attributes':{'app_id':'custom-display'}}]
        self.assertTrue(self.s.external_display_active(self.t,players,'x'))
        self.app.set(self.t['remote'],'on',current_activity='com.sony.dtv.tvx')
        self.assertFalse(self.s.external_display_active(self.t,players,'x'))
        self.app.set(self.t['remote'],'on',current_activity='com.google.android.apps.mediashell')
        for until in (time.time()-1,time.time()+500,None):
            self.app.set('sensor.explicit_display','active',lease_until=until)
            self.assertFalse(self.s.external_display_active(self.t,players,'x'))

    def test_rendered_display_uses_direct_receiver_proof_before_ha_metadata_catches_up(self):
        self.t.update(external_display_entity='sensor.explicit_display',external_display_app_id='custom-display')
        self.app.set(self.t['remote'],'on',current_activity='com.google.android.apps.mediashell')
        players=[{'state':'off','attributes':{}}, {'state':'idle','attributes':{'app_id':self.s.DASHCAST_APP_ID}}]
        attrs=dict(lease_until=time.time()+40,receiver_connected=True,receiver_app='custom-display')
        self.app.set('sensor.explicit_display','active',**attrs)
        self.assertTrue(self.s.external_display_active(self.t,players,'x'))
        for patch in ({'receiver_connected':False},{'receiver_app':self.s.DASHCAST_APP_ID},{'lease_until':time.time()-1}):
            self.app.set('sensor.explicit_display','active',**{**attrs,**patch})
            self.assertFalse(self.s.external_display_active(self.t,players,'x'))
        self.app.set('sensor.explicit_display','active',**attrs)
        self.app.set(self.t['remote'],'on',current_activity='com.netflix.ninja')
        self.assertFalse(self.s.external_display_active(self.t,players,'x'))


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
        real_time = va.time.time
        va.time.time = lambda: self.now[0]
        self.addCleanup(setattr, va.time, "time", real_time)   # the time module is shared: later tests need the real clock
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

    def test_a_held_sensor_counts_from_when_it_turned_on(self):
        """A thermostat's motion stays On for half an hour: its On is the moment someone moved, not someone there now."""
        T = "binary_sensor.kitchen_thermostat_motion"
        self.S(T, "on", 25 * 60)
        q, who = self.p.quiet_for()
        self.assertEqual((round(q), who), (25 * 60, T))
        self.S(T, "on", 60)
        self.assertEqual(round(self.p.quiet_for()[0]), 60)                # a fresh turn-on still holds the start back
        self.S("binary_sensor.kitchen_motion", "on", 5)
        self.assertEqual(self.p.quiet_for()[0], 0)                        # an ordinary sensor's On still means "there now"

    def test_a_held_sensor_the_robot_tripped_does_not_stall_a_run(self):
        T = "binary_sensor.kitchen_thermostat_motion"
        self.S(T, "on", 60)                                               # the robot just mopped the kitchen
        self.assertEqual(self.p.quiet_for(held=False)[0], 3000)           # between groups / paused: left out
        self.p.D.update(phase="paused", paused_at=self.now[0], cur="Family Room", groups=[{"mode": "vacuum", "rooms": ["Family Room"]}], gi=0, pauses=[])
        self.S(self.va.VAC, "paused")
        self.assertIn("vacuum.send_command", self.tick(30))              # resumes

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

    def test_presence_interruption_stays_incomplete_even_after_dwell_credit(self):
        self.run_into_living_room()
        for _ in range(13):
            self.tick(10)                                      # enough to credit an ordinary visit
        self.S(self.va.ROOM_SENSOR, "Family Room")             # leaving the room records the dwell visit
        self.tick()
        self.assertIn("Living Room", self.p.D["visits"])
        self.S("binary_sensor.front_door", "on")
        self.assertIn("vacuum.pause", self.tick())
        self.S(self.va.VAC, "paused")
        self.S("binary_sensor.entry_motion", "on")
        self.assertIn("vacuum.return_to_base", self.tick(610))
        self.S(self.va.VAC, "docked")
        self.S(self.va.IN_CLEAN, "off")
        self.tick()
        self.assertEqual(self.p.D["phase"], "idle")
        self.assertIn("Living Room", self.p.D["incomplete"])
        st = self.status()["Living Room"]
        self.assertTrue(st["eligible"])
        self.assertTrue(st["incomplete"])
        self.assertIn("unfinished", st["why"])

    def test_incomplete_job_is_retried_at_the_next_vacancy(self):
        self.p.D.update(incomplete=["Living Room"], incomplete_since=self.now[0] - 600,
                        incomplete_why="docked: person stayed downstairs", last_attempt=0)
        self.p.D["visits"]["Living Room"] = self.now[0]
        self.board["kitchen"]["last_clean"] = 0
        self.board["family_room"]["last_clean"] = 0
        self.p.D["visits"].update({"Kitchen": self.now[0], "Family Room": self.now[0],
                                    "Entry": self.now[0], "Hall": self.now[0]})
        self.S("binary_sensor.kitchen_motion", "off", 1300)
        self.assertIn(self.va.CLEAN_SCRIPT, self.tick())
        self.assertEqual([r for g in self.p.D["groups"] for r in g["rooms"]], ["Living Room"])

    def test_successful_retry_clears_the_incomplete_marker(self):
        self.p.D.update(incomplete=["Living Room"], incomplete_since=self.now[0] - 600,
                        incomplete_why="interrupted", groups=[{"mode": "vacuum", "rooms": ["Living Room"]}],
                        gi=0, phase="running", run_started=self.now[0] - 300, pauses=[], saved={})
        self.p.finish("done", self.now[0])
        self.assertEqual(self.p.D["incomplete"], [])

    def test_failed_retry_keeps_the_incomplete_marker(self):
        self.p.D.update(incomplete=["Living Room"], incomplete_since=self.now[0] - 600,
                        incomplete_why="interrupted", groups=[{"mode": "vacuum", "rooms": ["Living Room"]}],
                        gi=0, phase="running", run_started=self.now[0] - 30, clean_first=self.now[0] - 20,
                        clean_last=self.now[0] - 10, pauses=[], saved={})
        self.p.finish("done", self.now[0])
        self.assertEqual(self.p.D["incomplete"], ["Living Room"])
        self.assertIn("gave up", self.p.D["incomplete_why"])

    def test_dock_service_does_not_erase_the_active_plan(self):
        self.run_into_living_room()
        groups = self.p.D["groups"]
        self.S(self.va.VAC, "docked")
        self.S(self.va.IN_CLEAN, "on")
        self.S(self.va.BATTERY, "40")
        self.tick(180)
        self.assertEqual(self.p.D["phase"], "running")
        self.assertEqual(self.p.D["groups"], groups)

    def finish_first_group(self):
        self.run_into_living_room()
        self.S(self.va.VAC, "docked")
        self.S(self.va.IN_CLEAN, "off")
        self.tick(30)
        self.tick(70)

    def test_stuck_then_sent_home_by_hand_ends_the_run(self):
        # the robot got stuck mid-run; a grown-up freed it and sent it home hours later: no more rooms (kids are up)
        self.run_into_living_room()
        self.S(self.va.VAC, "error")
        self.tick()
        self.S(self.va.VAC, "returning")
        self.tick(3 * 3600)
        self.finish_first_group()
        self.assertEqual(self.p.D["phase"], "idle")
        self.S("binary_sensor.kitchen_motion", "off", 120)          # the kids are up: someone in the kitchen 2 min ago
        self.assertNotIn(self.va.CLEAN_SCRIPT, self.tick(10))

    def test_next_group_waits_five_quiet_minutes(self):
        self.finish_first_group()
        self.assertEqual(self.p.D["phase"], "between")
        self.S("binary_sensor.kitchen_motion", "off", 60)          # someone was in the kitchen a minute ago
        self.tick(10)
        self.assertEqual(self.p.D["phase"], "between")
        self.S("binary_sensor.kitchen_motion", "off", 400)
        self.tick(10)
        self.assertEqual((self.p.D["phase"], self.p.D["gi"]), ("running", 1))

    def test_sent_home_by_hand_while_paused_never_resumes(self):
        self.run_into_living_room()
        self.S("binary_sensor.front_door", "on")
        self.assertIn("vacuum.pause", self.tick())
        self.S("binary_sensor.front_door", "off", 0)
        self.S(self.va.VAC, "returning")                           # a grown-up: "go home"
        self.assertNotIn("vacuum.send_command", self.tick(60))
        self.S(self.va.VAC, "docked")
        self.S(self.va.IN_CLEAN, "off")
        self.tick(10)
        self.assertEqual(self.p.D["phase"], "idle")

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

    def test_a_tie_is_unknown_not_a_guess(self):
        L, v = self.L, "vacuum.robot"
        L.learn(v, 100, 100, "Kitchen")
        L.learn(v, 110, 110, "Family Room")
        self.assertEqual(L.classify(v, 100, 100), (None, {"Kitchen": 1, "Family Room": 1}))


class RobotLocatorFreshness(unittest.TestCase):
    """Off-cleaning the room must come from a recent position or read unknown: never the frozen map room, never an
    old fix (a lamp following the robot would light a room it left an hour ago)."""
    V, R, P = "vacuum.robot", "sensor.robot_current_room", "sensor.robot_room_live"

    def setUp(self):
        sys.path.insert(0, str(REPO))
        import locator
        self.loc = locator
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.now, self.pos, self.out = 1000.0, None, []
        self.vac = {"state": "returning", "last_changed": self.iso(900)}
        self.ha = {self.V: lambda: self.vac, self.R: lambda: {"state": "Family Room"}}
        self.g = {"HOUSE": {"robot_locator": {"robots": {self.V: {"room_sensor": self.R, "publish": self.P}}}},
                  "DATA": self.tmp, "ha_token": lambda: "t",
                  "ha_get": lambda e, tok: self.ha[e](),
                  "ha_post_state": lambda e, st, a, tok: self.out.append(dict(a, state=st))}
        real = locator.time.time
        locator.time.time = lambda: self.now
        self.addCleanup(setattr, locator.time, "time", real)
        self.L = self.fresh()

    def iso(self, ts):
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()

    def fresh(self):
        L = self.loc.Locator(self.g)
        L.position = lambda vac: self.pos
        for x in range(9000, 11000, 100):
            L.learn(self.V, x, 10000, "Kitchen")
        return L

    def tick(self, at):
        self.now = at
        self.L.tick()
        return self.out[-1]

    def test_returning_without_a_position_is_unknown_not_the_frozen_map_room(self):
        s = self.tick(1000)
        self.assertEqual((s["state"], s["source"], s["why"]), ("unknown", "unknown", "no position yet"))
        self.assertIsNone(s["observed_at"])

    def test_a_fix_expires_when_the_robot_stops_answering(self):
        self.pos = (10000, 10000)
        s = self.tick(1000)
        self.assertEqual((s["state"], s["source"], s["observed_at"], s["expires_at"]), ("Kitchen", "position", 1000, 1060))
        self.pos = None                                       # "map failure" from here on
        self.assertEqual(self.tick(1030)["state"], "Kitchen")  # still within a minute
        for later in (1065, 4600, 87400):
            s = self.tick(later)
            self.assertEqual((s["state"], s["why"]), ("unknown", "position too old"))

    def test_a_fix_from_before_the_robot_changed_state_is_not_used(self):
        self.pos = (10000, 10000)
        self.vac = {"state": "paused", "last_changed": self.iso(900)}
        self.assertEqual(self.tick(1000)["state"], "Kitchen")
        self.pos = None
        self.vac = {"state": "returning", "last_changed": self.iso(1010)}
        self.assertEqual(self.tick(1020)["why"], "no position yet")

    def test_a_restart_never_brings_back_an_old_fix(self):
        (self.tmp / "robot_map.json").write_text(json.dumps({"_last": {self.V: {"room": "Kitchen", "src": "position",
                                                                                "xy": [10000, 10000], "votes": {"Kitchen": 1}}}}))
        self.L = self.fresh()
        self.assertEqual(self.tick(1000)["state"], "unknown")

    def test_unlearned_spot_is_unknown(self):
        self.pos = (40000, 40000)
        self.assertEqual(self.tick(1000)["why"], "unlearned")

    def test_cleaning_uses_the_map_and_docked_is_unknown_unless_a_dock_room_is_set(self):
        self.vac = {"state": "cleaning", "last_changed": self.iso(900)}
        s = self.tick(1000)
        self.assertEqual((s["state"], s["source"], s["observed_at"]), ("Family Room", "map", 1000))
        self.vac = {"state": "docked", "last_changed": self.iso(1100)}
        self.assertEqual(self.tick(1200)["why"], "robot docked")
        self.g["HOUSE"]["robot_locator"]["robots"][self.V]["dock_room"] = "Corridor"
        self.L = self.fresh()
        self.assertEqual(self.tick(1300)["state"], "Corridor")

    def test_unchanged_is_resent_as_a_heartbeat(self):
        self.pos = (10000, 10000)
        self.tick(1000)
        self.now = 1005
        self.L.tick()
        self.assertEqual(len(self.out), 1)                     # nothing new to say yet
        s = self.tick(1031)
        self.assertEqual(len(self.out), 2)
        self.assertEqual((s["observed_at"], s["published_at"]), (1031, 1031))   # a fresh fix, re-sent


    def test_motion_during_an_autopilot_run_teaches_the_robots_room(self):
        # "use motion detectors to feed into the self-improving vacuum location system"
        rc = self.g["HOUSE"]["robot_locator"]["robots"][self.V]
        rc["motion"], rc["motion_when"] = {"binary_sensor.den_motion": "Den"}, "sensor.auto"
        self.L = self.fresh()
        auto, m = {"state": "running"}, {"state": "off"}
        self.ha["sensor.auto"], self.ha["binary_sensor.den_motion"] = (lambda: auto), (lambda: m)
        self.pos = (30000, 30000)                              # somewhere never learned
        self.tick(1000)
        m["state"] = "on"
        self.tick(1010)
        self.assertIsNone(self.L.classify(self.V, 30000, 30000)[0])   # not yet: a person would pause the run first
        self.tick(1080)
        self.assertEqual(self.L.classify(self.V, 30000, 30000)[0], "Den")
        m["state"] = "off"
        self.tick(1100)
        auto["state"] = "paused"                               # a person came in: their motion teaches nothing
        m["state"] = "on"
        self.pos = (50000, 50000)
        self.tick(1110)
        self.tick(1200)
        self.assertIsNone(self.L.classify(self.V, 50000, 50000)[0])

    def fusion_setup(self):
        rc = self.g["HOUSE"]["robot_locator"]["robots"][self.V]
        rc["motion"] = {"binary_sensor.hall_motion": "Corridor"}
        rc["occupancy"] = {"sensor": "sensor.occ", "floor": "down"}
        self.L = self.fresh()
        self.m = {"state": "off"}
        self.ha["binary_sensor.hall_motion"] = lambda: self.m
        self.ha["sensor.occ"] = lambda: {"state": "ok", "attributes": {"valid_until": self.now + 60,
                                                                       "floors": {"down": {"state": "vacant"}}}}
        self.vac = {"state": "cleaning", "last_changed": self.iso(900)}

    def test_fusion_trusts_a_fresh_room_sensor_over_a_held_map_room(self):
        # 11-18: the map room blanked and was held as Great Room; the robot was really in the entry hall, where the
        # hall's motion sensor (nobody downstairs) saw it
        self.fusion_setup()
        self.ha[self.R] = lambda: {"state": "Great Room"}
        self.tick(1000)
        self.ha[self.R] = lambda: {"state": "unknown"}
        self.tick(1070)                                       # held: the map room is over a minute old
        self.m["state"] = "on"
        s = self.tick(1075)
        self.assertEqual((s["state"], s["source"], s["fused_from"]), ("Corridor", "fused", "binary_sensor.hall_motion"))
        self.assertIsNone(s["held_since"])

    def test_each_source_earns_its_trust(self):
        # a sensor that keeps naming the wrong room while the robot's map and position agree loses weight
        self.fusion_setup()
        self.pos = (10000, 10000)                             # learned Kitchen
        self.ha[self.R] = lambda: {"state": "Kitchen"}
        t = 1000
        for i in range(6):
            self.m["state"] = "off"
            self.tick(t); t += 16
            self.m["state"] = "on"                            # the hall sensor fires (a draft, a pet...)
            self.tick(t); t += 16
        rel = self.out[-1]["reliability"]
        self.assertLess(rel["binary_sensor.hall_motion"], 0.5)
        self.assertEqual(self.out[-1]["state"], "Kitchen")

    def test_a_person_saying_where_it_is_teaches_the_spot(self):
        self.fusion_setup()
        self.pos = (40000, 40000)                             # never learned
        self.ha[self.R] = lambda: {"state": "unknown"}
        self.tick(1000)
        self.g["LOCATOR_TRUTH"] = [{"vacuum": self.V, "room": "Corridor", "at": 1001}]
        self.tick(1002)
        self.assertEqual(self.L.classify(self.V, 40000, 40000)[0], "Corridor")

    def test_a_blank_map_room_mid_clean_holds_the_last_room(self):
        # 11-18: the integration's room blanked to unknown for 30 s to 2.5 min at a time while the robot cleaned the
        # living room, and the floor plan jumped it back to its dock each time
        self.vac = {"state": "cleaning", "last_changed": self.iso(900)}
        self.ha[self.R] = lambda: {"state": "Great Room"}
        self.tick(1000)
        self.ha[self.R] = lambda: {"state": "unknown"}
        self.tick(1060)
        self.assertEqual((self.out[-1]["state"], self.out[-1]["why"]), ("Great Room", "map room blank: last room held"))
        self.tick(1000 + self.loc.HOLD_S + 1)                    # blank too long: unknown after all
        self.assertEqual(self.out[-1]["state"], "unknown")

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

    def test_a_job_can_wait_on_another_task(self):
        """'Garage door on scooter departure' only once the garage is cleared of cardboard; the clearing chore until then."""
        self.grocy["/objects/tasks"] = [
            {"id": 70, "name": "Clear the garage cardboard", "done": 0, "userfields": {}},
            {"id": 62, "name": "Garage door on scooter departure", "done": 0, "userfields": {"ctx_show_when": "task.70 = done"}},
            {"id": 71, "name": "Cardboard into the blue bin", "done": 0, "userfields": {"ctx_show_when": "task.70 = open"}}]
        self.H.HouseTasks(self.g).tick()
        names = lambda: sorted(i["name"] for i in self.ha["sensor.house_tasks"]["attributes"]["items"])
        self.assertEqual(names(), ["Cardboard into the blue bin", "Clear the garage cardboard"])
        self.grocy["/objects/tasks"][0]["done"] = 1
        self.H.HouseTasks(self.g).tick()
        self.assertEqual(names(), ["Garage door on scooter departure"])

    def test_clock_conditions(self):
        """'Evenings only' and 'weekends only' without a Home Assistant helper entity."""
        sat_evening = datetime(2026, 1, 3, 18, 30)
        get = lambda e: {"state": self.H.now_value(e, sat_evening)}
        self.assertTrue(self.H.check("now.weekday >= 5 and now.hour >= 17", get))
        self.assertFalse(self.H.check("now.hour < 12", get))
        h = self.H.HouseTasks(self.g)
        self.assertIsNotNone(h.get("now.hour")["state"])

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

    def test_each_screen_ranks_its_own_room_first(self):
        """The kitchen screen leads with the kitchen job; the upstairs screen with the upstairs one; overdue still wins."""
        self.g["HOUSE"]["house_tasks"] = {"floors": {"Down": ["Kitchen", "Hall"], "Up": ["Bath", "Bedroom"]},
                                          "displays": {"kitchen": "Kitchen", "bedroom": "Bedroom"}}
        self.grocy["/objects/tasks"] = [
            {"id": 1, "name": "Descale kettle", "done": 0, "userfields": {"ctx_rooms": "Kitchen"}},
            {"id": 2, "name": "Fix bath fan", "done": 0, "userfields": {"ctx_rooms": "Bath"}},
            {"id": 3, "name": "Pay the plumber", "done": 0, "due_date": "2000-01-01", "userfields": {}}]
        self.H.HouseTasks(self.g).tick()
        d = self.ha["sensor.house_tasks"]["attributes"]["displays"]
        self.assertEqual([i["name"] for i in d["kitchen"]], ["Pay the plumber", "Descale kettle", "Fix bath fan"])
        self.assertEqual([i["name"] for i in d["bedroom"]], ["Pay the plumber", "Fix bath fan", "Descale kettle"])
        self.assertTrue(d["kitchen"][1]["here"])

    def test_high_priority_comes_first_everywhere(self):
        self.g["HOUSE"]["house_tasks"] = {"floors": {"Down": ["Kitchen"], "Out": ["Yard"]}, "displays": {"kitchen": "Kitchen"}}
        self.grocy["/objects/tasks"] = [
            {"id": 1, "name": "Pay the plumber", "done": 0, "due_date": "2000-01-01", "userfields": {"ctx_rooms": "Kitchen"}},
            {"id": 2, "name": "Turn off the sprinkler", "done": 0, "userfields": {"ctx_rooms": "Yard", "ctx_priority": "High"}}]
        self.H.HouseTasks(self.g).tick()
        a = self.ha["sensor.house_tasks"]["attributes"]
        self.assertEqual(a["items"][0]["name"], "Turn off the sprinkler")
        self.assertEqual(a["displays"]["kitchen"][0]["name"], "Turn off the sprinkler")

    def test_a_floor_wide_screen_leads_with_its_floor(self):
        self.g["HOUSE"]["house_tasks"] = {"floors": {"Down": ["Kitchen", "Hall"], "Up": ["Bath", "Bedroom"]},
                                          "displays": {"up": "Up"}}
        self.grocy["/objects/tasks"] = [{"id": 1, "name": "Descale kettle", "done": 0, "userfields": {"ctx_rooms": "Kitchen"}},
                                        {"id": 2, "name": "Fix bath fan", "done": 0, "userfields": {"ctx_rooms": "Bath"}}]
        self.H.HouseTasks(self.g).tick()
        up = self.ha["sensor.house_tasks"]["attributes"]["displays"]["up"]
        self.assertEqual([(i["name"], i["here"]) for i in up], [("Fix bath fan", True), ("Descale kettle", False)])

    def _fake_list(self):
        """A tiny Home Assistant to-do list: get_items / add_item / remove_item, uids like HA's."""
        L = {"items": [], "n": 0}

        def svc(domain, service, data, response):
            if service == "get_items":
                return {data["entity_id"]: {"items": [dict(i) for i in L["items"] if i["status"] in data.get("status", ["needs_action"])]}}
            if service == "add_item":
                L["n"] += 1
                L["items"].append({"uid": "u%d" % L["n"], "summary": data["item"], "status": "needs_action", "due": data.get("due_date")})
            if service == "remove_item":
                L["items"] = [i for i in L["items"] if i["uid"] not in data["item"]]
            return {}
        self.g["ha_service"] = svc
        self.g["HOUSE"]["house_tasks"] = {"todo_entity": "todo.house_jobs"}
        return L

    def test_the_ha_list_mirrors_due_jobs_both_ways(self):
        """The house list (Assist / the HA app) and Grocy agree: ticks go to Grocy, new lines become tasks."""
        L = self._fake_list()
        created = []
        gcall = self.g["gcall"]

        def grocy(method, path, body=None):
            if method == "POST" and path == "/objects/tasks":
                created.append(body["name"])
                return {"created_object_id": "70"}
            return gcall(method, path, body)
        self.g["gcall"] = grocy
        self.grocy["/objects/tasks"] = [{"id": 5, "name": "Fix the gate", "done": 0, "userfields": {"ctx_rooms": "Garage"}},
                                        {"id": 6, "name": "Call the plumber", "done": 0, "userfields": {}}]
        h = self.H.HouseTasks(self.g)
        h.tick()
        self.assertEqual(sorted(i["summary"] for i in L["items"]), ["Call the plumber", "Fix the gate"])
        h.tick()
        self.assertEqual(len(L["items"]), 2)                                         # no echo, no duplicates
        next(i for i in L["items"] if i["summary"] == "Fix the gate")["status"] = "completed"   # ticked in the app
        L["items"].append({"uid": "spoken", "summary": "Buy furnace filter", "status": "needs_action"})   # "add … to house jobs"
        h.tick()
        self.assertIn(("POST", "/tasks/5/complete"), self.calls)
        self.assertEqual(created, ["Buy furnace filter"])
        self.assertEqual(sorted(i["summary"] for i in L["items"]), ["Buy furnace filter", "Call the plumber"])
        self.grocy["/objects/tasks"] = [{"id": 70, "name": "Buy furnace filter", "done": 0, "userfields": {}}]   # plumber done on a phone
        h.tick()
        self.assertEqual([i["summary"] for i in L["items"]], ["Buy furnace filter"])
        self.assertEqual(created, ["Buy furnace filter"])                            # the spoken task wasn't re-added

    def test_a_tick_stays_until_grocy_records_it(self):
        L = self._fake_list()
        self.grocy["/objects/tasks"] = [{"id": 5, "name": "Fix the gate", "done": 0, "userfields": {}}]
        h = self.H.HouseTasks(self.g)
        h.tick()
        L["items"][0]["status"] = "completed"
        gcall = self.g["gcall"]
        self.g["gcall"] = lambda m, p, b=None: (_ for _ in ()).throw(OSError("grocy down")) if m == "POST" else gcall(m, p, b)
        h.tick()
        self.assertEqual([(i["summary"], i["status"]) for i in L["items"]], [("Fix the gate", "completed")])

    def test_done_by_itself_once(self):
        self.grocy["/objects/tasks"] = [{"id": 3, "name": "Move the wash to the dryer", "done": 0,
                                         "userfields": {"ctx_done_when": "sensor.dryer = running"}}]
        self.ha["sensor.dryer"] = {"state": "running"}
        h = self.H.HouseTasks(self.g)
        h.tick()
        h.tick()
        self.assertEqual(self.calls.count(("POST", "/tasks/3/complete")), 1)



# ---------------------------------------------------------------- the grown-ups' house jobs on the screens
class HouseJobsApi(Case):
    def test_a_screen_gets_its_ranking_and_a_grown_up_ticks_one_off(self):
        self.s.GUARD["house_tasks"] = [{"kind": "task", "id": 7, "name": "Descale kettle"}, {"kind": "chore", "id": 9, "name": "Oven"}]
        self.s.GUARD["house_tasks_displays"] = {"kitchen": [{"kind": "chore", "id": 9, "name": "Oven", "here": True}]}
        self.s.GUARD["house_tasks_meta"] = {"private": [{"owner": "Jordan", "open": 2}], "to_review": 5}
        r = json.loads(urllib.request.urlopen(self.app.base + "/api/house_jobs?display=kitchen").read())
        self.assertEqual(([i["name"] for i in r["items"]], r["to_review"], r["private"][0]["owner"]), (["Oven"], 5, "Jordan"))
        calls = []
        self.s.GROCY = {"url": "x"}
        self.s.gcall = lambda m, p, b=None: calls.append((m, p)) or {}
        self.assertEqual(self.app.post("/api/house_done", {"kind": "chore", "id": 9, "by": "max"})[0], 400)   # kids can't
        self.assertEqual(self.app.post("/api/house_done", {"kind": "chore", "id": 9, "by": "jordan"})[0], 200)
        self.assertEqual(calls, [("POST", "/chores/9/execute")])
        self.assertEqual(self.s.GUARD["house_tasks_displays"]["kitchen"], [])
        self.assertEqual([i["id"] for i in self.s.GUARD["house_tasks"]], [7])
        r = json.loads(urllib.request.urlopen(self.app.base + "/api/house_jobs?display=all").read())
        self.assertEqual([i["id"] for i in r["items"]], [7])                     # 'all' = every due job, unranked

    def test_a_grown_up_adds_a_one_off_job(self):
        self.s.GUARD["house_tasks"] = []
        self.s.GROCY = {"url": "x"}
        sent = []
        self.s.gcall = lambda m, p, b=None: sent.append((m, p, b)) or {"created_object_id": "41"}
        self.assertEqual(self.app.post("/api/house_add", {"name": "  Fix   the gate ", "by": "max"})[0], 400)
        code, r = self.app.post("/api/house_add", {"name": "  Fix   the gate ", "by": "jordan", "note": "from a list"})
        self.assertEqual((code, r["id"]), (200, 41))
        self.assertEqual(sent[0][:2], ("POST", "/objects/tasks"))
        self.assertEqual(sent[0][2]["name"], "Fix the gate")
        self.assertEqual(self.s.GUARD["house_tasks"][0]["name"], "Fix the gate")


# ---------------------------------------------------------------- two households
def two_houses():
    c = base_config()
    c["households"] = [{"id": "dad", "name": "Dad's house"}, {"id": "mom", "name": "Mom's house"}]
    c["kids"][2]["where"] = {"entity": "input_boolean.casey_home", "on": "dad", "off": "mom"}
    c["chores"] += [{"id": "homework", "name": "Homework", "icon": "1f4da", "points": 3, "household": "any", "only": ["zoe"]},
                    {"id": "mom_dishes", "name": "Dishes", "icon": "1f37d", "points": 2, "household": "mom"}]
    c["rewards"] += [{"id": "mom_movie", "name": "Movie night at Mom's", "icon": "1f3ac", "cost": 4, "household": "mom"}]
    return c


class TwoHouseholds(Case):
    """A child who lives in two homes: her board follows where she is; one star balance across both."""
    config = two_houses()

    def at(self, house):
        self.s.GUARD["kid_where"] = {"zoe": house}

    def test_jobs_and_rewards_follow_the_house_she_is_at(self):
        self.at("mom")
        st = self.app.state()
        self.assertEqual(st["kid_where"]["zoe"], "mom")
        self.assertEqual(st["kid_where"]["ava"], "dad")                      # no 'where': the default (first) house
        self.assertEqual(self.app.post("/api/start", {"kid": "zoe", "chore": "kitchen"})[0], 409)   # Dad's kitchen
        self.assertEqual(self.app.post("/api/start", {"kid": "zoe", "chore": "mom_dishes"})[0], 200)
        self.assertEqual(self.app.post("/api/start", {"kid": "ava", "chore": "mom_dishes"})[0], 409)  # Ava is at Dad's

    def test_homework_counts_at_either_house_and_stars_are_one_balance(self):
        for house in ("mom", "dad"):
            self.at(house)
            self.app.pass_report("zoe", "homework", points=3)
        self.assertEqual(self.app.state()["balance"]["zoe"], 6)
        self.at("mom")
        self.assertEqual(self.app.post("/api/spend", {"kid": "zoe", "reward": "show"})[0], 409)       # Dad's TVs
        self.assertEqual(self.app.post("/api/spend", {"kid": "zoe", "reward": "mom_movie"})[0], 200)

    def test_a_parents_ha_login_acts_only_for_her_own_child(self):
        self.s.HA["user_kids"] = {"casey-ha-user": ["zoe"]}
        self.at("mom")

        def as_casey(path, body):
            req = urllib.request.Request(self.app.base + path, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "X-HA-User": "casey-ha-user"})
            try:
                return urllib.request.urlopen(req, timeout=10).status
            except urllib.error.HTTPError as e:
                return e.code
        self.assertEqual(as_casey("/api/start", {"kid": "max", "chore": "kitchen"}), 403)
        self.assertEqual(as_casey("/api/start", {"kid": "zoe", "chore": "mom_dishes"}), 200)
        self.s.HA.pop("user_kids")

    def test_where_comes_from_home_assistant(self):
        zoe = self.s.cfg()["kids"][2]
        for state, house in (("on", "dad"), ("off", "mom"), ("unavailable", None)):
            self.app.set("input_boolean.casey_home", state)
            self.assertEqual(self.s.where_of(zoe, "t"), house)

# ---------------------------------------------------------------- pages
class Pages(unittest.TestCase):
    def test_no_unwrapped_string_plus_or(self):
        # tvlock: `html + f() || alt` == `(html + f()) || alt` dropped every card's closing </div>
        import re
        for f in (REPO / "static").glob("*.html"):
            for n, line in enumerate(f.read_text().splitlines(), 1):
                if re.search(r"'\s*\+\s*\(function\(\)\{.*\}\)\(\)\s*\|\|", line):
                    self.fail("%s:%d string + IIFE || alt (wrap the alternatives in parentheses)" % (f.name, n))



class FixAJob(Case):
    """A grown-up corrects a waiting job: who did it and what each kid gets; a duplicate report is thrown out."""

    def test_award_each_kid_and_drop_the_duplicate(self):
        code, r1 = self.app.post("/api/report", {"kid": "max", "chore": "kitchen", "feeling": "ok"})
        code, r2 = self.app.post("/api/report", {"kid": "ava", "chore": "living_room", "feeling": "ok"})
        self.assertEqual(code, 200, r2)
        before = self.app.state()["balance"]
        self.assertEqual(self.app.post("/api/undo", {"ref": r2["event"]["id"], "by": "jordan"})[0], 200)
        code, r = self.app.post("/api/inspect", {"ref": r1["event"]["id"], "by": "jordan", "ok": True, "points": 0,
                                                 "award": {"ava": 3, "max": 1, "nobody": 9}})
        self.assertEqual(code, 200, r)
        st = self.app.state()
        self.assertEqual(st["balance"]["ava"] - before["ava"], 3)
        self.assertEqual(st["balance"]["max"] - before["max"], 1)
        rep = next(h for h in st["history"] if h.get("id") == r1["event"]["id"])
        self.assertEqual((rep["team"], rep["awarded"]), (["max", "ava"], 4))
        self.assertFalse(any(h.get("id") == r2["event"]["id"] and h.get("status") == "pending" for h in st["history"]))


    def test_the_same_kid_cannot_report_a_waiting_job_twice(self):
        c = self.s.cfg(); c["chores"].append({"id": "letter", "name": "Letter", "icon": "1f48c", "points": None, "shared": True, "propose": True})
        self.s.save_cfg(c)
        self.assertEqual(self.app.post("/api/report", {"kid": "max", "chore": "letter", "feeling": "ok", "ask": 2})[0], 200)
        self.assertEqual(self.app.post("/api/report", {"kid": "max", "chore": "letter", "feeling": "ok", "ask": 2})[0], 409)
        self.assertEqual(self.app.post("/api/report", {"kid": "ava", "chore": "letter", "feeling": "ok", "ask": 2})[0], 200)   # her own

class ChoreForm(Case):
    """The parents' full chore form: subtitle, one-time, a room (floor + robot-map id), who, which house, times."""
    config = two_houses()

    def test_new_one_time_chore_with_subtitle_room_and_kids(self):
        self.s.HOUSE.setdefault("house_tasks", {})["floors"] = {"Downstairs": ["Living Room", "Kitchen"]}
        c = self.s.cfg(); c["chores"][0]["map_rooms"] = ["living_room"]; self.s.save_cfg(c)
        code, r = self.app.post("/api/chore", {"by": "jordan", "chore": {
            "name": "Plug things in", "points": 2, "icon": "1f50c", "desc": "Everything at the outlet by the speaker",
            "once": True, "each_daily": False, "on_demand": False, "cooldown_h": 0, "where": "Living Room",
            "only": ["ava", "max"], "household": "any", "after": "07:30", "before": "bad", "from": "", "until": "2026-12-01",
            "daylight": False, "est_minutes": 5}})
        self.assertEqual(code, 200, r)
        ch = next(x for x in self.s.cfg()["chores"] if x["name"] == "Plug things in")
        self.assertEqual((ch["desc"], ch["once"], ch["where"], ch["floor"], ch["map_rooms"]),
                         ("Everything at the outlet by the speaker", True, "Living Room", "Downstairs", ["living_room"]))
        self.assertEqual((ch["only"], ch["household"], ch["after"], ch["until"], ch["est_minutes"]), (["ava", "max"], "any", "07:30", "2026-12-01", 5))
        for gone in ("before", "from", "daylight", "each_daily", "on_demand"):
            self.assertNotIn(gone, ch)
        self.assertIn("Living Room", self.app.state()["room_choices"]["Downstairs"])
        # edit: every kid = no "only"; first house = default (no key); comes back every 2 days
        code, r = self.app.post("/api/chore", {"by": "jordan", "chore": {"id": ch["id"], "name": "Plug things in", "points": 2,
                                "once": False, "cooldown_h": 48, "only": ["ava", "max", "zoe"], "household": "", "desc": ""}})
        ch = next(x for x in self.s.cfg()["chores"] if x["id"] == ch["id"])
        self.assertEqual(ch["cooldown_h"], 48.0)
        for gone in ("once", "only", "household", "desc"):
            self.assertNotIn(gone, ch)
        self.assertEqual(ch["where"], "Living Room")   # untouched fields stay

    def test_form_fields_survive_the_grocy_pull(self):
        """Grocy is the master list: what the form saves must come back from the next pull."""
        G = {"chores": {}, "uf": {}}

        def gcall(method, path, body=None):
            parts = path.strip("/").split("/")
            if method == "POST" and path == "/objects/chores":
                gid = 100 + len(G["chores"]); G["chores"][gid] = dict(body, id=gid); return {"created_object_id": gid}
            if method == "PUT" and parts[0] == "objects":
                G["chores"][int(parts[2])].update(body); return None
            if parts[0] == "userfields":
                uf = G["uf"].setdefault(int(parts[2]), {})
                if method == "PUT":
                    uf.update(body); return None
                return dict(uf)
            if method == "GET" and path == "/objects/chores":
                return [dict(g, userfields=G["uf"].get(i, {})) for i, g in G["chores"].items()]
            raise AssertionError(path)
        self.s.gcall = gcall
        self.s.HOUSE.setdefault("house_tasks", {})["floors"] = {"Downstairs": ["Living Room"]}
        code, r = self.app.post("/api/chore", {"by": "jordan", "chore": {
            "name": "Plug things in", "points": 1, "desc": "At the outlet by the speaker", "once": True, "cooldown_h": 0,
            "each_daily": False, "on_demand": False, "where": "Living Room", "only": ["ava"], "household": "any", "after": "07:30"}})
        self.assertEqual(code, 200, r)
        self.s.grocy_chores_pull()
        ch = next(x for x in self.s.cfg()["chores"] if x["name"] == "Plug things in")
        self.assertEqual((ch["desc"], ch.get("once"), ch.get("where"), ch.get("floor"), ch.get("only"), ch.get("household"), ch.get("after"), ch.get("hidden")),
                         ("At the outlet by the speaker", True, "Living Room", "Downstairs", ["ava"], "any", "07:30", None))
        # now every 3 days, not a room, and no longer one-time
        self.app.post("/api/chore", {"by": "jordan", "chore": {"id": ch["id"], "name": "Plug things in", "points": 1, "once": False, "cooldown_h": 72}})
        self.s.grocy_chores_pull()
        ch = next(x for x in self.s.cfg()["chores"] if x["id"] == ch["id"])
        self.assertEqual((ch.get("cooldown_h"), ch.get("room"), ch.get("once"), ch.get("where")), (72, None, None, "Living Room"))


class Envelopes(Case):
    """A kid prints the envelope for a letter: from a kid, to a listed relative, through lp; never twice in a row."""
    CFG = {"printer": "Office", "lp_options": {"PageSize": "Env10", "InputSlot": "Manual"}, "chores": ["letter"],
           "return_address": ["1 Main St", "Town"], "senders": {"max": "Max Example"},
           "recipients": [{"id": "gran", "label": "Granny", "name": "Ann Example", "address": ["2 Elm St", "City"]}]}

    def setUp(self):
        super().setUp()
        import envelopes
        self.e = envelopes
        envelopes.configure(self.CFG)
        envelopes.LAST["ts"] = 0
        self.cmds = []
        real = envelopes.print_envelope
        envelopes.print_envelope = lambda f, t, run=None: real(f, t, run=self.fake_lp)
        self.addCleanup(setattr, envelopes, "print_envelope", real)
        self.addCleanup(envelopes.configure, {})

    def fake_lp(self, cmd, **kw):
        self.cmds.append(cmd)
        self.pdf = open(cmd[-1], "rb").read()
        return type("R", (), {"returncode": 0, "stdout": "request id is Office-7 (1 file(s))", "stderr": ""})()

    def test_kids_screen_gets_labels_not_addresses(self):
        env = self.app.state()["envelope"]
        self.assertEqual((env["chores"], env["senders"], env["recipients"][0]["label"]), (["letter"], ["max"], "Granny"))
        self.assertNotIn("2 Elm St", json.dumps(env))

    def test_print_sends_a_pdf_with_both_addresses_to_lp(self):
        code, r = self.app.post("/api/envelope", {"kid": "max", "from": "max", "to": "gran"})
        self.assertEqual(code, 200, r)
        cmd = self.cmds[0]
        self.assertEqual(cmd[:3], ["lp", "-d", "Office"])
        self.assertIn("PageSize=Env10", cmd)
        self.assertTrue(self.pdf.startswith(b"%PDF") and b"(Max Example) Tj" in self.pdf and b"(2 Elm St) Tj" in self.pdf)
        self.assertEqual(self.app.post("/api/envelope", {"kid": "max", "from": "max", "to": "gran"})[0], 409)   # double tap

    def test_unknown_sender_or_recipient_is_refused(self):
        self.assertEqual(self.app.post("/api/envelope", {"kid": "max", "from": "ava", "to": "gran"})[0], 409)
        self.assertEqual(self.app.post("/api/envelope", {"kid": "max", "from": "max", "to": "nobody"})[0], 409)
        self.assertEqual(self.cmds, [])


    def test_status_follows_the_job_and_names_a_jam(self):
        self.e.CFG["printer_uri"] = "ipp://printer/ipp/print"
        answers = {"job": "processing", "reasons": "media-jam-error"}

        def run(cmd, **kw):
            out = ("job-state (enum) = %s" % answers["job"]) if "ipp://localhost/printers/Office" in cmd else \
                  ("printer-state-reasons (keyword) = %s" % answers["reasons"])
            return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()
        self.assertEqual(self.e.status("Office-9", run), {"state": "gone", "problem": None})   # not ours
        self.e.JOBS.add("Office-9")
        self.assertEqual(self.e.status("Office-9", run), {"state": "printing", "problem": "jam"})
        answers.update(job="canceled", reasons="none")
        self.assertEqual(self.e.status("Office-9", run)["state"], "stopped")
        answers["job"] = "completed"
        self.assertEqual(self.e.status("Office-9", run), {"state": "done", "problem": None})


    def test_a_letter_is_handed_over_after_its_report(self):
        c = self.s.cfg(); c["chores"].append({"id": "letter", "name": "Letter", "icon": "1f48c", "points": 2, "shared": True})
        self.s.save_cfg(c)
        self.assertEqual(self.app.post("/api/envelope_delivered", {"kid": "max", "chore": "letter"})[0], 409)   # nothing waiting
        code, r = self.app.post("/api/report", {"kid": "max", "chore": "letter", "feeling": "ok"})
        self.assertNotIn("delivered", r["event"])
        code, d = self.app.post("/api/envelope_delivered", {"kid": "max", "chore": "letter"})
        self.assertEqual(code, 200, d)
        rep = next(h for h in d["state"]["history"] if h.get("id") == r["event"]["id"])
        self.assertTrue(rep["delivered"])
        code, r2 = self.app.post("/api/report", {"kid": "ava", "chore": "letter", "feeling": "ok", "delivered": True})
        self.assertTrue(r2["event"]["delivered"])                    # handed over before reporting
        self.assertIn("handoff_done", self.app.state()["envelope"])


class FixItJobs(Case):
    """'Plug it back in': a job that exists only while a device is down, pays a trophy; reminders show the next step."""

    def setUp(self):
        super().setUp()
        c = self.s.cfg()
        c["chores"] += [{"id": "plug", "name": "Plug the sensor in", "icon": "1f50c", "points": 0, "cooldown_h": 0,
                         "when_down": ["binary_sensor.sensor"], "down_min": 10, "trophy": {"title": "Fixer!"}},
                        {"id": "letter", "name": "Letter", "icon": "1f48c", "points": None, "shared": True, "remind": True}]
        self.s.save_cfg(c)
        self.addCleanup(self.s.DOWN.clear)

    def visible(self):
        return {x["id"] for x in self.app.state()["config"]["chores"] if not x.get("hidden")}

    def test_only_while_the_device_has_been_down_ten_minutes(self):
        self.assertNotIn("plug", self.visible())
        self.s.DOWN["binary_sensor.sensor"] = time.time() - 60
        self.assertNotIn("plug", self.visible())                       # a blip
        self.s.DOWN["binary_sensor.sensor"] = time.time() - 700
        self.assertIn("plug", self.visible())

    def test_every_fix_earns_a_trophy(self):
        self.s.DOWN["binary_sensor.sensor"] = time.time() - 700
        for _ in range(2):
            self.app.pass_report("max", "plug", points=0)
        mine = [a for a in self.app.state()["awards"]["max"] if a["id"].startswith("trophy:plug")]
        self.assertEqual((len(mine) >= 1, mine[0]["title"]), (True, "Fixer!"))

    def test_a_remind_job_in_progress_shows_its_next_step(self):
        self.assertEqual(self.app.post("/api/start", {"kid": "max", "chore": "letter"})[0], 200)
        self.assertIn("I'm done", self.app.state()["next_step"]["max"])


class PutOnClothes(Case):
    """A grown-up's switch: listed kids can't do other jobs until each is inspected dressed; then it turns itself off."""

    def test_blocks_until_everyone_is_dressed_then_turns_off(self):
        code, r = self.app.post("/api/dress", {"by": "jordan", "on": True, "kids": ["ava", "max"]})
        self.assertEqual(code, 200, r)
        self.assertEqual(self.app.state()["dress"]["kids"], {"ava": "todo", "max": "todo"})
        code, r = self.app.post("/api/start", {"kid": "ava", "chore": "kitchen"})
        self.assertEqual((code, r.get("error")), (409, "Put on your clothes first!"))
        ids = {}
        for k in ("ava", "max"):
            code, r = self.app.post("/api/report", {"kid": k, "chore": "get_dressed", "feeling": "ok"})
            self.assertEqual(code, 200, r)
            ids[k] = r["event"]["id"]
        self.assertEqual(self.app.state()["dress"]["kids"]["ava"], "waiting")
        self.app.post("/api/inspect", {"ref": ids["ava"], "by": "jordan", "ok": True, "points": 0})
        self.assertEqual(self.app.post("/api/start", {"kid": "ava", "chore": "kitchen"})[0], 200)   # ava is free
        self.assertTrue(self.app.state()["dress"]["on"])
        self.app.post("/api/inspect", {"ref": ids["max"], "by": "jordan", "ok": True, "points": 0})
        self.assertFalse(self.app.state()["dress"]["on"])                                          # all ready: off
        st = self.app.state()
        rep = next(h for h in st["history"] if h.get("id") == ids["ava"])
        self.assertTrue(rep.get("started"), rep)                               # always a timed race
        self.assertTrue(any(a["id"].startswith("trophy:get_dressed") and a["sub"].startswith("in ") for a in st["awards"]["ava"]), st["awards"]["ava"])

    def test_laundry_toggles_closed_again(self):
        c = self.s.cfg(); c["chores"].append({"id": "wash", "name": "Laundry", "icon": "1f455", "points": 3, "on_demand": True})
        self.s.save_cfg(c)
        self.app.post("/api/reopen", {"chore": "wash", "by": "jordan"})
        self.assertTrue(self.app.state()["board"]["wash"]["opened"])
        self.assertEqual(self.app.post("/api/close_od", {"chore": "wash", "by": "jordan"})[0], 200)
        self.assertFalse(self.app.state()["board"]["wash"]["opened"])


class SchoolMornings(Case):
    """On school days "Put on clothes!" switches itself on at 5:30 for the kids at this house, quietly, and off at 7:30.
    The kids' calendar's holidays and breaks (keyword match) skip it; one naming a kid skips only that kid."""
    MON = datetime(2026, 11, 16)   # a Monday

    def setUp(self):
        super().setUp()
        self.s.SCHOOL.clear()
        self.s.SCHOOL.update({"at": "05:30", "until": "07:30"})
        self.sch = self.s.schedule
        self.saved = (dict(self.sch.CACHE["days"]), dict(self.sch.CACHE["off"]))
        self.sch.CACHE["days"].clear()
        self.sch.CACHE["off"].clear()
        for i in range(14):
            self.sch.CACHE["days"][(self.MON.date() + timedelta(days=i)).isoformat()] = True

    def tearDown(self):
        self.s.SCHOOL.clear()
        self.sch.CACHE["days"].clear(); self.sch.CACHE["days"].update(self.saved[0])
        self.sch.CACHE["off"].clear(); self.sch.CACHE["off"].update(self.saved[1])
        super().tearDown()

    def step(self, when, home=("ava", "max")):
        return self.s.school_morning_step(when, self.s.read_events(), self.s.cfg()["kids"], lambda: list(home))

    def test_on_at_half_past_five_on_a_school_day_only(self):
        self.assertEqual(self.step(self.MON.replace(hour=5, minute=29)), None)
        self.assertEqual(self.step(self.MON.replace(hour=5, minute=31)), ("on", ["ava", "max"]))
        self.assertEqual(self.step(self.MON.replace(hour=7, minute=31)), None)               # too late to start
        self.assertEqual(self.step((self.MON + timedelta(days=5)).replace(hour=6)), None)   # Saturday
        self.assertEqual(self.step(self.MON.replace(hour=6), home=()), None)                # nobody here

    def test_holidays_skip_it_and_a_named_kid_only_skips_that_kid(self):
        self.sch.CACHE["off"][self.MON.date().isoformat()] = ["District: Thanksgiving Break"]
        self.assertEqual(self.step(self.MON.replace(hour=6)), None)
        self.sch.CACHE["off"][self.MON.date().isoformat()] = ["Ava Intersession (No School)"]
        self.assertEqual(self.step(self.MON.replace(hour=6)), ("on", ["max"]))

    def test_once_a_morning_and_off_at_the_cutoff(self):
        at = self.MON.replace(hour=5, minute=30).timestamp()
        self.app.event(type="dress", on=True, kids=["ava"], by="auto", school=True, ts=at)
        self.assertEqual(self.step(self.MON.replace(hour=7)), None)
        self.assertEqual(self.step(self.MON.replace(hour=7, minute=30)), ("off", None))
        self.app.event(type="dress", on=False, kids=[], by="jordan", ts=at + 60)       # a grown-up turned it off
        self.assertEqual(self.step(self.MON.replace(hour=7)), None)                    # stays off this morning

    def test_quiet_and_the_race_starts_when_they_are_up(self):
        tts, spk = self.s.ANNOUNCE_TTS, self.s.DRESS.get("speakers")
        self.s.ANNOUNCE_TTS, self.s.DRESS["speakers"] = "tts.test", ["media_player.test"]
        self.addCleanup(lambda: (setattr(self.s, "ANNOUNCE_TTS", tts), self.s.DRESS.pop("speakers") if spk is None else self.s.DRESS.update(speakers=spk)))
        code, r = self.app.post("/api/dress", {"by": "jordan", "on": True, "kids": ["ava"]})
        self.assertEqual(code, 200, r)
        time.sleep(0.5)                                                                # its first reminder (a thread)
        n = len(self.app.calls)
        self.s.dress_announce()
        self.assertEqual(len(self.app.calls), n + 1)                                   # the grown-up's switch speaks
        self.app.event(type="dress", on=True, kids=["ava"], by="auto", school=True)
        self.s.dress_announce()
        self.assertEqual(len(self.app.calls), n + 1)                                   # a school morning doesn't
        d = self.app.state()["dress"]
        self.assertTrue(d["school"])
        self.assertGreaterEqual(d["go"], d["since"])
        code, r = self.app.post("/api/report", {"kid": "ava", "chore": "get_dressed", "feeling": "ok"})
        self.assertEqual(code, 200, r)
        self.assertNotIn("started", r["event"])                                        # no clock unless the kid started it

    def test_the_kid_starts_their_own_clock(self):
        self.app.event(type="dress", on=True, kids=["ava", "max"], by="auto", school=True)
        self.assertEqual(self.app.state()["dress"]["started"], {})                     # nothing running when they wake up
        code, r = self.app.post("/api/start", {"kid": "ava", "chore": "get_dressed"})
        self.assertEqual(code, 200, r)
        st = self.app.state()["dress"]
        self.assertEqual(list(st["started"]), ["ava"])
        self.assertEqual(st["kids"], {"ava": "todo", "max": "todo"})
        code, r = self.app.post("/api/report", {"kid": "ava", "chore": "get_dressed", "feeling": "ok"})
        self.assertEqual((code, r["event"]["started"]), (200, st["started"]["ava"]))

    def test_a_second_start_keeps_the_first_clock(self):
        self.app.event(type="dress", on=True, kids=["ava"], by="auto", school=True)
        self.app.post("/api/start", {"kid": "ava", "chore": "get_dressed"})
        first = self.app.state()["dress"]["started"]["ava"]
        time.sleep(0.05)
        code, r = self.app.post("/api/start", {"kid": "ava", "chore": "get_dressed"})
        self.assertEqual((code, r.get("already")), (200, True))
        self.assertEqual(self.app.state()["dress"]["started"]["ava"], first)

    def test_personal_bests_beside_the_fastest_kid(self):
        now = time.time()
        for kid, sec, ago in (("max", 300, 3000), ("ava", 600, 2000), ("ava", 500, 1000)):
            ev = self.app.event(type="report", kid=kid, chore="get_dressed", feeling="ok", points=0,
                                ts=now - ago, started=now - ago - sec)
            self.app.post("/api/inspect", {"ref": ev["id"], "by": "jordan", "ok": True, "points": 0})
        st = self.app.state()
        self.assertEqual(st["records"]["get_dressed"]["kid"], "max")
        self.assertEqual({k: v["sec"] for k, v in st["personal_records"]["get_dressed"].items()}, {"max": 300, "ava": 500})
        self.assertTrue(any(a["id"].startswith("pb:get_dressed") and "beat your 10:00" in a["sub"] for a in st["awards"]["ava"]),
                        st["awards"]["ava"])

    def test_next_school_morning_skips_the_weekend_and_holidays(self):
        sat = (self.MON + timedelta(days=5)).replace(hour=9)
        self.assertEqual(self.s.school_next(sat, self.s.cfg()["kids"])["day"], "Mon")
        self.sch.CACHE["off"][(self.MON + timedelta(days=7)).date().isoformat()] = ["MLK Day Holiday"]
        self.assertEqual(self.s.school_next(sat, self.s.cfg()["kids"])["day"], "Tue")

    def test_calendar_keywords(self):
        ics = "\n".join(["BEGIN:VCALENDAR",
                         "BEGIN:VEVENT", "UID:a", "DTSTART;VALUE=DATE:20261123", "DTEND;VALUE=DATE:20261128", "SUMMARY:District: Thanksgiving Break", "END:VEVENT",
                         "BEGIN:VEVENT", "UID:b", "DTSTART;VALUE=DATE:20261120", "DTEND;VALUE=DATE:20261121", "SUMMARY:Last school day before break", "END:VEVENT",
                         "BEGIN:VEVENT", "UID:c", "DTSTART;VALUE=DATE:20261012", "DTEND;VALUE=DATE:20261013", "SUMMARY:No school - teacher development", "END:VEVENT",
                         "BEGIN:VEVENT", "UID:d", "DTSTART:20261030T120000", "DTEND:20261030T130000", "SUMMARY:Early release holiday party", "END:VEVENT",
                         "END:VCALENDAR"])
        off = self.sch.days_off(ics, start=date(2026, 10, 1), horizon_days=90)
        self.assertEqual(sorted(off), ["2026-10-12", "2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27"])


class GrownUpFromHA(unittest.TestCase):
    """The grown-up watch check from the kids' page inside Home Assistant (a parent's own phone or tablet)."""

    def test_relay_hands_back_the_request_id(self):
        src = (REPO / "server.py").read_text()
        self.assertIn('RELAY_PASS = ("nonce", "status")', src)
        self.assertIn("for k in RELAY_PASS if k in res", src)
        kids = (REPO / "static" / "kids.html").read_text()
        self.assertIn("nonce:lr.nonce,status:lr.status", kids)

    def test_approval_opens_the_ha_copy_and_comes_back(self):
        kids = (REPO / "static" / "kids.html").read_text()
        self.assertIn('HA_MODE?"/local/chore-inspect.html?back=kids&"', kids)
        self.assertNotIn('location.href="/inspect?', kids)
        self.assertIn('BK==="kids"&&HA_MODE?"/local/chore-kids.html"', (REPO / "static" / "inspect.html").read_text())

    def test_parent_controls_use_music_policy_switch_and_regular_laundry_card(self):
        inspect = (REPO / "static" / "inspect.html").read_text()
        self.assertIn("Spotify waits for mandatory chores", inspect)
        self.assertIn('data-a="od"', inspect)
        self.assertNotIn('data-od="', inspect)
        block = (REPO / "static" / "music-block.html").read_text()
        self.assertIn("These kids need to finish mandatory chores before music:", block)


class SpotifyChoreInterruption(Case):
    def test_only_fresh_complete_ha_receipt_is_exposed(self):
        raw = {"state": "blocked", "attributes": {"receiver": "media_player.den", "rooms": ["den"],
                                                      "attempted_at": 90, "valid_until": 130, "attempt_id": "a1"}}
        self.assertEqual(self.s.normalize_spotify_interruption(raw, now=100),
                         {"receiver": "media_player.den", "rooms": ["den"], "attempted_at": 90.0,
                          "valid_until": 130.0, "attempt_id": "a1"})
        self.assertIsNone(self.s.normalize_spotify_interruption(raw, now=130))
        raw["attributes"].pop("receiver")
        self.assertIsNone(self.s.normalize_spotify_interruption(raw, now=100))

    def test_display_ip_maps_to_physical_room(self):
        hubs, tvs = self.s.HUBS, self.s.TVS
        try:
            self.s.HUBS, self.s.TVS = {"den_hub": {"ip": "10.0.0.2", "room": "den"}}, {"tv": {"ip": "10.0.0.3", "room": "kitchen"}}
            self.assertEqual(self.s.display_for_ip("10.0.0.2"), {"kind": "hub", "id": "den_hub", "room": "den"})
            self.assertEqual(self.s.display_for_ip("10.0.0.3"), {"kind": "tv", "id": "tv", "room": "kitchen"})
            self.assertIsNone(self.s.display_for_ip("10.0.0.9"))
        finally:
            self.s.HUBS, self.s.TVS = hubs, tvs


class PausedTeamJobs(Case):
    """A sent-back team job put off for later, and a teammate's other paused job."""

    def test_report_after_finish_later_keeps_the_team_and_split(self):
        self.assertEqual(self.app.post("/api/start", {"kid": "max", "chore": "organize"})[0], 200)
        code, r = self.app.post("/api/team", {"kid": "max", "chore": "organize", "partners": ["ava"]})
        self.assertEqual(code, 200, r)
        self.assertEqual(self.app.post("/api/pause", {"kid": "max"})[0], 200)
        code, r = self.app.post("/api/report", {"kid": "max", "chore": "organize"})
        self.assertEqual(code, 200, r)
        self.assertEqual(sorted(r["event"].get("team") or []), ["ava", "max"])

    def test_sending_back_a_team_job_keeps_a_teammates_other_paused_job(self):
        self.assertEqual(self.app.post("/api/start", {"kid": "ava", "chore": "organize"})[0], 200)
        self.assertEqual(self.app.post("/api/pause", {"kid": "ava"})[0], 200)
        with open(self.app.dir / "ledger.jsonl", "a") as f:   # a grown-up sends a two-kid job back (what the parents' page writes)
            f.write(json.dumps({"id": "rw1", "ts": time.time(), "type": "rework", "ref": "r1", "chore": "other",
                                "team": ["max", "ava"], "worked": 0, "timed": False, "feedback": "", "by": "jordan", "round": 2}) + "\n")
        self.assertEqual(self.app.state()["paused"].get("ava", {}).get("chore"), "organize")


class SavedJobsAndNames(Case):
    """Finish later keeps a job even if the kid starts something else; kids can name their job."""

    def test_starting_another_job_keeps_the_saved_one(self):
        self.assertEqual(self.app.post("/api/start", {"kid": "ava", "chore": "organize"})[0], 200)
        self.assertEqual(self.app.post("/api/pause", {"kid": "ava"})[0], 200)
        self.assertEqual(self.app.post("/api/start", {"kid": "ava", "chore": "kitchen"})[0], 200)
        self.assertEqual(self.app.state()["paused"]["ava"]["chore"], "organize")
        self.assertEqual(self.app.post("/api/report", {"kid": "ava", "chore": "kitchen"})[0], 200)
        self.assertEqual(self.app.state()["paused"]["ava"]["chore"], "organize")

    def test_a_second_saved_job_comes_back_after_the_first(self):
        self.app.post("/api/start", {"kid": "ava", "chore": "organize"})
        self.app.post("/api/pause", {"kid": "ava"})
        self.app.post("/api/start", {"kid": "ava", "chore": "kitchen"})
        self.app.post("/api/pause", {"kid": "ava"})
        self.assertEqual(self.app.state()["paused"]["ava"]["chore"], "kitchen")
        self.assertEqual(self.app.post("/api/resume", {"kid": "ava"})[0], 200)
        self.assertEqual(self.app.post("/api/report", {"kid": "ava", "chore": "kitchen"})[0], 200)
        self.assertEqual(self.app.state()["paused"]["ava"]["chore"], "organize")

    def test_a_named_job_keeps_its_name_through_later_and_reaches_the_grown_up(self):
        self.app.post("/api/start", {"kid": "ava", "chore": "organize"})
        self.assertEqual(self.app.post("/api/title", {"kid": "ava", "title": "ORGANIZING  ROCKS"})[0], 200)
        self.assertEqual(self.app.state()["active"]["ava"]["title"], "ORGANIZING ROCKS")
        self.app.post("/api/pause", {"kid": "ava"})
        self.assertEqual(self.app.state()["paused"]["ava"]["title"], "ORGANIZING ROCKS")
        self.app.post("/api/resume", {"kid": "ava"})
        code, r = self.app.post("/api/report", {"kid": "ava", "chore": "organize"})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["event"]["note"], "ORGANIZING ROCKS")

    def test_a_saved_job_can_be_named_and_kept_longer(self):
        self.app.post("/api/start", {"kid": "ava", "chore": "organize"})
        self.app.post("/api/pause", {"kid": "ava"})
        self.assertEqual(self.app.post("/api/title", {"kid": "ava", "title": "ROCKS"})[0], 200)
        until = time.time() + 30 * 86400
        self.assertEqual(self.app.post("/api/keep", {"kid": "ava", "by": "jordan", "until": until})[0], 200)
        z = self.app.state()["paused"]["ava"]
        self.assertEqual((z["title"], round(z["until"])), ("ROCKS", round(until)))


class ServiceSchedule(unittest.TestCase):
    """Which Sundays a church-style slot chore happens: every other week, unless the shared calendar says otherwise."""

    ICS = "\r\n".join([
        "BEGIN:VCALENDAR",
        "BEGIN:VEVENT", "UID:a", "DTSTART;VALUE=DATE:20261111", "DTEND;VALUE=DATE:20261117",
        "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=WE", "SUMMARY:Home", "END:VEVENT",
        "BEGIN:VEVENT", "UID:a", "RECURRENCE-ID;VALUE=DATE:20261125", "DTSTART;VALUE=DATE:20261120",
        "DTEND;VALUE=DATE:20261130", "SUMMARY:Home - holiday", "END:VEVENT",
        "BEGIN:VEVENT", "UID:b", "DTSTART;VALUE=DATE:20261118", "DTEND;VALUE=DATE:20261121",
        "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=WE", "EXDATE;VALUE=DATE:20261202", "SUMMARY:Home", "END:VEVENT",
        "BEGIN:VEVENT", "UID:c", "DTSTART;VALUE=DATE:20261106", "DTEND;VALUE=DATE:20261111",
        "RRULE:FREQ=WEEKLY;INTERVAL=2", "SUMMARY:Other parent", "END:VEVENT",
        "END:VCALENDAR", ""])

    def setUp(self):
        sys.path.insert(0, str(REPO))
        import schedule
        self.sc = schedule

    def test_recurring_blocks_moved_instances_and_exdates(self):
        from datetime import date
        d = self.sc.days_here(self.ICS, "^Home", start=date(2026, 11, 1), horizon_days=60)
        self.assertTrue(d["2026-11-15"])    # Wed 11th - Mon 16th
        self.assertFalse(d["2026-11-08"])   # the other parent's weekend
        self.assertTrue(d["2026-11-22"])    # the holiday block moved in (Fri 20th - Sun 29th)
        self.assertTrue(d["2026-11-29"])
        self.assertFalse(d["2026-11-30"])   # the regular Nov 25 - Dec 1 instance it replaced is gone
        self.assertFalse(d["2026-12-03"])   # EXDATE drops that midweek block
        self.assertTrue(d["2026-12-13"])    # the regular rhythm again (Wed 9th - Mon 14th)

    def test_agenda_keeps_only_safe_display_fields_and_filters_routine_events(self):
        self.sc.CFG.clear(); self.sc.CFG.update({"timezone": "America/Denver"})
        text = "\r\n".join([
            "BEGIN:VCALENDAR",
            "BEGIN:VEVENT", "UID:1", "DTSTART;TZID=America/Denver:20261119T153000", "DTEND;TZID=America/Denver:20261119T163000",
            "SUMMARY:Science fair", "DESCRIPTION:private notes", "LOCATION:private place", "END:VEVENT",
            "BEGIN:VEVENT", "UID:2", "DTSTART;VALUE=DATE:20261120", "DTEND;VALUE=DATE:20261121", "SUMMARY:School - Lunch", "END:VEVENT",
            "END:VCALENDAR", ""])
        rows = self.sc.agenda_events(text, source="Family", start=datetime(2026, 11, 18, 12, tzinfo=timezone(timedelta(hours=-7))),
                                     exclude=r"^School - (Lunch|Dismissal)$")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["title"], rows[0]["source"], rows[0]["all_day"]), ("Science fair", "Family", False))
        self.assertEqual(set(rows[0]), {"title", "source", "start", "end", "all_day"})

    def test_week_calendar_includes_recent_and_future_events_but_not_older_ones(self):
        self.sc.CFG.clear(); self.sc.CFG.update({"timezone": "America/Denver", "agenda": {"past_days": 14, "days": 28}})
        now = datetime(2026, 11, 18, 12, tzinfo=timezone(timedelta(hours=-7)))
        self.sc.CACHE["agenda_sources"] = {"base": [
            {"title": "Too old", "source": "Kids", "start": "2026-10-01", "end": "2026-10-02", "all_day": True},
            {"title": "Last week", "source": "Kids", "start": "2026-11-10", "end": "2026-11-11", "all_day": True},
            {"title": "Next week", "source": "Family", "start": "2026-11-25", "end": "2026-11-26", "all_day": True},
        ]}
        rows = self.sc.agenda(now)
        self.assertEqual([(x["title"], x["source"]) for x in rows], [("Last week", "Kids"), ("Next week", "Family")])


class MustDoFirst(Case):
    def test_thursday_homework_blocks_chores_then_only_rewards_until_reported(self):
        c = self.s.cfg()
        c["chores"].append({"id": "homework", "name": "Homework", "icon": "1f4da", "points": 5, "only": ["zoe"],
                            "weekly": {"open_dow": 3, "due_dow": 6},
                            "must_do_first": {"weekday": 3, "after_arrival": True, "until": "19:30"}})
        self.s.save_cfg(c)
        before = datetime(2026, 11, 19, 16, 0)
        gate = self.s.must_do_first_state(c, {"homework": {"last_clean": 0, "pending_by": []}}, before,
                                          {"zoe": {"2026-11-19": "15:30"}})["zoe"]
        self.assertTrue(gate["on"]); self.assertTrue(gate["rewards"])
        after = self.s.must_do_first_state(c, {"homework": {"last_clean": 0, "pending_by": []}},
                                           datetime(2026, 11, 19, 19, 30), {"zoe": {"2026-11-19": "15:30"}})["zoe"]
        self.assertFalse(after["on"]); self.assertTrue(after["rewards"])
        done = self.s.must_do_first_state(c, {"homework": {"last_clean": 0, "pending_by": ["zoe"]}}, before,
                                          {"zoe": {"2026-11-19": "15:30"}})["zoe"]
        self.assertFalse(done["on"]); self.assertFalse(done["rewards"])


class ChoresFirst(Case):
    def test_rewards_blocked_until_the_clean_up_passes_inspection(self):
        a = App(base_config())
        a.event(type="adjust", kid="ava", points=20, reason="test")
        a.event(type="adjust", kid="max", points=20, reason="test")
        job = lambda: next(x for x in a.state()["config"]["chores"] if x["id"] == "cleanup_ava")
        self.assertEqual(job().get("closed"), "fine")   # nothing to clean up: no job
        self.assertEqual(a.post("/api/start", {"kid": "ava", "chore": "cleanup_ava"})[0], 409)
        self.assertEqual(a.post("/api/chores_first", {"kid": "ava", "on": True})[0], 400)   # grown-ups only
        code, r = a.post("/api/chores_first", {"kid": "ava", "on": True, "by": "jordan", "items": ["Legos off the floor", " ", "Shoes away"]})
        self.assertEqual(code, 200, r)
        st = a.state()
        self.assertEqual((st["chores_first"]["ava"]["on"], st["chores_first"]["max"]["on"]), (True, False))   # one kid at a time
        self.assertEqual(st["chores_first"]["ava"]["items"], ["Legos off the floor", "Shoes away"])
        self.assertIsNone(job().get("closed"))
        self.assertEqual(st["board"]["cleanup_ava"]["note"], "Legos off the floor\nShoes away")
        self.assertEqual(a.post("/api/spend", {"kid": "ava", "reward": "show"})[0], 409)
        self.assertEqual(a.post("/api/spend", {"kid": "max", "reward": "show", "with": ["ava"]})[0], 409)   # not by watching along
        # not yet: still blocked, back to the kid
        rep = a.post("/api/report", {"kid": "ava", "chore": "cleanup_ava", "feeling": "ok"})[1]["event"]
        self.assertEqual(a.post("/api/inspect", {"ref": rep["id"], "by": "jordan", "ok": False, "feedback": "shoes"})[0], 200)
        self.assertTrue(a.state()["chores_first"]["ava"]["on"])
        rep = a.post("/api/report", {"kid": "ava", "chore": "cleanup_ava", "feeling": "ok"})[1]["event"]
        self.assertEqual(a.post("/api/inspect", {"ref": rep["id"], "by": "jordan", "ok": True, "points": 0})[0], 200)
        st = a.state()
        self.assertFalse(st["chores_first"]["ava"]["on"])
        self.assertEqual(st["chores_first"]["ava"]["lifted"]["by"], "jordan")
        self.assertEqual(job().get("closed"), "fine")
        self.assertEqual(a.post("/api/spend", {"kid": "ava", "reward": "show"})[0], 200)
        # blocking again later starts fresh (the old pass doesn't count)
        a.post("/api/chores_first", {"kid": "ava", "on": True, "by": "jordan", "items": ["Dishes"]})
        self.assertTrue(a.state()["chores_first"]["ava"]["on"])

    def test_spotify_switch_is_armed_then_effective_only_while_required_work_remains(self):
        a = App(base_config())
        self.assertEqual(a.state()["music_block"], {"enabled": False, "effective": False, "by": None,
                                                    "since": None, "blockers": []})
        self.assertEqual(a.post("/api/music_block", {"on": True, "by": "ava"})[0], 400)
        self.assertEqual(a.post("/api/music_block", {"on": True, "by": "jordan"})[0], 200)
        self.assertTrue(a.state()["music_block"]["enabled"])
        self.assertFalse(a.state()["music_block"]["effective"])
        a.post("/api/chores_first", {"kid": "ava", "on": True, "by": "jordan", "items": ["Dishes"]})
        block = a.state()["music_block"]
        self.assertTrue(block["effective"])
        self.assertEqual(block["blockers"], [{"kid": "ava", "kind": "cleanup", "name": "Mandatory chores",
                                               "items": ["Dishes"]}])
        self.assertEqual(a.post("/api/music_block", {"on": False, "by": "jordan"})[0], 200)
        self.assertFalse(a.state()["music_block"]["effective"])

    def test_spotify_block_includes_scheduled_mandatory_jobs(self):
        state = self.s.music_block_state(
            [{"id": "1", "type": "music_block", "on": True, "by": "jordan", "ts": 10}],
            {"ava": {"on": False}},
            {"ava": {"rewards": True, "name": "Homework", "chore": "homework"}},
        )
        self.assertTrue(state["effective"])
        self.assertEqual(state["blockers"][0]["name"], "Homework")


class GrownUpStartsVR(Case):
    def test_vr_is_paid_then_a_grown_up_starts_the_timer_or_refunds(self):
        c = base_config()
        for r in c["rewards"]:
            if r["id"] == "vr":
                r["needs_ok"] = True
        a = App(c)
        a.event(type="adjust", kid="ava", points=10, reason="test")
        code, r = a.post("/api/spend", {"kid": "ava", "reward": "vr"})
        self.assertEqual(code, 200, r)
        self.assertTrue(r["event"]["await_ok"])
        st = a.state()
        self.assertEqual((st["balance"]["ava"], st["timers"], len(st["requests"])), (8, [], 1))   # paid, not started
        self.assertEqual(a.post("/api/spend", {"kid": "ava", "reward": "vr"})[0], 409)   # bought already
        self.assertEqual(a.post("/api/request_answer", {"ref": r["event"]["id"], "by": "jordan", "ok": True})[0], 200)
        st = a.state()
        self.assertEqual((st["balance"]["ava"], len(st["timers"]), st["requests"]), (8, 1, []))
        self.assertGreaterEqual(st["timers"][0]["start"], r["event"]["ts"])   # the clock starts at the grown-up's go
        self.assertEqual(a.post("/api/timer_done", {"ref": r["event"]["id"]})[0], 400)   # grown-ups only
        self.assertEqual(a.post("/api/timer_done", {"ref": r["event"]["id"], "by": "jordan"})[0], 200)
        self.assertEqual(a.state()["timers"], [])
        # another kid's, answered "not now": refunded, and a note waits on their screen
        a.event(type="adjust", kid="max", points=5, reason="test")
        code, r2 = a.post("/api/spend", {"kid": "max", "reward": "vr"})
        self.assertEqual(a.state()["balance"]["max"], 3)
        self.assertEqual(a.post("/api/request_answer", {"ref": r2["event"]["id"], "by": "jordan", "ok": False})[0], 200)
        st = a.state()
        self.assertEqual((st["balance"]["max"], [t for t in st["timers"] if t["kid"] == "max"]), (5, []))
        note = st["kid_notes"]["max"][0]
        self.assertIn("Not right now", note["text"])
        self.assertEqual(a.post("/api/kid_note_seen", {"kid": "max", "ref": note["id"]})[0], 200)
        self.assertEqual(a.state()["kid_notes"].get("max"), None)

    def test_kid_can_refund_own_reward_for_exactly_two_minutes(self):
        a = App(base_config())
        a.event(type="adjust", kid="ava", points=20, reason="test")
        bought = a.post("/api/spend", {"kid": "ava", "reward": "show", "tv": "living"})[1]["event"]
        self.assertEqual(a.state()["balance"]["ava"], 15)
        self.assertEqual(a.state()["refundable"]["ava"][0]["id"], bought["id"])
        self.assertEqual(a.post("/api/refund", {"kid": "max", "ref": bought["id"]})[0], 400)
        code, r = a.post("/api/refund", {"kid": "ava", "ref": bought["id"]})
        self.assertEqual(code, 200, r)
        self.assertEqual((r["state"]["balance"]["ava"], r["state"]["refundable"]["ava"]), (20, []))
        old = a.event(type="spend", kid="ava", reward="show", cost=5, tv="living", ts=time.time() - 121)
        self.assertEqual(a.post("/api/refund", {"kid": "ava", "ref": old["id"]})[0], 409)

    def test_announcements_say_it_twice(self):
        s = self.app.s
        s.ANNOUNCE_TTS, s.ANNOUNCE_SPEAKER, s.ANNOUNCE_MEDIA_BASE = "tts.x", "media_player.x", None
        self.app.orig["announce"]("Ava, VR time is up.", "tok", repeat=2)
        said = [d for svc, d in self.app.calls if svc == "tts.speak"]
        self.assertEqual(said[-1]["message"], "Ava, VR time is up. Ava, VR time is up.")


class PrankInspection(Case):
    def test_nothing_done_costs_a_star_and_is_not_handed_back(self):
        a = App(base_config())
        a.event(type="adjust", kid="ava", points=5, reason="test")
        rep = a.post("/api/report", {"kid": "ava", "chore": "kitchen", "feeling": "ok"})[1]["event"]
        self.assertEqual(a.post("/api/inspect", {"ref": rep["id"], "by": "jordan", "prank": True, "ok": True})[0], 200)
        st = a.state()
        r = next(h for h in st["history"] if h.get("id") == rep["id"])
        self.assertEqual((r["status"], st["balance"]["ava"], st["active"]), ("rejected", 4, {}))
        self.assertIn("against the rules", r["feedback"])


class SoloJobs(Case):
    def test_no_team_on_a_personal_job(self):
        c = base_config()
        c["chores"].append({"id": "teeth", "name": "Brush teeth", "icon": "1faa5", "points": 1, "cooldown_h": 0, "each_daily": True})
        a = App(c)
        self.assertEqual(a.post("/api/team", {"kid": "ava", "chore": "teeth", "partners": ["max"]})[0], 409)
        self.assertEqual(a.post("/api/team", {"kid": "ava", "chore": "kitchen", "partners": ["max"]})[0], 200)


class WindDown(Case):
    def test_no_rewards_after_the_evening_cutoff(self):
        c = base_config()
        c["rewards_until"] = "00:00"   # always past it
        a = App(c)
        a.event(type="adjust", kid="ava", points=20, reason="test")
        self.assertTrue(a.state()["wind_down"]["on"])
        code, r = a.post("/api/spend", {"kid": "ava", "reward": "games"})
        self.assertEqual(code, 409)
        self.assertIn("bedtime", r["error"])
        self.assertEqual(a.post("/api/report", {"kid": "ava", "chore": "other", "feeling": "ok"})[0], 200)   # jobs still fine

    def test_off_without_a_cutoff(self):
        self.assertFalse(App(base_config()).state()["wind_down"]["on"])


class NoRecordForSomethingElse(Case):
    def test_something_else_sets_no_time_record(self):
        a = App(base_config())
        r = a.event(type="report", kid="ava", chore="other", feeling=None, points=2, started=time.time() - 120)
        self.assertEqual(a.post("/api/inspect", {"ref": r["id"], "by": "jordan", "ok": True, "points": 2})[0], 200)
        st = a.state()
        self.assertNotIn("other", st["records"])
        self.assertFalse([x for x in st["awards"].get("ava", []) if x["id"].startswith("record:")])
        self.assertNotIn("other", (st["personal_best"] or {}).get("ava", {}))


class WatchingTogether(Case):
    def test_a_movie_together_costs_less_each(self):
        c = base_config()
        for r in c["rewards"]:
            if r["id"] == "movie":
                r["together_cost"] = {"2": 12, "3": 10}
        a = App(c)
        for k in ("ava", "max", "zoe"):
            a.event(type="adjust", kid=k, points=12, reason="test")
        self.assertEqual(a.post("/api/spend", {"kid": "ava", "reward": "movie"})[0], 409)   # alone: 15
        code, r = a.post("/api/spend", {"kid": "ava", "reward": "movie", "with": ["max"]})
        self.assertEqual(code, 200, r)
        b = a.state()["balance"]
        self.assertEqual((b["ava"], b["max"], b["zoe"]), (0, 0, 12))
        self.assertEqual(a.s.reward_cost(c["rewards"][1], 3), 10)
        self.assertEqual(a.s.reward_cost(c["rewards"][1], 4), 10)
        self.assertEqual(a.s.reward_cost(c["rewards"][1], 1), 15)


class AbandonedJob(Case):
    def test_grown_up_stops_a_forgotten_team_job(self):
        a = App(base_config())
        self.assertEqual(a.post("/api/start", {"kid": "ava", "chore": "other"})[0], 200)
        self.assertEqual(a.post("/api/team", {"kid": "ava", "partners": ["max"]})[0], 200)
        self.assertEqual(sorted(a.state()["active"]), ["ava", "max"])
        self.assertEqual(a.post("/api/abandon", {"kid": "ava"})[0], 400)   # grown-ups only
        code, r = a.post("/api/abandon", {"kid": "ava", "by": "jordan"})
        self.assertEqual(code, 200, r)
        st = a.state()
        self.assertEqual((st["active"], st["paused"], st["balance"]["ava"]), ({}, {}, 0))

    def test_grown_up_cancels_a_job_saved_for_later(self):
        a = App(base_config())
        self.assertEqual(a.post("/api/start", {"kid": "ava", "chore": "other"})[0], 200)
        self.assertEqual(a.post("/api/pause", {"kid": "ava"})[0], 200)
        self.assertEqual(a.state()["paused"]["ava"]["chore"], "other")
        code, r = a.post("/api/abandon", {"kid": "ava", "chore": "other", "paused": True, "by": "jordan"})
        self.assertEqual(code, 200, r)
        self.assertEqual(a.state()["paused"], {})


class CertifiedSlot(Case):
    """Church reading: kids claim a Sunday; a grown-up confirms it after the service; weeks away are skipped."""

    @staticmethod
    def config():
        from datetime import datetime, timedelta
        c = base_config()
        y = datetime.now() - timedelta(days=1)
        c["chores"].append({"id": "church", "name": "Read at church", "icon": "26ea", "points": 3, "cooldown_h": 0,
                            "slot": {"dow": y.weekday(), "time": "11:00", "certify_after": "12:00"}})
        return c

    def setUp(self):
        self.app = App(self.config())

    def last_service(self):
        from datetime import datetime, timedelta
        return (datetime.now() - timedelta(days=1)).replace(hour=11, minute=0, second=0, microsecond=0).timestamp()

    def test_grown_up_certifies_after_the_service(self):
        a = self.app
        a.event(type="claim", kid="ava", chore="church", slot=self.last_service())
        b = a.state()["board"]["church"]
        self.assertEqual((b["slot"], b["claimed_by"], b["certified"]), (self.last_service(), "ava", None))
        code, r = a.post("/api/certify", {"chore": "church", "by": "jordan", "ok": True})
        self.assertEqual(code, 200, r)
        st = a.state()
        self.assertEqual(st["balance"]["ava"], 3)
        self.assertGreater(st["board"]["church"]["slot"], time.time())   # on to the next service
        self.assertIsNone(st["board"]["church"]["claimed_by"])

    def test_it_didnt_happen(self):
        a = self.app
        a.event(type="claim", kid="ava", chore="church", slot=self.last_service())
        self.assertEqual(a.post("/api/certify", {"chore": "church", "by": "jordan", "ok": False})[0], 200)
        st = a.state()
        self.assertEqual(st["balance"]["ava"], 0)
        self.assertGreater(st["board"]["church"]["slot"], time.time())

    def test_kid_says_they_did_it_and_the_grown_up_inspects(self):
        a = self.app
        a.event(type="claim", kid="ava", chore="church", slot=self.last_service())
        code, r = a.post("/api/report", {"kid": "ava", "chore": "church"})
        self.assertEqual(code, 200, r)
        b = a.state()["board"]["church"]
        self.assertEqual((b["slot"], b["claim_done"]), (self.last_service(), True))   # waits for the grown-up, no certify card
        self.assertEqual(a.post("/api/inspect", {"ref": r["event"]["id"], "by": "jordan", "ok": True, "points": 3})[0], 200)
        st = a.state()
        self.assertEqual(st["balance"]["ava"], 3)
        self.assertGreater(st["board"]["church"]["slot"], time.time())

    def test_kids_see_and_claim_it_only_a_few_days_ahead(self):
        from datetime import datetime, timedelta
        c = base_config()
        c["chores"].append({"id": "church", "name": "Read at church", "icon": "26ea", "points": 3, "cooldown_h": 0,
                            "slot": {"dow": (datetime.now() + timedelta(days=6)).weekday(), "time": "11:00",
                                     "claim_days": 4}})
        a = App(c)
        church = lambda: next(x for x in a.state()["config"]["chores"] if x["id"] == "church")
        self.assertEqual(a.state()["board"]["church"]["days_until"], 6)
        self.assertEqual(church().get("closed"), "soon")
        self.assertEqual(a.post("/api/claim", {"kid": "ava", "chore": "church"})[0], 409)
        a.event(type="claim", kid="ava", chore="church", slot=a.state()["board"]["church"]["slot"])   # held from before
        self.assertIsNone(church().get("closed"))   # the kid holding it still sees it
        self.assertEqual(a.post("/api/claim", {"kid": "ava", "chore": "church", "release": True})[0], 200)
        self.assertEqual(church().get("closed"), "soon")

    def test_every_other_week_and_the_calendar_wins(self):
        from datetime import datetime, timedelta
        s = self.app.s
        ch = {"slot": {"dow": 6, "time": "11:00", "every_weeks": 2, "anchor": "2026-11-15"}}
        from datetime import date
        self.assertTrue(s.slot_on(ch, date(2026, 11, 29)))
        self.assertFalse(s.slot_on(ch, date(2026, 11, 22)))
        s.schedule.CACHE["days"]["2026-11-22"] = True    # a holiday weekend with the kids
        s.schedule.CACHE["days"]["2026-12-27"] = False   # and one away that the rhythm would have kept
        self.assertTrue(s.slot_on(ch, date(2026, 11, 22)))
        self.assertFalse(s.slot_on(ch, date(2026, 12, 27)))
        nxt = datetime.fromtimestamp(s.next_slot(ch, after=datetime(2026, 12, 21).timestamp())).date()
        self.assertEqual(nxt, date(2027, 1, 10))   # skips Dec 27 (calendar) and Jan 3 (off week, no calendar data)


class LetterFixes(Case):
    """Keep working (not a fail) and changing an award after passing it."""

    def team_report(self):
        a = self.app
        a.post("/api/start", {"kid": "max", "chore": "organize"})
        a.post("/api/team", {"kid": "max", "chore": "organize", "partners": ["ava"]})
        code, r = a.post("/api/report", {"kid": "max", "chore": "organize"})
        self.assertEqual(code, 200, r)
        return r["event"]

    def test_keep_working_goes_back_in_progress_with_the_same_team(self):
        ev = self.team_report()
        code, r = self.app.post("/api/inspect", {"ref": ev["id"], "by": "jordan", "ok": False, "keep": True, "points": 0,
                                                 "feedback": "Fold it in"})
        self.assertEqual(code, 200, r)
        st = self.app.state()
        rep = next(h for h in st["history"] if h.get("id") == ev["id"])
        self.assertEqual(rep["status"], "returned")
        self.assertEqual(sorted(st["active"]["max"]["team"]), ["ava", "max"])
        self.assertTrue(st["active"]["ava"].get("keep"))
        self.assertEqual(st["active"]["max"]["round"], 1)   # not a new "try again" round

    def test_change_an_award_after_passing(self):
        ev = self.team_report()
        self.assertEqual(self.app.post("/api/inspect", {"ref": ev["id"], "by": "jordan", "ok": True, "points": 2})[0], 200)
        self.assertEqual(self.app.state()["balance"], {"ava": 1, "max": 1, "zoe": 0})
        code, r = self.app.post("/api/reaward", {"ref": ev["id"], "by": "jordan", "award": {"max": 2, "ava": 2}})
        self.assertEqual(code, 200, r)
        st = self.app.state()
        self.assertEqual((st["balance"]["max"], st["balance"]["ava"]), (2, 2))
        rep = next(h for h in st["history"] if h.get("id") == ev["id"])
        self.assertEqual(rep["awarded"], 4)


if __name__ == "__main__":
    unittest.main()
