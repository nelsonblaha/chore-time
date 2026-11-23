"""Chore Time regression tests: each case comes from something that went wrong in real family use.

Runs the real server module against a throwaway data dir, with Home Assistant / Grocy / notifications faked.
    python3 -m unittest discover -s tests -v        (from the repo root)
Nothing here talks to the network or the live ledger.
"""
import importlib
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
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
        s.ha_fire_event = lambda event, data, tok: self.calls.append(("event." + event, data))
        s.ha_post_state = lambda *a, **k: None
        self.orig = {n: getattr(s, n) for n in ("notify_report", "notify_inspecting", "announce", "push_state_sensor",
                                                "send_adult_request") if hasattr(s, n)}
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

    def post(self, path, body, headers=None):
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers=request_headers)
        try:
            r = urllib.request.urlopen(req, timeout=10)
            return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def post_from(self, ip, path, body, headers=None):
        """Exercise display-bound APIs from two distinct loopback addresses."""
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=10,
                                          source_address=(ip, 0))
        conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json", **(headers or {})})
        response = conn.getresponse()
        payload = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, payload

    def get(self, path, headers=None, source_ip=None):
        if source_ip:
            conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=10,
                                              source_address=(source_ip, 0))
            conn.request("GET", path, headers=headers or {})
            response = conn.getresponse()
            payload = json.loads(response.read() or b"{}")
            conn.close()
            return response.status, payload
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            response = urllib.request.urlopen(req, timeout=10)
            return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def state(self):
        return json.loads(urllib.request.urlopen(self.base + "/api/state", timeout=10).read())

    def event(self, **ev):
        return self.s.append(ev)

    def kid_sessions(self, *kids, pins=None):
        """Fresh, same-display identity tokens for shared reward tests."""
        out = {}
        for kid in kids:
            code, reply = self.post("/api/kid_unlock", {"kid": kid, **({"pin": pins[kid]} if pins else {})})
            assert code == 200, (code, reply)
            out[kid] = reply["kid_session"]
        return out

    def pass_report(self, kid, chore, points=3, **extra):
        code, r = self.post("/api/report", {"kid": kid, "chore": chore, "feeling": "ok", **extra})
        assert code == 200, (code, r)
        code, r2 = self.post("/api/inspect", {"ref": r["event"]["id"], "by": "jordan", "ok": True, "points": points})
        assert code == 200, (code, r2)
        return r["event"]


class CoinFlipEligibilityTests(unittest.TestCase):
    def test_room_pool_requires_fresh_unambiguous_locations_for_every_kid_home(self):
        app = App()
        self.addCleanup(app.close)
        settings = {"max_age_seconds": 120, "min_probability": .8,
                    "room_aliases": {"den_hub": "den"}}
        now = 1000
        display = {"kind": "hub", "id": "den", "room": "den_hub"}
        tracked = {"attributes": {"observed_at": 990, "valid_until": 1060, "people": [
            {"person_key": "ava", "confirmed": True, "located": True,
             "rooms": [{"room": "den", "probability": 1, "valid_until": 1060}]},
            {"person_key": "max", "confirmed": True, "located": True,
             "rooms": [{"room": "kitchen", "probability": 1, "valid_until": 1060}]},
        ]}}
        room = app.s.coin_flip_state(base_config(), ["ava", "max"], display, tracked, now, settings)
        self.assertEqual(room, {"kids": ["ava"], "scope": "room", "room": "den_hub"})

        tracked["attributes"]["people"][1]["located"] = False
        unknown = app.s.coin_flip_state(base_config(), ["ava", "max"], display, tracked, now, settings)
        self.assertEqual(unknown, {"kids": ["ava", "max"], "scope": "home"})

        tracked["attributes"]["people"][1]["located"] = True
        tracked["attributes"]["observed_at"] = 800
        stale = app.s.coin_flip_state(base_config(), ["ava", "max"], display, tracked, now, settings)
        self.assertEqual(stale, {"kids": ["ava", "max"], "scope": "home"})


class StaticUiRegressions(unittest.TestCase):
    """Small wiring checks for browser-only failures the fake DOM cannot click through."""

    def test_nested_inspection_card_does_not_rebind_full_job_without_a_report_id(self):
        html = (REPO / "static" / "inspect.html").read_text()
        self.assertIn("'#pending > .card:not([data-swap]):not([data-dr]):not([data-hub-placement])'", html)
        self.assertNotIn("'#pending .card:not([data-swap]):not([data-dr])'", html)

    def test_two_hub_reset_ui_requires_a_continuous_helper_and_parent_placement_review(self):
        kids = (REPO / "static" / "kids.html").read_text()
        grown = (REPO / "static" / "inspect.html").read_text()
        self.assertIn("Ask another kid on the other Nest Hub", kids)
        self.assertIn("S.peer_pin_reset_capable?", kids)
        self.assertIn("HOLD TO HELP", kids)
        self.assertIn("onpointerleave=stop", kids)
        self.assertIn("The helper cannot see this screen", kids)
        self.assertIn("I physically checked both Hubs are in their proper rooms", grown)
        self.assertIn("/api/hub_placement_attest", grown)

    def test_physical_hub_heartbeat_reports_and_immediately_clears_the_foreground_child(self):
        kids = (REPO / "static" / "kids.html").read_text()
        self.assertIn('api("/api/hub_heartbeat",active?{kid:kid,kid_session:active}:{kid:null})', kids)
        self.assertIn("renderKid();poke();hubHeartbeat()", kids)
        self.assertIn('$("pick").classList.add("on");render();hubHeartbeat()', kids)
        self.assertIn("if(HA_MODE||SANDBOX!=null", kids)
        self.assertIn('leaveHubForeground(function(){api("/api/exit",{})})', kids)
        self.assertIn('leaveHubForeground(function(){location.href=adultPage', kids)

    def test_real_kid_packing_is_one_job_at_a_time_and_parent_inspection_is_separate(self):
        kids = (REPO / "static" / "kids.html").read_text()
        grown = (REPO / "static" / "packing.html").read_text()
        inspect = (REPO / "static" / "inspect.html").read_text()
        self.assertIn('$("packreset").style.display=kid==="packing_test"?"":"none"', kids)
        self.assertIn("Family packing progress</summary>", kids)
        self.assertIn("I remembered something — keep laying out", kids)
        self.assertIn("Waiting for grown-up inspection", kids)
        self.assertIn("data-present", grown)
        self.assertIn("data-final-present", grown)
        self.assertIn('ready+checked+packed', grown)
        self.assertIn('laid out":phase==="inspect"', grown)
        self.assertIn("data-openitem", grown)
        self.assertIn('mark:{item:item,value:value}', grown)
        self.assertIn('stage:stage', grown)
        self.assertIn("Inspection passed", grown)
        self.assertIn('if(packWasOpen&&PACK)packRender()', kids)
        self.assertIn('q.push("v="+Date.now())', inspect)

    def test_mandatory_editor_searches_rooms_and_uses_room_backed_api(self):
        inspect = (REPO / "static" / "inspect.html").read_text()
        self.assertIn('id="cfroomresults"', inspect)
        self.assertIn("Object.keys(S.tidy_spaces||{})", inspect)
        self.assertIn('post("/api/space_tidy",{space:CFSPACE,tidy:false,block_kids:[CFK],items:its}', inspect)
        self.assertIn("clear_room:!!(CFSPACE0&&!CFSPACE)", inspect)

    def test_tv_departure_timeline_replaces_half_the_jobs_grid(self):
        tv = (REPO / "static" / "tvlock.html").read_text()
        self.assertIn('id="trip"', tv)
        self.assertIn("drawDeparture(S.departure_display", tv)
        self.assertIn("J=openJobs(S,hasTrip?3:6)", tv)
        self.assertIn('"at "+n.name+" for "+left+" more "', tv)
        self.assertIn('left===1?"minute":"minutes"', tv)
        self.assertIn('n.name+" in "+', tv)
        self.assertIn('n.name+" "+Math.max(1,Math.round((now-dep)/60))+"m ago"', tv)
        self.assertIn('class="leave"', tv)
        self.assertIn('E("1f6b6")', tv)
        self.assertIn("T.board_label+' '+clockAt(T.board_at)", tv)
        self.assertIn('id="trainmap"', tv)
        self.assertIn('Math.floor(now/60)%2===1', tv)
        self.assertIn('fixed==="train"', tv)
        self.assertIn('fixed==="chores"', tv)
        self.assertIn('queryValue("embed")==="1"', tv)
        self.assertIn('stroke="#38a8ff"', tv)
        self.assertIn('stroke="#aeb5c4"', tv)
        self.assertIn('h+":"+("0"+m).slice(-2)+":"+("0"+s).slice(-2)', tv)
        self.assertTrue((REPO / "static" / "us48.json").is_file())

    def test_letter_stays_out_of_the_envelope_until_inspection(self):
        html = (REPO / "static" / "kids.html").read_text()
        self.assertIn("Keep your letter OUT of the envelope", html)
        self.assertIn("Keep the letter out of the envelope until a grown-up approves it", html)

    def test_shared_games_offer_split_players_or_one_player_with_watchers(self):
        html = (REPO / "static" / "kids.html").read_text()
        self.assertIn("function gameTogether", html)
        self.assertIn('start(parts,"player_watch",player)', html)
        self.assertIn("Two players", html)
        self.assertIn("watchers", html)

    def test_tv_reward_setup_names_the_viewer_choice_before_purchase(self):
        html = (REPO / "static" / "kids.html").read_text()
        self.assertIn("Choose TV &amp; viewers", html)
        self.assertIn("Choose the TV and everyone who will watch. No points are spent yet.", html)

    def test_shared_screen_rewards_require_every_pin_and_can_join_without_more_time(self):
        html = (REPO / "static" / "kids.html").read_text()
        self.assertIn("sharedRewardPins([kid].concat(ks)", html)
        self.assertIn('api("/api/reward_join"', html)
        self.assertIn("Joining adds no time.", html)
        self.assertIn("The original end time did not change.", html)
        self.assertIn('data-join="', html)

    def test_parent_can_pause_and_resume_saved_tv_time_and_lock_page_explains_it(self):
        grown = (REPO / "static" / "inspect.html").read_text()
        locked = (REPO / "static" / "tvlock.html").read_text()
        self.assertIn("/api/tv/reward_pause", grown)
        self.assertIn("/api/tv/reward_resume", grown)
        self.assertIn("Pause &amp; save", grown)
        self.assertIn("Your movie is paused.", locked)
        self.assertIn("You’ll get the rest of your movie time later.", locked)

    def test_kid_gets_easy_saved_movie_news_and_can_resume_or_share_it(self):
        kids = (REPO / "static" / "kids.html").read_text()
        self.assertIn("Your movie is waiting!", kids)
        self.assertIn("You can keep watching the movie from yesterday.", kids)
        self.assertIn("Watch now", kids)
        self.assertIn("savedRewardNews(k)", kids)
        self.assertIn('api("/api/tv/reward_resume_child"', kids)
        self.assertIn("sharedRewardPins(all", kids)
        self.assertIn("Watching now costs no more stars.", kids)

    def test_parent_chore_page_has_independent_spotify_volume_cap_slider(self):
        grown = (REPO / "static" / "inspect.html").read_text()
        self.assertIn('id="spotifybtn"', grown)
        self.assertIn('openPM("spotify")', grown)
        self.assertIn('<b>Spotify</b>', grown)
        self.assertIn('id="spotifycap" min="0" max="100" step="1"', grown)
        self.assertIn("Maximum Spotify volume", grown)
        self.assertIn("mandatory-chores wait switch are independent", grown)
        self.assertIn('post("/api/spotify_volume_cap",{cap:cap}', grown)

    def test_parent_mandatory_chore_controls_are_in_one_modal(self):
        grown = (REPO / "static" / "inspect.html").read_text()
        self.assertIn('id="mandatorybtn"', grown)
        self.assertIn('openPM("mandatory")', grown)
        self.assertIn('data-cfstart="', grown)
        self.assertIn('data-cfdone="', grown)
        self.assertIn('data-cflift="', grown)

    def test_paused_reward_lock_page_never_borrows_another_rooms_reward(self):
        locked = (REPO / "static" / "tvlock.html").read_text()
        helper = re.search(r"function rewardPauseFor\(S,search\)\{.*?\n\}", locked, re.S)
        self.assertIsNotNone(helper)
        script = helper.group(0) + r'''
const saved={session_id:"saved-here",remaining_s:123};
const state={display:{kind:"tv",id:"livingroom"},tv:{tvs:{
  livingroom:{}, kitchen:{paused_reward:saved}
}}};
function same(a,b,msg){if(JSON.stringify(a)!==JSON.stringify(b))throw Error(msg)}
same(rewardPauseFor(state,"?paused_reward=1&room=kitchen"),saved,"explicit kitchen room");
same(rewardPauseFor(state,"?paused_reward=1&room=livingroom"),null,"cleared local room");
same(rewardPauseFor(state,"?paused_reward=1"),null,"display room must not borrow kitchen");
state.tv.tvs.livingroom.paused_reward={session_id:"local"};
same(rewardPauseFor(state,"?paused_reward=1&room=livingroom"),{session_id:"local"},"local room");
delete state.tv.tvs.livingroom.paused_reward;
same(rewardPauseFor(state,"?paused_reward=1&room=livingroom"),null,"resumed local room");
const noDisplay={tv:state.tv};
same(rewardPauseFor(noDisplay,"?paused_reward=1&room=kitchen"),saved,"explicit room without display id");
same(rewardPauseFor(noDisplay,"?paused_reward=1"),null,"no unidentified fallback");
'''
        ran = subprocess.run(["node", "-e", script], text=True, capture_output=True)
        self.assertEqual(ran.returncode, 0, ran.stderr)

    def test_login_coin_flip_is_separate_visual_spoken_celebration(self):
        kids = (REPO / "static" / "kids.html").read_text()
        self.assertIn('id="coinflip"', kids)
        self.assertIn('"The winner is "+winner.name+"."', kids)
        self.assertIn("SpeechSynthesisUtterance", kids)
        self.assertIn("confetti(260);speakWinner", kids)

    def test_give_away_a_toy_is_an_earning_job_not_a_buy_button(self):
        html = (REPO / "static" / "kids.html").read_text()
        self.assertIn('if(c&&c.id==="toy_out"){toyGoodbye();return}', html)
        self.assertNotIn('id="toyout"', html)
        toy = next(c for c in json.loads((REPO / "example-config" / "chores.json").read_text())["chores"]
                   if c["id"] == "toy_out")
        self.assertEqual((toy["name"], toy.get("hidden")), ("Give away a toy", None))

    def test_reward_door_block_has_a_direct_kid_message(self):
        html = (REPO / "static" / "kids.html").read_text()
        self.assertIn("Close the downstairs toilet door", html)

    def test_school_dressing_is_framed_by_finish_time_not_a_start_button(self):
        html = (REPO / "static" / "kids.html").read_text()
        self.assertIn("Get dressed by the clock", html)
        self.assertIn("Your finish time counts", html)
        self.assertIn("Not finished yet", html)
        self.assertIn('$("mx").style.display="none"', html)
        self.assertNotIn("The clock starts when you press Start", html)

    def test_untimed_dressing_has_an_explicit_timerless_screen(self):
        html = (REPO / "static" / "kids.html").read_text()
        self.assertIn('function kidUntimed(k,chore)', html)
        self.assertIn('No timer—just get dressed and tap when you are ready.', html)
        self.assertIn('No personal timer. Your finish time still counts for the school-day award.', html)

    def test_parent_dashboard_has_an_explicit_mandatory_chore_done_action(self):
        html = (REPO / "static" / "inspect.html").read_text()
        self.assertIn("Finished — mark done", html)
        self.assertIn("done:true,items:F.items", html)

    def test_paper_shelves_and_local_previews_are_wired_into_the_kid_menu(self):
        kids = (REPO / "static" / "kids.html").read_text()
        inspect = (REPO / "static" / "inspect.html").read_text()
        self.assertIn("School Papers", kids)
        self.assertIn("Personal Papers", kids)
        self.assertIn("paper_scan_start", kids)
        self.assertIn("data-doc-pages", inspect)
        self.assertNotIn("drive.google.com/file/d/\"+encodeURIComponent", inspect)

    def test_forgotten_pin_waits_on_screen_and_advances_after_approval(self):
        html = (REPO / "static" / "kids.html").read_text()
        self.assertIn('id="pinresetwait"', html)
        self.assertIn('api("/api/adult_status",{nonce:r.nonce})', html)
        self.assertIn('changeKidPin(true,k)', html)

    def test_pending_non_room_chore_does_not_run_the_completion_flow_twice(self):
        html = (REPO / "static" / "kids.html").read_text()
        self.assertIn("if(mineWaiting&&!envelopeWaiting)", html)
        self.assertIn("You do not need to do it again.", html)

    def test_hand_back_includes_an_unadded_reason_draft(self):
        html = (REPO / "static" / "inspect.html").read_text()
        self.assertIn('feedback:listValue("nyfb")', html)
        self.assertIn("if(draft)all.push(draft)", html)

    def test_stepped_project_ui_keeps_safety_and_awards_explicit(self):
        kids = (REPO / "static" / "kids.html").read_text()
        grown = (REPO / "static" / "inspect.html").read_text()
        self.assertIn("Stop safely for now", kids)
        self.assertIn('if(c.stepped){projectModal(c);return}', kids)
        self.assertIn('data-project-open', kids)
        self.assertNotIn('function renderProjects(){var rows=', kids)
        self.assertIn("No sensor verifies this", grown)
        self.assertIn("No amount is suggested", grown)
        self.assertIn('data-paward="', grown)
        self.assertIn('data-pemergency="', grown)

    def test_packing_preview_can_show_child_specific_items_without_leaking_them_to_the_other_child(self):
        kids = (REPO / "static" / "kids.html").read_text()
        self.assertIn("function packPacker(p)", kids)
        self.assertIn("x.name_for&&x.name_for[who]", kids)
        self.assertIn("x.icon_for&&x.icon_for[who]", kids)
        self.assertIn("!x.only||x.only.indexOf(who)>=0", kids)
        self.assertIn('data-ppacker="', kids)
        self.assertIn("function packFamilyProgress(c)", kids)
        self.assertIn("S.config.inspectors", kids)
        grown = (REPO / "static" / "inspect.html").read_text()
        self.assertIn("Item-by-item progress", grown)
        self.assertIn("last actual packing", grown)
        self.assertIn('id="packadminview"', grown)
        self.assertIn("openPackingWorkspace", grown)
        packing = (REPO / "static" / "packing.html").read_text()
        self.assertIn("Family packing", packing)
        self.assertIn('api("/api/packing_admin"', packing)
        self.assertIn('data-present="', packing)
        self.assertIn('data-missing="', packing)
        self.assertIn('data-complete-stage="1"', packing)
        self.assertIn('stageProgress(id,"complete_stage")', packing)
        self.assertIn("one tap completes only this stage", packing)
        self.assertIn("function finalWindow(x)", packing)
        self.assertIn("Waiting for Travel's live leave-home time", packing)

    def test_all_saved_jobs_are_rendered_without_an_expiry_button(self):
        kids = (REPO / "static" / "kids.html").read_text()
        grown = (REPO / "static" / "inspect.html").read_text()
        self.assertIn("S.paused_all||[]", kids)
        self.assertIn("S.paused_all||[]", grown)
        self.assertIn("held until completed or explicitly ended", grown)
        self.assertNotIn("Keep 7 more days", grown)


class DepartureDisplayTests(unittest.TestCase):
    def setUp(self):
        self.app = App()
        self.addCleanup(self.app.close)
        self.path = self.app.dir / "departure.json"
        self.app.s.HOUSE["departure_display_file"] = str(self.path)

    def write(self, **changes):
        value = {"schema": 1, "active": True, "updated_at": 9990, "trip_id": "trip-1", "mode": "train",
                 "title": "Train to River City", "wake_at": 9000, "board_at": 12000,
                 "pull_in_at": 11700, "leave_home_at": 9000,
                 "map_route": {"traveled": [{"lat": 34, "lon": -118}, {"lat": 33, "lon": -112}],
                               "remaining": [{"lat": 33, "lon": -112}, {"lat": 31, "lon": -106}],
                               "train": {"lat": 33, "lon": -112},
                               "home": {"lat": 31.75, "lon": -106.5, "label": "Home station"}},
                 "board_label": "Board downtown", "nearest": {"name": "West Station",
                 "arrival_at": 10600, "departure_at": 10660}}
        value.update(changes)
        self.path.write_text(json.dumps(value))

    def test_fresh_travel_snapshot_is_published_for_the_tv(self):
        self.write(travelers=["ben", "ava", "BEN", "bad person", ""])
        out = self.app.s.departure_display(now=10000)
        self.assertEqual(out["mode"], "train")
        self.assertEqual(out["travelers"], ["ben", "ava"])
        self.assertEqual(out["nearest"], {"name": "West Station", "arrival_at": 10600.0,
                                          "departure_at": 10660.0, "at_now": False})
        self.assertEqual(out["pull_in_at"], 11700.0)
        self.assertEqual(out["leave_home_at"], 9000.0)
        self.assertEqual(out["map_route"]["home"], {"lat": 31.75, "lon": -106.5,
                                                      "label": "Home station"})

    def test_walking_milestone_must_be_45_minutes_before_pull_in_not_departure(self):
        self.write(pull_in_at=11500, leave_home_at=9000)
        out = self.app.s.departure_display(now=10000)
        self.assertNotIn("pull_in_at", out)
        self.assertNotIn("leave_home_at", out)

    def test_bad_optional_map_does_not_hide_the_departure_timeline(self):
        self.write(map_route={"traveled": [{"lat": 34, "lon": -118}]})
        out = self.app.s.departure_display(now=10000)
        self.assertIsNotNone(out)
        self.assertNotIn("map_route", out)

    def test_missing_or_invalid_travelers_does_not_guess_who_is_boarding(self):
        self.write(travelers="ben")
        self.assertNotIn("travelers", self.app.s.departure_display(now=10000))
        self.write(travelers=["bad person", "!"])
        self.assertNotIn("travelers", self.app.s.departure_display(now=10000))

    def test_stale_inactive_or_invalid_snapshot_fails_closed(self):
        self.write(updated_at=7000)
        self.assertIsNone(self.app.s.departure_display(now=10000))
        self.write(active=False)
        self.assertIsNone(self.app.s.departure_display(now=10000))
        self.write(wake_at=13000)
        self.assertIsNone(self.app.s.departure_display(now=10000))

    def test_trip_details_are_not_added_to_public_demo_state(self):
        self.assertNotIn("departure_display", self.app.s.demo_attrs({"departure_display": {"title": "Private trip"}}))


