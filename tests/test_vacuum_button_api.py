import threading
import unittest

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

    def tearDown(self):
        self.app.close()

    def test_internal_contract_routes_to_the_floor_planner(self):
        body = {"floor": "upstairs", "gesture": "double", "rooms": ["Office"], "request_id": "ha-context-1"}
        code, result = self.app.post("/api/vacuum/button", body)
        self.assertEqual(code, 200)
        self.assertEqual(result, {"ok": True, "rooms": ["Office"], "why": "queued next"})
        self.assertEqual(self.planner.calls, [body])

    def test_contract_rejects_unknown_or_not_yet_started_floor(self):
        code, result = self.app.post("/api/vacuum/button", {"floor": "basement"})
        self.assertEqual((code, result["ok"]), (400, False))
        code, result = self.app.post("/api/vacuum/button", {"floor": "downstairs"})
        self.assertEqual((code, result["ok"]), (503, False))


if __name__ == "__main__":
    unittest.main()
