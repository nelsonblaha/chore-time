"""A robot docked and charged with its cleaning flag still on has left its task unfinished: the run is over."""
import unittest
import test_chore_app as fixture


class LeftoverTask(unittest.TestCase):
    tearDown = fixture.Vacuum.tearDown
    S = fixture.Vacuum.S
    tick = fixture.Vacuum.tick

    def setUp(self):
        fixture.Vacuum.setUp(self)
        self.va.START_QUIET_S = 0
        self.p.D.update(phase="running", groups=[{"mode": "vac_and_mop", "rooms": ["Kitchen"]}], gi=0,
                        run_started=self.now[0] - 7200, saw_clean=True, pauses=[], saved={"mode": "vacuum", "fan": "max"})

    def test_docked_charged_with_the_flag_stuck_on_ends_the_run(self):
        # 11-17: a paused-and-resumed run docked at 19:52; the flag stayed on, the run stayed "running" all night and
        # the map's Clean now button said the robot was busy
        self.S(self.va.VAC, "docked", 30)
        self.S(self.va.IN_CLEAN, "on", 3600)
        self.tick()
        self.assertEqual(self.p.D["phase"], "running")            # a mop wash or recharge stop: give it time
        self.S(self.va.VAC, "docked", self.va.LEFTOVER_S + 60)
        calls = self.tick()
        self.assertEqual(self.p.D["phase"], "idle")
        self.assertIn("input_boolean.turn_off", calls)
        self.assertIn("unfinished", self.p.D["runs"][-1]["why"])

    def test_a_recharge_stop_is_not_mistaken_for_a_leftover(self):
        self.S(self.va.BATTERY, "40")
        self.S(self.va.VAC, "docked", self.va.LEFTOVER_S + 60)
        self.S(self.va.IN_CLEAN, "on", 3600)
        self.tick()
        self.assertEqual(self.p.D["phase"], "running")


if __name__ == "__main__":
    unittest.main()
