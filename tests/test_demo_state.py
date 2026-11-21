"""A visitor's demo copy of the chore state carries no private details."""
import json
import unittest

from test_chore_app import Case


class DemoState(Case):
    def test_chunked_ha_state_round_trips_under_recorder_limit_and_deduplicates(self):
        s = self.s
        attrs = {"friendly_name": "Chore state", "config": {"chores": [
            {"id": "job-%d" % i, "name": ("Quoted \\\"job\\\" ✨ " * 80)} for i in range(40)]},
                 "board": {"job-%d" % i: {"available": True} for i in range(40)}}
        revision, chunks = s.chunked_state_payload(attrs)
        self.assertEqual(json.loads("".join(chunks)), attrs)
        self.assertGreater(len(chunks), 1)
        for i, payload in enumerate(chunks, 1):
            part = {"friendly_name": "Chore state part %d" % i, "revision": revision,
                    "part": i, "parts": len(chunks), "payload": payload}
            self.assertLessEqual(len(json.dumps(part, ensure_ascii=False, separators=(",", ":")).encode()),
                                 s.HA_STATE_ATTR_TARGET)
        calls, old = [], s.ha_post_state
        s.ha_post_state = lambda *args: calls.append(args)
        try:
            s.GUARD.pop("test_chunk_sig", None)
            self.assertTrue(s.publish_chunked_state("sensor.test_chore", attrs, "token", "test_chunk_sig"))
            first = len(calls)
            self.assertFalse(s.publish_chunked_state("sensor.test_chore", attrs, "token", "test_chunk_sig"))
            self.assertEqual(len(calls), first)
            self.assertEqual(calls[-1][0], "sensor.test_chore")
            self.assertLess(len(json.dumps(calls[-1][2], separators=(",", ":")).encode()), 16384)
        finally:
            s.ha_post_state = old
            s.GUARD.pop("test_chunk_sig", None)

    def test_demo_copy_drops_private_details(self):
        s = self.s
        attrs = {"config": {"kids": [{"id": "ava", "name": "Ava", "color": "#f00", "icon": "1f430", "home_entity": "input_boolean.x"}],
                            "inspectors": [{"id": "jordan", "name": "Jordan", "icon": "1f9d1", "watch": "notify.w"},
                                           {"id": "sam", "name": "Sam", "icon": "1f9d1"}],
                            "households": [{"id": "a", "name": "A's house"}], "chores": [{"id": "dishes", "note": "private"}]},
                 "kid_where": {"ava": "a"}, "households": [1], "envelope": {"recipients": [{"label": "Cousin"}]},
                 "calendar": [{"title": "Private appointment", "start": "2026-11-20", "all_day": True}],
                 "packing_game": {"title": "Private trip", "items": ["Secret destination"]},
                 "music_block": {"effective": True, "blockers": [{"kid": "ava", "items": ["Private cleanup"]}]},
                 "room_choices": {"Upstairs": ["Private bedroom"]},
                 "kid_notes": {"ava": "x"}, "requests": [1], "last_result": {"n": 1},
                 "history": [{"id": "1", "kid": "ava", "note": "secret", "feedback": "f", "points": 3}],
                 "board": {"dishes": {"price": 3, "note": "on the counter", "rest_note": "r"}},
                 "chore_mode": {"on": True, "home": ["ava"]}, "goals": {"ava": "movie"}}
        d = s.demo_attrs(attrs)
        t = json.dumps(d)
        for w in ("secret", "on the counter", "Cousin", "A's house", "home_entity", "kid_where", "notify.w", "Sam", "private", "Private appointment", "Secret destination", "Private cleanup"):
            self.assertNotIn(w, t)
        self.assertEqual(d["goals"], {"ava": "movie"})
        self.assertEqual(d["board"]["dishes"]["price"], 3)
        self.assertEqual(d["history"][0]["points"], 3)
        self.assertEqual(d["chore_mode"]["home"], [])
        self.assertEqual([k["name"] for k in d["config"]["kids"]], ["Ava"])


if __name__ == "__main__":
    unittest.main()