class PackingRewardGateTests(unittest.TestCase):
    def setUp(self):
        self.app = App()
        self.addCleanup(self.app.close)
        self.app.s.HA["user_adults"] = {"jordan-ha": "jordan"}
        self.adult_headers = {"X-HA-User": "jordan-ha"}
        self.app.s.HOUSE["packing_game"] = {
            "version": "trip-test-v1", "activated": True, "required_before_rewards": True,
            "required_before_other_chores": True,
            "title": "Train packing", "icon": "1f9f3", "award_reason": "Finished train packing",
            "profiles": [{"id": "kid", "kids": ["ava", "max"], "points": 10,
                          "awards": {"layout": 1, "inspect": 2, "pack": 2, "complete": 5},
                          "piles": [{"id": "bag", "items": [
                              {"id": "shirt", "name": "Shirt"},
                              {"id": "hair", "name": "Hair things", "only": ["ava"]}]}],
                          "final_checks": [{"id": "zip", "name": "Bag zipped"}]},
                         {"id": "adult", "packers": ["jordan"], "points": 0,
                          "piles": [{"id": "grownup_bag", "items": [
                              {"id": "charger", "name": "Charger"}]}],
                          "final_checks": [{"id": "adult_zip", "name": "Bag zipped"}]}]}

    @staticmethod
    def complete(items, seq=10):
        return {"version": "trip-test-v1", "profile": "kid", "phase": "complete", "seq": seq,
                "ready": {x: True for x in items}, "checked": {x: "pass" for x in items},
                "packed": {x: True for x in items}, "finals": {"zip": "pass"}}

    @staticmethod
    def stage(items, phase, seq):
        phases = ["layout", "inspect", "pack", "final", "complete"]
        at = phases.index(phase)
        return {"version": "trip-test-v1", "profile": "kid", "phase": phase, "seq": seq,
                "ready": {x: True for x in items},
                "checked": {x: "pass" for x in items} if at >= 2 else {},
                "packed": {x: True for x in items} if at >= 3 else {},
                "finals": {"zip": "pass"} if at >= 4 else {}}

    def test_packing_blocks_rewards_and_ordinary_chores_but_keeps_required_jobs_open(self):
        a = self.app
        self.assertEqual(a.state()["packing_game"]["progress"], {})
        a.event(type="adjust", kid="ava", points=20, reason="test", by="jordan")
        code, blocked = a.post("/api/spend", {"kid": "ava", "reward": "show", "tv": "livingroom"})
        self.assertEqual(code, 409, blocked)
        self.assertIn("Pack for the train first", blocked["error"])
        code, chore_blocked = a.post("/api/start", {"kid": "ava", "chore": "kitchen"})
        self.assertEqual(code, 409, chore_blocked)
        self.assertIn("Pack for the train first", chore_blocked["error"])
        self.assertEqual(a.post("/api/start", {"kid": "zoe", "chore": "kitchen"})[0], 200)
        code, team_blocked = a.post("/api/team", {"kid": "zoe", "chore": "kitchen",
                                                  "partners": ["ava"]})
        self.assertEqual(code, 409, team_blocked)
        self.assertIn("Ava: Pack for the train first", team_blocked["error"])
        self.assertEqual(a.post("/api/stop", {"kid": "zoe"})[0], 200)
        self.assertEqual(a.post("/api/start", {"kid": "ava", "chore": "get_dressed"})[0], 200)
        self.assertEqual(a.post("/api/stop", {"kid": "ava"})[0], 200)
        self.assertEqual(a.post("/api/start", {"kid": "ava", "chore": "brush_teeth"})[0], 200)
        self.assertEqual(a.post("/api/stop", {"kid": "ava"})[0], 200)

        gate_state = a.state()
        gate_state["must_do_first"]["ava"] = {"on": True, "rewards": True, "chore": "kitchen"}
        self.assertIsNone(a.s.packing_chore_gate(gate_state, "ava", "kitchen"))
        gate_state["must_do_first"]["ava"] = {"on": False}
        gate_state["chores_first"]["ava"] = {"on": True, "chore": "kitchen"}
        self.assertIsNone(a.s.packing_chore_gate(gate_state, "ava", "kitchen"))

        token = a.kid_sessions("ava")["ava"]
        items = ["shirt", "hair"]
        code, layout = a.post("/api/packing_update", {"kid": "ava", "kid_session": token,
                                                      "progress": self.stage(items, "inspect", 10)})
        self.assertEqual(code, 200, layout)
        self.assertEqual(layout["state"]["balance"]["ava"], 21)
        self.assertEqual(layout["state"]["packing_game"]["progress"]["ava"]["awards"], {"layout": 1})
        self.assertIsNotNone(layout["state"]["packing_game"]["progress"]["ava"]["started_at"])

        # Re-saving the same milestone is harmless; later stages add their own award once.
        self.assertEqual(a.post("/api/packing_update", {"kid": "ava", "kid_session": token,
                                                        "progress": self.stage(items, "inspect", 11)})[1]
                         ["state"]["balance"]["ava"], 21)
        blocked = a.post("/api/packing_update", {"kid": "ava", "kid_session": token,
                                                  "progress": self.stage(items, "pack", 12)})
        self.assertEqual(blocked[0], 403)
        self.assertIn("Grown-up inspection", blocked[1]["error"])
        skipped = a.post("/api/packing_update", {"kid": "ava", "kid_session": token,
                                                  "progress": self.stage(items, "final", 12)})
        self.assertEqual(skipped[0], 403)
        self.assertIn("grown-up", skipped[1]["error"].lower())
        inspected = a.post("/api/packing_update", {"packer": "ava", "by": "jordan",
                                                   "progress": self.stage(items, "pack", 12)},
                           self.adult_headers)[1]
        self.assertEqual(inspected["state"]["balance"]["ava"], 23)
        packed = a.post("/api/packing_update", {"kid": "ava", "kid_session": token,
                                                "progress": self.stage(items, "final", 13)})[1]
        self.assertEqual(packed["state"]["balance"]["ava"], 25)
        code, done = a.post("/api/packing_update", {"packer": "ava", "by": "jordan",
                                                    "progress": self.complete(items, 14)},
                            self.adult_headers)
        self.assertEqual(code, 200, done)
        self.assertTrue(done["state"]["packing_game"]["progress"]["ava"]["complete"])
        self.assertEqual(done["state"]["packing_game"]["progress"]["ava"]["awarded_points"], 10)
        self.assertEqual(done["state"]["packing_game"]["progress"]["ava"]["complete_prize"], 5)
        self.assertEqual(done["state"]["balance"]["ava"], 30)
        self.assertEqual(a.post("/api/packing_update", {"packer": "ava", "by": "jordan",
                                                        "progress": self.complete(items, 15)},
                                self.adult_headers)[1]
                         ["state"]["balance"]["ava"], 30)
        packing_events = [e for e in a.s.read_events() if e.get("packing_version") == "trip-test-v1"]
        self.assertEqual([(e["packing_milestone"], e["points"]) for e in packing_events],
                         [("layout", 1), ("inspect", 2), ("pack", 2), ("complete", 5)])
        self.assertEqual(a.post("/api/start", {"kid": "ava", "chore": "kitchen"})[0], 200)
        self.assertEqual(a.post("/api/spend", {"kid": "ava", "reward": "show", "tv": "livingroom"})[0], 200)

    def test_child_specific_item_does_not_block_the_other_child_and_an_unassigned_child_is_exempt(self):
        a = self.app
        code, done = a.post("/api/packing_update", {"packer": "max", "by": "jordan",
                                                    "progress": self.complete(["shirt"])},
                            self.adult_headers)
        self.assertEqual(code, 200, done)
        self.assertTrue(done["state"]["packing_game"]["progress"]["max"]["complete"])
        self.assertEqual(done["state"]["packing_game"]["progress"]["max"]["awarded_points"], 10)
        self.assertIsNone(a.s.packing_reward_gate(done["state"], ["zoe"]))
        self.assertIsNone(a.s.packing_chore_gate(done["state"], "zoe", "kitchen"))

    def test_undone_packing_award_disappears_and_can_be_earned_once_again(self):
        a = self.app
        token = a.kid_sessions("ava")["ava"]
        items = ["shirt", "hair"]
        self.assertEqual(a.post("/api/packing_update", {"kid": "ava", "kid_session": token,
                                                        "progress": self.stage(items, "inspect", 40)})[0], 200)
        first = a.post("/api/packing_update", {"packer": "ava", "by": "jordan",
                                               "progress": self.stage(items, "pack", 41)},
                       self.adult_headers)[1]
        self.assertEqual(first["state"]["packing_game"]["progress"]["ava"]["awards"],
                         {"layout": 1, "inspect": 2})
        inspect_event = next(e for e in a.s.read_events()
                             if e.get("packing_milestone") == "inspect")
        a.event(type="undo", ref=inspect_event["id"], by="jordan")
        after_undo = a.state()["packing_game"]["progress"]["ava"]
        self.assertEqual(after_undo["awards"], {"layout": 1})
        again = a.post("/api/packing_update", {"packer": "ava", "by": "jordan",
                                               "progress": self.stage(items, "pack", 42)},
                       self.adult_headers)[1]
        self.assertEqual(again["state"]["packing_game"]["progress"]["ava"]["awards"],
                         {"layout": 1, "inspect": 2})
        active_inspect = [e for e in a.s.active_packing_award_events(a.s.packing_config()).values()
                          if e.get("packing_milestone") == "inspect"]
        self.assertEqual(len(active_inspect), 1)

    def test_active_job_or_incomplete_packing_marks_the_hub_workflow_open(self):
        base = {"active": {}, "packing_game": {"activated": False}}
        self.assertFalse(self.app.s.kid_workflow_open(base))
        self.assertTrue(self.app.s.kid_workflow_open({"active": {"ava": {"chore": "kitchen"}},
                                                      "packing_game": {"activated": False}}))
        packing = self.app.state()["packing_game"]
        self.assertTrue(self.app.s.kid_workflow_open({"active": {}, "packing_game": packing}))

    def test_authenticated_adult_has_progress_without_a_star_balance_or_fake_interaction_clock(self):
        a = self.app
        opened = {"version": "trip-test-v1", "profile": "adult", "packer": "jordan", "phase": "layout",
                  "seq": 1, "ready": {}, "checked": {}, "packed": {}, "finals": {}}
        self.assertEqual(a.post("/api/packing_update", {"packer": "jordan", "by": "jordan",
                                                        "progress": opened})[0], 403)
        code, first = a.post("/api/packing_update", {"packer": "jordan", "by": "jordan",
                                                     "progress": opened}, {"X-HA-User": "jordan-ha"})
        self.assertEqual(code, 200, first)
        self.assertIsNone(first["state"]["packing_game"]["progress"]["jordan"]["interacted_at"])
        packed = dict(opened, seq=2, phase="pack", ready={"charger": True},
                      checked={"charger": "pass"}, packed={"charger": True})
        code, changed = a.post("/api/packing_update", {"packer": "jordan", "by": "jordan",
                                                       "progress": packed}, {"X-HA-User": "jordan-ha"})
        self.assertEqual(code, 200, changed)
        self.assertIsNotNone(changed["state"]["packing_game"]["progress"]["jordan"]["interacted_at"])
        finished = dict(packed, seq=3, phase="complete", finals={"adult_zip": "pass"})
        code, done = a.post("/api/packing_update", {"packer": "jordan", "by": "jordan",
                                                    "progress": finished}, {"X-HA-User": "jordan-ha"})
        self.assertEqual(code, 200, done)
        self.assertTrue(done["state"]["packing_game"]["progress"]["jordan"]["complete"])
        self.assertNotIn("jordan", done["state"]["balance"])
        self.assertFalse([e for e in a.s.read_events() if e.get("kid") == "jordan"])

    def test_active_adult_packing_projects_next_items_for_ambient_displays(self):
        a = self.app
        a.s.LEAD_ADULT = "jordan"
        upcoming = a.state()["packing_up_next"]
        self.assertTrue(upcoming["active"])
        self.assertEqual(upcoming["packer"], "jordan")
        self.assertEqual([x["name"] for x in upcoming["items"]], ["Charger"])
        self.assertEqual(upcoming["items"][0]["pile"], "Packing")

        row = {"version": "trip-test-v1", "profile": "adult", "packer": "jordan", "phase": "final",
               "seq": 2, "ready": {"charger": True}, "checked": {"charger": "pass"},
               "packed": {"charger": True}, "finals": {}}
        (a.dir / "packing_progress.json").write_text(json.dumps({
            "version": "trip-test-v1", "kids": {"jordan": row}
        }))
        checks = a.state()["packing_up_next"]
        self.assertEqual([(x["name"], x["kind"]) for x in checks["items"]], [("Bag zipped", "check")])

        row.update(phase="complete", finals={"adult_zip": "pass"})
        (a.dir / "packing_progress.json").write_text(json.dumps({
            "version": "trip-test-v1", "kids": {"jordan": row}
        }))
        self.assertIsNone(a.state()["packing_up_next"])
        a.s.HOUSE["packing_game"]["activated"] = False
        self.assertIsNone(a.state()["packing_up_next"])

    def test_packing_up_next_is_not_added_to_public_demo_state(self):
        projected = {"active": True, "items": [{"name": "Private thing"}]}
        self.assertNotIn("packing_up_next", self.app.s.demo_attrs({"packing_up_next": projected}))

    def test_departure_house_checks_are_injected_and_cannot_be_marked_before_their_window(self):
        a = self.app
        a.s.LEAD_ADULT = "jordan"
        a.s.HOUSE["packing_game"]["departure_house_checks"] = True
        snapshot = a.dir / "departure.json"
        a.s.HOUSE["departure_display_file"] = str(snapshot)
        now = time.time()

        def departure(leave):
            snapshot.write_text(json.dumps({
                "schema": 1, "active": True, "updated_at": now, "trip_id": "trip-test",
                "mode": "train", "title": "Train day", "wake_at": now - 3600,
                "leave_home_at": leave, "pull_in_at": leave + 2700,
                "board_at": leave + 3600, "board_label": "Train leaves",
            }))

        departure(now + 7200)
        adult = next(p for p in a.state()["packing_game"]["profiles"] if p["id"] == "adult")
        final_ids = {x["id"] for x in adult["final_checks"]}
        self.assertTrue({"adult_zip", "house_smelly_trash", "house_toilets_flushed"} <= final_ids)
        packed = {"version": "trip-test-v1", "profile": "adult", "packer": "jordan",
                  "phase": "pack", "seq": 1, "ready": {"charger": True},
                  "checked": {"charger": "pass"}, "packed": {"charger": True},
                  "finals": {"adult_zip": "pass", "house_smelly_trash": "pass"}}
        code, saved = a.post("/api/packing_update", {"packer": "jordan", "by": "jordan",
                                                     "progress": packed}, self.adult_headers)
        self.assertEqual(code, 200, saved)
        too_early = json.loads(json.dumps(packed))
        too_early.update(seq=2)
        too_early["finals"]["house_toilets_flushed"] = "pass"
        code, blocked = a.post("/api/packing_update", {"packer": "jordan", "by": "jordan",
                                                       "progress": too_early}, self.adult_headers)
        self.assertEqual(code, 409, blocked)
        self.assertIn("closer to the actual leave-home time", blocked["error"])

        departure(now + 1800)
        too_early.update(seq=3, phase="complete")
        code, done = a.post("/api/packing_update", {"packer": "jordan", "by": "jordan",
                                                    "progress": too_early}, self.adult_headers)
        self.assertEqual(code, 200, done)
        self.assertTrue(done["state"]["packing_game"]["progress"]["jordan"]["complete"])

    def test_legacy_physical_progress_uses_its_last_update_as_the_interaction_clock(self):
        row = self.stage(["shirt", "hair"], "inspect", 10)
        row.update({"started_at": 100, "updated_at": 123})
        (self.app.dir / "packing_progress.json").write_text(json.dumps({
            "version": "trip-test-v1", "kids": {"ava": row}
        }))
        progress = self.app.state()["packing_game"]["progress"]["ava"]
        self.assertEqual(progress["interacted_at"], 123)

    def test_authenticated_parent_can_mark_a_child_item_present_or_missing(self):
        a = self.app
        token = a.kid_sessions("ava")["ava"]
        self.assertEqual(a.post("/api/packing_update", {"kid": "ava", "kid_session": token,
                                                        "progress": self.stage(["shirt", "hair"], "inspect", 20)})[0], 200)
        body = {"packer": "ava", "by": "jordan",
                "mark": {"item": "shirt", "value": "present"}}
        self.assertEqual(a.post("/api/packing_update", body)[0], 403)
        code, present = a.post("/api/packing_update", body, {"X-HA-User": "jordan-ha"})
        self.assertEqual(code, 200, present)
        self.assertEqual(present["state"]["packing_game"]["progress"]["ava"]["phase"], "inspect")
        self.assertEqual(present["state"]["packing_game"]["progress"]["ava"]["checked"]["shirt"], "pass")
        code, all_present = a.post("/api/packing_update", {
            "packer": "ava", "by": "jordan", "mark": {"item": "hair", "value": "present"}},
            {"X-HA-User": "jordan-ha"})
        self.assertEqual(code, 200, all_present)
        self.assertEqual(all_present["state"]["packing_game"]["progress"]["ava"]["phase"], "inspect")
        stage = {"packer": "ava", "by": "jordan", "stage": "pack"}
        self.assertEqual(a.post("/api/packing_update", stage)[0], 403)
        code, packing = a.post("/api/packing_update", stage, {"X-HA-User": "jordan-ha"})
        self.assertEqual(code, 200, packing)
        self.assertEqual(packing["state"]["packing_game"]["progress"]["ava"]["phase"], "pack")
        code, handed_back = a.post("/api/packing_update", {
            "packer": "ava", "by": "jordan", "mark": {"item": "hair", "value": "missing"}},
            {"X-HA-User": "jordan-ha"})
        self.assertEqual(code, 200, handed_back)
        self.assertEqual(handed_back["state"]["packing_game"]["progress"]["ava"]["checked"]["hair"], "fix")
        self.assertEqual(handed_back["state"]["packing_game"]["progress"]["ava"]["phase"], "layout")

    def test_authenticated_parent_can_complete_exactly_one_packing_stage_per_tap(self):
        a = self.app
        body = {"packer": "ava", "by": "jordan", "stage": "complete_stage"}
        self.assertEqual(a.post("/api/packing_update", body)[0], 403)

        phases = ("inspect", "pack", "final", "complete")
        balances = (1, 3, 5, 10)
        for phase, balance in zip(phases, balances):
            code, result = a.post("/api/packing_update", body, self.adult_headers)
            self.assertEqual(code, 200, result)
            progress = result["state"]["packing_game"]["progress"]["ava"]
            self.assertEqual(progress["phase"], phase)
            self.assertEqual(result["state"]["balance"]["ava"], balance)

        self.assertTrue(progress["complete"])
        self.assertEqual(set(progress["ready"]), {"shirt", "hair"})
        self.assertEqual(set(progress["checked"]), {"shirt", "hair"})
        self.assertEqual(set(progress["packed"]), {"shirt", "hair"})
        self.assertEqual(progress["finals"], {"zip": "pass"})
        code, already = a.post("/api/packing_update", body, self.adult_headers)
        self.assertEqual(code, 409, already)
        self.assertIn("already complete", already["error"])

    def test_private_packing_editor_preserves_version_and_saved_progress(self):
        a = self.app
        private = a.dir / "packing_game.json"
        private.write_text(json.dumps(a.s.HOUSE["packing_game"]))
        os.chmod(private, 0o600)
        a.s.HOUSE["packing_game_file"] = str(private)
        progress = {"version": "trip-test-v1", "kids": {"ava": {"seq": 4, "phase": "layout"}}}
        (a.dir / "packing_progress.json").write_text(json.dumps(progress))
        body = {"by": "jordan", "action": "add_category", "profile": "adult", "title": "Day bag"}
        self.assertEqual(a.post("/api/packing_admin", body)[0], 403)
        code, result = a.post("/api/packing_admin", body, {"X-HA-User": "jordan-ha"})
        self.assertEqual(code, 200, result)
        saved = json.loads(private.read_text())
        self.assertEqual(saved["version"], "trip-test-v1")
        self.assertEqual(saved["profiles"][1]["piles"][-1]["title"], "Day bag")
        self.assertEqual(json.loads((a.dir / "packing_progress.json").read_text()), progress)
        self.assertEqual(private.stat().st_mode & 0o777, 0o600)

    def test_forgotten_item_can_be_added_for_one_child_and_returns_only_that_child_to_gathering(self):
        a = self.app
        private = a.dir / "packing_game.json"
        private.write_text(json.dumps(a.s.HOUSE["packing_game"]))
        os.chmod(private, 0o600)
        a.s.HOUSE["packing_game_file"] = str(private)
        ava = self.stage(["shirt", "hair"], "pack", 30)
        max_row = self.stage(["shirt"], "pack", 31)
        (a.dir / "packing_progress.json").write_text(json.dumps({
            "version": "trip-test-v1", "kids": {"ava": ava, "max": max_row}
        }))
        code, result = a.post("/api/packing_admin", {
            "by": "jordan", "action": "add_item", "profile": "kid", "pile": "bag",
            "name": "Forgotten thing", "only": "ava"
        }, {"X-HA-User": "jordan-ha"})
        self.assertEqual(code, 200, result)
        saved = json.loads(private.read_text())
        item = saved["profiles"][0]["piles"][0]["items"][-1]
        self.assertEqual(item["only"], ["ava"])
        progress = json.loads((a.dir / "packing_progress.json").read_text())["kids"]
        self.assertEqual(progress["ava"]["phase"], "layout")
        self.assertEqual(progress["max"]["phase"], "pack")


class Case(unittest.TestCase):
    config = None

    def setUp(self):
        self.app = App(self.config)
        self.s = self.app.s

    def tearDown(self):
        self.app.close()


