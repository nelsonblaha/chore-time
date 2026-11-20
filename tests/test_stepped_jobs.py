import unittest

import stepped_jobs as sj


def chore():
    return {
        "id": "yard_project",
        "stepped": {
            "version": "v1",
            "min_team": 2,
            "contexts": ["daylight", "dry"],
            "steps": [
                {"id": "prepare", "phase": "session_start", "actor": "kid"},
                {"id": "location", "phase": "session_start", "scope": "project", "actor": "adult"},
                {"id": "work", "phase": "work", "actor": "kid", "control": True},
                {"id": "equipment", "phase": "session_stop", "actor": "kid", "safety": True},
                {"id": "gate", "phase": "session_stop", "actor": "kid", "safety": True},
                {"id": "inspect", "phase": "job_finish", "scope": "project", "actor": "adult"},
            ],
        },
    }


class SteppedReducer(unittest.TestCase):
    def setUp(self):
        self.events = [{"type": "project_start", "instance": "p1", "chore": "yard_project",
                        "definition_version": "v1", "kid": "a", "ts": 1}]

    def state(self):
        return sj.derive([chore()], self.events)[0]

    def add(self, typ, **kw):
        self.events.append({"type": typ, "instance": "p1", "ts": len(self.events) + 1, **kw})

    def test_definition_is_strict_and_versioned(self):
        self.assertEqual(sj.validate_definition(chore()["stepped"])["min_team"], 2)
        bad = chore()["stepped"] | {"steps": [{"id": "x", "phase": "bogus"}]}
        with self.assertRaises(ValueError):
            sj.validate_definition(bad)
        self.events[0]["definition_version"] = "old"
        self.assertEqual(self.state()["status"], "definition_missing")

    def test_session_walks_project_once_work_stop_award_and_final(self):
        p = self.state()
        self.assertEqual(sj.current(p), {"kind": "start", "min_team": 2})
        self.add("project_session_start", session="s1", team=["a", "b"])
        self.assertEqual(sj.current(self.state())["step"]["id"], "prepare")
        self.add("project_step", session="s1", step="prepare", scope="session")
        self.assertEqual(sj.current(self.state())["step"]["id"], "location")
        self.add("project_step", session="s1", step="location", scope="project")
        self.assertEqual(sj.current(self.state())["kind"], "work")
        self.add("project_stop", session="s1", mode="finish")
        self.assertEqual(sj.current(self.state())["step"]["id"], "equipment")
        self.add("project_step", session="s1", step="equipment", scope="session")
        self.add("project_step", session="s1", step="gate", scope="session")
        self.assertEqual(sj.current(self.state())["kind"], "award")
        self.add("project_session_award", session="s1", awards={"a": 2, "b": 3}, by="adult")
        self.add("project_session_close", session="s1", mode="finish", by="adult")
        self.assertEqual(sj.current(self.state())["step"]["id"], "inspect")
        self.add("project_step", session="s1", step="inspect", scope="project", by="adult")
        self.assertEqual(sj.current(self.state())["kind"], "complete")
        self.add("project_complete", by="adult")
        self.assertEqual(self.state()["status"], "complete")

    def test_project_once_step_is_skipped_in_later_session(self):
        self.add("project_session_start", session="s1", team=["a", "b"])
        self.add("project_step", session="s1", step="prepare", scope="session")
        self.add("project_step", session="s1", step="location", scope="project")
        self.add("project_stop", session="s1", mode="pause")
        self.add("project_step", session="s1", step="equipment", scope="session")
        self.add("project_step", session="s1", step="gate", scope="session")
        self.add("project_session_award", session="s1", awards={"a": 1, "b": 1}, by="adult")
        self.add("project_session_close", session="s1", mode="pause", by="adult")
        self.add("project_session_start", session="s2", team=["b", "c"])
        self.add("project_step", session="s2", step="prepare", scope="session")
        self.assertEqual(sj.current(self.state())["kind"], "work")

    def test_stale_revision_is_rejected(self):
        p = self.state()
        sj.require_revision(p, 0)
        with self.assertRaises(sj.TransitionError):
            sj.require_revision(p, 1)

    def test_public_shape_omits_definition_copy(self):
        p = sj.public_project(self.state())
        self.assertNotIn("config", p)
        self.assertEqual(p["next"]["kind"], "start")


if __name__ == "__main__":
    unittest.main()
