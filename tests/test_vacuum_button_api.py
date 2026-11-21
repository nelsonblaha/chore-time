import json
import threading
import unittest
import urllib.error
import urllib.request

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
