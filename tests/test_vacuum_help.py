"""Robot hero jobs (vacuum_help): a stuck robot or an empty water tank opens a job for the kids by itself.

    python3 -m unittest discover -s tests -v        (from the repo root)
"""
import time
import unittest

from test_chore_app import Case

ERR, DOCK, ROOM, WATER = ("sensor.down_robot_vacuum_error", "sensor.down_robot_dock_error",
                          "sensor.down_robot_current_room", "binary_sensor.down_robot_dock_dirty_water_box")
HELP = {"points": {"rescue": 3, "water": 2},
        "robots": [{"id": "down", "name": "Robo", "floor": "Downstairs", "error": ERR, "dock_error": DOCK,
                    "water": [WATER], "room": ["sensor.down_robot_room_live", ROOM],
                    "rooms": {"Kitchen": ["kitchen"], "Porch": ["porch"]}}]}
SPACES = [{"id": "kitchen", "name": "Kitchen", "floor": "Downstairs", "map_rooms": ["kitchen"], "chores": ["kitchen"]},
          {"id": "porch", "name": "Porch", "floor": "Downstairs", "map_rooms": ["porch"], "chores": []}]


class RobotHeroJobs(Case):
    def setUp(self):
        super().setUp()
        import vacuum_help
        self.vh = vacuum_help
        self.s.HOUSE["vacuum_help"], self.s.HOUSE["tidy_spaces"] = HELP, SPACES
        self.h = vacuum_help.Helper(vars(self.s), HELP)
        for e, v in ((ERR, "none"), (DOCK, "ok"), (WATER, "off"), (ROOM, "Kitchen")):
            self.app.set(e, v, ago=3600)

    def job(self, cid):
        return next(c for c in self.app.state()["config"]["chores"] if c["id"] == cid)

    def tick(self, now=None):
        self.h.tick(now)

    def test_main_brush_jam_marks_the_room_untidy_and_opens_a_rescue_as_big_news(self):
        # "if a vacuum gets main brush jammed in a room, it's instantly declared untidy"; rescuing it is a hero job
        self.app.pass_report("zoe", "kitchen", points=4)
        self.assertTrue(self.app.state()["board"]["kitchen"]["clean_until"])
        self.app.set(ERR, "main_brush_jammed", ago=60)
        self.tick()
        st = self.app.state()
        self.assertFalse(st["board"]["kitchen"]["clean_until"])                 # untidy at once
        self.assertEqual(st["tidy_spaces"]["kitchen"]["state"], "untidy")
        self.assertIsNone(self.job("help_rescue_down").get("closed"))
        b = st["board"]["help_rescue_down"]
        self.assertEqual(b["price"], 3)
        self.assertIn("Home button", b["note"])
        self.assertIn("tidy at least part of Kitchen", b["note"])               # the rescue comes with tidying
        self.assertIn("plus stars for the part of Kitchen you tidy", b["note"])
        self.assertIn("help_rescue_down", [a["chore"] for a in st["announce_all"]])   # flashes + fanfare on the kids' screens
        self.tick()                                                              # once per jam
        self.assertEqual(sum(1 for e in self.s.read_events() if e.get("chore") == "help_rescue_down" and e["type"] == "reopen"), 1)

    def test_the_tidied_part_comes_off_the_room_job(self):
        # "it comes with cleaning at least some part of the room for partial points"
        self.app.pass_report("zoe", "kitchen", points=4)
        self.app.set(ERR, "main_brush_jammed", ago=60)
        self.tick()
        ev = self.app.pass_report("max", "help_rescue_down", points=5)            # 3 rescue + 2 for part of the kitchen
        self.assertEqual(ev["partial_of"], ["kitchen"])
        st = self.app.state()
        self.assertEqual(st["board"]["kitchen"]["price"], 4 - 2)                 # the rest of the kitchen is still a job
        self.assertFalse(st["board"]["kitchen"]["clean_until"])
        self.assertIn("tidied part of Kitchen", next(h for h in st["history"] if h.get("id") == ev["id"])["chore_name"])
        self.assertEqual(self.job("help_rescue_down").get("closed"), "on_demand")
        self.assertNotIn("help_rescue_down", [a["chore"] for a in st["announce_all"]])

    def test_a_room_without_jobs_is_marked_untidy(self):
        self.app.post("/api/space_tidy", {"space": "porch", "tidy": True, "by": "jordan"})
        self.app.set(ROOM, "Porch")
        self.app.set(ERR, "wheels_jammed", ago=60)
        self.tick()
        st = self.app.state()
        self.assertEqual(st["tidy_spaces"]["porch"]["state"], "untidy")
        self.assertEqual(st["board"]["help_rescue_down"]["price"], 3)
        self.assertIn("part of Porch", st["board"]["help_rescue_down"]["note"])

    def test_stranded_with_a_flat_battery_is_a_carry_home_rescue_without_tidying(self):
        self.app.pass_report("zoe", "kitchen", points=4)
        self.app.set(ERR, "low_battery", ago=60)
        self.tick()
        st = self.app.state()
        self.assertTrue(st["board"]["kitchen"]["clean_until"])                  # not the room's fault
        self.assertEqual(st["board"]["help_rescue_down"]["price"], 3)
        self.assertIn("Carry it", st["board"]["help_rescue_down"]["note"])
        self.assertNotIn("Tidy", st["board"]["help_rescue_down"]["note"])
        ev = self.app.pass_report("ava", "help_rescue_down", points=3)
        self.assertNotIn("partial_of", ev)

    def test_water_tanks_open_a_water_job(self):
        self.app.set(DOCK, "water_empty", ago=60)
        self.app.set(WATER, "on", ago=60)
        self.tick()
        b = self.app.state()["board"]["help_water_down"]
        self.assertEqual(b["price"], 2)
        self.assertIn("empty its dirty water tank and fill its clean water tank", b["note"])
        self.assertEqual(self.job("help_rescue_down").get("closed"), "on_demand")

    def test_a_wedged_bumper_says_how_to_free_it(self):
        # "jiggle the laser turret on top of the robot and press the center button to go again"
        self.app.set(ERR, "vertical_bumper_pressed", ago=60)
        self.tick()
        note = self.app.state()["board"]["help_rescue_down"]["note"]
        self.assertIn("jiggle the laser tower", note)
        self.assertIn("center button", note)
        self.assertNotIn("Home button", note)

    def test_maintenance_never_opens_a_job(self):
        # worn parts, filters, the dust bag and other dock upkeep are grown-up work
        for err, dock in (("filter_blocked", "ok"), ("no_dustbin", "ok"), ("side_brush_error", "ok"),
                          ("none", "no_dustbin_or_filter"), ("none", "maintenance_brush_jammed"), ("none", "duct_blockage")):
            self.app.set(ERR, err, ago=600)
            self.app.set(DOCK, dock, ago=600)
            self.tick()
        self.assertEqual(self.job("help_rescue_down").get("closed"), "on_demand")
        self.assertEqual(self.job("help_water_down").get("closed"), "on_demand")

    def test_a_blip_is_ignored(self):
        self.app.set(ERR, "robot_trapped", ago=5)
        self.tick()
        self.assertEqual(self.job("help_rescue_down").get("closed"), "on_demand")

    def test_no_job_at_bedtime_but_the_room_is_untidy_at_once(self):
        self.app.pass_report("zoe", "kitchen", points=4)
        self.app.post("/api/bedtime", {"by": "jordan", "on": True})
        self.app.set(ERR, "main_brush_jammed", ago=60)
        self.tick()
        st = self.app.state()
        self.assertFalse(st["board"]["kitchen"]["clean_until"])
        self.assertEqual(self.job("help_rescue_down").get("closed"), "on_demand")
        self.app.post("/api/bedtime", {"by": "jordan", "on": False})
        self.tick()
        self.assertIsNone(self.job("help_rescue_down").get("closed"))

    def test_fixed_by_a_grownup_and_nobody_took_it_closes_again(self):
        self.app.set(ERR, "main_brush_jammed", ago=60)
        self.tick()
        self.app.set(ERR, "none")
        now = time.time()
        self.tick(now)
        self.assertIsNone(self.job("help_rescue_down").get("closed"))           # a kid may have just done it: grace
        self.tick(now + self.vh.CLOSE_AFTER_S + 1)
        st = self.app.state()
        self.assertEqual(self.job("help_rescue_down").get("closed"), "on_demand")
        self.assertNotIn("help_rescue_down", [a["chore"] for a in st["announce_all"]])
        self.assertEqual(st["tidy_spaces"]["kitchen"]["state"], "untidy")       # the room still needs tidying

    def test_a_job_a_kid_started_is_never_closed_under_them(self):
        self.app.set(ERR, "main_brush_jammed", ago=60)
        self.tick()
        self.assertEqual(self.app.post("/api/start", {"kid": "max", "chore": "help_rescue_down"})[0], 200)
        self.app.set(ERR, "none")
        now = time.time()
        self.tick(now)
        self.tick(now + self.vh.CLOSE_AFTER_S + 1)
        self.assertIsNone(self.job("help_rescue_down").get("closed"))

    def test_a_grownup_closing_it_is_not_undone_while_the_same_jam_lasts(self):
        self.app.set(ERR, "main_brush_jammed", ago=60)
        self.tick()
        self.assertEqual(self.app.post("/api/close_od", {"chore": "help_rescue_down", "by": "jordan"})[0], 200)
        self.tick()
        self.assertEqual(self.job("help_rescue_down").get("closed"), "on_demand")


if __name__ == "__main__":
    unittest.main()