class KidPreviewProjectionTests(Case):
    def setUp(self):
        super().setUp()
        config = json.loads((self.app.dir / "chores.json").read_text())
        config["kids"][0]["pin_hash"] = self.s.make_kid_pin_hash("1234")
        (self.app.dir / "chores.json").write_text(json.dumps(config))
        (self.app.dir / "whatsnew.json").write_text(json.dumps([
            {"v": "preview-news", "ts": time.time(), "items": [{"icon": "2728", "text": "Preview news"}]}
        ]))
        (self.app.dir / "news_seen.json").write_text(json.dumps({
            "kid:ava": "older-news", "kid:max": "preview-news",
            "ann:kid:ava": [], "ann:kid:max": ["already-seen"]
        }))
        self.s.ADULT_REQ["login-preview"] = {"who": "jordan", "ip": "127.0.0.1", "ts": time.time(),
                                               "status": "ok", "purpose": "login"}

    def preview(self, kid="ava", user="home-assistant-user-id", source_ip=None):
        return self.app.get("/api/kid_preview?kid=" + kid, {"X-HA-User": user}, source_ip)

    def test_lead_adult_only_projection_matches_canonical_state_without_side_effects_or_secrets(self):
        before_files = {path.relative_to(self.app.dir).as_posix(): path.read_bytes()
                        for path in self.app.dir.rglob("*") if path.is_file()}
        before_sessions = json.loads(json.dumps(self.s.GUARD.get("kid_sessions") or {}))
        before_presence = json.loads(json.dumps(self.s.GUARD.get("kid_display_where") or {}))

        code, reply = self.preview()
        self.assertEqual(code, 200, reply)
        state = reply["state"]
        self.assertEqual(reply, {"ok": True, "kid": "ava", "read_only": True, "state": state})
        self.assertEqual(state["preview"], {"kid": "ava", "read_only": True, "skip_pin": True,
                                             "canonical_state": True, "mutation_transport": None,
                                             "inspection_watermarks": self.s.inspection_watermarks("ava"),
                                             "inspection_seen_displays": self.s.inspection_seen_snapshots("ava")})
        self.assertTrue(state["news_unseen"])
        self.assertEqual(state["news_seen_kids"]["ava"], "older-news")
        self.assertEqual(state["awards"], self.s.derive()["awards"])
        self.assertEqual((state["peer_pin_reset"], state["peer_pin_reset_capable"], state["pin_reset_open"]),
                         (None, False, {}))
        self.assertNotIn("adult_go", state)
        self.assertNotIn("pin_hash", json.dumps(reply))
        self.assertNotIn("kid_session", json.dumps(reply))
        self.assertEqual(self.s.GUARD.get("kid_sessions") or {}, before_sessions)
        self.assertEqual(self.s.GUARD.get("kid_display_where") or {}, before_presence)
        self.assertNotIn("used", self.s.ADULT_REQ["login-preview"])
        after_files = {path.relative_to(self.app.dir).as_posix(): path.read_bytes()
                       for path in self.app.dir.rglob("*") if path.is_file()}
        self.assertEqual(after_files, before_files)

        code, max_reply = self.preview("max")
        self.assertEqual(code, 200, max_reply)
        self.assertFalse(max_reply["state"]["news_unseen"])

    def test_preview_rejects_missing_or_spoofed_identity_remote_clients_and_unknown_kids(self):
        self.assertEqual(self.app.get("/api/kid_preview?kid=ava")[0], 403)
        self.assertEqual(self.preview(user="someone-else")[0], 403)
        self.assertEqual(self.preview(source_ip="127.0.0.2")[0], 403)
        self.assertEqual(self.preview("nobody")[0], 404)

    def test_real_hub_snapshot_preserves_zero_vs_missing_for_private_preview_only(self):
        hub_ip = "127.0.0.2"
        self.s.HUBS.clear()
        self.s.HUBS["test_hub"] = {"ip": hub_ip, "room": "Play room", "entity": "media_player.test_hub"}
        self.s.HOUSE["hubs"] = self.s.HUBS
        code, saved = self.app.post_from(hub_ip, "/api/inspection_seen_snapshot", {"kids": {
            "ava": {"present": True, "value": 0}, "max": {"present": False}}})
        self.assertEqual((code, saved), (200, {"ok": True, "display": "test_hub"}))
        snapshots = self.preview()[1]["state"]["preview"]["inspection_seen_displays"]
        self.assertEqual(len(snapshots), 1)
        self.assertEqual({k: snapshots[0][k] for k in ("display_id", "room", "entity", "source", "observed",
                                                       "present", "value")},
                         {"display_id": "test_hub", "room": "Play room", "entity": "media_player.test_hub",
                          "source": "trusted_configured_hub", "observed": True, "present": True, "value": 0.0})
        self.assertIsNotNone(snapshots[0]["age_s"])
        max_snap = self.preview("max")[1]["state"]["preview"]["inspection_seen_displays"][0]
        self.assertEqual((max_snap["observed"], max_snap["present"], max_snap["value"]), (True, False, None))
        watermarks = self.preview()[1]["state"]["preview"]["inspection_watermarks"]
        self.assertEqual(watermarks["default_display"], "test_hub")
        self.assertEqual({k: watermarks["displays"][0][k] for k in ("id", "label", "seen")},
                         {"id": "test_hub", "label": "Play room", "seen": 0.0})
        self.assertIsNotNone(watermarks["displays"][0]["observed_at"])
        self.assertIsNotNone(watermarks["displays"][0]["age"])
        missing = self.preview("max")[1]["state"]["preview"]["inspection_watermarks"]["displays"][0]
        self.assertIsNone(missing["seen"])
        self.assertNotIn("inspection_seen_displays", self.app.state())
        self.assertEqual(self.app.post_from("127.0.0.3", "/api/inspection_seen_snapshot",
                                            {"kids": {"ava": {"present": True, "value": 99}}})[0], 403)


class HubChildSessions(Case):
    HUB_ONE = "127.0.0.2"
    HUB_TWO = "127.0.0.3"

    def setUp(self):
        super().setUp()
        config = json.loads((self.app.dir / "chores.json").read_text())
        config["kids"][0]["pin_hash"] = self.s.make_kid_pin_hash("1234")
        (self.app.dir / "chores.json").write_text(json.dumps(config))
        self.s.HUBS.clear()
        self.s.HUBS.update({
            "room_hub": {"ip": self.HUB_ONE, "room": "den", "floorplan_room": "great_room",
                         "entity": "media_player.den_hub"},
            "bed_hub": {"ip": self.HUB_TWO, "room": "kids_bed", "floorplan_room": "kids_bedroom",
                        "entity": "media_player.bed_hub"},
        })
        self.s.queue_hub_child_state_push = lambda: None

    def test_only_each_physical_hubs_live_child_credential_sets_its_foreground_name(self):
        self.assertEqual(self.app.post("/api/hub_heartbeat", {"kid": "ava", "kid_session": "fake"})[0], 403)
        self.assertEqual(self.app.post_from(self.HUB_ONE, "/api/hub_heartbeat", {"kid": None})[0], 200)
        unlocked = self.app.post_from(self.HUB_ONE, "/api/kid_unlock", {"kid": "ava", "pin": "1234"})
        self.assertEqual(unlocked[0], 200, unlocked)
        token = unlocked[1]["kid_session"]
        beat = self.app.post_from(self.HUB_ONE, "/api/hub_heartbeat",
                                  {"kid": "ava", "kid_session": token})
        self.assertEqual(beat[1]["active_kid"], "ava")

        second = self.app.post_from(self.HUB_TWO, "/api/kid_unlock", {"kid": "max"})
        self.assertEqual(second[0], 200, second)
        self.assertEqual(self.app.post_from(self.HUB_TWO, "/api/hub_heartbeat",
                         {"kid": "max", "kid_session": second[1]["kid_session"]})[1]["active_kid"], "max")
        manifest = self.s.hub_child_sessions_manifest()
        self.assertEqual((manifest["great_room"]["display_id"], manifest["great_room"]["kid"],
                          manifest["great_room"]["name"], manifest["great_room"]["entity"]),
                         ("room_hub", "ava", "Ava", "media_player.den_hub"))
        self.assertEqual(manifest["kids_bedroom"]["kid"], "max")
        self.assertNotIn(token, json.dumps(manifest))
        self.assertNotIn(self.HUB_ONE, json.dumps(manifest))

        # A wrong or expired credential cannot keep a name up, and one Hub never changes the other.
        cleared = self.app.post_from(self.HUB_ONE, "/api/hub_heartbeat",
                                     {"kid": "ava", "kid_session": "wrong"})
        self.assertIsNone(cleared[1]["active_kid"])
        manifest = self.s.hub_child_sessions_manifest()
        self.assertIsNone(manifest["great_room"]["kid"])
        self.assertEqual(manifest["kids_bedroom"]["kid"], "max")

    def test_picker_logout_and_missing_hub_clear_without_historical_login_fallback(self):
        unlocked = self.app.post_from(self.HUB_ONE, "/api/kid_unlock", {"kid": "ava", "pin": "1234"})[1]
        self.app.post_from(self.HUB_ONE, "/api/hub_heartbeat",
                           {"kid": "ava", "kid_session": unlocked["kid_session"]})
        self.s.GUARD["kid_display_where"] = {"ava": {"display_id": "room_hub", "valid_until": time.time() + 900}}
        row = self.s.GUARD["hub_child_sessions"]["room_hub"]
        observed = row["last_observed"]
        self.assertTrue(self.s.expire_hub_child_sessions(observed + self.s.HUB_CHILD_OFFLINE_S + .1))
        self.assertIsNone(self.s.hub_child_sessions_manifest(observed + 11)["great_room"]["kid"])
        self.assertIsNotNone(self.s.GUARD.get("kid_display_where", {}).get("ava"))

        self.app.post_from(self.HUB_ONE, "/api/hub_heartbeat", {"kid": None})
        picker = self.s.hub_child_sessions_manifest()
        self.assertIsNone(picker["great_room"]["kid"])
        self.assertIsNotNone(picker["great_room"]["last_observed"])

    def test_fast_heartbeats_use_one_global_public_refresh_clock(self):
        start = time.time()
        first = self.s.observe_hub_child(self.HUB_ONE, now=start)
        second = self.s.observe_hub_child(self.HUB_TWO, now=start + 1)
        self.assertTrue(first["publish"])
        self.assertTrue(second["publish"])

        quiet_one = self.s.observe_hub_child(self.HUB_ONE, now=start + 20)
        quiet_two = self.s.observe_hub_child(self.HUB_TWO, now=start + 21)
        self.assertFalse(quiet_one["publish"])
        self.assertFalse(quiet_two["publish"])
        before = self.s.hub_child_sessions_manifest(start + 21)
        self.assertEqual(before["great_room"]["last_observed"], start)
        self.assertEqual(before["kids_bedroom"]["last_observed"], start + 1)

        refresh = self.s.observe_hub_child(self.HUB_ONE, now=start + 32)
        following = self.s.observe_hub_child(self.HUB_TWO, now=start + 33)
        self.assertTrue(refresh["publish"])
        self.assertFalse(following["publish"])
        after = self.s.hub_child_sessions_manifest(start + 33)
        self.assertEqual(after["great_room"]["last_observed"], start + 32)
        self.assertEqual(after["kids_bedroom"]["last_observed"], start + 21)
        self.assertEqual(after["great_room"]["valid_until"] - after["great_room"]["last_observed"], 75)

    def test_offline_clear_uses_private_liveness_not_throttled_public_time(self):
        unlocked = self.app.post_from(self.HUB_ONE, "/api/kid_unlock", {"kid": "ava", "pin": "1234"})[1]
        start = time.time()
        self.s.observe_hub_child(self.HUB_ONE, "ava", unlocked["kid_session"], start)
        self.s.observe_hub_child(self.HUB_ONE, "ava", unlocked["kid_session"], start + 8)
        self.assertFalse(self.s.expire_hub_child_sessions(start + 11))
        self.assertTrue(self.s.expire_hub_child_sessions(start + 19))

    def test_manifest_only_refresh_does_not_rewrite_chunk_parts(self):
        sent, old_post = [], self.s.ha_post_state
        self.s.ha_post_state = lambda entity, value, attrs, tok: sent.append((entity, attrs))
        try:
            now = time.time()
            self.s.GUARD["hub_child_sessions"] = {"room_hub": {"kid": "ava", "since": now,
                "last_observed": now, "valid_until": now + 75, "published_at": now}}
            self.s.GUARD.pop("state_sig", None)
            self.s.GUARD.pop("state_sig_manifest", None)
            state = self.s.derive()
            self.app.orig["push_state_sensor"](state)
            first_parts = [x for x in sent if x[0].startswith("sensor.chore_state_part_")]
            first_manifest = next(x[1] for x in sent if x[0] == "sensor.chore_state")
            self.assertTrue(first_parts)
            self.assertEqual(first_manifest["hub_child_sessions"]["great_room"]["kid"], "ava")

            sent.clear()
            row = self.s.GUARD["hub_child_sessions"]["room_hub"]
            row.update(last_observed=now + 31, valid_until=now + 106, published_at=now + 31)
            self.app.orig["push_state_sensor"](state)
            self.assertFalse([x for x in sent if x[0].startswith("sensor.chore_state_part_")])
            refreshed = [x for x in sent if x[0] == "sensor.chore_state"]
            self.assertEqual(len(refreshed), 1)
            self.assertEqual(refreshed[0][1]["hub_child_sessions"]["great_room"]["last_observed"], now + 31)
        finally:
            self.s.ha_post_state = old_post


# ---------------------------------------------------------------- who may do what
class SharedVsExclusive(Case):
    def test_stopped_work_after_a_turn_in_withdraws_the_stale_report_and_resumes(self):
        self.assertEqual(self.app.post("/api/start", {"kid": "max", "chore": "kitchen"})[0], 200)
        code, turned_in = self.app.post("/api/report", {"kid": "max", "chore": "kitchen", "feeling": "fun"})
        self.assertEqual(code, 200, turned_in)
        report_id = turned_in["event"]["id"]

        # Reproduce the legacy state: an older client was allowed to work again while that report
        # still waited, then Stop discarded the active session and exposed the waiting tile again.
        self.s.append({"type": "start", "kid": "max", "chore": "kitchen"})
        self.s.append({"type": "stop", "kid": "max", "chore": None})
        stale = self.app.state()
        self.assertIn("max", stale["board"]["kitchen"]["pending_by"])
        self.assertIn("max", stale["board"]["kitchen"]["resume_pending_by"])

        code, resumed = self.app.post("/api/start", {"kid": "max", "chore": "kitchen"})
        self.assertEqual(code, 200, resumed)
        self.assertEqual(resumed["state"]["active"]["max"]["chore"], "kitchen")
        self.assertNotIn("max", resumed["state"]["board"]["kitchen"]["pending_by"])
        self.assertNotIn(report_id, {r["id"] for r in resumed["state"]["history"]})
        self.assertTrue(any(e["type"] == "withdraw" and e.get("ref") == report_id
                            for e in self.s.read_events()))

    def test_an_ordinary_pending_turn_in_cannot_be_restarted(self):
        self.assertEqual(self.app.post("/api/start", {"kid": "max", "chore": "kitchen"})[0], 200)
        self.assertEqual(self.app.post("/api/report", {"kid": "max", "chore": "kitchen"})[0], 200)
        code, waiting = self.app.post("/api/start", {"kid": "max", "chore": "kitchen"})
        self.assertEqual((code, waiting.get("error")), (409, "already waiting for inspection"))

    def test_a_rejected_resume_does_not_withdraw_the_stale_turn_in(self):
        self.app.post("/api/start", {"kid": "max", "chore": "kitchen"})
        report_id = self.app.post("/api/report", {"kid": "max", "chore": "kitchen"})[1]["event"]["id"]
        self.s.append({"type": "start", "kid": "max", "chore": "kitchen"})
        self.s.append({"type": "stop", "kid": "max", "chore": None})
        self.s.append({"type": "start", "kid": "ava", "chore": "kitchen"})

        code, busy = self.app.post("/api/start", {"kid": "max", "chore": "kitchen"})
        self.assertEqual(code, 409, busy)
        self.assertIn("max", self.app.state()["board"]["kitchen"]["pending_by"])
        self.assertFalse(any(e["type"] == "withdraw" and e.get("ref") == report_id
                             for e in self.s.read_events()))

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
    def test_login_response_keeps_per_child_news_acknowledgements(self):
        version = "news-shown-once"
        (self.app.dir / "whatsnew.json").write_text(json.dumps([
            {"v": version, "ts": time.time(), "items": [{"icon": "2728", "text": "Shown once"}]}
        ]))
        (self.app.dir / "news_seen.json").write_text(json.dumps({"kid:ava": version}))
        code, unlocked = self.app.post("/api/kid_unlock", {"kid": "ava"})
        self.assertEqual(code, 200, unlocked)
        self.assertEqual(unlocked["state"]["news_seen_kids"]["ava"], version)

    def test_watch_buzz_gate_fails_quiet_until_ha_confirms_it(self):
        self.s.ADULTS["jordan"]["buzz_gate"] = {"entity": "binary_sensor.jordan_left_bedroom_today",
                                                    "states": ["on"]}
        self.app.set("binary_sensor.jordan_left_bedroom_today", "unavailable")
        self.assertFalse(self.s.adult_buzz_allowed("jordan", "test"))
        self.app.set("binary_sensor.jordan_left_bedroom_today", "off")
        self.assertFalse(self.s.adult_buzz_allowed("jordan", "test"))
        self.app.set("binary_sensor.jordan_left_bedroom_today", "on")
        self.assertTrue(self.s.adult_buzz_allowed("jordan", "test"))
        self.app.set("binary_sensor.jordan_left_bedroom_today", "on", ago=90000)
        self.assertFalse(self.s.adult_buzz_allowed("jordan", "test"))
        self.assertTrue(self.s.adult_buzz_allowed("casey", "test"))

    def test_pre_gate_pin_reset_phone_is_ongoing_but_watch_still_buzzes(self):
        s = self.s
        gate = "binary_sensor.jordan_left_bedroom_today"
        s.ADULTS["jordan"]["buzz_gate"] = {"entity": gate, "states": ["on"]}
        s.ADULT_BUZZ_NOTIFY["jordan"] = ["notify.jordan_watch"]
        s.ADULT_REQ["quiet123"] = {"who": "jordan", "ip": "127.0.0.1", "ts": time.time(),
                                   "status": "pending", "purpose": "pin_reset", "kid": "ava"}
        self.app.set(gate, "off")
        self.app.orig["send_adult_request"]("quiet123", "jordan", "test Hub")
        phone = next(data for svc, data in self.app.calls if svc == "notify.mobile_app_jordans_phone")
        self.assertIs(phone["data"]["persistent"], True)
        self.assertTrue(any(svc == "notify.jordan_watch" for svc, data in self.app.calls))
        self.app.calls.clear()
        self.app.set(gate, "on")
        self.app.states[gate]["attributes"]["cycle_started_at"] = time.time()
        self.app.orig["send_adult_request"]("quiet123", "jordan", "test Hub")
        phone = next(data for svc, data in self.app.calls if svc == "notify.mobile_app_jordans_phone")
        self.assertNotIn("persistent", phone["data"])
        self.assertTrue(any(svc == "notify.jordan_watch" for svc, data in self.app.calls))

    def test_pre_gate_ephemeral_phone_notice_stays_dashboard_only(self):
        gate = "binary_sensor.jordan_left_bedroom_today"
        self.s.ADULTS["jordan"]["buzz_gate"] = {"entity": gate, "states": ["on"]}
        self.app.set(gate, "unavailable")
        self.assertIsNone(self.s.phone_notice_data("jordan", {"tag": "chore-doing-ava"}, "test"))

    def test_abandoned_adult_request_expires_without_waiting_for_the_kid_to_poll(self):
        now = time.time()
        self.s.ADULT_REQ["oldreq"] = {"who": "jordan", "ip": "127.0.0.1",
                                      "ts": now - self.s.ADULT_REQUEST_TTL_S - 1,
                                      "status": "pending", "purpose": "pin_reset", "kid": "ava"}
        self.assertEqual(self.s.expire_adult_requests(now), ["oldreq"])
        self.assertEqual(self.s.ADULT_REQ["oldreq"]["status"], "timeout")

    def test_house_can_mute_every_kids_interface_sound_effect(self):
        s=self.s;s.HOUSE["sound_effects"] = False
        self.assertIs(self.app.state()["sound_effects"], False)
        s.ANNOUNCE_TTS,s.ANNOUNCE_SPEAKER,s.ANNOUNCE_MEDIA_BASE="tts.test","media_player.test",None
        self.app.orig["announce"]("This must stay quiet.","tok")
        self.assertEqual(self.app.calls,[])

        old_derive,old_cfg=s.derive,s.cfg
        s.derive=lambda:{"dress":{"on":True,"school":False,"kids":{"ava":"todo"}}}
        s.cfg=lambda:{"kids":[{"id":"ava","name":"Ava"}]}
        s.DRESS["speakers"]=["media_player.test"]
        self.addCleanup(lambda:setattr(s,"derive",old_derive))
        self.addCleanup(lambda:setattr(s,"cfg",old_cfg))
        self.addCleanup(lambda:s.DRESS.pop("speakers",None))
        s.dress_announce()
        self.assertEqual(self.app.calls,[])

    def test_kid_pin_is_verified_but_never_published(self):
        salt="00112233445566778899aabbccddeeff";rounds=120000
        digest=hashlib.pbkdf2_hmac("sha256",b"2353",bytes.fromhex(salt),rounds).hex()
        config=json.loads((self.app.dir/"chores.json").read_text())
        config["kids"][0]["pin_hash"]=f"pbkdf2_sha256${rounds}${salt}${digest}"
        (self.app.dir/"chores.json").write_text(json.dumps(config))
        kid=next(k for k in self.app.state()["config"]["kids"] if k["id"]=="ava")
        self.assertTrue(kid["pin_required"])
        self.assertNotIn("pin_hash",kid)
        self.assertEqual(self.app.post("/api/kid_unlock",{"kid":"ava","pin":"9999"})[0],403)
        self.assertEqual(self.app.post("/api/kid_unlock",{"kid":"ava","pin":"2353"})[0],200)

    def test_document_chore_allows_one_pending_row_per_safe_drive_link(self):
        config=json.loads((self.app.dir/"chores.json").read_text())
        config["chores"].append({"id":"scan","name":"Scan paper","icon":"1f4c4","points":1,
                                  "cooldown_h":0,"only":["ava"],"multi_pending":True,"document_review":True})
        (self.app.dir/"chores.json").write_text(json.dumps(config))
        one=self.app.post("/api/report",{"kid":"ava","chore":"scan","note":"Capitol project",
                          "document_title":"Capitol project sheet",
                          "document_url":"https://drive.google.com/file/d/abc123/view?secret=drop"})
        two=self.app.post("/api/report",{"kid":"ava","chore":"scan","note":"Break notice",
                          "document_title":"Thanksgiving break notice",
                          "document_url":"https://drive.google.com/file/d/def456/view"})
        self.assertEqual((one[0],two[0]),(200,200))
        rows=[r for r in two[1]["state"]["history"] if r.get("chore")=="scan" and r.get("status")=="pending"]
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[-1]["document_url"],"https://drive.google.com/file/d/abc123/view")
        bad=self.app.post("/api/report",{"kid":"ava","chore":"scan","note":"Bad link",
                          "document_url":"https://example.com/not-drive"})
        self.assertNotIn("document_url",bad[1]["event"])

    def test_neutral_hosted_school_and_personal_papers_need_no_google_login(self):
        config=json.loads((self.app.dir/"chores.json").read_text())
        config["chores"].append({"id":"scan","name":"Scan paper","icon":"1f4c4","points":1,
                                  "cooldown_h":0,"only":["ava"],"multi_pending":True,"document_review":True})
        (self.app.dir/"chores.json").write_text(json.dumps(config))
        started=self.app.post("/api/paper_scan_start",{"kid":"ava","kind":"school"})
        self.assertEqual(started[0],200)
        done=self.app.post("/api/paper_ingest",{"scan":started[1]["scan"],
                           "pages":["/local/chore-papers/abc123-1.jpg","/local/chore-papers/abc123-2.jpg"]})
        self.assertEqual(done[0],200)
        school=done[1]["state"]["papers"]["ava"][0]
        self.assertEqual((school["kind"],len(school["pages"]),school["status"]),("school",2,"pending"))
        self.assertTrue(school["title"].startswith("School paper · "))
        report=next(r for r in done[1]["state"]["history"] if r["id"]==school["id"])
        self.assertNotIn("document_url",report)
        personal_start=self.app.post("/api/paper_scan_start",{"kid":"ava","kind":"personal"})[1]
        personal=self.app.post("/api/paper_ingest",{"scan":personal_start["scan"],
                              "pages":["/local/chore-papers/def456-1.jpg"]})
        self.assertEqual(personal[0],200)
        self.assertEqual(personal[1]["state"]["papers"]["ava"][0]["kind"],"personal")

    def test_existing_drive_report_can_be_repointed_to_ha_images_without_rewriting_it(self):
        config=json.loads((self.app.dir/"chores.json").read_text())
        config["chores"].append({"id":"scan","name":"Scan paper","icon":"1f4c4","points":1,
                                  "cooldown_h":0,"only":["ava"],"multi_pending":True,"document_review":True})
        (self.app.dir/"chores.json").write_text(json.dumps(config))
        report=self.app.post("/api/report",{"kid":"ava","chore":"scan","document_url":"https://drive.google.com/file/d/abc/view"})[1]["event"]
        hosted=self.app.post("/api/document_host",{"ref":report["id"],"title":"School paper",
                             "pages":["/local/chore-papers/abc-1.jpg"]})
        self.assertEqual(hosted[0],200)
        row=next(r for r in hosted[1]["state"]["history"] if r["id"]==report["id"])
        self.assertNotIn("document_url",row)
        self.assertEqual(row["document_pages"],["/local/chore-papers/abc-1.jpg"])

    def test_grownup_can_open_first_come_forgotten_pin_reset(self):
        salt="00112233445566778899aabbccddeeff";rounds=120000
        digest=hashlib.pbkdf2_hmac("sha256",b"2353",bytes.fromhex(salt),rounds).hex()
        config=json.loads((self.app.dir/"chores.json").read_text())
        config["kids"][0]["pin_hash"]=f"pbkdf2_sha256${rounds}${salt}${digest}"
        (self.app.dir/"chores.json").write_text(json.dumps(config))
        code,requested=self.app.post("/api/kid_pin_reset_request",{"kid":"ava"})
        self.assertEqual(code,200)
        nonce=requested["nonce"]
        self.assertEqual(next(q for q in requested["state"]["assist_requests"] if q["nonce"]==nonce)["purpose"],"pin_reset")
        self.s.ADULT_REQ[nonce]["ts"]-=180
        code,waiting=self.app.post("/api/adult_status",{"nonce":nonce})
        self.assertEqual((code,waiting["status"]),(200,"pending"))
        code,allowed=self.app.post("/api/assist_answer",{"nonce":nonce,"ok":True,"by":"jordan"})
        self.assertEqual(code,200)
        self.assertIn("ava",allowed["state"]["pin_reset_open"])
        repeated=self.app.post("/api/kid_pin_reset_request",{"kid":"ava"})
        self.assertEqual(repeated[1]["nonce"],nonce)
        code,ready=self.app.post("/api/adult_status",{"nonce":nonce})
        self.assertEqual((code,ready["status"]),(200,"ok"))
        self.assertIn("ava",ready["state"]["pin_reset_open"])
        before=allowed["state"]["pin_epoch"]
        changed=self.app.post("/api/kid_pin_change",{"kid":"ava","new_pin":"915"})
        self.assertEqual(changed[0],200)
        self.assertNotEqual(before,changed[1]["state"]["pin_epoch"])
        self.assertEqual(self.app.post("/api/kid_unlock",{"kid":"ava","pin":"2353"})[0],403)
        self.assertEqual(self.app.post("/api/kid_unlock",{"kid":"ava","pin":"915"})[0],200)
        self.assertNotIn("ava",self.app.state()["pin_reset_open"])

    def test_kid_pin_can_be_eleven_digits(self):
        config=json.loads((self.app.dir/"chores.json").read_text())
        config["kids"][0]["pin_hash"]=self.s.make_kid_pin_hash("915")
        (self.app.dir/"chores.json").write_text(json.dumps(config))
        changed=self.app.post("/api/kid_pin_change",{
            "kid":"ava","current_pin":"915","new_pin":"12345678910"})
        self.assertEqual(changed[0],200)
        self.assertEqual(self.app.post("/api/kid_unlock",{
            "kid":"ava","pin":"12345678910"})[0],200)
        self.assertEqual(self.app.post("/api/kid_unlock",{
            "kid":"ava","pin":"1234567891"})[0],403)

    def test_kid_without_a_pin_can_set_the_first_one_from_their_open_hub_profile(self):
        self.assertEqual(self.app.post("/api/kid_pin_change", {
            "kid": "max", "new_pin": "8642"})[0], 403)
        opened = self.app.post("/api/kid_unlock", {"kid": "max"})
        self.assertEqual(opened[0], 200)
        self.assertEqual(self.app.post("/api/kid_pin_change", {
            "kid": "max", "kid_session": opened[1]["kid_session"], "new_pin": "8642"})[0], 403)
        requested = self.app.post("/api/kid_pin_reset_request", {"kid": "max"})
        self.assertEqual(requested[0], 200)
        nonce = requested[1]["nonce"]
        request = next(q for q in requested[1]["state"]["assist_requests"] if q["nonce"] == nonce)
        self.assertTrue(request["first_pin"])
        self.assertEqual(self.app.post("/api/assist_answer", {
            "nonce": nonce, "ok": True, "by": "jordan"})[0], 200)
        changed = self.app.post("/api/kid_pin_change", {
            "kid": "max", "kid_session": opened[1]["kid_session"], "new_pin": "8642"})
        self.assertEqual(changed[0], 200, changed)
        self.assertTrue(next(k for k in changed[1]["state"]["config"]["kids"]
                             if k["id"] == "max")["pin_required"])
        self.assertEqual(self.app.post("/api/kid_unlock", {"kid": "max"})[0], 403)
        self.assertEqual(self.app.post("/api/kid_unlock", {"kid": "max", "pin": "8642"})[0], 200)


