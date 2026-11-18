"""Streaks skip days away; an arrival day counts as away when the kid got home too late for a job (the grace hour)."""
import unittest
from datetime import date, datetime, time as dtime, timedelta

from test_chore_app import Case


class ArrivalDay(Case):
    def streak(self, job_days, home, arrivals):
        s = self.s
        reports = {str(i): {"status": "inspected", "chore": "dishes", "kid": "zoe",
                            "ts": datetime.combine(d, dtime(12)).timestamp()} for i, d in enumerate(job_days)}
        s.GUARD["home_days"] = {"zoe": {d.isoformat() for d in home}}
        s.GUARD["home_arrivals"] = {"zoe": {d.isoformat(): t for d, t in arrivals.items()}}
        return s.achievements({"kids": [{"id": "zoe"}]}, reports, {}, {})[1]["zoe"]

    def test_a_late_arrival_day_does_not_break_the_streak(self):
        t = date.today()
        d = [t - timedelta(days=i) for i in range(7)]   # d[0] today ... d[6]
        # jobs on d[6..4], away d[3..2], home again d[1] at 19:30 with no job, nothing yet today
        jobs, home = [d[6], d[5], d[4]], [d[6], d[5], d[4], d[1], d[0]]
        self.assertEqual(self.streak(jobs, home, {d[1]: "19:30"})["current"], 3)
        # home at 18:00 instead: time to do a job, so the empty day ends the streak
        self.assertEqual(self.streak(jobs, home, {d[1]: "18:00"})["current"], 0)
        # a job done on a late arrival day still counts
        self.assertEqual(self.streak(jobs + [d[1]], home, {d[1]: "19:30"})["current"], 4)


if __name__ == "__main__":
    unittest.main()
