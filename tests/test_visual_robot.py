import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from locator import Locator
from robot_camera import MovingObject, record, record_calibration


class VisualRobotTest(unittest.TestCase):
    def test_calibration_frames_are_private_bounded_and_separate_from_sightings(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for at in (1,2,3):
                count=record_calibration(root,'dining',dict(at=at,xy=[at,at+1],map='floor'),
                                         ('jpeg%d' % at).encode(),limit=2)
            rows=json.loads((root/'dining.json').read_text())
            self.assertEqual(count,2)
            self.assertEqual([row['at'] for row in rows],[2,3])
            self.assertEqual(sorted(p.name for p in root.glob('*.jpg')),
                             ['dining-2000.jpg','dining-3000.jpg'])
            self.assertEqual((root.stat().st_mode & 0o777),0o700)
            self.assertTrue(all((p.stat().st_mode & 0o777) == 0o600 for p in root.iterdir()))

    def test_motion_needs_coherent_real_travel(self):
        track=MovingObject()
        self.assertEqual([track.observe(t,(t*5,10)) for t in (1,2,3)],[False,False,True])
        self.assertFalse(track.observe(4,None))
        self.assertFalse(any(track.observe(t,(10,10)) for t in (5,6,7)))
        self.assertFalse(track.observe(30,(30,10)))
        self.assertFalse(track.observe(31,(170,10)))

    def test_replay_teaches_only_once_and_cannot_publish_a_current_position(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);loc=object.__new__(Locator);loc.M={};loc.cfg={'vac':{}};loc.tok='test'
            cfg=dict(vacuum='vac',room='target',map_select='select.map',map_name='floor')
            loc.g={'DATA':root,'HOUSE':{'room_eye':{'cameras':{'cam':{'visual_robot':cfg}}}},
                   'ha_get':lambda e,_:{'state':'floor' if e=='select.map' else 'cleaning','last_changed':'1970-01-01T00:00:01Z'}}
            loc.add=Mock();loc.floor_empty=lambda *args:True
            path=root/'robot-camera-cam.json';row=dict(at=100,vacuum='vac',camera='cam',room='target',map='floor',xy=[1500,2000],pixel=[50,80])
            record(path,row)
            self.assertTrue(loc.visual_sightings(180));loc.add.assert_not_called()
            self.assertEqual(loc.classify('vac',1500,2000)[0],'target')
            self.assertFalse(loc.visual_sightings(181))
            record(path,dict(row,at=182,xy=None))
            self.assertTrue(loc.visual_sightings(185));loc.add.assert_called_once_with(
                'vac','camera:cam','camera','target',182,map_xy=None,
                position_source='visual_robot',location_source='visual_robot')
            loc.add.reset_mock();record(path,dict(row,at=186,map='other'))
            self.assertFalse(loc.visual_sightings(189));loc.add.assert_not_called()
