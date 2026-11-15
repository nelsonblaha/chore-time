"""Explicit map requests use normal vacancy rules and survive a restart."""
import unittest
import test_chore_app as fixture

class RoomRequests(unittest.TestCase):
    tearDown=fixture.Vacuum.tearDown
    S=fixture.Vacuum.S
    tick=fixture.Vacuum.tick
    status=fixture.Vacuum.status
    # Reuse the isolated robot fixture, not the inherited unrelated suite.
    def setUp(self):
        fixture.Vacuum.setUp(self)
        self.va.REQUESTS='input_text.room_requests'
        for seg in self.va.SEGMENTS:self.p.D['visits'][seg]=self.now[0]-60
        for d in self.board.values():d['last_clean']=0

    def test_requested_room_is_due_until_a_real_visit(self):
        self.S(self.va.REQUESTS,'{"Kitchen":%d}' % self.now[0])
        self.assertTrue(self.status()['Kitchen']['requested'])
        self.assertTrue(self.status()['Kitchen']['eligible'])
        self.assertFalse(self.status()['Family Room']['eligible'])
        self.p.D['visits']['Kitchen']=self.now[0]+120
        self.assertFalse(self.status()['Kitchen']['requested'])
        self.assertFalse(self.status()['Kitchen']['eligible'])

    def test_request_does_not_override_occupancy_or_child_work(self):
        self.S(self.va.REQUESTS,'{"Kitchen":%d}' % self.now[0])
        self.S(next(iter(self.va.SENSORS)),'on')
        self.tick();self.assertEqual(self.p.D['phase'],'idle')
        self.board['kitchen']['pending_by']='kid'
        self.assertFalse(self.status()['Kitchen']['eligible'])
        self.assertTrue(self.status()['Kitchen']['requested'])

    def test_bad_or_unknown_requests_are_ignored(self):
        for value in ('unavailable','[]','{"Not a room":1}','{"Kitchen":"yes"}','{"Kitchen":99999999999}'):
            self.S(self.va.REQUESTS,value)
            self.assertEqual(self.p.room_requests(),{})

if __name__=='__main__':unittest.main()
