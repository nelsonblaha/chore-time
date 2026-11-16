"""Brief door pulses survive polling; no replayed/restored arrival or commands."""
import unittest
from datetime import datetime, timezone
import test_chore_app as fixture
from entry_edges import EntryEdges, entry_edges


def event(entity, stamp, old="off", new="on"):
    return {"entity_id": entity, "old_state": {"state": old},
            "new_state": {"state": new, "last_changed": datetime.fromtimestamp(stamp, timezone.utc).isoformat()}}


class Buffer(unittest.TestCase):
    def test_rejects_replay_restoration_and_unrelated_events(self):
        b = EntryEdges()
        for old in (None, "unknown", "unavailable", "on"):
            self.assertFalse(b.record(event("door", 100, old), {"door"}, 101))
        self.assertFalse(b.record(event("other", 100), {"door"}, 101))
        self.assertFalse(b.record(event("door", 50), {"door"}, 101))
        self.assertFalse(b.record(event("door", 110), {"door"}, 101))
        self.assertTrue(b.record(event("door", 100), {"door"}, 101))
        self.assertFalse(b.record(event("door", 104, "on", "off"), {"door"}, 104))
        self.assertEqual(b.recent(110), {"door": 100})
        self.assertEqual(b.recent(1301), {})


class VacuumEntries(unittest.TestCase):
    tearDown = fixture.Vacuum.tearDown
    S = fixture.Vacuum.S
    tick = fixture.Vacuum.tick
    status = fixture.Vacuum.status

    def setUp(self):
        fixture.Vacuum.setUp(self)
        self.va.ENTRY_DOORS = {"binary_sensor.door"}
        entry_edges.edges.clear()
        self.addCleanup(entry_edges.edges.clear)

    def test_closed_again_before_poll_still_pauses_once(self):
        self.tick()
        self.assertEqual(self.p.D["phase"], "running")
        t = self.now[0] + 2
        entry_edges.record(event("binary_sensor.door", t), self.va.ENTRY_DOORS, t)
        self.S("binary_sensor.door", "off")
        self.S(self.va.VAC, "cleaning")
        self.tick()
        self.assertEqual(self.p.D["phase"], "paused")
        self.assertEqual(self.p.D["pauses"][-1]["who"], "binary_sensor.door")
        self.assertIsNone(self.p.person_entered(self.now[0] + 1))

    def test_entry_resets_quiet_clock_even_with_no_room_motion(self):
        t = self.now[0] - 4
        entry_edges.record(event("binary_sensor.door", t), self.va.ENTRY_DOORS, self.now[0])
        self.S("binary_sensor.door", "off")
        self.assertEqual(self.p.quiet_for(), (4, "binary_sensor.door"))
        self.tick()
        self.assertEqual(self.p.D["phase"], "idle")

    def test_pre_run_edge_does_not_pause_later_run(self):
        t = self.now[0] - 20
        entry_edges.record(event("binary_sensor.door", t), self.va.ENTRY_DOORS, self.now[0])
        self.p.D["run_started"] = self.now[0]
        self.S("binary_sensor.door", "off")
        self.assertIsNone(self.p.person_entered(self.now[0]))


if __name__ == "__main__":
    unittest.main()