class PeerKidPinReset(Case):
    TARGET_IP = "127.0.0.2"
    HELPER_IP = "127.0.0.3"

    def setUp(self):
        super().setUp()
        config = json.loads((self.app.dir / "chores.json").read_text())
        config["kids"][0]["pin_hash"] = self.s.make_kid_pin_hash("1111")
        config["kids"][1]["pin_hash"] = self.s.make_kid_pin_hash("2222")
        (self.app.dir / "chores.json").write_text(json.dumps(config))
        self.s.PEER_PIN_CFG.clear()
        self.s.PEER_PIN_CFG.update({"displays": ["target_hub", "helper_hub"],
                                    "kids": ["ava", "max"], "adult": "jordan",
                                    "offline_seconds": 8, "hold_fresh_seconds": 1.5})
        self.s.HUBS.clear()
        self.s.HUBS.update({"target_hub": {"ip": self.TARGET_IP, "room": "Living room"},
                            "helper_hub": {"ip": self.HELPER_IP, "room": "Kids room"}})
        self.s.HUB_PLACEMENT.clear()
        self.s.HUB_PLACEMENT.update({"trusted": True, "pending": False})
        self.app.event(type="adjust", kid="ava", points=3, reason="test", earned=False)
        self.heartbeat_both()

    def heartbeat_both(self):
        self.assertEqual(self.app.post_from(self.TARGET_IP, "/api/hub_heartbeat", {})[0], 200)
        self.assertEqual(self.app.post_from(self.HELPER_IP, "/api/hub_heartbeat", {})[0], 200)

    def begin(self):
        code, requested = self.app.post_from(self.TARGET_IP, "/api/kid_peer_reset_request", {"kid": "ava"})
        self.assertEqual(code, 200, requested)
        unlocked = self.app.post_from(self.HELPER_IP, "/api/kid_unlock", {"kid": "max", "pin": "2222"})
        self.assertEqual(unlocked[0], 200, unlocked[1])
        nonce = requested["nonce"]
        helped = self.app.post_from(self.HELPER_IP, "/api/kid_peer_reset_help",
                                    {"nonce": nonce, "kid": "max", "kid_session": unlocked[1]["kid_session"]})
        self.assertEqual(helped[0], 200, helped[1])
        return nonce, unlocked[1]["kid_session"]

    def test_reset_requires_other_authenticated_kid_other_hub_and_live_hold(self):
        self.assertTrue(self.app.post_from(self.TARGET_IP, "/api/kid_unlock",
                        {"kid": "ava", "pin": "1111"})[1]["state"]["peer_pin_reset_capable"])
        self.assertFalse(self.app.state()["peer_pin_reset_capable"])
        requested = self.app.post_from(self.TARGET_IP, "/api/kid_peer_reset_request", {"kid": "ava"})
        nonce = requested[1]["nonce"]
        target_session = self.app.post_from(self.TARGET_IP, "/api/kid_unlock",
                                            {"kid": "ava", "pin": "1111"})[1]["kid_session"]
        same_hub = self.app.post_from(self.TARGET_IP, "/api/kid_peer_reset_help",
                                      {"nonce": nonce, "kid": "max", "kid_session": target_session})
        self.assertEqual(same_hub[0], 403)
        helper_unlock = self.app.post_from(self.HELPER_IP, "/api/kid_unlock",
                                           {"kid": "max", "pin": "2222"})[1]
        wrong_kid = self.app.post_from(self.HELPER_IP, "/api/kid_peer_reset_help",
                                       {"nonce": nonce, "kid": "ava",
                                        "kid_session": helper_unlock["kid_session"]})
        self.assertEqual(wrong_kid[0], 403)
        self.assertEqual(self.app.post_from(self.HELPER_IP, "/api/kid_peer_reset_help",
                         {"nonce": nonce, "kid": "max", "kid_session": "made-up"})[0], 403)
        self.assertEqual(self.app.post_from(self.HELPER_IP, "/api/kid_peer_reset_help",
                         {"nonce": nonce, "kid": "max",
                          "kid_session": helper_unlock["kid_session"]})[0], 200)
        no_hold = self.app.post_from(self.TARGET_IP, "/api/kid_peer_reset_finish",
                                     {"nonce": nonce, "kid": "ava", "new_pin": "6767"})
        self.assertEqual(no_hold[0], 409)

    def test_success_changes_pin_once_and_moves_one_existing_point_without_inflating_earnings(self):
        before = self.app.state()
        nonce, token = self.begin()
        held = self.app.post_from(self.HELPER_IP, "/api/kid_peer_reset_hold",
                                  {"nonce": nonce, "kid": "max", "kid_session": token, "holding": True})
        self.assertEqual(held[0], 200)
        code, finished = self.app.post_from(self.TARGET_IP, "/api/kid_peer_reset_finish",
                                            {"nonce": nonce, "kid": "ava", "new_pin": "6767"})
        self.assertEqual(code, 200, finished)
        self.assertNotIn("6767", json.dumps(finished))
        self.assertNotIn(token, (self.app.dir / "peer_pin_resets.json").read_text())
        after = finished["state"]
        self.assertEqual(after["balance"]["ava"], before["balance"]["ava"] - 1)
        self.assertEqual(after["balance"]["max"], before["balance"]["max"] + 1)
        self.assertEqual(after["earned"], before["earned"])
        transfers = [x for x in after["history"] if x["type"] == "transfer"]
        self.assertEqual(len(transfers), 1)
        self.assertEqual((transfers[0]["from"], transfers[0]["to"], transfers[0]["points"]),
                         ("ava", "max", 1))
        self.assertEqual(self.app.post_from(self.TARGET_IP, "/api/kid_unlock",
                                            {"kid": "ava", "pin": "1111"})[0], 403)
        self.assertEqual(self.app.post_from(self.TARGET_IP, "/api/kid_unlock",
                                            {"kid": "ava", "pin": "6767"})[0], 200)
        self.assertEqual(self.app.post_from(self.TARGET_IP, "/api/kid_peer_reset_finish",
                                            {"nonce": nonce, "kid": "ava", "new_pin": "9999"})[0], 409)
        self.assertEqual(len([x for x in self.app.state()["history"] if x["type"] == "transfer"]), 1)

    def test_unplug_revokes_trust_until_ben_checks_two_fresh_hubs(self):
        self.s.GUARD["hub_heartbeat"]["helper_hub"] -= 20
        self.assertTrue(self.s.invalidate_hub_placement("A kid Nest Hub stopped checking in", ["helper_hub"]))
        blocked = self.app.post_from(self.TARGET_IP, "/api/kid_peer_reset_request", {"kid": "ava"})
        self.assertEqual(blocked[0], 409)
        review = self.app.state()["hub_placement_review"]
        self.assertEqual(review["nonce"], self.s.HUB_PLACEMENT["nonce"])
        self.assertEqual(review["adult"], "jordan")
        self.assertFalse(next(x for x in review["displays"] if x["id"] == "helper_hub")["fresh"])
        refused = self.app.post("/api/hub_placement_attest", {"nonce": review["nonce"], "by": "casey"})
        self.assertEqual(refused[0], 403)
        stale = self.app.post("/api/hub_placement_attest", {"nonce": review["nonce"], "by": "jordan"})
        self.assertEqual(stale[0], 409)
        self.heartbeat_both()
        attested = self.app.post("/api/hub_placement_attest", {"nonce": review["nonce"], "by": "jordan"})
        self.assertEqual(attested[0], 200)
        self.assertTrue(self.s.HUB_PLACEMENT["trusted"])
        self.assertIsNone(attested[1]["state"]["hub_placement_review"])


class RewardBlockers(Case):
    DOOR = "binary_sensor.test_toilet_door"

    def setUp(self):
        super().setUp()
        self.s.HOUSE["reward_blockers"] = [{"entity": self.DOOR, "states": ["on"],
                                             "name": "Downstairs toilet door", "icon": "1f6aa",
                                             "message": "Close the downstairs toilet door, then try again."}]
        self.s.REWARD_BLOCK_CACHE.update(ts=0, value={"on": False, "items": []})
        self.app.event(type="adjust", kid="ava", points=20, reason="test")

    def test_open_door_blocks_redemption_but_closing_it_immediately_clears_the_gate(self):
        self.app.set(self.DOOR, "on")
        before = self.app.state()["balance"]["ava"]
        code, blocked = self.app.post("/api/spend", {"kid": "ava", "reward": "show", "tv": "livingroom"})
        self.assertEqual(code, 409)
        self.assertIn("Close the downstairs toilet door", blocked["error"])
        self.assertEqual(self.app.state()["balance"]["ava"], before)
        self.app.set(self.DOOR, "off")
        self.assertEqual(self.app.post("/api/spend", {"kid": "ava", "reward": "show", "tv": "livingroom"})[0], 200)

    def test_offline_sensor_fails_open_and_does_not_block_chores(self):
        self.app.set(self.DOOR, "unavailable")
        self.assertFalse(self.app.state()["reward_block"]["on"])
        self.assertEqual(self.app.post("/api/spend", {"kid": "ava", "reward": "show", "tv": "livingroom"})[0], 200)
        self.app.set(self.DOOR, "on")
        self.assertEqual(self.app.post("/api/start", {"kid": "ava", "chore": "organize"})[0], 200)

    def test_open_door_stops_adult_consent_before_any_request_is_created(self):
        next(r for r in self.s.cfg()["rewards"] if r["id"] == "vr")["needs_ok"] = True
        self.app.set(self.DOOR, "on")
        code, blocked = self.app.post("/api/reward_consent_request", {"kid": "ava", "reward": "vr"})
        self.assertEqual(code, 409)
        self.assertIn("Close the downstairs toilet door", blocked["error"])
        self.assertEqual(self.s.ADULT_REQ, {})


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

    def test_reward_audience_contract_distinguishes_solo_group_and_waiting_sessions(self):
        a = self.app
        for kid in ("ava", "max", "zoe"):
            a.event(type="adjust", kid=kid, points=50, by="jordan", reason="test")
        code, bought = a.post("/api/spend", {"kid": "ava", "reward": "movie", "tv": "livingroom",
                                             "with": ["max"], "kid_sessions": a.kid_sessions("ava", "max")})
        self.assertEqual(code, 200, bought)
        code, queued = a.post("/api/spend", {"kid": "zoe", "reward": "show", "tv": "livingroom"})
        self.assertEqual(code, 200, queued)

        tv = a.state()["tv"]["tvs"]["livingroom"]
        group = next(x for x in tv["reward_audiences"] if x["reward"] == "movie")
        solo = next(x for x in tv["reward_audiences"] if x["reward"] == "show")
        self.assertEqual((group["expected_kids"], group["expected_count"], group["state"]),
                         (["ava", "max"], 2, "waiting"))
        self.assertEqual((solo["expected_kids"], solo["expected_count"], solo["state"]),
                         (["zoe"], 1, "waiting"))
        self.assertTrue(all("group" in g for g in tv["grants"]))

        for grant in tv["grants"]:
            if grant["group"] == group["session_id"]:
                a.event(type="tv_started", ref=grant["id"], tv="livingroom", what="stream")
        tv = a.state()["tv"]["tvs"]["livingroom"]
        group = next(x for x in tv["reward_audiences"] if x["reward"] == "movie")
        solo = next(x for x in tv["reward_audiences"] if x["reward"] == "show")
        self.assertEqual((group["state"], solo["state"]), ("active", "waiting"))
        self.assertIn("started_at", group)

    def test_games_break_comes_after_the_games(self):
        # "screen break until 8:54" showed before Max had played at all
        ev = self.spend("max", "games")
        started = time.time() - 60
        self.app.event(type="tv_started", ref=ev["id"], tv="livingroom", what="hdmi", at=started)
        brk = self.app.state()["screen_break"]["max"]
        self.assertAlmostEqual(brk, started + 60 * 60 + 3600, delta=2)


