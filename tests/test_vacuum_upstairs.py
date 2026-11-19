"""A second robot's autopilot (an upstairs floor): its own copy, floor vacancy from the people tracker.

    python3 -m unittest discover -s tests -v        (from the repo root)
"""
import importlib.util
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from test_chore_app import iso  # noqa: E402

OCC = "sensor.room_occupancy"
ROOMS = ["hall", "bedroom", "bath", "study", "study_closet"]
UP = {
    "name": "Upstairs autopilot", "vacuum": "vacuum.up", "room_sensor": "sensor.up_room", "battery": "sensor.up_battery",
    "error": "sensor.up_error", "cleaning": "binary_sensor.up_cleaning", "mode_select": "select.up_mode",
    "enable": "input_boolean.up_autopilot", "active": "input_boolean.up_autopilot_active", "map_flag": 1,
    "room_requests": "input_text.up_requests", "map_select": "select.up_map", "map_name": "Upstairs", "publish": "sensor.up_autopilot", "log_src": "vac_up", "tvs": [], "smart_mode": "always", "untidy_blocks": True,
    "segments": {"Hall": {"rooms": ["hall"], "mop": True}, "Bedroom": {"rooms": ["bedroom"], "chore": "bedroom"},
                 "Bath": {"rooms": ["bath"], "mop": True}, "Study": {"rooms": ["study"]}, "Study Closet": {"rooms": ["study_closet"]}},
    "guest_switches": {"Bedroom": "input_boolean.bedroom_guest"},
    "approach_sensors": ["binary_sensor.stairs_motion", "binary_sensor.landing_motion"],
    "approach_rooms": ["stairs", "stairs_down"],
    "occupancy": {"sensor": OCC, "floor": "upstairs",
                  "seal": {"study": {"doors": ["binary_sensor.study_hall_door", "binary_sensor.study_bath_door"],
                                     "segments": ["Study", "Study Closet"]}}},
}
SPACES = [{"id": r, "name": r.title(), "map_rooms": [r], "chores": ["bedroom"] if r == "bedroom" else []} for r in ROOMS]


