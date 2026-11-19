import unittest
from unittest.mock import patch
import vacuum_auto as va


class VerifiedCleaningTests(unittest.TestCase):
    def setUp(self):
        self.p=object.__new__(va.Pilot)
        self.p.room_cleaning_sensor='sensor.room_evidence'
        self.p.D={'visits':{'Hall':70},'cur':'Hall','cur_clean':300,'t_vs':'cleaning','t_last':100}
        self.attrs={'updated_at':190,'rooms':{'Hall':{'current_job':{'targeted':True,'seconds':18}},'Kitchen':{}}}
        self.p.get=lambda entity:{'state':'ready','attributes':self.attrs}
        self.patch=patch.object(va,'SEGMENTS',['Hall','Kitchen']);self.patch.start();self.addCleanup(self.patch.stop)

    def test_move_and_dock_do_not_confirm_room(self):
        self.p.track('cleaning','Hall',200)
        self.assertEqual(self.p.D['cur_clean'],18)
        self.p.track('cleaning','Kitchen',205);self.p.track('docked','Kitchen',220)
        self.assertEqual(self.p.D['visits'],{})
        self.assertTrue(self.p.request_serviced('Hall',60))
        self.assertFalse(self.p.request_serviced('Hall',202))
        self.assertEqual(self.p.D['prior_schedule_dates']['Hall'],70)

    def test_success_survives_stale_read_and_restart(self):
        self.attrs['rooms']['Hall']['last_completed']=195
        self.p.track('docked','Hall',200)
        self.assertEqual(self.p.D['visits'],{'Hall':195})
        self.attrs['rooms']['Kitchen']['last_completed']=210
        self.p.track('docked','Hall',500)
        self.assertEqual(self.p.D['visits'],{'Hall':195})
        self.assertFalse(self.p.request_serviced('Hall',196))

if __name__=='__main__':unittest.main()
