import time
import unittest
from test_chore_app import App


class RoomTidiness(unittest.TestCase):
    def setUp(self):
        self.app = App()
        self.app.s.HOUSE['tidy_spaces'] = [
            {'id':'kitchen', 'name':'Kitchen', 'map_rooms':['kitchen'], 'chores':['kitchen']},
            {'id':'study', 'name':'Study', 'map_rooms':['study'], 'chores':[], 'cooldown_h':72},
        ]

    def tearDown(self):
        self.app.close()

    def test_room_only_tidy_never_creates_jobs_points_or_robot_commands(self):
        before = self.app.s.derive()
        code, result = self.app.post('/api/space_tidy', {'space':'study','tidy':True,'by':'jordan'})
        self.assertEqual(code,200)
        self.assertTrue(result['ok'])
        after = self.app.s.derive()
        self.assertEqual(after['tidy_spaces']['study']['state'], 'tidy')
        self.assertEqual(after['config']['chores'], before['config']['chores'])
        self.assertEqual(after['balance'], before['balance'])
        self.assertEqual(after['board'], before['board'])
        self.assertEqual(self.app.calls, [])
        self.app.post('/api/space_tidy', {'space':'study','tidy':False,'by':'casey'})
        self.assertEqual(self.app.s.derive()['tidy_spaces']['study']['state'], 'untidy')

    def test_existing_room_job_uses_its_tidy_clock_without_points(self):
        before = self.app.s.derive()['balance']
        self.app.post('/api/space_tidy', {'space':'kitchen','tidy':True,'by':'jordan'})
        state = self.app.s.derive()
        self.assertEqual(state['tidy_spaces']['kitchen']['state'], 'tidy')
        self.assertGreater(state['board']['kitchen']['clean_until'], time.time())
        self.assertEqual(state['balance'], before)
        self.assertEqual(self.app.calls, [])

    def test_no_room_condition_inferred_before_adult_checks_and_expiry(self):
        from tidy_spaces import space_states
        spaces = self.app.s.HOUSE['tidy_spaces'][1:]
        self.assertEqual(space_states(spaces,{}, {}, {}, [], 100)['study']['state'], 'unknown')
        event = {'type':'space_tidy','space':'study','tidy':True,'ts':100,'by':'jordan'}
        self.assertEqual(space_states(spaces,{}, {}, {}, [event], 100+72*3600)['study']['state'], 'untidy')

    def test_room_controls_require_adult_and_known_room(self):
        for body in [{'space':'study','tidy':True,'by':'ava'}, {'space':'missing','tidy':True,'by':'jordan'}, {'space':'study','tidy':'yes','by':'jordan'}]:
            self.assertEqual(self.app.post('/api/space_tidy',body)[0],400)
        self.assertEqual(self.app.s.derive()['tidy_spaces']['study']['state'], 'unknown')

    def test_tidy_does_not_bypass_a_pending_inspection(self):
        self.app.post('/api/report', {'kid':'ava','chore':'kitchen','feeling':'ok'})
        before = self.app.s.derive()
        self.assertEqual(self.app.post('/api/space_tidy', {'space':'kitchen','tidy':True,'by':'jordan'})[0],409)
        after = self.app.s.derive()
        self.assertEqual(after['board']['kitchen'],before['board']['kitchen'])
        self.assertEqual(after['balance'],before['balance'])
        self.assertEqual(after['tidy_spaces']['kitchen']['state'],'waiting')