def load_copy():
    spec = importlib.util.spec_from_file_location("vacuum_auto_test_up", REPO / "vacuum_auto.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Upstairs(unittest.TestCase):
    def setUp(self):
        self.va = load_copy()
        self.va.configure(UP)
        self.now = [datetime(2026, 11, 18, 10, 0).timestamp()]
        self.va.time = type("T", (), {"time": staticmethod(lambda: self.now[0]), "sleep": staticmethod(lambda s: None)})
        self.H, self.calls, self.tidy, self.posts = {}, [], {}, []
        for e, v in (("vacuum.up", "docked"), ("sensor.up_room", "Hall"), ("binary_sensor.up_cleaning", "off"),
                     ("sensor.up_battery", "100"), ("sensor.up_error", "none"), ("select.up_mode", "vacuum"),
                     ("input_boolean.up_autopilot", "on"), ("input_boolean.bedroom_guest", "off"),
                     ("binary_sensor.study_hall_door", "on"), ("binary_sensor.study_bath_door", "on"),
                     ("binary_sensor.stairs_motion", "off"), ("binary_sensor.landing_motion", "off"),
                     ("select.up_map", "Upstairs ")):
            self.S(e, v)
        self.occ({})
        self.tmp = Path(tempfile.mkdtemp())
        house = {"tidy_spaces": SPACES, "up": UP}
        g = {"DATA": self.tmp, "ilog": lambda r: None, "ha_get": lambda e, t: self.H.get(e, {"state": "unavailable", "attributes": {}}),
             "ha_call": lambda svc, d, t: self.calls.append((svc, d)), "ha_post_state": lambda *a: self.posts.append(a),
             "derive": lambda: {"board": {"bedroom": {"clean_until": 0, "last_clean": 0}}, "active": {}, "paused": {},
                                "tidy_spaces": self.tidy},
             "TVS": {"living": {}}, "tv_status": lambda t, k: ("stream", 0), "ADULTS": {}, "HOUSE": house, "ha_token": lambda: "x",
             "adult_home": lambda who, tok: False}
        p = self.va.Pilot.__new__(self.va.Pilot)
        p.g, p.path, p.tok, p.last_pub = g, self.tmp / "v.json", "x", None
        p.D = {"visits": {}, "phase": "idle", "runs": []}
        self.p = p

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def S(self, e, st, ago=0, **attrs):
        self.H[e] = {"state": st, "attributes": attrs, "last_changed": iso(self.now[0] - ago)}

    def occ(self, who, fresh=True, roster=True, floor=None):
        """who = {room: state}; everything else clear"""
        rooms = {r: {"state": who.get(r, "clear"), "people": ["X"] if who.get(r) in ("occupied", "still") else []} for r in ROOMS}
        self.S(OCC, "ok", valid_until=self.now[0] + (90 if fresh else -10), roster_complete=roster,
               floors={"upstairs": {"state": floor or ("occupied" if who else "vacant"), "rooms": ROOMS}}, rooms=rooms)

    def tick(self, dt=30):
        self.now[0] += dt
        if self.H[OCC]["attributes"]["valid_until"] > self.now[0] - dt:   # a live tracker keeps its lease
            self.H[OCC]["attributes"]["valid_until"] = self.now[0] + 90
        self.calls.clear()
        self.p.tick()
        return self.calls

    def run_until_start(self):
        for _ in range(45):
            calls = self.tick()
            s = [c for c in calls if c[0] == "script.vacuum_clean_rooms"]
            if s:
                return s[0][1], calls
        return None, []

    def test_floor_empty_20_min_cleans_everything_in_the_robots_own_automatic_mode(self):
        launch, calls = self.run_until_start()
        self.assertIsNotNone(launch)
        self.assertEqual(sorted(launch["rooms"]), ["Bath", "Bedroom", "Hall", "Study", "Study Closet"])
        self.assertEqual(launch["map_flag"], 1)
        self.assertIn(("select.select_option", {"entity_id": "select.up_mode", "option": "smart_mode"}), calls)
        self.assertIn(("vacuum.set_fan_speed", {"entity_id": "vacuum.up", "fan_speed": "smart_mode"}), calls)

    def test_someone_in_a_bedroom_means_no_run(self):
        self.occ({"bedroom": "still"})
        launch, _ = self.run_until_start()
        self.assertIsNone(launch)
        self.assertEqual(self.p.quiet_for()[1], "bedroom still")

    def test_a_downstairs_tv_does_not_count_upstairs(self):
        self.assertGreater(self.run_until_start()[0] and 1 or 0, 0)

    def test_an_occupied_study_shut_off_by_closed_doors_is_skipped_not_waited_for(self):
        # "it can do other rooms if the doors leading to the vacuum are verifiably closed"
        self.occ({"study": "occupied"})
        self.assertIsNone(self.run_until_start()[0])                 # doors open: someone is on the floor
        self.S("binary_sensor.study_hall_door", "off")
        self.S("binary_sensor.study_bath_door", "off")
        self.p.D["occ_quiet_since"] = None
        launch, _ = self.run_until_start()
        self.assertEqual(sorted(launch["rooms"]), ["Bath", "Bedroom", "Hall"])
        self.S("vacuum.up", "cleaning")
        self.tick()
        self.S("binary_sensor.study_hall_door", "on")                # they come out: pause
        calls = self.tick()
        self.assertIn("vacuum.pause", [c[0] for c in calls])

    def test_a_stale_tracker_or_incomplete_roster_or_unknown_floor_never_starts(self):
        self.occ({}, floor="unknown")
        self.assertIsNone(self.run_until_start()[0])
        self.occ({}, fresh=False)
        self.assertIsNone(self.run_until_start()[0])
        self.occ({}, roster=False)
        self.assertIsNone(self.run_until_start()[0])

    def test_uncertain_holds_back_a_start_and_pauses_a_run_for_privacy(self):
        self.occ({"hall": "uncertain"})
        self.assertIsNone(self.run_until_start()[0])
        self.occ({})
        self.assertIsNotNone(self.run_until_start()[0])
        self.S("vacuum.up", "cleaning")
        self.tick()
        self.occ({"hall": "uncertain"})
        self.assertIn("vacuum.pause", [c[0] for c in self.tick()])
        signal = next(p for p in reversed(self.posts) if p[0] == "binary_sensor.upstairs_vacuum_presence_pause")
        self.assertEqual((signal[1], signal[2]["reason_code"]), ("on", "floor_uncertain"))
        self.occ({"hall": "occupied"})
        self.assertIn("vacuum.pause", [c[0] for c in self.tick()])

    def test_stair_tracker_room_pauses_even_before_floor_placement_changes(self):
        self.run_until_start()
        self.S("vacuum.up", "cleaning")
        self.H[OCC]["attributes"]["rooms"]["stairs"] = {"state": "uncertain", "since": self.now[0]}
        self.assertIn("vacuum.pause", [c[0] for c in self.tick()])
        signal = next(p for p in reversed(self.posts) if p[0] == "binary_sensor.upstairs_vacuum_presence_pause")
        self.assertEqual((signal[1], signal[2]["reason_code"], signal[2]["sensor"]),
                         ("on", "approach_room", OCC))

    def test_person_in_active_segment_stays_paused_until_robot_docks(self):
        self.run_until_start()
        self.S("vacuum.up", "cleaning")
        self.S("sensor.up_room", "Hall")
        self.occ({"hall":"occupied"})
        self.assertIn("vacuum.pause", [c[0] for c in self.tick()])
        pause = self.p.D["paused_at"]
        # A delayed device acknowledgment must not reset the original deadline.
        self.tick(11)
        self.assertEqual((self.p.D["phase"], self.p.D["paused_at"]), ("paused",pause))
        self.S("vacuum.up","paused")
        self.occ({"hall":"still"})
        self.assertFalse(self.tick(40))
        calls = self.tick(550)
        self.assertIn(("vacuum.return_to_base", {"entity_id":"vacuum.up"}), calls)
        self.S("vacuum.up","docked")
        self.tick()
        self.assertIn("Hall", self.p.D["incomplete"])

    def test_resume_rechecks_floor_before_retry_and_never_starts_a_whole_floor(self):
        self.run_until_start()
        self.S("vacuum.up","cleaning")
        self.occ({"hall":"occupied"})
        self.tick()
        self.S("vacuum.up","paused")
        self.occ({})
        self.tick(5)
        self.assertNotIn("vacuum.send_command", [c[0] for c in self.tick(10)])
        self.assertIn("vacuum.send_command", [c[0] for c in self.tick(11)])
        self.occ({"hall":"still"})
        self.assertNotIn("vacuum.start", [c[0] for c in self.tick(31)])
        self.assertEqual(self.p.D["phase"], "paused")

    def test_empty_roster_needs_fresh_house_vacancy_to_release_unknown_floor(self):
        self.va.OCC = dict(self.va.OCC, house_vacancy="sensor.house")
        self.occ({}, floor="unknown")
        attrs=self.H[OCC]["attributes"]
        attrs.update(expected=0,people_min=0,people_max=0,unexplained=0)
        for state,expiry in [("checking",self.now[0]+15),("vacant",self.now[0]-1)]:
            self.S("sensor.house",state,expires_at=expiry)
            self.assertIsNotNone(self.p.occ_presence()[0])
        self.S("sensor.house","vacant",expires_at=self.now[0]+15)
        self.assertIsNone(self.p.occ_presence()[0])
        attrs["expected"]=1
        self.assertIsNotNone(self.p.occ_presence()[0])

    def test_native_interruption_corrects_only_targeted_rooms_for_the_same_run(self):
        now=self.now[0];start=now-600
        run=dict(start=start,end=now-60,kind="auto",why="done",groups=[dict(rooms=["Hall","Bath","Study"])])
        self.p.D["runs"]=[run]
        self.p.room_cleaning_sensor="sensor.room_work"
        rooms={name:dict(last_job=dict(begin=begin,end=end,targeted=True,outcome=outcome))
               for name,begin,end,outcome in [
                   ("Hall",start+2,now-120,"incomplete"),("Bath",start-1000,start-120,"incomplete"),
                   ("Study",start+2,now-120,"completed"),("Bedroom",start+2,now-120,"incomplete")]}
        self.S("sensor.room_work","ready",updated_at=now,rooms=rooms)
        self.p.update_room_cleaning("Hall",now)
        self.assertEqual(self.p.D["incomplete"],["Hall"])
        self.assertEqual(run["why"],"robot reported interrupted cleaning")
        self.assertEqual(self.p.D["visits"],{})
        self.p.update_room_cleaning("Hall",now)
        self.assertEqual(self.p.D["incomplete"],["Hall"])

    def test_guest_room_and_untidy_rooms_are_skipped(self):
        self.S("input_boolean.bedroom_guest", "on")
        self.tidy["bath"] = {"state": "untidy"}
        launch, _ = self.run_until_start()
        self.assertEqual(sorted(launch["rooms"]), ["Hall", "Study", "Study Closet"])
        st = self.p.segment_status()
        self.assertEqual(st["Bedroom"]["why"], "a guest is staying here")
        self.assertIn("marked untidy", st["Bath"]["why"])

    def test_on_another_map_it_still_starts_and_the_clean_script_switches_maps(self):
        # the robot is switched to the right map automatically: the clean script loads map_flag's map first
        self.S("select.up_map", "Map 2")
        launch, _ = self.run_until_start()
        self.assertEqual(launch["map_flag"], 1)
        self.assertEqual(self.posts[-1][2]["map_other"], "Map 2")

    def test_status_stays_fresh_while_switched_off(self):
        # the floor plan only trusts a status updated in the last 10 minutes; switched off, nothing else changes
        self.S("input_boolean.up_autopilot", "off")
        self.tick()
        self.tick(301)
        autopilot = [post for post in self.posts if post[0] == "sensor.up_autopilot"]
        self.assertEqual(len(autopilot), 2)
        self.assertNotEqual(autopilot[0][2]["heartbeat"], autopilot[1][2]["heartbeat"])
        self.assertEqual(autopilot[1][2]["why"], "switched off")

    def test_a_pressed_clean_when_vacant_runs_with_the_switch_off(self):
        # "clean when vacant runs at next vacancy regardless of switch"; the switch only governs the autopilot's own runs
        self.S("input_boolean.up_autopilot", "off")
        self.assertIsNone(self.run_until_start()[0])
        self.S("input_text.up_requests", '{"Bath": %d}' % (self.now[0] - 5))
        launch, _ = self.run_until_start()
        self.assertEqual(launch["rooms"], ["Bath"])                  # only the pressed room

    def button(self, gesture="single", rooms=None, request_id="press-1"):
        return self.p.button_request({"gesture": gesture, "rooms": rooms or [], "request_id": request_id})

    def finish_manual_clean(self):
        self.S("vacuum.up", "cleaning")
        self.S("binary_sensor.up_cleaning", "on")
        self.tick(10)
        self.tick(70)                                                # confirmed real cleaning, not a failed launch
        self.S("vacuum.up", "returning")
        self.tick(10)
        self.S("vacuum.up", "docked")
        self.S("binary_sensor.up_cleaning", "off")
        self.tick(10)
        self.tick(61)                                                # one minute docked confirms completion

    def test_physical_button_fifo_is_durable_idempotent_and_runs_in_order(self):
        one = self.button(rooms=["Bath"], request_id="one")
        two = self.button("double", ["Hall"], "two")
        self.assertTrue(one["ok"] and two["ok"])
        self.assertEqual(self.button(rooms=["Bedroom"], request_id="one")["why"], "already queued")
        self.assertEqual([x["id"] for x in self.p.D["manual_queue"]], ["one", "two"])
        self.p.save()
        self.assertEqual([x["id"] for x in __import__("json").loads(self.p.path.read_text())["manual_queue"]], ["one", "two"])

        calls = self.tick()
        launch = next(d for svc, d in calls if svc == "script.vacuum_clean_rooms")
        self.assertEqual(launch["rooms"], ["Bath"])
        self.assertEqual(self.p.D["manual_active"], "one")
        self.finish_manual_clean()
        self.assertEqual([x["id"] for x in self.p.D["manual_queue"]], ["two"])
        launch = next(d for svc, d in self.tick() if svc == "script.vacuum_clean_rooms")
        self.assertEqual(launch["rooms"], ["Hall"])

    def test_button_deduplicates_rooms_already_active_or_queued(self):
        self.button(rooms=["Bath"], request_id="one")
        result = self.button("double", ["Bath", "Hall"], "two")
        self.assertEqual(result["rooms"], ["Bath", "Hall"])
        self.assertIn("already pending Bath", result["why"])
        self.assertEqual([x["rooms"] for x in self.p.D["manual_queue"]], [["Bath"], ["Hall"]])
        again = self.button(rooms=["Bath"], request_id="three")
        self.assertEqual(again["rooms"], ["Bath"])
        self.assertEqual(again["why"], "already pending: Bath")
        self.assertEqual(len(self.p.D["manual_queue"]), 2)

    def test_fifo_never_preempts_another_clean_pause_return_or_dock_break(self):
        self.button(rooms=["Bath"])
        for state, cleaning in (("cleaning", "on"), ("paused", "on"), ("returning", "on"), ("docked", "on")):
            self.S("vacuum.up", state)
            self.S("binary_sensor.up_cleaning", cleaning)
            calls = self.tick()
            self.assertNotIn("script.vacuum_clean_rooms", [svc for svc, _ in calls])
            self.assertNotIn("vacuum.return_to_base", [svc for svc, _ in calls])
        self.S("binary_sensor.up_cleaning", "off")
        calls = self.tick()
        self.assertIn("script.vacuum_clean_rooms", [svc for svc, _ in calls])

    def test_failed_manual_launch_stays_at_fifo_head_and_retries(self):
        self.H[OCC]["attributes"]["floors"]["upstairs"]["since"] = self.now[0] - self.va.START_QUIET_S
        self.button(rooms=["Bath"])
        self.tick()
        self.tick(301)                                               # never entered cleaning
        head = self.p.D["manual_queue"][0]
        self.assertEqual(head["attempts"], 1)
        self.assertIn("never started", head["last_failure"])
        self.assertNotIn("script.vacuum_clean_rooms", [svc for svc, _ in self.tick(299)])
        calls = self.tick(2)
        self.assertIn("script.vacuum_clean_rooms", [svc for svc, _ in calls])

    def test_acknowledged_long_dock_preparation_never_requeues(self):
        self.button(rooms=["Bath"])
        self.tick()
        self.S("binary_sensor.up_cleaning", "on")
        self.tick(180)
        self.tick(180)
        self.assertEqual(self.p.D["phase"], "running")
        self.assertEqual(self.p.D["manual_queue"][0]["attempts"], 0)
        self.S("binary_sensor.up_cleaning", "off")
        self.tick(10)
        self.assertEqual(self.p.D["manual_queue"], [])
        self.assertEqual(self.p.D["phase"], "idle")

    def test_retry_of_explicit_room_does_not_require_floor_vacancy(self):
        self.button(rooms=["Bath"])
        self.tick()
        self.tick(301)
        self.occ({"bedroom": "occupied"})
        self.assertIn("script.vacuum_clean_rooms", [svc for svc, _ in self.tick(301)])

    def test_explicit_tidy_override_waits_for_sleep_and_closed_entrances(self):
        self.va.SLEEP_PROTECTION = {"Study": "binary_sensor.resting"}
        self.tidy["study"] = {"state": "untidy"}
        self.occ({"study": "occupied"})
        self.S("binary_sensor.resting", "on")
        self.button(rooms=["Study"])
        self.assertIn("sleep protection", self.p.manual_wait("docked", "off", self.now[0]))
        self.S("binary_sensor.resting", "off")
        self.S("binary_sensor.study_hall_door", "off")
        self.S("binary_sensor.study_bath_door", "off")
        self.assertIn("entrances", self.p.manual_wait("docked", "off", self.now[0]))
        self.S("binary_sensor.study_hall_door", "on")
        self.assertIn("script.vacuum_clean_rooms", [svc for svc, _ in self.tick()])
        self.assertEqual(self.tidy["study"]["state"], "untidy")

    def test_long_press_skips_a_protected_sleeper(self):
        self.va.SLEEP_PROTECTION = {"Bedroom": "binary_sensor.resting"}
        self.S("binary_sensor.resting", "on")
        self.assertNotIn("Bedroom", self.button("long", [])["rooms"])
        self.assertFalse(self.p.D["manual_queue"][0]["whole_floor"])

    def test_interrupted_manual_job_is_retired_after_return_to_dock(self):
        self.button(rooms=["Bath"], request_id="original")
        self.tick()
        self.S("vacuum.up", "cleaning")
        self.tick(10)
        self.S("vacuum.up", "error")
        self.tick(80)
        self.button(rooms=["Bath"], request_id="second-press")
        self.S("vacuum.up", "docked")
        self.tick(10)
        self.tick(61)
        self.assertEqual(self.p.D["manual_queue"], [])
        self.assertIn("error", self.p.D["runs"][-1]["why"])

    def test_cancel_clears_active_and_pending_jobs_once_even_across_reload(self):
        self.button(rooms=["Bath"])
        self.tick()
        self.button(rooms=["Hall"], request_id="later")
        got = self.button("cancel", request_id="clear")
        self.assertEqual(got["rooms"], ["Bath", "Hall"])
        self.assertEqual(self.p.D["phase"], "cancelled")
        self.p.D = __import__("json").loads(self.p.path.read_text())
        self.button(rooms=["Study"], request_id="fresh")
        self.assertEqual(self.button("cancel", request_id="clear")["why"], "already cancelled")
        self.S("vacuum.up", "returning")
        self.assertNotIn("script.vacuum_clean_rooms", [svc for svc, _ in self.tick(300)])
        self.S("vacuum.up", "docked")
        self.tick()
        self.assertEqual([job["rooms"] for job in self.p.D["manual_queue"]], [["Study"]])
        self.assertEqual(self.p.D["runs"][-1]["why"], "manual jobs cancelled")

    def test_cancel_before_launch_does_not_start_or_dock_robot(self):
        self.button(rooms=["Bath"])
        self.assertTrue(self.button("cancel", request_id="clear")["ok"])
        self.assertEqual(self.p.D["manual_queue"], [])
        self.assertEqual(self.p.D["phase"], "idle")
        self.assertEqual(self.calls, [])

    def test_long_press_skips_accessible_untidy_and_other_blocked_rooms(self):
        self.tidy["bath"] = {"state": "untidy"}
        self.S("input_boolean.bedroom_guest", "on")
        result = self.button("long", [], "whole-1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["rooms"], ["Hall", "Study", "Study Closet"])
        self.assertIn("Bath (untidy)", result["why"])
        self.assertIn("Bedroom", result["why"])
        calls = self.tick()
        launch = next(d for svc, d in calls if svc == "script.vacuum_clean_rooms")
        self.assertEqual(launch["rooms"], ["Hall"])
        self.assertEqual([g["rooms"] for g in self.p.D["groups"]], [["Hall"], ["Study"], ["Study Closet"]])

    def test_queued_native_floor_rechecks_untidy_rooms_at_launch(self):
        result = self.button("long", [], "whole-later")
        self.assertEqual(result["rooms"], ["Hall", "Bedroom", "Bath", "Study", "Study Closet"])
        self.assertTrue(self.p.D["manual_queue"][0]["whole_floor"])
        self.tidy["bath"] = {"state": "untidy"}
        calls = self.tick()
        self.assertNotIn("vacuum.start", [svc for svc, _ in calls])
        launch = next(d for svc, d in calls if svc == "script.vacuum_clean_rooms")
        self.assertEqual(launch["rooms"], ["Hall"])
        self.assertFalse(self.p.D["manual_queue"][0]["whole_floor"])
        self.assertTrue(self.p.D["manual_queue"][0]["one_at_time"])

    def test_long_press_uses_genuine_whole_floor_when_no_accessible_room_is_untidy(self):
        self.tidy["study"] = {"state": "untidy"}
        self.S("binary_sensor.study_hall_door", "off")
        self.S("binary_sensor.study_bath_door", "off")
        result = self.button("long", [], "whole-2")
        self.assertTrue(result["ok"])
        self.assertEqual(result["rooms"], ["Hall", "Bedroom", "Bath"])
        self.assertTrue(self.p.D["manual_queue"][0]["whole_floor"])
        calls = self.tick()
        self.assertIn(("vacuum.start", {"entity_id": "vacuum.up"}), calls)
        self.assertNotIn("script.vacuum_clean_rooms", [svc for svc, _ in calls])

    def test_long_press_segments_around_an_accessible_standing_exclusion(self):
        self.S("input_boolean.bedroom_guest", "on")
        result = self.button("long", [], "whole-blocked")
        self.assertTrue(result["ok"])
        self.assertFalse(self.p.D["manual_queue"][0]["whole_floor"])
        self.assertEqual(result["rooms"], ["Hall", "Bath", "Study", "Study Closet"])
        self.assertIn("Bedroom", result["why"])
        calls = self.tick()
        self.assertNotIn("vacuum.start", [svc for svc, _ in calls])
        launch = next(d for svc, d in calls if svc == "script.vacuum_clean_rooms")
        self.assertEqual(launch["rooms"], ["Hall"])

    def test_button_validation_rejects_unknown_rooms_and_empty_short_gestures(self):
        self.assertFalse(self.button(rooms=["Not a room"])["ok"])
        self.assertFalse(self.button(rooms=[])["ok"])
        self.assertFalse(self.p.button_request({"gesture": "triple", "rooms": ["Bath"], "request_id": "x"})["ok"])

    def gives_up_at_once(self):
        launch, _ = self.run_until_start()
        self.assertIsNotNone(launch)
        self.S("vacuum.up", "cleaning")
        self.S("binary_sensor.up_cleaning", "on")
        self.tick(10)
        self.S("vacuum.up", "returning")
        self.tick(10)
        self.S("vacuum.up", "docked")
        self.S("binary_sensor.up_cleaning", "off")
        self.S("select.up_map", "Map 2")              # it flips itself back to the other map
        for _ in range(4):
            self.tick()

    def test_a_robot_that_gives_up_at_once_is_a_failed_start_and_backs_off(self):
        # 11-18: on its old map the robot cleaned 14 s, went home, and the status said "done" with the rooms still due
        self.gives_up_at_once()
        self.assertEqual(self.p.D["phase"], "idle")
        why = self.p.D["runs"][-1]["why"]
        self.assertIn("gave up after", why)
        self.assertIn("'Map 2'", why)
        self.S("select.up_map", "Upstairs")
        self.assertIsNone(self.run_until_start()[0])             # no retry every quiet window
        self.assertIn("start(s) failed", self.posts[-1][2]["why"])
        self.assertIn("gave up after", self.posts[-1][2]["last_run"])

    def test_a_room_pressed_after_the_failure_still_goes(self):
        self.gives_up_at_once()
        self.tick(1800)                                          # (the usual 30 minutes between attempts)
        self.S("input_text.up_requests", '{"Bath": %d}' % self.now[0])
        self.assertIsNotNone(self.run_until_start()[0])

    def test_the_status_says_what_it_waits_for(self):
        self.occ({"bedroom": "still"})
        self.tick()
        self.assertTrue(self.posts[-1][2]["why"].startswith("waiting for upstairs to be empty"), self.posts[-1][2]["why"])

    def test_pin_map_puts_a_docked_robot_back_on_its_map(self):
        self.va.PIN_MAP = True
        self.S("select.up_map", "Map 2")
        calls = self.tick()
        self.assertIn(("select.select_option", {"entity_id": "select.up_map", "option": "Upstairs"}), calls)
        self.assertNotIn("select.select_option", [c[0] for c in self.tick()])   # not every tick
        self.S("vacuum.up", "cleaning")
        self.assertNotIn("select.select_option", [c[0] for c in self.tick(120)])   # never while it drives

    def explore_setup(self, here=(True, True)):
        import schedule
        from datetime import date, timedelta
        d0 = datetime.fromtimestamp(self.now[0]).date()
        schedule.CACHE["days"] = {d0.isoformat(): here[0], (d0 + timedelta(days=1)).isoformat(): here[1]}
        schedule.CACHE["off"] = {}
        self.va.EXPLORE = {"school_from_h": 15, "free_from_h": 9, "until_h": 19}
        for seg in UP["segments"]:
            self.p.D["visits"][seg] = self.now[0]                 # everything freshly cleaned
        self.tidy["bath"] = {"state": "untidy"}

    def test_tidiness_check_explores_an_untidy_room_while_the_kids_are_here(self):
        # "robot exploring area to determine tidiness, with a robot rescue as the fallback robot retrieval mechanism"
        self.explore_setup()
        self.assertIsNone(self.run_until_start()[0])             # a school day: not before 3 pm
        self.now[0] = datetime(2026, 11, 18, 15, 0).timestamp()
        self.occ({})                                              # (the tracker's lease, after the jump in time)
        launch, _ = self.run_until_start()
        self.assertEqual(launch["rooms"], ["Bath"])
        self.S("vacuum.up", "cleaning")
        self.tick()
        self.assertTrue(self.posts[-1][2]["why"].startswith("🔍 tidiness check"))
        self.S("vacuum.up", "error")                              # stuck
        self.tick()
        self.assertIn("untidy confirmed", self.posts[-1][2]["why"])
        self.S("vacuum.up", "docked")                             # rescued and home
        for _ in range(4):
            self.tick()
        self.assertIn("untidy confirmed", self.p.D["runs"][-1]["why"])
        self.assertIsNone(self.run_until_start()[0])             # once a day

    def test_no_tidiness_check_on_their_last_day_here(self):
        self.explore_setup(here=(True, False))
        self.now[0] = datetime(2026, 11, 18, 15, 0).timestamp()
        self.occ({})                                              # (the tracker's lease, after the jump in time)
        self.assertIsNone(self.run_until_start()[0])

    def test_two_robots_never_share_settings(self):
        down = load_copy()
        down.configure({"vacuum": "vacuum.down", "room_sensor": "sensor.down_room", "cleaning": "binary_sensor.down_cleaning",
                        "mode_select": "select.down_mode", "enable": "input_boolean.a", "active": "input_boolean.b"})
        self.assertEqual((self.va.VAC, self.va.PUBLISH, bool(self.va.OCC)), ("vacuum.up", "sensor.up_autopilot", True))
        self.assertEqual((down.VAC, down.PUBLISH, bool(down.OCC)), ("vacuum.down", "sensor.vacuum_autopilot", False))


if __name__ == "__main__":
    unittest.main()
