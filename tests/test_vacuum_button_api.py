import json
import time
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from test_chore_app import App


class FakePlanner:
    def __init__(self):
        self.lock = threading.RLock()
        self.calls = []

    def button_request(self, body):
        self.calls.append(body)
        return {"ok": True, "rooms": list(body["rooms"]), "why": "queued next"}


class VacuumButtonApiTest(unittest.TestCase):
    def setUp(self):
        self.app = App()
        self.planner = FakePlanner()
        self.app.s.VACUUM_PLANNERS = {"upstairs": self.planner}
        self.token = "test-vacuum-button-token"
        self.app.s.VACUUM_BUTTON_TOKEN_FILE = self.app.dir / "vacuum-button.token"
        self.app.s.VACUUM_BUTTON_TOKEN_FILE.write_text(self.token)

    def tearDown(self):
        self.app.close()

    def post(self, body, token=None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Vacuum-Button"] = token
        req = urllib.request.Request(self.app.base + "/api/vacuum/button", data=json.dumps(body).encode(),
                                     method="POST", headers=headers)
        try:
            r = urllib.request.urlopen(req, timeout=10)
            return r.status, json.loads(r.read())
        except urllib.error.HTTPError as ex:
            return ex.code, json.loads(ex.read())

    def room_planner(self, floor):
        from test_vacuum_upstairs import Upstairs
        fixture = Upstairs()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.now[0] = time.time()
        fixture.va.FLOOR = floor
        fixture.va.SEG_ROOMS["Bath"] = ["left", "right"]
        self.app.s.VACUUM_PLANNERS[floor] = fixture.p
        self.app.s.HOUSE["tidy_spaces"] = [
            {"id": name, "name": name, "chores": ["kitchen"] if name in ("left", "right") else []}
            for name in ("left", "right", "hall", "other")]
        return fixture

    def test_map_selection_confirms_one_shared_tile_and_can_recover_after_queue_commit(self):
        fixture = self.room_planner("downstairs")
        before = self.app.s.derive()
        body = {"floor": "downstairs", "gesture": "map_vacant_on", "rooms": ["Bath"],
                "request_id": "tap", "map_room": "other"}
        self.assertEqual(self.post(body, self.token)[0], 400)
        self.assertFalse(fixture.p.room_requests())
        body["map_room"] = "left"
        with patch.object(self.app.s, "append", side_effect=OSError("test storage failure")):
            self.assertEqual(self.post(body, self.token)[0], 503)
        self.assertIn("Bath", fixture.p.room_requests())
        with ThreadPoolExecutor(2) as pool:
            for code, _ in pool.map(lambda _: self.post(body, self.token), range(2)):
                self.assertEqual(code, 200)
        after = self.app.s.derive()
        self.assertEqual(after["tidy_spaces"]["left"]["state"], "tidy")
        for other in ("right", "other", "hall"):
            self.assertEqual(after["tidy_spaces"][other], before["tidy_spaces"][other])
        for key in ("balance", "board"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(fixture.p.D["visits"], {})
        events = self.app.s.read_events()
        self.assertEqual([e["spaces"] for e in events], [["left"]])
        # A genuinely new selection of a tile on an already queued segment still confirms that tile.
        code, result = self.post(dict(body, request_id="another-tap", map_room="right"), self.token)
        self.assertEqual((code, result["already"]), (200, ["Bath"]))
        self.assertEqual(self.app.s.derive()["tidy_spaces"]["right"]["state"], "tidy")
        events = self.app.s.read_events()
        del body["map_room"]
        for gesture in ("map_vacant_off", "map_now_on", "map_now_off"):
            self.assertEqual(self.post(dict(body, gesture=gesture, request_id=gesture), self.token)[0], 200)
        self.assertEqual(self.app.s.read_events(), events)

    def test_physical_selection_confirms_its_segments_once_and_promotion_does_not_reconfirm(self):
        fixture = self.room_planner("upstairs")
        body = {"floor": "upstairs", "gesture": "double", "rooms": ["Bath", "Hall"], "request_id": "button"}
        for _ in range(2):
            code, result = self.post(body, self.token)
            self.assertEqual((code, result["action"]), (200, "queued_when_vacant"))
        rows = self.app.s.derive()["tidy_spaces"]
        self.assertEqual({k for k, row in rows.items() if row["state"] == "tidy"}, {"left", "right", "hall"})
        self.assertEqual(len(self.app.s.read_events()), 1)
        self.app.s.append({"type": "reopen", "chore": "kitchen", "by": "jordan"})
        events = self.app.s.read_events()
        self.assertEqual(self.post(dict(body, request_id="second-press"), self.token)[0], 200)
        self.assertEqual(self.post(body, self.token)[0], 200)
        fixture.tick()
        self.assertEqual(self.app.s.read_events(), events)
        self.assertEqual(self.app.s.derive()["tidy_spaces"]["left"]["state"], "untidy")

    def test_internal_contract_routes_to_the_floor_planner(self):
        body = {"floor": "upstairs", "gesture": "map_now_off", "rooms": ["Office"],
                "request_id": "ha-context-1"}
        code, result = self.post(body, self.token)
        self.assertEqual(code, 200)
        self.assertEqual(result, {"ok": True, "rooms": ["Office"], "why": "queued next"})
        self.assertEqual(self.planner.calls, [body])

    def test_contract_rejects_unknown_or_not_yet_started_floor(self):
        code, result = self.post({"floor": "basement"}, self.token)
        self.assertEqual((code, result["ok"]), (400, False))
        code, result = self.post({"floor": "downstairs"}, self.token)
        self.assertEqual((code, result["ok"]), (503, False))

    def test_contract_fails_closed_without_the_shared_header_or_token_file(self):
        body = {"floor": "upstairs", "gesture": "single", "rooms": ["Office"], "request_id": "x"}
        self.assertEqual(self.post(body)[0], 403)
        self.assertEqual(self.post(body, "wrong")[0], 403)
        self.app.s.VACUUM_BUTTON_TOKEN_FILE.unlink()
        self.assertEqual(self.post(body, self.token)[0], 503)
        self.assertEqual(self.planner.calls, [])


if __name__ == "__main__":
    unittest.main()