class TvRewardPauseAndSave(Case):
    def setUp(self):
        super().setUp()
        self.s.HA["user_adults"] = {"jordan-ha": "jordan"}
        self.physical = []
        self.s.queue_tv_reward_pause = lambda tv, playback: self.physical.append(("pause", tv, playback))
        self.s.queue_tv_reward_resume = lambda tv, playback: self.physical.append(("resume", tv, playback))
        self.app.event(type="adjust", kid="ava", points=50, by="jordan", reason="test")
        code, bought = self.app.post("/api/spend", {"kid": "ava", "reward": "movie", "tv": "livingroom"})
        self.assertEqual(code, 200, bought)
        self.purchase = bought["event"]
        self.began = time.time() - 300
        self.app.event(type="tv_started", ref=self.purchase["id"], tv="livingroom", what="stream", at=self.began)
        player = self.s.TVS["livingroom"]["players"][0]
        self.app.set(player, "playing", media_duration=7200, media_position=1200,
                     media_position_updated_at=iso(time.time()), media_title="Fixture Movie",
                     media_content_id="fixture://movie")

    def action(self, path, **body):
        return self.app.post(path, {"by": "jordan", "tv": "livingroom",
                                    "session_id": self.purchase["id"], **body},
                             {"X-HA-User": "jordan-ha"})

    def test_pause_is_authorized_idempotent_and_restores_exact_unspent_balance(self):
        before = self.app.state()
        grant = before["tv"]["tvs"]["livingroom"]["grants"][0]
        remaining = grant["until"] - time.time()
        balance = before["balance"]["ava"]

        denied = self.app.post("/api/tv/reward_pause", {"by": "jordan", "tv": "livingroom",
                                                         "session_id": self.purchase["id"]})
        self.assertEqual(denied[0], 403)
        code, paused = self.action("/api/tv/reward_pause")
        self.assertEqual(code, 200, paused)
        self.assertTrue(paused["lock_requested"])
        self.assertAlmostEqual(paused["saved_reward"]["remaining_s"], remaining, delta=2)
        tv = paused["state"]["tv"]["tvs"]["livingroom"]
        paused_audience = tv["reward_audiences"][0]
        self.assertEqual((paused_audience["session_id"], paused_audience["state"]),
                         (self.purchase["id"], "paused"))
        self.assertAlmostEqual(paused_audience["remaining_s"], remaining, delta=2)
        self.assertTrue(all(x["state"] != "active" for x in tv["reward_audiences"]))
        self.assertEqual(tv["paused_reward"]["session_id"], self.purchase["id"])
        self.assertEqual(paused["state"]["balance"]["ava"], balance)
        self.assertEqual(self.physical[0][0:2], ("pause", "livingroom"))
        self.assertEqual(self.physical[0][2]["title"], "Fixture Movie")

        again = self.action("/api/tv/reward_pause")
        self.assertEqual((again[0], again[1].get("already")), (200, True))
        self.assertEqual(len([e for e in self.s.read_events() if e["type"] == "tv_reward_pause"]), 1)
        self.assertEqual(len(self.physical), 1)
        # The ledger alone survives a process/app restart; no GUARD entry is needed to recover the credit.
        self.s.GUARD["status"] = {}
        self.assertEqual(self.s.derive()["tv"]["tvs"]["livingroom"]["paused_reward"]["session_id"],
                         self.purchase["id"])

        time.sleep(.01)
        code, resumed = self.action("/api/tv/reward_resume")
        self.assertEqual(code, 200, resumed)
        live = resumed["state"]["tv"]["tvs"]["livingroom"]
        self.assertNotIn("paused_reward", live)
        audience = live["reward_audiences"][0]
        self.assertEqual((audience["session_id"], audience["state"]), (self.purchase["id"], "active"))
        self.assertAlmostEqual(audience["valid_until"] - time.time(), remaining, delta=2)
        self.assertEqual(resumed["state"]["balance"]["ava"], balance)
        self.assertEqual(self.physical[-1][0:2], ("resume", "livingroom"))
        again = self.action("/api/tv/reward_resume")
        self.assertEqual((again[0], again[1].get("already")), (200, True))
        self.assertEqual(len([e for e in self.s.read_events() if e["type"] == "tv_reward_resume"]), 1)

    def test_expired_pause_rejects(self):
        self.app.event(type="tv_set_until", tv="livingroom", until=time.time() - 1,
                       by="jordan", action="lock")
        code, expired = self.action("/api/tv/reward_pause")
        self.assertEqual(code, 409, expired)

    def test_competing_grant_does_not_consume_saved_time(self):
        self.assertEqual(self.action("/api/tv/reward_pause")[0], 200)
        self.app.event(type="tv_unlock", tv="livingroom", by="jordan", minutes=10,
                       until=time.time() + 600, kinds=["stream"], what="other")
        code, blocked = self.action("/api/tv/reward_resume")
        self.assertEqual(code, 409, blocked)
        self.assertIn("saved time is still safe", blocked["error"])
        self.assertEqual(self.app.state()["tv"]["tvs"]["livingroom"]["paused_reward"]["session_id"],
                         self.purchase["id"])

    def test_child_can_resume_saved_movie_without_another_charge(self):
        before = self.app.state()
        balance = before["balance"]["ava"]
        remaining = before["tv"]["tvs"]["livingroom"]["grants"][0]["until"] - time.time()
        self.assertEqual(self.action("/api/tv/reward_pause")[0], 200)
        denied = self.app.post("/api/tv/reward_resume_child", {
            "kid": "ava", "tv": "livingroom", "session_id": self.purchase["id"]})
        self.assertEqual(denied[0], 403)
        token = self.app.kid_sessions("ava")["ava"]
        code, resumed = self.app.post("/api/tv/reward_resume_child", {
            "kid": "ava", "tv": "livingroom", "session_id": self.purchase["id"],
            "kid_session": token})
        self.assertEqual(code, 200, resumed)
        self.assertEqual(resumed["participants"], ["ava"])
        self.assertEqual(resumed["state"]["balance"]["ava"], balance)
        audience = resumed["state"]["tv"]["tvs"]["livingroom"]["reward_audiences"][0]
        self.assertEqual((audience["session_id"], audience["expected_kids"], audience["state"]),
                         (self.purchase["id"], ["ava"], "active"))
        self.assertAlmostEqual(audience["valid_until"] - time.time(), remaining, delta=2)
        self.assertEqual(self.physical[-1][0:2], ("resume", "livingroom"))

    def test_child_can_add_sibling_at_shared_price_without_extending_saved_time(self):
        config_path = self.app.dir / "chores.json"
        config = json.loads(config_path.read_text())
        movie = next(x for x in config["rewards"] if x["id"] == "movie")
        movie["together_cost"] = {"2": 12}
        config_path.write_text(json.dumps(config))
        self.app.event(type="adjust", kid="max", points=30, by="jordan", reason="test")
        before = self.app.state()
        remaining = before["tv"]["tvs"]["livingroom"]["grants"][0]["until"] - time.time()
        self.assertEqual(self.action("/api/tv/reward_pause")[0], 200)
        tokens = self.app.kid_sessions("ava", "max")
        code, resumed = self.app.post("/api/tv/reward_resume_child", {
            "kid": "ava", "tv": "livingroom", "session_id": self.purchase["id"],
            "with": ["max"], "kid_sessions": tokens})
        self.assertEqual(code, 200, resumed)
        self.assertEqual(resumed["participants"], ["ava", "max"])
        self.assertEqual(resumed["cost_each"], 12)
        self.assertEqual(resumed["refunds"], {"ava": 3})
        self.assertEqual((resumed["join_cost"], resumed["price_source"]), (10, "movie_time"))
        self.assertEqual((resumed["state"]["balance"]["ava"], resumed["state"]["balance"]["max"]),
                         (38, 20))
        audience = resumed["state"]["tv"]["tvs"]["livingroom"]["reward_audiences"][0]
        self.assertEqual((audience["session_id"], audience["expected_kids"], audience["state"]),
                         (self.purchase["id"], ["ava", "max"], "active"))
        self.assertAlmostEqual(audience["valid_until"] - time.time(), remaining, delta=2)
        self.assertEqual(resumed["state"]["refundable"]["ava"], [])

    def test_saved_join_falls_back_to_prorated_reward_time_without_a_movie_cursor(self):
        config_path = self.app.dir / "chores.json"
        config = json.loads(config_path.read_text())
        next(x for x in config["rewards"] if x["id"] == "movie")["together_cost"] = {"2": 12}
        config_path.write_text(json.dumps(config))
        self.app.event(type="adjust", kid="max", points=30, by="jordan", reason="test")
        self.app.event(type="tv_set_until", tv="livingroom", until=time.time() + 3000,
                       by="jordan", action="minus")
        self.app.set(self.s.TVS["livingroom"]["players"][0], "idle")
        self.assertEqual(self.action("/api/tv/reward_pause")[0], 200)
        saved = self.app.state()["tv"]["tvs"]["livingroom"]["saved_rewards"][0]
        self.assertNotIn("movie_remaining_fraction", saved)
        tokens = self.app.kid_sessions("ava", "max")
        code, resumed = self.app.post("/api/tv/reward_resume_child", {
            "kid": "ava", "tv": "livingroom", "session_id": self.purchase["id"],
            "with": ["max"], "kid_sessions": tokens})
        self.assertEqual(code, 200, resumed)
        self.assertEqual((resumed["join_cost"], resumed["price_source"]), (6, "reward_time"))
        self.assertEqual((resumed["state"]["balance"]["ava"], resumed["state"]["balance"]["max"]),
                         (38, 24))

    def test_isolated_physical_lock_path_pauses_player_then_casts_explanation(self):
        calls = []
        self.s.cast = lambda ip, path: calls.append(("cast", ip, path))
        playback = {"player": self.s.TVS["livingroom"]["players"][0], "pos": 1200,
                    "title": "Fixture Movie"}
        self.s.pause_and_lock_tv_reward("livingroom", playback, tok="fixture-token")
        self.assertIn(("media_player.media_pause", {"entity_id": playback["player"]}), self.app.calls)
        self.assertEqual(calls, [("cast", self.s.TVS["livingroom"]["ip"],
                                  "/tv?paused_reward=1&room=livingroom")])

    def test_adjustment_before_pause_is_not_reapplied_after_resume(self):
        events = [
            {"id": "buy", "type": "spend", "ts": 1000, "kid": "ava", "reward": "movie",
             "cost": 15, "tv": "livingroom", "until": 2000, "by": "jordan"},
            {"id": "adjust", "type": "tv_set_until", "ts": 1100, "tv": "livingroom",
             "until": 1500, "by": "jordan", "action": "minus"},
            {"id": "pause", "type": "tv_reward_pause", "ts": 1200, "session": "buy",
             "tv": "livingroom", "remaining_s": 300, "by": "jordan"},
            {"id": "resume", "type": "tv_reward_resume", "ts": 5000, "session": "buy",
             "tv": "livingroom", "remaining_s": 300, "until": 5300, "by": "jordan"},
        ]
        grant = self.s.tv_grants(events, now=5001)["livingroom"][0]
        self.assertEqual(grant["until"], 5300)


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


class TrainAmbientHandoff(Case):
    def setUp(self):
        super().setUp()
        self.t = self.s.TVS["livingroom"]
        self.t["room"] = "family_room"
        self.now = time.time()
        self.attrs = {"room": "family_room", "dashboard": "downstairs-faults",
                      "view": "train-family-room", "receiver_app": self.s.HA_CAST_APP_ID,
                      "observed_at": self.now - 1, "valid_until": self.now + 24}

    def lease(self, state="rotate-family_room", **patch):
        self.app.set(self.s.TRAIN_AMBIENT_ENTITY, state, **{**self.attrs, **patch})
        return self.s.train_ambient_lease(self.t, "x", self.now)

    def test_short_exact_room_lease_is_required(self):
        self.assertTrue(self.lease()["allowed"])
        self.assertTrue(self.lease("map-family_room")["allowed"])
        for field, value in (("room", "kitchen"), ("dashboard", "upstairs-faults"),
                             ("view", "train-kitchen"), ("receiver_app", self.s.DASHCAST_APP_ID),
                             ("valid_until", self.now + 60), ("valid_until", self.now - 1)):
            with self.subTest(field=field, value=value):
                self.assertFalse(self.lease(**{field: value})["allowed"])
        self.assertFalse(self.lease("shared-family_room")["allowed"])

    def test_only_the_proven_ordinary_chore_page_is_stopped(self):
        self.app.set(self.t["television"], "on", source="CAST")
        self.app.set(self.t["remote"], "on", current_activity="com.google.android.apps.mediashell")
        for player in self.t["players"]:
            self.app.set(player, "idle", app_name="DashCast", app_id=self.s.DASHCAST_APP_ID)
        self.s.GUARD["last_cast"][self.t["ip"]] = self.now
        self.s.GUARD["seen"][self.t["ip"]] = (self.now, "http://candidate/tv?paused_reward=1&v=1")
        self.assertFalse(self.s.yield_chore_screen_to_train(self.t, "x", self.now))
        self.s.GUARD["seen"][self.t["ip"]] = (self.now, "http://candidate/tv?v=1")
        started = []
        old_thread = self.s.threading.Thread

        class FakeThread:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def start(self):
                started.append(self.kwargs["name"])

        self.s.threading.Thread = FakeThread
        try:
            self.assertTrue(self.s.yield_chore_screen_to_train(self.t, "x", self.now))
        finally:
            self.s.threading.Thread = old_thread
        self.assertEqual(started, ["train-ambient-yield"])


