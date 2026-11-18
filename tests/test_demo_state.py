"""A visitor's demo copy of the chore state carries no private details."""
import json
import unittest

from test_chore_app import Case


class DemoState(Case):
    def test_demo_copy_drops_private_details(self):
        s = self.s
        attrs = {"config": {"kids": [{"id": "ava", "name": "Ava", "color": "#f00", "icon": "1f430", "home_entity": "input_boolean.x"}],
                            "inspectors": [{"id": "jordan", "name": "Jordan", "icon": "1f9d1", "watch": "notify.w"},
                                           {"id": "sam", "name": "Sam", "icon": "1f9d1"}],
                            "households": [{"id": "a", "name": "A's house"}], "chores": [{"id": "dishes", "note": "private"}]},
                 "kid_where": {"ava": "a"}, "households": [1], "envelope": {"recipients": [{"label": "Cousin"}]},
                 "kid_notes": {"ava": "x"}, "requests": [1], "last_result": {"n": 1},
                 "history": [{"id": "1", "kid": "ava", "note": "secret", "feedback": "f", "points": 3}],
                 "board": {"dishes": {"price": 3, "note": "on the counter", "rest_note": "r"}},
                 "chore_mode": {"on": True, "home": ["ava"]}, "goals": {"ava": "movie"}}
        d = s.demo_attrs(attrs)
        t = json.dumps(d)
        for w in ("secret", "on the counter", "Cousin", "A's house", "home_entity", "kid_where", "notify.w", "Sam", "private"):
            self.assertNotIn(w, t)
        self.assertEqual(d["goals"], {"ava": "movie"})
        self.assertEqual(d["board"]["dishes"]["price"], 3)
        self.assertEqual(d["history"][0]["points"], 3)
        self.assertEqual(d["chore_mode"]["home"], [])
        self.assertEqual([k["name"] for k in d["config"]["kids"]], ["Ava"])


if __name__ == "__main__":
    unittest.main()
