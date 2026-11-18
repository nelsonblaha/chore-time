"""While the robot maps the floor, the autopilot sends it nothing at all."""
import unittest
import test_chore_app as fixture


class HandsOff(unittest.TestCase):
    tearDown = fixture.Vacuum.tearDown
    S = fixture.Vacuum.S
    tick = fixture.Vacuum.tick

    def setUp(self):
        fixture.Vacuum.setUp(self)
        keep = self.va.STATUS
        self.addCleanup(setattr, self.va, "STATUS", keep)
        self.status = self.va.STATUS = keep or "sensor.robot_status"

    def test_a_run_in_progress_is_left_alone_while_mapping(self):
        # 11-18: a grown-up started a mapping run; the vacuum entity reads unknown the whole time
        self.p.D.update(phase="paused", paused_at=self.now[0] - 3600, groups=[{"mode": "vacuum", "rooms": ["Kitchen"]}], gi=0,
                        run_started=self.now[0] - 3600, pauses=[], saved={"mode": "vacuum", "fan": "max"})
        self.S(self.status, "mapping")
        self.S(self.va.VAC, "unknown")
        for _ in range(5):
            self.assertEqual(self.tick(60), [])                   # no dock, no fan, no voice: nothing
        self.assertEqual(self.p.D["phase"], "paused")
        self.S(self.status, "charging")
        self.S(self.va.VAC, "docked")
        self.tick()
        self.assertIsNone(self.p.D.get("hands_off"))

    def test_never_starts_while_mapping(self):
        self.S(self.status, "mapping")
        self.va.START_QUIET_S, keep = 0, self.va.START_QUIET_S
        self.addCleanup(setattr, self.va, "START_QUIET_S", keep)
        self.assertEqual(self.tick(3000), [])


if __name__ == "__main__":
    unittest.main()