class AdultRoomPresence(Case):
    """An adult face in this room may exempt this TV, but no looser household signal may."""

    def setUp(self):
        super().setUp()
        self.t = self.s.TVS["livingroom"]
        self.t["adult_presence"] = {
            "entity": "sensor.living_room_adult_presence",
            "room": "living_room",
            "people": ["jordan"],
            "privacy_away_field": "privacy_adult_away",
        }
        self.now = time.time()
        self.attrs = {
            "schema_version": 1,
            "authority": "chore_tv_only",
            "room": "living_room",
            "person_key": "jordan",
            "confidence": "high",
            "kid_home": ["ava"],
            "privacy_adult_away": True,
            "privacy_clear": True,
            "continuous_occupancy": True,
            "continuous_since": self.now - 120,
            "face_at": self.now - 30,
            "observed_at": self.now - 1,
            "valid_until": self.now + 14,
        }

    def result(self, state="present", **patch):
        self.app.set("sensor.living_room_adult_presence", state, **{**self.attrs, **patch})
        return self.s.tv_adult_presence(self.t, "x", self.now)

    def test_named_adult_ambient_screen_respects_cooccupants_freshness_and_manual_selection(self):
        self.t.update(kind="sony", room="den", external_display_entity="sensor.den_panel")
        validity = dict(schema_version=1, observed_at=self.now-1, valid_until=self.now+50)
        row = dict(state="occupied", people=["Jordan"], valid_until=self.now+50)
        self.app.set("sensor.room_retained_occupancy", "ok", rooms={"den":row}, expected=2, people_min=2, people_max=2, roster_complete=True, **validity)
        adult = dict(person_key="jordan", display_name="Jordan", possible_rooms=["den"], confirmed=True, located=True)
        child = dict(person_key="ava", possible_rooms=["bedroom"], located=True)
        for rooms, eligible in [(["bedroom"],True), (["bedroom","den"],False), (["den"],False), ([],False)]:
            self.app.set("sensor.occupant_tracks","2",people=[adult,dict(child,possible_rooms=rooms)],**validity)
            self.assertEqual(self.s.adult_idle_screen(self.t,"x",self.now),eligible)
        self.app.set("sensor.occupant_tracks","2",people=[adult,child],**validity)
        self.app.set("sensor.den_panel","active",lease_until=self.now+45)
        self.assertFalse(self.s.adult_idle_screen(self.t,"x",self.now))
        self.app.set("sensor.den_panel","idle")
        self.assertFalse(self.s.adult_idle_screen(self.t,"x",self.now+60))
        self.assertFalse(self.s.tv_adult_presence(self.t,"x",self.now)["allowed"])

    def test_fresh_purpose_limited_room_identity_allows_the_tv(self):
        self.assertTrue(self.result()["allowed"])

    def test_absent_unknown_and_offline_never_allow(self):
        for state in ("absent", "unknown", "unavailable"):
            with self.subTest(state=state):
                self.assertFalse(self.result(state)["allowed"])

    def test_every_identity_and_privacy_field_is_fail_closed(self):
        bad = {
            "schema_version": 2,
            "authority": "general_presence",
            "room": "kitchen",
            "person_key": "casey",
            "confidence": "medium",
            "kid_home": [],
            "privacy_adult_away": False,
            "privacy_clear": False,
            "continuous_occupancy": False,
        }
        for field, value in bad.items():
            with self.subTest(field=field):
                self.assertFalse(self.result(**{field: value})["allowed"])

    def test_stale_future_and_inconsistent_times_never_allow(self):
        bad = [
            {"continuous_since": self.attrs["face_at"]},
            {"face_at": self.attrs["observed_at"] + 1},
            {"observed_at": self.now + 1},
            {"valid_until": self.now},
            {"valid_until": self.attrs["observed_at"] + 15.1},
        ]
        for patch in bad:
            with self.subTest(patch=patch):
                self.assertFalse(self.result(**patch)["allowed"])

    def test_public_demo_does_not_publish_room_adult_presence(self):
        self.s.GUARD["status"]["livingroom"] = {"adult_presence": self.result()}
        demo = self.s.demo_attrs(self.s.derive())
        self.assertNotIn("adult_presence", demo["tv"]["tvs"]["livingroom"])


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
        self.H, self.calls, self.posts = {}, [], []
        for e in va.SENSORS:
            self.S(e, "off", 3000)
        for e in va.APPROACH_SENSORS:
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
             "ha_call": lambda svc, d, t: self.calls.append((svc, d)), "ha_post_state": lambda *a: self.posts.append(a),
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

    def test_downstairs_physical_clean_marks_vacancy_then_promotes_on_a_new_event(self):
        first = self.p.button_request({"gesture": "single", "rooms": ["Kitchen"], "request_id": "down-1"})
        self.assertEqual(first["action"], "queued_when_vacant")
        self.assertEqual(self.p.button_request(
            {"gesture": "single", "rooms": ["Living Room"], "request_id": "down-1"}), first)
        second = self.p.button_request({"gesture": "single", "rooms": ["Kitchen"], "request_id": "down-2"})
        self.assertEqual(second["action"], "clean_now_queued")
        self.assertEqual(self.p.D["manual_queue"][0]["rooms"], ["Kitchen"])

    def test_downstairs_map_buttons_set_each_kind_independently_and_deselect(self):
        vacant = self.p.button_request({"gesture": "map_vacant_on", "rooms": ["Kitchen"],
                                        "request_id": "down-map-vacant-on"})
        immediate = self.p.button_request({"gesture": "map_now_on", "rooms": ["Kitchen", "Hall"],
                                           "request_id": "down-map-now-on"})
        self.assertEqual(vacant["changed"], ["Kitchen"])
        self.assertEqual(immediate["changed"], ["Kitchen", "Hall"])
        off_now = self.p.button_request({"gesture": "map_now_off", "rooms": ["Kitchen"],
                                         "request_id": "down-map-now-off"})
        self.assertEqual(off_now["changed"], ["Kitchen"])
        self.assertEqual(self.p.D["manual_queue"][0]["rooms"], ["Hall"])
        self.assertIn("Kitchen", self.p.room_requests())
        off_vacant = self.p.button_request({"gesture": "map_vacant_off", "rooms": ["Kitchen"],
                                            "request_id": "down-map-vacant-off"})
        self.assertEqual(off_vacant["changed"], ["Kitchen"])
        self.assertNotIn("Kitchen", self.p.room_requests())
        self.assertEqual(self.p.D["manual_queue"][0]["rooms"], ["Hall"])
        self.assertNotIn(("vacuum.return_to_base", {"entity_id": self.va.VAC}), self.calls)

    def test_downstairs_busy_robot_press_has_cancel_and_dock_precedence(self):
        self.S(self.va.VAC, "cleaning")
        self.S(self.va.IN_CLEAN, "on")
        result = self.p.button_request({"gesture": "double", "rooms": ["Kitchen", "Hall"],
                                        "request_id": "down-stop"})
        self.assertEqual(result["action"], "cancelled_and_docking")
        self.assertIn(("vacuum.return_to_base", {"entity_id": self.va.VAC}), self.calls)
        self.assertEqual(self.p.D.get("manual_queue", []), [])

    def test_downstairs_idle_maintenance_probe_is_non_scheduling(self):
        result = self.p.button_request({"gesture": "maintenance_probe", "rooms": [],
                                        "request_id": "down-maintenance"})
        self.assertEqual(result["action"], "maintenance_allowed")
        self.assertEqual(result["rooms"], [])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.p.D.get("manual_queue", []), [])
        self.assertEqual(self.p.room_requests(), {})

    def test_downstairs_explicit_request_uses_validated_room_clearance_not_remote_floor_occupancy(self):
        tracker = "sensor.downstairs_people"
        self.va.OCC = {"sensor": tracker, "floor": "downstairs", "request_clearance": {
            "Kitchen": {"complete": True, "rooms": ["kitchen"],
                        "passages": [{"room": "hall", "openings": [{"contact": "binary_sensor.kitchen_hall"}]}]}
        }}
        self.S("binary_sensor.kitchen_hall", "on")                 # irrelevant while the adjacent hall is clear
        self.S(tracker, "ok", valid_until=self.now[0] + 90, roster_complete=True,
               floors={"downstairs": {"state": "occupied", "rooms": ["living"]}},
               rooms={"kitchen": {"state": "clear", "people": [], "since": self.now[0] - 3600},
                      "hall": {"state": "clear", "people": [], "since": self.now[0] - 3600},
                      "living": {"state": "occupied", "people": ["X"], "since": self.now[0] - 300}})
        self.S(self.va.ENABLE, "off")
        result = self.p.button_request({"gesture": "single", "rooms": ["Kitchen"],
                                        "request_id": "down-remote-kitchen"})
        self.assertEqual(result["action"], "queued_when_vacant")
        calls = self.tick()
        self.assertIn("script.vacuum_clean_rooms", calls)
        self.assertEqual(self.p.D["run_kind"], "request")

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

    def test_stairs_alone_pauses_before_someone_can_reach_the_floor(self):
        self.run_into_living_room()
        self.S("binary_sensor.stairs_motion", "on")
        self.assertIn("vacuum.pause", self.tick())
        entity, state, attrs, _ = next(p for p in reversed(self.posts) if p[0].endswith("vacuum_presence_pause"))
        self.assertEqual((entity, state, attrs["reason_code"], attrs["sensor"]),
                         ("binary_sensor.downstairs_vacuum_presence_pause", "on", "approach_motion",
                          "binary_sensor.stairs_motion"))

    def test_person_pauses_and_it_resumes_once_every_sensor_is_off(self):
        self.run_into_living_room()
        self.S("binary_sensor.stairs_motion", "on")
        self.assertIn("vacuum.pause", self.tick())
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

    def test_radar_explains_motion_and_publishes_plan_coordinates(self):
        rc = self.g["HOUSE"]["robot_locator"]["robots"][self.V]
        rc.update(
            occupancy={"sensor": "sensor.occ", "floor": "down"},
            room_ids={"Family Room": "family"},
            radars={"binary_sensor.den_presence": {
                "room": "Family Room", "x": 100, "y": 200, "direction": 0, "px_per_inch": 2,
                "moving": "binary_sensor.den_moving", "still": "binary_sensor.den_still",
                "moving_distance": "sensor.den_moving_distance", "distance": "sensor.den_distance"}},
        )
        self.ha.update({
            "sensor.occ": lambda: {"state": "ok", "attributes": {"valid_until": self.now + 60,
                "rooms": {"family": {"state": "clear", "valid_until": self.now + 60}},
                "floors": {"down": {"state": "vacant"}}}},
            "binary_sensor.den_presence": lambda: {"state": "on"},
            "binary_sensor.den_moving": lambda: {"state": "on"},
            "binary_sensor.den_still": lambda: {"state": "off"},
            "sensor.den_moving_distance": lambda: {"state": "50.8", "attributes": {"unit_of_measurement": "cm"}},
            "sensor.den_distance": lambda: {"state": "20", "attributes": {"unit_of_measurement": "in"}},
        })
        self.pos = (10000, 10000)
        self.vac = {"state": "cleaning", "last_changed": self.iso(900)}
        s = self.tick(1000)
        self.assertEqual((s["plan_x"], s["plan_y"], s["plan_source"]), (140.0, 200.0, "binary_sensor.den_presence"))
        explanation = s["explains"]["binary_sensor.den_presence"]
        self.assertEqual(s["explain_schema"], 1)
        self.assertEqual(explanation["kind"], "radar")
        self.assertEqual((explanation["observed_at"], explanation["valid_until"]), (1000, 1060))
        self.assertEqual(explanation["confidence_scale"], "0..1")
        self.assertTrue(explanation["independent_position"])
        self.assertEqual((explanation["position_source"], explanation["position_at"]), ("native_navigation", 1000))
        self.assertEqual(explanation["location_source"], "map")
        rows = (self.tmp / "robot-location-training.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(rows[-1])["plan_x"], 140.0)
        self.assertTrue((self.tmp / "robot_map.json").exists())

    def test_room_occupied_suppresses_robot_attribution(self):
        rc = self.g["HOUSE"]["robot_locator"]["robots"][self.V]
        rc.update(
            occupancy={"sensor": "sensor.occ", "floor": "down"},
            room_ids={"Family Room": "family"},
            radars={"binary_sensor.den_presence": {
                "room": "Family Room", "x": 100, "y": 200, "direction": 0,
                "moving": "binary_sensor.den_moving", "still": "binary_sensor.den_still",
                "moving_distance": "sensor.den_distance", "distance": "sensor.den_distance"}},
        )
        self.ha.update({
            "sensor.occ": lambda: {"state": "ok", "attributes": {"valid_until": self.now + 60,
                "rooms": {"family": {"state": "occupied", "valid_until": self.now + 60}},
                "floors": {"down": {"state": "vacant"}}}},
            "binary_sensor.den_presence": lambda: {"state": "on"},
            "binary_sensor.den_moving": lambda: {"state": "on"},
            "binary_sensor.den_still": lambda: {"state": "off"},
            "sensor.den_distance": lambda: {"state": "20", "attributes": {"unit_of_measurement": "in"}},
        })
        self.vac = {"state": "cleaning", "last_changed": self.iso(900)}
        s = self.tick(1000)
        self.assertEqual(s["explains"], {})
        self.assertIsNone(s["plan_x"])

    def test_audio_change_is_attributed_only_in_the_empty_robot_room(self):
        rc = self.g["HOUSE"]["robot_locator"]["robots"][self.V]
        rc.update(occupancy={"sensor": "sensor.occ", "floor": "down"},
                  room_ids={"Family Room": "family"},
                  audio={"sensor.den_sound": {"room": "Family Room", "min": 30, "delta": 5,
                                               "requires_independent_position": True,
                                               "corroborate_only": True}})
        sound = {"state": "20", "attributes": {"observed_at": 1000, "valid_until": 1015}}
        self.ha["sensor.den_sound"] = lambda: sound
        self.ha["sensor.occ"] = lambda: {"state": "ok", "attributes": {"valid_until": self.now + 60,
            "rooms": {"family": {"state": "clear", "valid_until": self.now + 60}},
            "floors": {"down": {"state": "vacant"}}}}
        self.vac = {"state": "cleaning", "last_changed": self.iso(900)}
        self.pos = (10000, 10000)
        self.tick(1000)
        sound["state"] = "42"
        sound["attributes"] = {"observed_at": 1005, "valid_until": 1020}
        s = self.tick(1005)
        self.assertEqual(s["explains"]["sensor.den_sound"]["kind"], "audio")
        self.assertTrue(s["explains"]["sensor.den_sound"]["independent_position"])
        self.assertTrue(next(e for e in s["evidence"] if e["src"] == "sensor.den_sound")["corroborate_only"])

    def test_corroboration_cannot_create_a_robot_location_by_itself(self):
        self.L.add(self.V, "sensor.sound", "audio", "Family Room", self.now,
                   reading=0.5, corroborate_only=True)
        self.assertEqual(self.L.fuse(self.V, self.now), (None, 0.0, None, []))

    def test_learned_affine_transform_converts_robot_map_to_plan(self):
        samples = [
            {"map_x": 0, "map_y": 0, "plan_x": 5, "plan_y": 7, "sensor": "a"},
            {"map_x": 10, "map_y": 0, "plan_x": 45, "plan_y": 7, "sensor": "a"},
            {"map_x": 0, "map_y": 10, "plan_x": 5, "plan_y": 47, "sensor": "b"},
            {"map_x": 10, "map_y": 10, "plan_x": 45, "plan_y": 47, "sensor": "b"},
        ]
        model = self.loc.affine(samples)
        self.assertEqual(model["samples"], 4)
        self.assertAlmostEqual(sum(model["x"][i] * [4, 6, 1][i] for i in range(3)), 21)
        self.assertAlmostEqual(sum(model["y"][i] * [4, 6, 1][i] for i in range(3)), 31)
        self.assertFalse(self.loc.calibrated(samples))
        enough = samples + [dict(samples[0], map_x=2, map_y=2), dict(samples[-1], map_x=8, map_y=8)]
        self.assertTrue(self.loc.calibrated(enough))

    def test_one_radar_never_creates_a_global_plan_transform(self):
        self.L.xy[self.V] = ((4, 6), self.now)
        self.L.M["_plan_samples"] = {self.V: [
            {"map_x": i, "map_y": i * i, "plan_x": 100, "plan_y": 100 + i * 30, "sensor": "one"}
            for i in range(6)]}
        self.assertIsNone(self.L.plan_position(self.V, "Family Room", self.now))

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

    def test_a_labelled_trip_stays_together_and_is_published(self):
        self.g["HOUSE"]["house_tasks"] = {"floors": {"Out": ["Yard"]}, "displays": {"door": "Yard"}}
        self.grocy["/objects/tasks"] = [
            {"id": 1, "name": "Turn off water", "done": 0,
             "userfields": {"ctx_priority": "High", "ctx_rooms": "Yard", "ctx_group": "One yard trip"}},
            {"id": 2, "name": "Unrelated urgent job", "done": 0,
             "userfields": {"ctx_priority": "High", "ctx_rooms": "Yard"}},
            {"id": 3, "name": "Measure roof", "done": 0,
             "userfields": {"ctx_rooms": "Yard", "ctx_group": "One yard trip"}},
        ]
        self.H.HouseTasks(self.g).tick()
        items = self.ha["sensor.house_tasks"]["attributes"]["items"]
        self.assertEqual([item["id"] for item in items], [1, 3, 2])
        self.assertEqual(items[0]["group"], "One yard trip")

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

    def test_login_on_a_known_hub_immediately_uses_that_hubs_house_and_room(self):
        self.at("mom")
        self.s.HUBS["family_hub"] = {"ip": "127.0.0.1", "room": "family_room", "household": "dad"}
        config = json.loads((self.app.dir / "chores.json").read_text())
        next(k for k in config["kids"] if k["id"] == "zoe")["pin_hash"] = self.s.make_kid_pin_hash("2468")
        (self.app.dir / "chores.json").write_text(json.dumps(config))
        code, reply = self.app.post("/api/kid_unlock", {"kid": "zoe", "pin": "2468"})
        self.assertEqual((code, reply["state"]["kid_where"]["zoe"]), (200, "dad"))
        evidence = self.s.GUARD["kid_display_where"]["zoe"]
        self.assertEqual((evidence["household"], evidence["room"], evidence["display_id"], evidence["schema"]),
                         ("dad", "family_room", "family_hub", 1))
        self.assertEqual(evidence["source"], "verified_kid_pin")
        self.assertGreater(evidence["valid_until"], evidence["observed_at"])
        self.assertEqual(self.app.post("/api/start", {"kid": "zoe", "chore": "mom_dishes"})[0], 409)

    def test_unknown_or_unlabelled_display_does_not_override_house_tracking(self):
        self.at("mom")
        self.s.HUBS["family_hub"] = {"ip": "127.0.0.1", "room": "family_room"}
        code, reply = self.app.post("/api/kid_unlock", {"kid": "zoe"})
        self.assertEqual((code, reply["state"]["kid_where"]["zoe"]), (200, "mom"))
        self.assertNotIn("zoe", self.s.GUARD.get("kid_display_where", {}))

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

    def test_print_can_use_a_non_desktop_cups_owner(self):
        """GNOME only announces jobs owned by the logged-in user; automated envelopes can use their own identity."""
        self.e.CFG["lp_user"] = "chore-envelope"
        code, r = self.app.post("/api/envelope", {"kid": "max", "from": "max", "to": "gran"})
        self.assertEqual(code, 200, r)
        self.assertEqual(self.cmds[0][:5], ["lp", "-U", "chore-envelope", "-d", "Office"])

    def test_prints_are_condensed_into_one_next_day_desktop_summary(self):
        now = datetime(2026, 11, 19, 9, 0).timestamp()
        self.e.CFG["desktop_summary_after"] = "08:00"
        self.e.PRINTS[:] = [{"ts": datetime(2026, 11, 18, 10, 0).timestamp()},
                           {"ts": datetime(2026, 11, 18, 16, 0).timestamp()}]
        calls = []
        run = lambda cmd, **kw: calls.append(cmd)
        self.assertEqual(self.e.desktop_summary(now, run), "Kids printed 2 envelopes yesterday.")
        self.assertIn("Kids printed 2 envelopes yesterday.", calls[0])
        self.assertIsNone(self.e.desktop_summary(now + 60, run))
        self.assertEqual(len(calls), 1)

    def test_print_and_already_recovery_each_record_an_envelope_use(self):
        self.assertEqual(self.app.post("/api/envelope", {"kid": "max", "from": "max", "to": "gran"})[0], 200)
        self.assertEqual(self.app.post("/api/envelope_already", {"kid": "max"})[0], 200)
        self.assertEqual([x["kind"] for x in self.e.USES], ["print", "already_printed"])
        self.assertEqual(len(self.e.PRINTS), 1)   # recovery affects stock, not the next-day print count

    def test_envelopes_are_consumed_when_used_and_stamps_at_handoff(self):
        self.e.CFG.update(chores=["letter"], grocy_envelope_product=91,
                          grocy_products={"Stamp": 92}, grocy_track_after=1)
        events = [{"id": "old", "type": "report", "chore": "letter", "ts": 0, "delivered": .5},
                  {"id": "r1", "type": "report", "chore": "letter", "ts": 1, "delivered": 2},
                  {"id": "r2", "type": "report", "chore": "letter", "ts": 3},
                  {"id": "d2", "type": "delivered", "ref": "r2", "ts": 4},
                  {"id": "r3", "type": "report", "chore": "other", "ts": 5, "delivered": 6}]
        uses = [{"id": "old-print", "ts": .5}, {"id": "printed", "ts": 2}, {"id": "already", "ts": 3}]
        calls = []
        sync = self.app.dir / "envelope-inventory.json"
        call = lambda method, path, body: calls.append((method, path, body))
        self.assertEqual(self.s.grocy_push_envelope_inventory(call, events, sync, uses), 4)
        self.assertEqual([x[1] for x in calls], ["/stock/products/91/consume"] * 2 + ["/stock/products/92/consume"] * 2)
        self.assertEqual(self.s.grocy_push_envelope_inventory(call, events, sync, uses), 0)
        self.assertEqual(len(calls), 4)

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
        self.assertEqual(self.app.post("/api/envelope_delivered", {"kid": "max", "chore": "letter"})[0], 409)
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

    def test_a_started_fix_auto_completes_when_the_device_is_verified_online(self):
        self.s.DOWN["binary_sensor.sensor"] = time.time() - 700
        self.assertEqual(self.app.post("/api/start", {"kid": "max", "chore": "plug"})[0], 200)
        time.sleep(0.02)
        refs = self.s.auto_verify_chore("plug", time.time(), "sensor", "Device came back online")
        self.assertEqual(len(refs), 1)
        st = self.app.state()
        done = next(h for h in st["history"] if h.get("id") == refs[0])
        self.assertEqual((done["status"], done["awarded"], st["active"]), ("inspected", 0, {}))
        self.assertTrue(any(a["title"] == "Fixer!" for a in st["awards"]["max"]), st["awards"]["max"])

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

    def test_a_kids_standing_untimed_preference_wins_for_dressing(self):
        c = self.s.cfg()
        next(k for k in c["kids"] if k["id"] == "max")["untimed_chores"] = ["get_dressed"]
        self.s.save_cfg(c)
        self.app.post("/api/dress", {"by": "jordan", "on": True, "kids": ["ava", "max"]})

        code, started = self.app.post("/api/start", {"kid": "max", "chore": "get_dressed"})
        self.assertEqual(code, 200, started)
        self.assertTrue(started["event"]["notimer"])
        self.assertNotIn("max", self.app.state()["dress"]["started"])

        code, report = self.app.post("/api/report", {"kid": "max", "chore": "get_dressed", "feeling": "ok"})
        self.assertEqual(code, 200, report)
        self.assertNotIn("started", report["event"])

        code, timed = self.app.post("/api/report", {"kid": "ava", "chore": "get_dressed", "feeling": "ok"})
        self.assertEqual(code, 200, timed)
        self.assertIn("started", timed["event"])

    def test_confirmed_house_departure_clears_the_gate_and_temporary_dressing_work(self):
        old = self.s.DRESS.get("house_vacancy")
        self.s.DRESS["house_vacancy"] = "sensor.house_occupancy"
        self.addCleanup(lambda: self.s.DRESS.pop("house_vacancy", None) if old is None else
                        self.s.DRESS.__setitem__("house_vacancy", old))
        self.app.post("/api/dress", {"by": "jordan", "on": True, "kids": ["ava", "max"]})
        since = self.app.state()["dress"]["since"]
        self.app.post("/api/start", {"kid": "ava", "chore": "get_dressed"})
        code, waiting = self.app.post("/api/report", {"kid": "max", "chore": "get_dressed", "feeling": "ok"})
        self.assertEqual(code, 200, waiting)

        vacant = {"state": "vacant", "last_changed": iso(since + 1)}
        self.assertTrue(self.s.dress_departure_step(vacant))
        st = self.app.state()
        self.assertFalse(st["dress"]["on"])
        self.assertFalse(any(x.get("chore") == "get_dressed" for x in st["active"].values()))
        self.assertFalse(any(x.get("chore") == "get_dressed" for x in st["paused_all"]))
        self.assertFalse(any(x.get("id") == waiting["event"]["id"] for x in st["history"]))
        self.assertEqual(st["balance"]["max"], 0)
        self.assertEqual(self.app.post("/api/start", {"kid": "max", "chore": "kitchen"})[0], 200)

    def test_a_stale_vacancy_from_before_the_dress_switch_is_not_a_departure(self):
        old = self.s.DRESS.get("house_vacancy")
        self.s.DRESS["house_vacancy"] = "sensor.house_occupancy"
        self.addCleanup(lambda: self.s.DRESS.pop("house_vacancy", None) if old is None else
                        self.s.DRESS.__setitem__("house_vacancy", old))
        self.app.post("/api/dress", {"by": "jordan", "on": True, "kids": ["ava"]})
        since = self.app.state()["dress"]["since"]
        self.assertFalse(self.s.dress_departure_step({"state": "vacant", "last_changed": iso(since - 1)}))
        self.assertTrue(self.app.state()["dress"]["on"])


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

    def test_school_morning_awards_use_absolute_completion_time(self):
        self.s.SCHOOL["awards"] = [
            {"by": "06:30", "icon": "1f680", "title": "Morning Rocket!", "sub": "Dressed by 6:30 — amazing!"},
            {"by": "06:45", "icon": "2705", "title": "School Ready!", "sub": "Dressed by 6:45 — ready on time"},
        ]
        morning = self.MON.replace(hour=5, minute=30).timestamp()
        self.app.event(type="dress", on=True, kids=["ava", "max", "zoe"], by="auto", school=True, ts=morning)
        for kid, hm in (("ava", (6, 30)), ("max", (6, 45)), ("zoe", (6, 46))):
            done = self.MON.replace(hour=hm[0], minute=hm[1]).timestamp()
            report = self.app.event(type="report", kid=kid, chore="get_dressed", feeling="ok", points=0,
                                    ts=done, started=done - 300)
            self.app.post("/api/inspect", {"ref": report["id"], "by": "jordan", "ok": True, "points": 0})
        awards = self.app.state()["awards"]
        self.assertTrue(any(a["title"] == "Morning Rocket!" for a in awards["ava"]), awards["ava"])
        self.assertTrue(any(a["title"] == "School Ready!" for a in awards["max"]), awards["max"])
        self.assertFalse(any(a["id"].startswith("school-dressed:") for a in awards["zoe"]), awards["zoe"])

    def test_active_laundry_adds_the_favorite_clothes_hint(self):
        old = {k: self.s.DRESS.get(k) for k in ("laundry_chore", "laundry_message")}
        self.s.DRESS.update(laundry_chore="laundry", laundry_message="Favorite clothes are in the laundry pile!")
        self.addCleanup(lambda: [self.s.DRESS.pop(k, None) if v is None else self.s.DRESS.__setitem__(k, v) for k, v in old.items()])
        self.s.SCHOOL["awards"] = [{"by": "06:30", "title": "Morning Rocket!"}]
        self.app.event(type="reopen", chore="laundry", by="jordan")
        self.app.event(type="dress", on=True, kids=["ava"], by="auto", school=True)
        dress = self.app.state()["dress"]
        self.assertEqual(dress["targets"], [{"by": "06:30", "icon": None, "title": "Morning Rocket!", "sub": None}])
        self.assertEqual(dress["laundry_hint"], "Favorite clothes are in the laundry pile!")

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
    def test_fresh_complete_ha_receipts_keep_concurrent_rooms(self):
        item = lambda receiver, room, at, until, aid: {"receiver": receiver, "rooms": [room], "attempted_at": at,
                                                       "valid_until": until, "attempt_id": aid}
        raw = {"state": "blocked", "attributes": {"recent": [item("media_player.den", "den", 90, 130, "a1"),
                                                                    item("media_player.kitchen", "kitchen", 95, 140, "a2"),
                                                                    item("media_player.old", "office", 70, 99, "old")]}}
        self.assertEqual(self.s.normalize_spotify_interruptions(raw, now=100),
                         [item("media_player.kitchen", "kitchen", 95.0, 140.0, "a2"),
                          item("media_player.den", "den", 90.0, 130.0, "a1")])
        self.assertEqual(self.s.normalize_spotify_interruptions(raw, now=140), [])
        raw["attributes"]["recent"][0].pop("receiver")
        self.assertEqual(len(self.s.normalize_spotify_interruptions(raw, now=100)), 1)

    def test_display_ip_maps_to_physical_room(self):
        hubs, tvs, aliases = self.s.HUBS, self.s.TVS, self.s.HOUSE.get("display_aliases")
        try:
            self.s.HUBS, self.s.TVS = {"den_hub": {"ip": "10.0.0.2", "room": "den"}}, {"tv": {"ip": "10.0.0.3", "room": "kitchen"}}
            self.s.HOUSE["display_aliases"] = {"office": {"kind": "tv", "ip": "10.0.0.4", "room": "office"}}
            self.assertEqual(self.s.display_for_ip("10.0.0.2"), {"kind": "hub", "id": "den_hub", "room": "den"})
            self.assertEqual(self.s.display_for_ip("10.0.0.3"), {"kind": "tv", "id": "tv", "room": "kitchen"})
            self.assertEqual(self.s.display_for_ip("10.0.0.4"), {"kind": "tv", "id": "office", "room": "office"})
            self.assertIsNone(self.s.display_for_ip("10.0.0.9"))
        finally:
            self.s.HUBS, self.s.TVS = hubs, tvs
            if aliases is None:
                self.s.HOUSE.pop("display_aliases", None)
            else:
                self.s.HOUSE["display_aliases"] = aliases

    def test_exempt_room_never_activates_from_stale_mixed_or_alias_receipt(self):
        old_spotify = self.s.HOUSE.get("spotify")
        old_interruptions = self.s.GUARD.get("music_interruptions")
        try:
            self.s.HOUSE["spotify"] = {"exempt_rooms": ["primary_bedroom", "office"],
                                         "room_aliases": {"master": "primary_bedroom"}}
            now = time.time()
            self.s.GUARD["music_interruptions"] = [
                {"receiver": "mixed", "rooms": ["master", "den"], "attempted_at": now - 1,
                 "valid_until": now + 60, "attempt_id": "fresh"},
                {"receiver": "stale", "rooms": ["office"], "attempted_at": now - 120,
                 "valid_until": now - 1, "attempt_id": "stale"},
            ]
            self.assertIsNone(self.s.active_spotify_interruption("primary_bedroom"))
            self.assertIsNone(self.s.active_spotify_interruption("master"))
            self.assertIsNone(self.s.active_spotify_interruption("office"))
            self.assertEqual(self.s.active_spotify_interruption("den")["attempt_id"], "fresh")
        finally:
            if old_spotify is None:
                self.s.HOUSE.pop("spotify", None)
            else:
                self.s.HOUSE["spotify"] = old_spotify
            if old_interruptions is None:
                self.s.GUARD.pop("music_interruptions", None)
            else:
                self.s.GUARD["music_interruptions"] = old_interruptions

    def test_trusted_proxy_recovers_only_the_actual_configured_display(self):
        self.s.HUBS.clear()
        self.s.HUBS["den_hub"] = {"ip": "10.88.0.20", "room": "den"}
        self.s.HUBS["play_hub"] = {"ip": "10.88.0.21", "room": "play"}
        token = self.app.dir / "proxy.token"
        token.write_text("proxy-secret\n")
        token.chmod(0o600)
        default_off = {"X-Chore-Proxy-Token": "proxy-secret", "X-Forwarded-For": "10.88.0.20"}
        self.assertEqual(self.app.post_from("127.0.0.2", "/api/hub_heartbeat", {}, default_off)[0], 403)
        self.s.HOUSE["trusted_display_proxy"] = {
            "token_file": str(token), "peer_cidrs": ["127.0.0.2/32"],
        }
        good = {"X-Chore-Proxy-Token": "proxy-secret", "X-Forwarded-For": "10.88.0.20"}
        code, reply = self.app.post_from("127.0.0.2", "/api/hub_heartbeat", {}, good)
        self.assertEqual((code, reply["hub"]), (200, "den_hub"))
        state = self.app.get("/api/state", headers=good, source_ip="127.0.0.2")[1]
        self.assertEqual(state["display"], {"kind": "hub", "id": "den_hub", "room": "den"})
        other = dict(good, **{"X-Forwarded-For": "10.88.0.21"})
        self.assertEqual(self.app.post_from("127.0.0.2", "/api/hub_heartbeat", {}, other)[1]["hub"],
                         "play_hub")
        leftmost_forged = dict(good, **{"X-Forwarded-For": "10.88.0.21, 10.88.0.20"})
        self.assertEqual(self.app.post_from("127.0.0.2", "/api/hub_heartbeat", {}, leftmost_forged)[1]["hub"],
                         "den_hub")

        no_token = self.app.post_from("127.0.0.2", "/api/hub_heartbeat", {},
                                      {"X-Forwarded-For": "10.88.0.20"})
        self.assertEqual(no_token[0], 403)
        forged = dict(good, **{"X-Forwarded-For": "10.88.0.20, 10.88.0.99"})
        self.assertEqual(self.app.post_from("127.0.0.2", "/api/hub_heartbeat", {}, forged)[0], 403)
        self.assertEqual(self.app.post_from("127.0.0.3", "/api/hub_heartbeat", {}, good)[0], 403)
        token.chmod(0o644)
        self.assertEqual(self.app.post_from("127.0.0.2", "/api/hub_heartbeat", {}, good)[0], 403)

    def test_proxy_identity_never_grants_loopback_parent_authorization(self):
        self.s.HUBS.clear()
        self.s.HUBS["den_hub"] = {"ip": "10.88.0.20", "room": "den"}
        token = self.app.dir / "proxy.token"
        token.write_text("proxy-secret\n")
        token.chmod(0o600)
        self.s.HOUSE["trusted_display_proxy"] = {
            "token_file": str(token), "peer_cidrs": ["127.0.0.2/32"],
        }
        headers = {"X-Chore-Proxy-Token": "proxy-secret", "X-Forwarded-For": "10.88.0.20",
                   "X-HA-User": "pretend-parent"}
        self.assertEqual(self.app.get("/api/kid_preview?kid=ava", headers=headers,
                                      source_ip="127.0.0.2")[0], 403)


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
        code, kept = self.app.post("/api/keep", {"kid": "ava", "by": "jordan", "until": until})
        self.assertEqual(code, 200)
        self.assertTrue(kept.get("already"))
        z = self.app.state()["paused"]["ava"]
        self.assertEqual((z["title"], z["until"], z["indefinite"]), ("ROCKS", None, True))

    def test_every_indefinite_hold_is_published_and_can_be_selected(self):
        self.app.post("/api/start", {"kid": "ava", "chore": "organize"})
        self.app.post("/api/pause", {"kid": "ava"})
        self.app.post("/api/start", {"kid": "ava", "chore": "kitchen"})
        self.app.post("/api/pause", {"kid": "ava"})
        st = self.app.state()
        self.assertEqual([x["chore"] for x in st["paused_all"]], ["organize", "kitchen"])
        self.assertTrue(all(x["indefinite"] and x["until"] is None for x in st["paused_all"]))
        self.assertEqual(self.app.post("/api/resume", {"kid": "ava", "chore": "organize"})[0], 200)
        st = self.app.state()
        self.assertEqual(st["active"]["ava"]["chore"], "organize")
        self.assertEqual([x["chore"] for x in st["paused_all"]], ["kitchen"])

    def test_expired_legacy_hold_does_not_resurrect_at_cutover(self):
        self.app.post("/api/start", {"kid": "ava", "chore": "organize"})
        self.app.event(type="pause", kid="ava", chore="organize", elapsed=10, team=["ava"],
                       until=time.time() - 1)
        self.assertEqual(self.app.state()["paused_all"], [])


class SteppedProjectApi(unittest.TestCase):
    def setUp(self):
        c = base_config()
        c["chores"].append({
            "id": "yard_project", "name": "Move yard material", "icon": "1f6de", "points": None,
            "shared": True, "household": "any", "session_label": "Yard project session",
            "stepped": {
                "version": "v1", "min_team": 2, "contexts": ["daylight", "dry"], "award_adult": "lead",
                "steps": [
                    {"id": "prepare", "phase": "session_start", "actor": "kid", "text": "Prepare the area"},
                    {"id": "location", "phase": "session_start", "scope": "project", "actor": "adult",
                     "adult_role": "lead", "text": "Show the destination"},
                    {"id": "work", "phase": "work", "actor": "kid", "control": True, "text": "Move material"},
                    {"id": "equipment", "phase": "session_stop", "actor": "kid", "safety": True,
                     "text": "Return equipment"},
                    {"id": "gate", "phase": "session_stop", "actor": "kid", "safety": True,
                     "text": "Close the gate"},
                    {"id": "inspect", "phase": "job_finish", "scope": "project", "actor": "adult",
                     "adult_role": "lead", "text": "Final inspection"},
                ],
            },
        })
        self.app = App(c)
        self.s = self.app.s
        self.s.GUARD.update(sun_up=True, sun_ts=time.time(),
                            outdoor_context={"state": "allowed", "reason": "dry", "checked_at": time.time()})

    def tearDown(self):
        self.app.close()

    def project(self):
        return self.app.state()["projects"][0]

    def step(self, project, step, **actor):
        return self.app.post("/api/project/step", {"instance": project["instance"], "revision": project["revision"],
                                                    "step": step, **actor})

    def test_full_multi_session_flow_and_per_kid_awards(self):
        code, started = self.app.post("/api/project/start", {"kid": "ava", "chore": "yard_project", "team": ["max"]})
        self.assertEqual(code, 200, started)
        p = self.project()
        self.assertEqual((p["status"], p["revision"], p["next"]["step"]["id"]), ("active", 1, "prepare"))
        self.assertEqual(self.step(p, "prepare", kid="ava")[0], 200)
        p = self.project()
        self.assertEqual(self.step(p, "location", by="jordan")[0], 200)
        p = self.project()
        self.assertEqual(p["next"]["kind"], "work")
        self.assertEqual(self.app.post("/api/project/stop", {"instance": p["instance"], "revision": p["revision"],
                                                              "kid": "max", "mode": "pause"})[0], 200)
        p = self.project()
        self.assertEqual(self.step(p, "equipment", kid="max")[0], 200)
        p = self.project()
        self.assertEqual(self.step(p, "gate", kid="ava")[0], 200)
        p = self.project()
        self.assertEqual(p["next"]["kind"], "award")
        code, awarded = self.app.post("/api/project/award", {"instance": p["instance"], "revision": p["revision"],
                                                               "by": "jordan", "awards": {"ava": 2, "max": 4}})
        self.assertEqual(code, 200, awarded)
        st = self.app.state()
        self.assertEqual((st["balance"]["ava"], st["balance"]["max"]), (2, 4))
        p = st["projects"][0]
        self.assertEqual(p["status"], "held")

        # A different team resumes; the project-once adult step is not repeated.
        self.assertEqual(self.app.post("/api/project/start", {"kid": "max", "chore": "yard_project", "team": ["zoe"]})[0], 200)
        p = self.project()
        self.assertEqual(self.step(p, "prepare", kid="zoe")[0], 200)
        self.assertEqual(self.project()["next"]["kind"], "work")

    def test_stale_hub_action_returns_authoritative_project(self):
        self.app.post("/api/project/start", {"kid": "ava", "chore": "yard_project", "team": ["max"]})
        p = self.project()
        self.assertEqual(self.step(p, "prepare", kid="ava")[0], 200)
        code, stale = self.step(p, "prepare", kid="ava")
        self.assertEqual(code, 409)
        self.assertGreater(stale["project"]["revision"], p["revision"])
        self.assertEqual(stale["project"]["next"]["step"]["id"], "location")

    def test_a_kid_must_end_an_active_project_session_before_another_chore(self):
        self.app.post("/api/project/start", {"kid": "ava", "chore": "yard_project", "team": ["max"]})
        code, blocked = self.app.post("/api/start", {"kid": "ava", "chore": "kitchen"})
        self.assertEqual(code, 409)
        self.assertIn("End your ongoing project session", blocked["error"])
        self.assertEqual(self.app.state()["projects"][0]["active_session"]["team"], ["ava", "max"])

    def test_unknown_weather_needs_adult_approval_and_rain_blocks(self):
        self.s.GUARD["outdoor_context"] = {"state": "unknown", "reason": "weather unavailable"}
        code, unknown = self.app.post("/api/project/start", {"kid": "ava", "chore": "yard_project", "team": ["max"]})
        self.assertEqual((code, unknown.get("needs_adult")), (409, True))
        self.assertEqual(self.app.post("/api/project/context_approve", {"chore": "yard_project", "by": "jordan"})[0], 200)
        self.assertEqual(self.app.post("/api/project/start", {"kid": "ava", "chore": "yard_project", "team": ["max"]})[0], 200)

        other = App(base_config() | {"chores": base_config()["chores"] + [
            next(x for x in self.app.s.cfg()["chores"] if x["id"] == "yard_project")]})
        try:
            other.s.GUARD.update(sun_up=True, sun_ts=time.time(),
                                 outdoor_context={"state": "blocked", "reason": "rain"})
            code, wet = other.post("/api/project/start", {"kid": "ava", "chore": "yard_project", "team": ["max"]})
            self.assertEqual(code, 409)
            self.assertIn("rain", wet["error"])
        finally:
            other.close()

    def test_context_loss_enters_unresolved_safety_stop(self):
        self.app.post("/api/project/start", {"kid": "ava", "chore": "yard_project", "team": ["max"]})
        p = self.project()
        self.step(p, "prepare", kid="ava")
        p = self.project()
        self.step(p, "location", by="jordan")
        self.s.GUARD["outdoor_context"] = {"state": "blocked", "reason": "rain"}
        self.assertEqual(self.s.project_safety_stop("context"), 1)
        p = self.project()
        self.assertEqual((p["status"], p["active_session"]["stop_reason"], p["next"]["step"]["id"]),
                         ("stopping", "context", "equipment"))

    def test_final_session_needs_adult_inspection_before_completion(self):
        self.app.post("/api/project/start", {"kid": "ava", "chore": "yard_project", "team": ["max"]})
        for sid, actor in (("prepare", {"kid": "ava"}), ("location", {"by": "jordan"})):
            p = self.project()
            self.assertEqual(self.step(p, sid, **actor)[0], 200)
        p = self.project()
        self.app.post("/api/project/stop", {"instance": p["instance"], "revision": p["revision"],
                                             "kid": "ava", "mode": "finish"})
        for sid in ("equipment", "gate"):
            p = self.project()
            self.step(p, sid, kid="max")
        p = self.project()
        self.app.post("/api/project/award", {"instance": p["instance"], "revision": p["revision"],
                                              "by": "jordan", "awards": {"ava": 1, "max": 1}})
        p = self.project()
        self.assertEqual((p["status"], p["next"]["step"]["id"]), ("finishing", "inspect"))
        self.assertEqual(self.step(p, "inspect", by="jordan")[0], 200)
        self.assertEqual(self.project()["status"], "complete")


class OutdoorContext(unittest.TestCase):
    def setUp(self):
        self.app = App()
        self.s = self.app.s
        self.old = self.s.HOUSE.get("weather_context")
        self.s.HOUSE["weather_context"] = {"max_age_s": 600, "weather": ["weather.home"],
                                            "rain": ["sensor.rain"], "rain_threshold": 0}

    def tearDown(self):
        if self.old is None:
            self.s.HOUSE.pop("weather_context", None)
        else:
            self.s.HOUSE["weather_context"] = self.old
        self.app.close()

    def test_stale_zero_rain_alone_is_unknown(self):
        self.app.set("weather.home", "unavailable", ago=0)
        self.app.set("sensor.rain", "0.0", ago=3600)
        self.assertEqual(self.s.read_outdoor_context("test")["state"], "unknown")

    def test_fresh_weather_can_establish_dry_despite_stale_numeric_sensor(self):
        self.app.set("weather.home", "partlycloudy", ago=5)
        self.app.set("sensor.rain", "0.0", ago=3600)
        self.assertEqual(self.s.read_outdoor_context("test")["state"], "allowed")

    def test_any_fresh_positive_rain_blocks(self):
        self.app.set("weather.home", "sunny", ago=5)
        self.app.set("sensor.rain", "0.2", ago=5)
        self.assertEqual(self.s.read_outdoor_context("test")["state"], "blocked")


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
    def test_room_can_be_reopened_as_one_mandatory_job_for_selected_present_kids(self):
        a = App(base_config())
        a.s.GUARD["home_now"] = {"ava", "max"}
        a.s.HOUSE["tidy_spaces"] = [{"id": "kitchen", "name": "Kitchen", "chores": ["kitchen"]}]
        code, r = a.post("/api/space_tidy", {"space": "kitchen", "tidy": False,
                                              "block_kids": ["ava", "max"],
                                              "items": ["Tidy Kitchen", "Clear the counters"], "by": "jordan"})
        self.assertEqual(code, 200, r)
        st = r["state"]
        self.assertEqual(st["present_kids"], ["ava", "max"])
        for kid in ("ava", "max"):
            self.assertEqual(st["chores_first"][kid]["chore"], "kitchen")
            self.assertEqual(st["chores_first"][kid]["name"], "Kitchen")
            self.assertEqual(st["chores_first"][kid]["items"], ["Tidy Kitchen", "Clear the counters"])
        self.assertEqual(a.post("/api/start", {"kid": "max", "chore": "family_room"})[0], 409)
        self.assertEqual(a.post("/api/start", {"kid": "ava", "chore": "kitchen"})[0], 200)
        rep = a.post("/api/report", {"kid": "ava", "chore": "kitchen", "feeling": "ok"})[1]["event"]
        self.assertEqual(a.post("/api/inspect", {"ref": rep["id"], "by": "jordan", "ok": True, "points": 3})[0], 200)
        self.assertFalse(a.state()["chores_first"]["ava"]["on"])
        self.assertFalse(a.state()["chores_first"]["max"]["on"])
        a.close()

    def test_room_block_rejects_absent_kid_and_adult_tidy_lifts_it(self):
        a = App(base_config())
        a.s.GUARD["home_now"] = {"ava"}
        a.s.HOUSE["tidy_spaces"] = [{"id": "kitchen", "name": "Kitchen", "chores": ["kitchen"]}]
        self.assertEqual(a.post("/api/space_tidy", {"space": "kitchen", "tidy": False,
                                                     "block_kids": ["max"], "by": "jordan"})[0], 409)
        self.assertEqual(a.post("/api/space_tidy", {"space": "kitchen", "tidy": False,
                                                     "block_kids": ["ava"], "by": "jordan"})[0], 200)
        self.assertTrue(a.state()["chores_first"]["ava"]["on"])
        self.assertEqual(a.post("/api/space_tidy", {"space": "kitchen", "tidy": True,
                                                     "by": "jordan"})[0], 200)
        gate = a.state()["chores_first"]["ava"]
        self.assertFalse(gate["on"])
        self.assertTrue(gate["lifted"]["manual"])
        a.close()

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
        # A free-text chores-first block has no configured room chore. It still permits only the
        # generated clean-up job (plus bedtime-safe duties), never an unrelated ordinary job.
        code, blocked = a.post("/api/start", {"kid": "ava", "chore": "kitchen"})
        self.assertEqual(code, 409, blocked)
        self.assertNotIn("ava", a.state()["active"])
        self.assertEqual(a.post("/api/start", {"kid": "ava", "chore": "cleanup_ava"})[0], 200)
        self.assertEqual(a.post("/api/spend", {"kid": "ava", "reward": "show"})[0], 409)
        self.assertEqual(a.post("/api/spend", {"kid": "max", "reward": "show", "with": ["ava"]})[0], 409)   # not by watching along
        # not yet: still blocked, back to the kid
        self.assertEqual(a.post("/api/report", {"kid": "ava", "chore": "cleanup_ava", "feeling": "ok"})[0], 409)
        checklist = ["Legos off the floor", "Shoes away"]
        rep = a.post("/api/report", {"kid": "ava", "chore": "cleanup_ava", "feeling": "ok", "checklist": checklist})[1]["event"]
        self.assertEqual(a.post("/api/inspect", {"ref": rep["id"], "by": "jordan", "ok": False, "feedback": "shoes"})[0], 200)
        self.assertTrue(a.state()["chores_first"]["ava"]["on"])
        rep = a.post("/api/report", {"kid": "ava", "chore": "cleanup_ava", "feeling": "ok", "checklist": checklist})[1]["event"]
        self.assertEqual(a.post("/api/inspect", {"ref": rep["id"], "by": "jordan", "ok": True, "points": 0})[0], 200)
        st = a.state()
        self.assertFalse(st["chores_first"]["ava"]["on"])
        self.assertEqual(st["chores_first"]["ava"]["lifted"]["by"], "jordan")
        self.assertEqual(job().get("closed"), "fine")
        self.assertEqual(a.post("/api/spend", {"kid": "ava", "reward": "show"})[0], 200)
        # blocking again later starts fresh (the old pass doesn't count)
        a.post("/api/chores_first", {"kid": "ava", "on": True, "by": "jordan", "items": ["Dishes"]})
        self.assertTrue(a.state()["chores_first"]["ava"]["on"])

    def test_parent_can_remove_a_room_assignment_and_keep_a_free_text_block(self):
        a = App(base_config())
        a.event(type="chores_first", kid="ava", on=True, items=["Tidy Kitchen"],
                chore="kitchen", name="Kitchen", by="jordan")
        code, reply = a.post("/api/chores_first", {"kid": "ava", "on": True,
                                                   "items": ["Put away shoes"], "clear_room": True,
                                                   "by": "jordan"})
        self.assertEqual(code, 200, reply)
        gate = reply["state"]["chores_first"]["ava"]
        self.assertEqual(gate["items"], ["Put away shoes"])
        self.assertIsNone(gate["chore"])
        self.assertIsNone(gate["name"])
        self.assertEqual(a.post("/api/start", {"kid": "ava", "chore": "kitchen"})[0], 409)
        self.assertEqual(a.post("/api/start", {"kid": "ava", "chore": "cleanup_ava"})[0], 200)
        a.close()

    def test_parent_can_record_already_finished_mandatory_work(self):
        self.s.GUARD["home_now"] = {"ava"}
        self.app.post("/api/music_block", {"on": True, "by": "jordan"})
        self.app.post("/api/chores_first", {"kid": "ava", "on": True, "by": "jordan", "items": ["Blanket away"]})
        self.assertTrue(self.app.state()["music_block"]["effective"])
        code, r = self.app.post("/api/chores_first", {"kid": "ava", "on": False, "done": True,
                                                       "items": ["Blanket away"], "by": "jordan"})
        self.assertEqual(code, 200)
        gate = r["state"]["chores_first"]["ava"]
        self.assertFalse(gate["on"])
        self.assertEqual(gate["items"], ["Blanket away"])
        self.assertEqual(gate["lifted"], {"by": "jordan", "ts": gate["since"], "manual": True})
        self.assertFalse(r["state"]["music_block"]["effective"])

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

    def test_spotify_volume_cap_is_parent_verified_durable_and_published_in_manifest(self):
        a = App(base_config())
        self.assertEqual(a.state()["spotify_volume_cap"], 100)
        self.assertEqual(a.state()["spotify_exempt_rooms"], ["office", "primary_bedroom"])
        self.assertEqual(a.post("/api/spotify_volume_cap", {"cap": 37, "by": "jordan"})[0], 403)
        self.assertEqual(a.post("/api/spotify_volume_cap", {"cap": 37, "by": "casey"},
                                {"X-HA-User": "home-assistant-user-id"})[0], 403)
        for bad in (-1, 101, 37.5, "37", True):
            self.assertEqual(a.post("/api/spotify_volume_cap", {"cap": bad, "by": "jordan"},
                                    {"X-HA-User": "home-assistant-user-id"})[0], 400)
        changed = []
        a.s.after_change = lambda: changed.append(a.s.derive()["spotify_volume_cap"])
        code, saved = a.post("/api/spotify_volume_cap", {"cap": 37, "by": "jordan"},
                             {"X-HA-User": "home-assistant-user-id"})
        self.assertEqual(code, 200, saved)
        self.assertEqual((saved["state"]["spotify_volume_cap"], changed), (37, [37]))

        # A fresh interpreter reads the append-only ledger, rather than relying on process memory.
        restarted = subprocess.run(
            [sys.executable, "-c", "import server; print(server.derive()['spotify_volume_cap'])"],
            cwd=REPO, env={**os.environ, "CHORE_DATA": str(a.dir), "PYTHONPATH": str(REPO)},
            text=True, capture_output=True)
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertEqual(restarted.stdout.strip().splitlines()[-1], "37")

        sent, old_post = [], a.s.ha_post_state
        a.s.ha_post_state = lambda entity, value, attrs, tok: sent.append(
            {"entity": entity, "state": value, "attributes": attrs})
        try:
            a.s.GUARD.pop("state_sig", None)
            a.orig["push_state_sensor"](saved["state"])
        finally:
            a.s.ha_post_state = old_post
        manifest = next(x["attributes"] for x in reversed(sent) if x["entity"] == "sensor.chore_state")
        self.assertEqual(manifest["spotify_volume_cap"], 37)
        self.assertEqual(manifest["spotify_exempt_rooms"], ["office", "primary_bedroom"])
        self.assertNotIn("music_block", manifest)
        self.assertNotIn("now", manifest)
        parts = sorted((x["attributes"] for x in sent if x["attributes"].get("revision") == manifest["revision"]
                        and "payload" in x["attributes"]), key=lambda x: x["part"])
        full = json.loads("".join(x["payload"] for x in parts))
        self.assertEqual(full["spotify_volume_cap"], 37)
        self.assertEqual(full["spotify_exempt_rooms"], ["office", "primary_bedroom"])
        self.assertEqual(full["music_block"], saved["state"]["music_block"])
        a.close()

    def test_spotify_exempt_rooms_are_deduplicated_and_published(self):
        self.s.HOUSE["spotify"] = {"exempt_rooms": ["office", "primary_bedroom", "office", ""]}
        state = self.app.state()
        self.assertEqual(state["spotify_exempt_rooms"], ["office", "primary_bedroom"])

    def test_spotify_block_includes_scheduled_mandatory_jobs(self):
        state = self.s.music_block_state(
            [{"id": "1", "type": "music_block", "on": True, "by": "jordan", "ts": 10}],
            {"ava": {"on": False}},
            {"ava": {"rewards": True, "name": "Homework", "chore": "homework"}},
        )
        self.assertTrue(state["effective"])
        self.assertEqual(state["blockers"][0]["name"], "Homework")

    def test_spotify_fallback_requires_one_approved_chore_from_each_kid_home(self):
        now = datetime.now().replace(hour=18, minute=0, second=0, microsecond=0)
        evs = [{"id": "1", "type": "music_block", "on": True, "by": "jordan", "ts": now.timestamp() - 60}]
        state = self.s.music_block_state(evs, {"ava": {"on": False}, "max": {"on": False}}, {},
                                         ["ava", "max"], {}, {}, now)
        self.assertEqual([b["kid"] for b in state["blockers"]], ["ava", "max"])
        reports = {"r": {"kid": "ava", "team": ["ava"], "status": "inspected", "partial": False,
                           "inspected_ts": now.timestamp()}}
        state = self.s.music_block_state(evs, {"ava": {"on": False}, "max": {"on": False}}, {},
                                         ["ava", "max"], reports, {}, now)
        self.assertEqual([b["kid"] for b in state["blockers"]], ["max"])


class ShowerByHouse(Case):
    def test_return_is_credit_and_second_consecutive_night_requires_shower(self):
        c = base_config()
        c["households"] = [{"id": "first", "name": "First"}, {"id": "second", "name": "Second"}]
        today = date(2026, 11, 18)
        first = self.s.shower_requirement_state(c, {}, datetime(2026, 11, 18, 19, 30),
                                                 {"ava": set()}, {"ava": "first"})["ava"]
        self.assertFalse(first["due"])
        second = self.s.shower_requirement_state(c, {}, datetime(2026, 11, 18, 19, 30),
                                                  {"ava": {(today - timedelta(days=1)).isoformat()}},
                                                  {"ava": "first"})["ava"]
        self.assertTrue(second["due"])
        reports = {"s": {"kid": "ava", "team": ["ava"], "chore": "shower", "status": "pending",
                           "partial": False, "ts": datetime(2026, 11, 18, 19, 31).timestamp()}}
        done = self.s.shower_requirement_state(c, reports, datetime(2026, 11, 18, 19, 32),
                                                {"ava": {(today - timedelta(days=1)).isoformat()}},
                                                {"ava": "first"})["ava"]
        self.assertFalse(done["due"])


class GrownUpStartsVR(Case):
    def test_vr_availability_countdown_never_speaks_on_google_devices(self):
        a = App(base_config())
        a.event(type="spend", kid="ava", reward="vr", cost=2, display_minutes=15,
                ts=time.time() - 14 * 60 - 30)
        heard = []
        a.s.announce = lambda *args, **kwargs: heard.append((args, kwargs))
        a.s.vr_timers("tok")
        self.assertEqual(heard, [])
        a.close()

    def test_first_stove_job_needs_help_but_later_ones_do_not(self):
        c = base_config()
        c["chores"].insert(0, {"id": "stove", "name": "Clean stove", "icon": "1f9fd", "points": 6,
                               "cooldown_h": 0, "adult_first": True, "shared": True})
        a = App(c)
        self.assertEqual(a.post("/api/start", {"kid": "ava", "chore": "stove"})[0], 409)
        self.assertEqual(a.post("/api/report", {"kid": "ava", "chore": "stove"})[0], 409)
        code, ask = a.post("/api/assist_request", {"kid": "ava", "chore": "stove"})
        self.assertEqual(code, 200)
        self.assertEqual(a.post("/api/assist_answer", {"nonce": ask["nonce"], "by": "jordan", "ok": True})[0], 200)
        self.assertEqual(a.post("/api/start", {"kid": "ava", "chore": "stove"})[0], 200)
        code, report = a.post("/api/report", {"kid": "ava", "chore": "stove", "feeling": "ok"})
        self.assertEqual(code, 200)
        self.assertEqual(a.post("/api/inspect", {"ref": report["event"]["id"], "by": "jordan", "ok": True,
                                                  "points": 6})[0], 200)
        trained = a.state()["adult_first"]["stove"]["kids"]["ava"]
        self.assertTrue(trained["trained"])
        self.assertEqual(a.post("/api/start", {"kid": "ava", "chore": "stove"})[0], 200)
        self.assertEqual(a.post("/api/start", {"kid": "max", "chore": "stove"})[0], 409)
        a.close()

    def test_vr_does_not_buzz_a_grown_up_when_the_purchase_is_blocked_and_timeout_is_honest(self):
        c = base_config()
        next(r for r in c["rewards"] if r["id"] == "vr")["needs_ok"] = True
        a = App(c)
        a.event(type="adjust", kid="ava", points=2, reason="test")
        a.event(type="chores_first", kid="ava", on=True, items=["Pick up"], by="jordan")
        code, blocked = a.post("/api/reward_consent_request", {"kid": "ava", "reward": "vr"})
        self.assertEqual(code, 409)
        self.assertIn("Chores first", blocked["error"])
        a.event(type="chores_first", kid="ava", on=False, items=[], by="jordan")
        code, ask = a.post("/api/reward_consent_request", {"kid": "ava", "reward": "vr"})
        self.assertEqual(code, 200)
        self.assertAlmostEqual(a.state()["assist_requests"][0]["expires"] - a.s.ADULT_REQ[ask["nonce"]]["ts"], 600, delta=1)
        a.s.ADULT_REQ[ask["nonce"]]["ts"] -= a.s.ADULT_REQUEST_TTL_S + 1
        changed = []
        a.s.after_change = lambda: changed.append(True)
        code, status = a.post("/api/adult_status", {"nonce": ask["nonce"]})
        self.assertEqual((code, status["status"]), (200, "timeout"))
        self.assertTrue(changed)
        self.assertEqual(status["state"]["adult_busy_until"], 0)
        self.assertEqual(status["state"]["assist_requests"], [])
        self.assertIn("No grown-up answered yet", (REPO / "static" / "kids.html").read_text())
        a.close()

    def test_kid_can_cancel_a_waiting_adult_request(self):
        c = base_config()
        next(r for r in c["rewards"] if r["id"] == "vr")["needs_ok"] = True
        a = App(c)
        a.event(type="adjust", kid="ava", points=2, reason="test")
        code, ask = a.post("/api/reward_consent_request", {"kid": "ava", "reward": "vr"})
        self.assertEqual(code, 200)
        self.assertEqual(a.post("/api/adult_request_cancel", {"kid": "max", "nonce": ask["nonce"]})[0], 409)
        code, canceled = a.post("/api/adult_request_cancel", {"kid": "ava", "nonce": ask["nonce"]})
        self.assertEqual(code, 200)
        self.assertEqual(canceled["state"]["assist_requests"], [])
        self.assertEqual(a.s.ADULT_REQ[ask["nonce"]]["status"], "canceled")
        a.close()

    def test_vr_consent_then_payment_starts_the_timer_or_can_be_refunded(self):
        c = base_config()
        for r in c["rewards"]:
            if r["id"] == "vr":
                r["needs_ok"] = True
                r["setup_tv"] = "livingroom"
                r["setup_tv_minutes"] = 15
        a = App(c)
        a.event(type="adjust", kid="ava", points=10, reason="test")
        self.assertEqual(a.post("/api/spend", {"kid": "ava", "reward": "vr"})[0], 409)
        changed = []
        a.s.after_change = lambda: changed.append(True)
        code, ask = a.post("/api/reward_consent_request", {"kid": "ava", "reward": "vr"})
        self.assertEqual(code, 200)
        self.assertTrue(changed)  # make the request appear on Home Assistant dashboards immediately
        state = a.state()
        sent = []
        old_post = a.s.ha_post_state
        a.s.ha_post_state = lambda entity, value, attrs, tok: sent.append(
            {"entity": entity, "state": value, "attributes": attrs})
        try:
            a.s.GUARD.pop("state_sig", None)
            a.s.GUARD.pop("demo_sig", None)
            a.orig["push_state_sensor"](state)
        finally:
            a.s.ha_post_state = old_post
        manifest = sent[-1]["attributes"]
        parts = sorted((x["attributes"] for x in sent if x["attributes"].get("revision") == manifest["revision"]
                        and "payload" in x["attributes"]), key=lambda x: x["part"])
        published = json.loads("".join(x["payload"] for x in parts))
        self.assertIn(ask["nonce"], {q["nonce"] for q in published["assist_requests"]})
        self.assertIn("room_choices", published)
        self.assertIn("packing_up_next", published)
        self.assertIn("departure_display", published)
        self.assertEqual(a.post("/api/assist_answer", {"nonce": ask["nonce"], "by": "jordan", "ok": True})[0], 200)
        # A reload/retry returns the unused yes instead of buzzing the grown-up again.
        self.assertEqual(a.post("/api/reward_consent_request", {"kid": "ava", "reward": "vr"})[1]["nonce"], ask["nonce"])
        self.assertEqual(a.post("/api/adult_status", {"nonce": ask["nonce"]})[1]["status"], "ok")  # polling does not consume it
        code, r = a.post("/api/spend", {"kid": "ava", "reward": "vr", "consent": ask["nonce"]})
        self.assertEqual(code, 200, r)
        st = a.state()
        self.assertEqual((st["balance"]["ava"], len(st["timers"]), st["requests"]), (8, 1, []))
        self.assertEqual(st["timers"][0]["minutes"], 15)
        grant = next(g for g in st["tv"]["tvs"]["livingroom"]["grants"] if g["id"] == r["event"]["id"])
        self.assertAlmostEqual(grant["until"] - grant["start"], 15 * 60, delta=2)
        self.assertFalse([g for g in st["tv"]["tvs"]["kitchen"].get("grants", []) if g["id"] == r["event"]["id"]])
        self.assertGreaterEqual(st["timers"][0]["start"], r["event"]["ts"])
        # Another child's adult-consent path remains live while this turn runs;
        # the headset/setup workflow is not a global lock.
        a.event(type="adjust", kid="max", points=2, reason="test")
        code, overlap = a.post("/api/reward_consent_request", {"kid": "max", "reward": "vr"})
        self.assertEqual(code, 200)
        a.s.ADULT_REQ = a.s.load_adult_requests()  # an ordinary service reload must not lose the dialog
        self.assertIn(overlap["nonce"], {q["nonce"] for q in a.state()["assist_requests"]})
        self.assertEqual(a.post("/api/assist_answer", {"nonce": overlap["nonce"], "by": "jordan", "ok": True})[0], 200)
        code, overlapping = a.post("/api/spend", {"kid": "max", "reward": "vr", "consent": overlap["nonce"]})
        self.assertEqual(code, 200, overlapping)
        self.assertEqual({t["kid"] for t in a.state()["timers"]}, {"ava", "max"})
        # A kid can end only their own timer from the countdown screen.
        self.assertEqual(a.post("/api/timer_done", {"ref": r["event"]["id"], "kid": "max"})[0], 400)
        self.assertEqual(a.post("/api/timer_done", {"ref": r["event"]["id"], "kid": "ava"})[0], 200)
        self.assertEqual([t["kid"] for t in a.state()["timers"]], ["max"])
        self.assertEqual(a.post("/api/spend", {"kid": "ava", "reward": "vr", "consent": ask["nonce"]})[0], 409)
        self.assertEqual(a.post("/api/timer_done", {"ref": overlapping["event"]["id"]})[0], 400)
        # The ordinary two-minute refund also ends that child's timer.
        self.assertEqual(a.post("/api/refund", {"ref": overlapping["event"]["id"], "kid": "max"})[0], 200)
        st = a.state()
        self.assertEqual((st["balance"]["max"], [t for t in st["timers"] if t["kid"] == "max"]), (2, []))

    def test_busy_later_snoozes_only_the_adult_who_answered(self):
        c = base_config()
        next(r for r in c["rewards"] if r["id"] == "vr")["needs_ok"] = True
        c["chores"].insert(0, {"id": "stove", "name": "Clean stove", "icon": "1f9fd", "points": 6,
                               "cooldown_h": 0, "adult_first": True, "shared": True})
        a = App(c)
        a.event(type="adjust", kid="ava", points=2, reason="test")
        code, ask = a.post("/api/reward_consent_request", {"kid": "ava", "reward": "vr"})
        self.assertEqual(code, 200)
        self.assertEqual(a.post("/api/assist_answer", {"nonce": ask["nonce"], "by": "jordan", "ok": False})[0], 200)
        state = a.state()
        self.assertGreater(state["adult_busy"]["jordan"], time.time())
        self.assertEqual(state["adult_busy_until"], 0)  # Casey is still available.
        code, other = a.post("/api/assist_request", {"kid": "ava", "chore": "stove"})
        self.assertEqual(code, 200)
        self.assertEqual(a.s.ADULT_REQ[other["nonce"]]["who"], "casey")
        self.assertFalse(next(ch for ch in a.state()["config"]["chores"] if ch["id"] == "stove").get("hidden"))
        self.assertEqual(a.post("/api/assist_answer", {"nonce": other["nonce"], "by": "casey", "ok": False})[0], 200)
        state = a.state()
        self.assertGreater(state["adult_busy"]["casey"], time.time())
        self.assertGreater(state["adult_busy_until"], time.time())  # Now every adult is busy.
        self.assertTrue(next(ch for ch in state["config"]["chores"] if ch["id"] == "stove")["hidden"])
        a.close()

    def test_busy_later_from_the_phone_also_targets_only_that_adult(self):
        c = base_config()
        next(r for r in c["rewards"] if r["id"] == "vr")["needs_ok"] = True
        a = App(c)
        a.event(type="adjust", kid="ava", points=2, reason="test")
        code, ask = a.post("/api/reward_consent_request", {"kid": "ava", "reward": "vr"})
        self.assertEqual(code, 200)
        self.assertEqual(a.s.resolve_phone_adult_request(ask["nonce"], False)["status"], "no")
        state = a.state()
        self.assertGreater(state["adult_busy"]["jordan"], time.time())
        self.assertEqual(state["adult_busy_until"], 0)
        code, retry = a.post("/api/reward_consent_request", {"kid": "ava", "reward": "vr"})
        self.assertEqual(code, 200)
        self.assertEqual(a.s.ADULT_REQ[retry["nonce"]]["who"], "casey")
        a.close()

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
    def test_only_rewards_with_an_evening_cutoff_close(self):
        c = base_config()
        next(r for r in c["rewards"] if r["id"] == "show")["before"] = "00:00"   # always past it
        a = App(c)
        a.event(type="adjust", kid="ava", points=20, reason="test")
        code, r = a.post("/api/spend", {"kid": "ava", "reward": "show", "tv": "livingroom"})
        self.assertEqual(code, 409)
        self.assertIn("not available", r["error"])
        self.assertEqual(a.post("/api/spend", {"kid": "ava", "reward": "games", "tv": "livingroom"})[0], 200)

    def test_evening_routine_reopens_chores_after_required_brushing(self):
        c = base_config()
        c.update(routine_at="00:00", bedtime="23:59")
        a = App(c)
        # This exercises the routine gate, not the independent overnight bedtime gate.
        a.s.bedtime_state = lambda config, events: {"on": False, "since": 0, "by": None, "at": "23:59"}
        a.event(type="report", kid="ava", chore="shower", feeling="ok", points=2, ts=time.time() - 86400)
        self.assertTrue(a.state()["evening_routine"]["ava"]["due"])
        self.assertEqual(a.post("/api/start", {"kid": "ava", "chore": "other"})[0], 409)
        self.assertEqual(a.post("/api/report", {"kid": "ava", "chore": "brush_teeth"})[0], 200)
        self.assertFalse(a.state()["evening_routine"]["ava"]["due"])
        self.assertEqual(a.post("/api/start", {"kid": "ava", "chore": "other"})[0], 200)

    def test_off_without_a_cutoff(self):
        self.assertFalse(App(base_config()).state()["wind_down"]["on"])

    def test_reward_cutoff_can_start_on_a_future_date(self):
        r = {"name": "VR", "before": "00:00", "before_from": "2026-11-19"}
        self.assertFalse(self.s.reward_closed(r, datetime(2026, 11, 18, 23, 59)))
        self.assertTrue(self.s.reward_closed(r, datetime(2026, 11, 19, 0, 0)))


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
    @staticmethod
    def game_config():
        c = base_config()
        game = next(r for r in c["rewards"] if r["id"] == "games")
        game.update(two_player_split=True, watcher_cost=3)
        return c

    def test_two_players_split_the_game_price_evenly(self):
        c = self.game_config()
        a = App(c)
        self.addCleanup(a.close)
        for kid in ("ava", "max", "zoe"):
            a.event(type="adjust", kid=kid, points=5, reason="test")
        body = {"kid": "ava", "reward": "games", "tv": "livingroom", "with": ["max"],
                "game_mode": "two_player", "kid_sessions": a.kid_sessions("ava", "max")}
        code, bought = a.post("/api/spend", body)
        self.assertEqual(code, 200, bought)
        self.assertEqual(bought["event"]["cost_by_kid"], {"ava": 5, "max": 5})
        self.assertEqual((bought["state"]["balance"]["ava"], bought["state"]["balance"]["max"]), (0, 0))
        body.update({"with": ["max", "zoe"], "kid_sessions": a.kid_sessions("ava", "max", "zoe")})
        self.assertEqual(a.post("/api/spend", body)[0], 400)

    def test_selected_game_player_pays_ten_and_each_watcher_pays_three(self):
        c = self.game_config()
        a = App(c)
        self.addCleanup(a.close)
        for kid, points in (("ava", 3), ("max", 10), ("zoe", 3)):
            a.event(type="adjust", kid=kid, points=points, reason="test")
        code, bought = a.post("/api/spend", {
            "kid": "ava", "reward": "games", "tv": "livingroom", "with": ["max", "zoe"],
            "game_mode": "player_watch", "game_player": "max",
            "kid_sessions": a.kid_sessions("ava", "max", "zoe")})
        self.assertEqual(code, 200, bought)
        self.assertEqual(bought["event"]["cost_by_kid"], {"ava": 3, "max": 10, "zoe": 3})
        self.assertEqual(tuple(bought["state"]["balance"][k] for k in ("ava", "max", "zoe")), (0, 0, 0))

    def test_joining_a_running_game_as_a_watcher_costs_three_without_repricing_player(self):
        c = self.game_config()
        a = App(c)
        self.addCleanup(a.close)
        a.event(type="adjust", kid="ava", points=10, reason="test")
        a.event(type="adjust", kid="max", points=3, reason="test")
        bought = a.post("/api/spend", {"kid": "ava", "reward": "games", "tv": "livingroom"})[1]["event"]
        began = time.time() - 60
        a.event(type="tv_started", ref=bought["id"], tv="livingroom", what="hdmi", at=began)
        code, joined = a.post("/api/reward_join", {
            "kid": "max", "session_id": bought["id"], "kid_sessions": a.kid_sessions("ava", "max")})
        self.assertEqual(code, 200, joined)
        self.assertEqual((joined["join_cost"], joined["price_source"]), (3, "game_watcher"))
        self.assertEqual(joined["refunds"], {"ava": 0})
        self.assertEqual((joined["state"]["balance"]["ava"], joined["state"]["balance"]["max"]), (0, 0))

    def test_late_join_price_prefers_movie_progress_then_reward_clock(self):
        reward = next(r for r in base_config()["rewards"] if r["id"] == "movie")
        reward["together_cost"] = {"2": 12}
        now = 2000
        self.s.TVS = {"livingroom": {}}
        fallback = self.s.reward_join_price(reward, 2, 1000, 3000, "livingroom", "session", now=now)
        self.assertEqual((fallback["cost"], fallback["source"], fallback["fraction"]),
                         (6, "reward_time", .5))
        self.s.TVS["livingroom"]["reward_timeline_room"] = "fixture_room"
        movie = self.s.reward_join_price(reward, 2, 1000, 3000, "livingroom", "session", now=now,
            timeline_get=lambda room: {"schema_version": 1, "grant_id": "session", "state": "paused",
                                       "duration_ms": 8_000_000, "position_ms": 6_000_000,
                                       "observed_at": now - 1, "valid_until": now + 30})
        self.assertEqual((movie["cost"], movie["source"], movie["fraction"]),
                         (3, "movie_time", .25))
        wrong_session = self.s.reward_join_price(reward, 2, 1000, 3000, "livingroom", "session", now=now,
            timeline_get=lambda room: {"schema_version": 1, "grant_id": "other", "state": "paused",
                                       "duration_ms": 8_000_000, "position_ms": 7_000_000})
        self.assertEqual((wrong_session["cost"], wrong_session["source"]), (6, "reward_time"))
        saved = self.s.reward_join_price(reward, 2, 1000, 3000, "livingroom", "session", now=now,
                                         movie_progress={"dur": 100, "pos": 75})
        self.assertEqual((saved["cost"], saved["source"], saved["fraction"]),
                         (3, "movie_time", .25))

    def test_a_movie_together_costs_less_each(self):
        c = base_config()
        for r in c["rewards"]:
            if r["id"] == "movie":
                r["together_cost"] = {"2": 12, "3": 10}
        a = App(c)
        for k in ("ava", "max", "zoe"):
            a.event(type="adjust", kid=k, points=12, reason="test")
        self.assertEqual(a.post("/api/spend", {"kid": "ava", "reward": "movie"})[0], 409)   # alone: 15
        code, r = a.post("/api/spend", {"kid": "ava", "reward": "movie", "with": ["max"],
                                               "kid_sessions": a.kid_sessions("ava", "max")})
        self.assertEqual(code, 200, r)
        b = a.state()["balance"]
        self.assertEqual((b["ava"], b["max"], b["zoe"]), (0, 0, 12))
        self.assertEqual(a.s.reward_cost(c["rewards"][1], 3), 10)
        self.assertEqual(a.s.reward_cost(c["rewards"][1], 4), 10)
        self.assertEqual(a.s.reward_cost(c["rewards"][1], 1), 15)

    def test_shared_purchase_requires_every_kids_pin_session(self):
        c = base_config()
        next(k for k in c["kids"] if k["id"] == "ava")["pin_hash"] = self.s.make_kid_pin_hash("1111")
        next(k for k in c["kids"] if k["id"] == "max")["pin_hash"] = self.s.make_kid_pin_hash("2222")
        next(r for r in c["rewards"] if r["id"] == "movie")["together_cost"] = {"2": 12}
        a = App(c)
        self.addCleanup(a.close)
        for kid in ("ava", "max"):
            a.event(type="adjust", kid=kid, points=20, reason="test")
        ava_only = a.kid_sessions("ava", pins={"ava": "1111"})
        blocked = a.post("/api/spend", {"kid": "ava", "reward": "movie", "tv": "livingroom",
                                                "with": ["max"], "kid_sessions": ava_only})
        self.assertEqual(blocked[0], 403)
        both = a.kid_sessions("ava", "max", pins={"ava": "1111", "max": "2222"})
        bought = a.post("/api/spend", {"kid": "ava", "reward": "movie", "tv": "livingroom",
                                               "with": ["max"], "kid_sessions": both})
        self.assertEqual(bought[0], 200, bought[1])
        self.assertEqual((bought[1]["state"]["balance"]["ava"], bought[1]["state"]["balance"]["max"]), (8, 8))

    def test_joining_running_movie_reprices_everyone_without_extending_clock(self):
        c = base_config()
        next(k for k in c["kids"] if k["id"] == "ava")["pin_hash"] = self.s.make_kid_pin_hash("1111")
        next(k for k in c["kids"] if k["id"] == "max")["pin_hash"] = self.s.make_kid_pin_hash("2222")
        next(r for r in c["rewards"] if r["id"] == "movie")["together_cost"] = {"2": 12}
        a = App(c)
        self.addCleanup(a.close)
        for kid in ("ava", "max"):
            a.event(type="adjust", kid=kid, points=30, reason="test")
        bought = a.post("/api/spend", {"kid": "ava", "reward": "movie", "tv": "livingroom"})[1]["event"]
        began = time.time() - 300
        a.event(type="tv_started", ref=bought["id"], tv="livingroom", what="stream", at=began)
        before = next(x for x in a.state()["tv"]["tvs"]["livingroom"]["reward_audiences"]
                      if x["session_id"] == bought["id"])
        tokens = a.kid_sessions("ava", "max", pins={"ava": "1111", "max": "2222"})
        code, joined = a.post("/api/reward_join", {"kid": "max", "session_id": bought["id"],
                                                    "kid_sessions": tokens})
        self.assertEqual(code, 200, joined)
        self.assertEqual((joined["state"]["balance"]["ava"], joined["state"]["balance"]["max"]), (18, 18))
        self.assertEqual(joined["refunds"], {"ava": 3})
        self.assertEqual((joined["join_cost"], joined["price_source"]), (12, "reward_time"))
        audience = joined["state"]["tv"]["tvs"]["livingroom"]["reward_audiences"][0]
        self.assertEqual(audience["session_id"], bought["id"])
        self.assertEqual(audience["expected_kids"], ["ava", "max"])
        self.assertEqual(audience["state"], "active")
        self.assertAlmostEqual(audience["valid_until"], before["valid_until"], delta=1)
        self.assertAlmostEqual(audience["started_at"], began, delta=1)
        self.assertEqual(joined["state"]["refundable"]["ava"], [])

    def test_join_rejects_waiting_reward_and_missing_second_pin(self):
        c = base_config()
        next(k for k in c["kids"] if k["id"] == "ava")["pin_hash"] = self.s.make_kid_pin_hash("1111")
        next(k for k in c["kids"] if k["id"] == "max")["pin_hash"] = self.s.make_kid_pin_hash("2222")
        a = App(c)
        self.addCleanup(a.close)
        for kid in ("ava", "max"):
            a.event(type="adjust", kid=kid, points=30, reason="test")
        bought = a.post("/api/spend", {"kid": "ava", "reward": "movie", "tv": "livingroom"})[1]["event"]
        tokens = a.kid_sessions("ava", "max", pins={"ava": "1111", "max": "2222"})
        self.assertEqual(a.post("/api/reward_join", {"kid": "max", "session_id": bought["id"],
                                                      "kid_sessions": tokens})[0], 409)
        a.event(type="tv_started", ref=bought["id"], tv="livingroom", what="stream")
        self.assertEqual(a.post("/api/reward_join", {"kid": "max", "session_id": bought["id"],
                                                      "kid_sessions": {"max": tokens["max"]}})[0], 403)


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
