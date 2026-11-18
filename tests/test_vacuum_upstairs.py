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
    "publish": "sensor.up_autopilot", "log_src": "vac_up", "tvs": [], "smart_mode": "always", "untidy_blocks": True,
    "segments": {"Hall": {"rooms": ["hall"], "mop": True}, "Bedroom": {"rooms": ["bedroom"], "chore": "bedroom"},
                 "Bath": {"rooms": ["bath"], "mop": True}, "Study": {"rooms": ["study"]}, "Study Closet": {"rooms": ["study_closet"]}},
    "guest_switches": {"Bedroom": "input_boolean.bedroom_guest"},
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
        self.H, self.calls, self.tidy = {}, [], {}
        for e, v in (("vacuum.up", "docked"), ("sensor.up_room", "Hall"), ("binary_sensor.up_cleaning", "off"),
                     ("sensor.up_battery", "100"), ("sensor.up_error", "none"), ("select.up_mode", "vacuum"),
                     ("input_boolean.up_autopilot", "on"), ("input_boolean.bedroom_guest", "off"),
                     ("binary_sensor.study_hall_door", "on"), ("binary_sensor.study_bath_door", "on")):
            self.S(e, v)
        self.occ({})
        self.tmp = Path(tempfile.mkdtemp())
        house = {"tidy_spaces": SPACES, "up": UP}
        g = {"DATA": self.tmp, "ilog": lambda r: None, "ha_get": lambda e, t: self.H.get(e, {"state": "unavailable", "attributes": {}}),
             "ha_call": lambda svc, d, t: self.calls.append((svc, d)), "ha_post_state": lambda *a: None,
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

    def occ(self, who, fresh=True, roster=True):
        """who = {room: state}; everything else clear"""
        rooms = {r: {"state": who.get(r, "clear"), "people": ["X"] if who.get(r) in ("occupied", "still") else []} for r in ROOMS}
        self.S(OCC, "ok", valid_until=self.now[0] + (90 if fresh else -10), roster_complete=roster,
               floors={"upstairs": {"state": "occupied" if who else "vacant", "rooms": ROOMS}}, rooms=rooms)

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

    def test_a_stale_tracker_or_incomplete_roster_never_starts(self):
        self.occ({}, fresh=False)
        self.assertIsNone(self.run_until_start()[0])
        self.occ({}, roster=False)
        self.assertIsNone(self.run_until_start()[0])

    def test_uncertain_holds_back_a_start_but_does_not_pause_a_run(self):
        self.occ({"hall": "uncertain"})
        self.assertIsNone(self.run_until_start()[0])
        self.occ({})
        self.assertIsNotNone(self.run_until_start()[0])
        self.S("vacuum.up", "cleaning")
        self.tick()
        self.occ({"hall": "uncertain"})
        self.assertNotIn("vacuum.pause", [c[0] for c in self.tick()])
        self.occ({"hall": "occupied"})
        self.assertIn("vacuum.pause", [c[0] for c in self.tick()])

    def test_guest_room_and_untidy_rooms_are_skipped(self):
        self.S("input_boolean.bedroom_guest", "on")
        self.tidy["bath"] = {"state": "untidy"}
        launch, _ = self.run_until_start()
        self.assertEqual(sorted(launch["rooms"]), ["Hall", "Study", "Study Closet"])
        st = self.p.segment_status()
        self.assertEqual(st["Bedroom"]["why"], "a guest is staying here")
        self.assertIn("marked untidy", st["Bath"]["why"])

    def test_two_robots_never_share_settings(self):
        down = load_copy()
        down.configure({"vacuum": "vacuum.down", "room_sensor": "sensor.down_room", "cleaning": "binary_sensor.down_cleaning",
                        "mode_select": "select.down_mode", "enable": "input_boolean.a", "active": "input_boolean.b"})
        self.assertEqual((self.va.VAC, self.va.PUBLISH, bool(self.va.OCC)), ("vacuum.up", "sensor.up_autopilot", True))
        self.assertEqual((down.VAC, down.PUBLISH, bool(down.OCC)), ("vacuum.down", "sensor.vacuum_autopilot", False))


if __name__ == "__main__":
    unittest.main()
