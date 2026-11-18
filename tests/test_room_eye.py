"""Room eye: cameras learn what tidy looks like from the grown-ups' clicks, and mark messy rooms untidy.

    python3 -m unittest discover -s tests -v        (from the repo root; needs numpy + Pillow)
Pictures here are synthetic: a fixed textured "room", lit a little differently each time, with or without mess.
"""
import io
import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from test_chore_app import Case

LIGHT = "light.kitchen"
CAM = {"capture_url": "http://fake", "every_h": 4, "settle_s": 0,
       "cameras": {"cam1": {"spaces": ["kitchen_space"], "lights": [LIGHT], "motion": "binary_sensor.cam1_motion",
                            "robots": {"vacuum.robo": "Kitchen"}}}}
SPACES = [{"id": "kitchen_space", "name": "Kitchen", "floor": "Downstairs", "map_rooms": ["kitchen"], "chores": ["kitchen"]}]
ROOM = np.random.default_rng(7).integers(40, 215, size=(60, 80)).astype(np.uint8)


def picture(light=0, mess=(), seed=0):
    """the room at 640x480, lighting shifted by `light`, sensor noise, and mess blobs (x, y, w, h in pixels)"""
    a = np.asarray(Image.fromarray(ROOM).resize((640, 480), Image.BILINEAR), dtype=np.float32) + light
    a += np.random.default_rng(seed).normal(0, 3, a.shape)
    for (x, y, w, h) in mess:
        a[y:y + h, x:x + w] = 250 - (np.indices((h, w)).sum(axis=0) % 40) * 4   # a striped sock
    out = io.BytesIO()
    Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()


class ImageMaths(unittest.TestCase):
    def setUp(self):
        import room_eye
        self.re = room_eye
        self.refs = [room_eye.prep(picture(light=l, seed=i))[0] for i, l in enumerate((-20, 0, 15, 30))]

    def test_tidy_room_under_other_light_is_tidy(self):
        ch = self.re.changed(self.re.prep(picture(light=8, seed=9))[0], self.refs)
        self.assertLess(self.re.score(ch), self.re.DEFAULT_THRESHOLD)

    def test_mess_is_found_where_it_is(self):
        ch = self.re.changed(self.re.prep(picture(light=5, mess=[(320, 300, 90, 60)], seed=9))[0], self.refs)
        self.assertGreater(self.re.score(ch), self.re.DEFAULT_THRESHOLD)
        rs = self.re.regions(ch)
        big = max(rs, key=lambda r: len(r["blocks"]))
        x0, y0, x1, y1 = big["box"]            # in 20x15 blocks: 320/32=10 .. 410/32~12.8, 300/32~9.4 .. 360/32=11.25
        self.assertTrue(9 <= x0 <= 10 and 12 <= x1 <= 14 and 8 <= y0 <= 10 and 11 <= y1 <= 13, big["box"])

    def test_threshold_sits_between_tidy_and_untidy(self):
        self.assertAlmostEqual(self.re.threshold([0.01, 0.02], [0.10, 0.2]), 0.06)
        self.assertGreater(self.re.score(self.re.changed(self.re.prep(picture(light=5, mess=[(100, 400, 40, 25)], seed=9))[0],
                                                         self.refs)), self.re.DEFAULT_THRESHOLD)   # one sock is enough
        self.assertEqual(self.re.threshold([], []), self.re.DEFAULT_THRESHOLD)


class RoomEyeLive(Case):
    def setUp(self):
        super().setUp()
        import room_eye
        self.re = room_eye
        self.s.HOUSE["tidy_spaces"] = SPACES
        cfg = copy.deepcopy(CAM)
        self.s.HOUSE["room_eye"] = cfg
        self.app.set(LIGHT, "off", entity_id=["light.kitchen_1", "light.kitchen_2"])
        self.app.set("vacuum.robo", "docked")
        self.eye = room_eye.Eye(vars(self.s), cfg)
        room_eye.EYE = self.eye
        self.on, self.people, self.next_pic, self.shots = True, 0, picture(), []
        self.eye.cameras_on = lambda: {"cam1": self.on}
        self.eye.capture_allowed = lambda cam, wake=False: (True, None)
        self.eye.capture = lambda cam, purpose, wake=False: (self.shots.append((purpose,)) or self.next_pic, self.people)
        self.eye.sleep = lambda s: None
        self.t = time.time()

    def tick(self, hours=4.01):
        self.t += hours * 3600
        self.eye.tick(self.t)

    def samples(self, label):
        return sum(1 for x in self.eye.S["samples"] if x["label"] == label)

    def teach_tidy(self):
        self.app.pass_report("zoe", "kitchen", points=4)          # the map says the kitchen is tidy
        for i, l in enumerate((-15, 0, 12)):
            self.next_pic = picture(light=l, seed=i)
            self.tick()

    def test_trust_the_map_while_new_then_check_and_mark_mess_live(self):
        self.teach_tidy()
        self.assertEqual(self.samples("tidy"), 3)                 # the first pictures are labelled by the map
        first = [c for c in self.app.calls if c[0] in ("scene.create", "light.turn_on", "scene.turn_on")][:3]
        self.assertEqual([c[0] for c in first], ["scene.create", "light.turn_on", "scene.turn_on"])   # snapshot, white, back
        self.assertEqual(first[0][1]["snapshot_entities"], [LIGHT, "light.kitchen_1", "light.kitchen_2"])
        self.assertEqual((first[1][1]["brightness"], first[1][1]["color_temp_kelvin"]), (255, 5000))
        self.assertFalse([c for c in self.app.calls if c[0] == "input_select.select_option"])   # never the mode selects
        self.next_pic = picture(light=5, seed=11)
        self.tick()
        self.assertEqual(self.eye.S["checks"][-1]["verdict"], "tidy")
        self.assertTrue(self.app.state()["board"]["kitchen"]["clean_until"])
        self.next_pic = picture(light=5, mess=[(320, 300, 90, 60)], seed=12)
        self.tick()
        self.assertEqual(self.eye.S["checks"][-1]["verdict"], "untidy")
        b = self.app.state()["board"]["kitchen"]
        self.assertFalse(b["clean_until"])                        # live: the room is untidy now
        self.assertIn("camera", b["note"])
        self.assertEqual(len(self.eye.open_queue()), 1)           # the spot waits for a grown-up's label

    def test_not_a_hazard_label_is_ignored_from_then_on(self):
        self.teach_tidy()
        mess = picture(light=5, mess=[(320, 300, 90, 60)], seed=12)
        self.next_pic = mess
        self.tick()
        qid = self.eye.open_queue()[0]["id"]
        code, r = self.app.post("/api/room_eye/label", {"id": qid, "name": "laundry basket", "hazard": False, "by": "jordan"})
        self.assertEqual(code, 200, r)
        self.next_pic = picture(light=-5, seed=20)                # tidied: a grown-up clicks tidy (a labelled picture)
        self.app.post("/api/mark_clean", {"chore": "kitchen", "by": "jordan"})
        self.tick()
        self.next_pic = picture(light=3, mess=[(320, 300, 90, 60)], seed=13)
        self.tick()
        self.assertEqual(self.eye.S["checks"][-1]["verdict"], "tidy")

    def test_a_hazard_label_names_the_mess(self):
        self.teach_tidy()
        self.next_pic = picture(light=5, mess=[(320, 300, 90, 60)], seed=12)
        self.tick()
        qid = self.eye.open_queue()[0]["id"]
        self.app.post("/api/room_eye/label", {"id": qid, "name": "sock", "hazard": True, "by": "jordan"})
        self.next_pic = picture(light=-5, seed=20)
        self.app.post("/api/mark_clean", {"chore": "kitchen", "by": "jordan"})
        self.tick()
        self.next_pic = picture(light=3, mess=[(330, 300, 80, 60)], seed=14)
        self.tick()
        self.assertEqual(self.eye.S["checks"][-1].get("hazards"), ["sock"])
        self.assertIn("sock", self.app.state()["board"]["kitchen"]["note"])

    def test_a_click_takes_a_labelled_picture_now_or_at_the_next_camera_window(self):
        self.tick(0.01)                                           # first run: clicks count from now (+ a map picture)
        self.shots.clear()
        self.on = False
        self.app.post("/api/space_tidy", {"space": "kitchen_space", "tidy": False, "by": "jordan"})
        self.tick(0.01)
        self.assertEqual(self.shots, [])                          # the camera is off: never turned on for it
        self.assertEqual(self.eye.S["pending"]["cam1"]["label"], "untidy")
        self.on = True
        before = self.samples("untidy")
        self.tick(0.01)
        self.assertEqual(self.samples("untidy"), before + 1)
        self.assertEqual(self.shots[-1][0], "tidy_label")

    def test_a_picture_with_people_in_it_is_never_kept(self):
        self.app.pass_report("zoe", "kitchen", points=4)
        self.people = 1
        self.tick()
        self.assertEqual(self.samples("tidy"), 0)

    def test_a_picture_with_unknown_people_count_is_never_kept(self):
        self.app.pass_report("zoe", "kitchen", points=4)
        self.people = -1
        self.tick()
        self.assertEqual(self.samples("tidy"), 0)

    def test_capture_requests_use_the_private_bearer_file(self):
        class Response:
            status, headers = 200, {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return b"ok"

        with tempfile.TemporaryDirectory() as td:
            token_file = Path(td) / "capture.token"
            token_file.write_text("test-secret\n")
            token_file.chmod(0o600)
            self.eye.cfg["capture_token_file"] = str(token_file)
            with mock.patch("urllib.request.urlopen", return_value=Response()) as opened:
                self.assertEqual(self.eye.http("GET", "/cameras"), (200, {}, b"ok"))
            req = opened.call_args.args[0]
            self.assertEqual(req.get_header("Authorization"), "Bearer test-secret")

    def test_capture_requests_fail_closed_without_a_bearer(self):
        self.eye.cfg["capture_token_file"] = "/definitely/missing/room-eye.token"
        with mock.patch("urllib.request.urlopen") as opened:
            with self.assertRaisesRegex(RuntimeError, "authentication unavailable"):
                self.eye.http("GET", "/cameras")
        opened.assert_not_called()

    def test_private_camera_is_rejected_before_lights_or_shades_change(self):
        self.eye.capture_allowed = self.re.Eye.capture_allowed.__get__(self.eye)
        self.eye.http = lambda *args, **kwargs: (200, {}, b'{"cam1":{"on":true,"inspection_allowed":false}}')
        self.eye.cfg["cameras"]["cam1"]["shades"] = ["cover.k_shade"]
        before = len(self.app.calls)
        jpeg, why = self.eye.shoot("cam1", "inspection_before", wake=True, shades=True)
        self.assertIsNone(jpeg)
        self.assertIn("private", why)
        self.assertEqual(self.app.calls[before:], [])

    def test_automatic_camera_permission_must_be_explicitly_on(self):
        self.eye.capture_allowed = self.re.Eye.capture_allowed.__get__(self.eye)
        for payload in (b'{"cam1":{"on":false,"inspection_allowed":true}}', b'{"cam1":{}}', b'{}'):
            self.eye.http = lambda *args, payload=payload, **kwargs: (200, {}, payload)
            self.assertFalse(self.eye.capture_allowed("cam1")[0])

    def test_robot_seen_by_a_camera_teaches_the_locator(self):
        import locator
        self.app.set("vacuum.robo", "returning")
        self.app.set("binary_sensor.cam1_motion", "off")
        self.eye.tick(self.t)
        self.app.set("binary_sensor.cam1_motion", "on")
        self.eye.tick(self.t + 60)
        g = vars(self.s)
        self.assertEqual([t["room"] for t in g["LOCATOR_TEACH"]], ["Kitchen"])
        self.s.HOUSE["robot_locator"] = {"robots": {"vacuum.robo": {"room_sensor": "sensor.robo_room"}}}
        loc = locator.Locator(g)
        loc.position = lambda vac: (1000, 2000)
        g["LOCATOR_TEACH"][0]["at"] = time.time()
        self.assertTrue(loc.teach(time.time()))
        self.assertEqual(loc.classify("vacuum.robo", 1000, 2000)[0], "Kitchen")
        self.assertEqual(loc.fix["vacuum.robo"]["room"], "Kitchen")

    def test_no_robot_teaching_right_after_our_own_flash(self):
        self.app.set("vacuum.robo", "returning")
        self.app.set("binary_sensor.cam1_motion", "off")
        self.app.pass_report("zoe", "kitchen", points=4)
        self.tick()                                               # flashes the lights
        self.app.set("binary_sensor.cam1_motion", "on")
        self.eye.robots({"cam1": True}, time.time())
        self.assertFalse(vars(self.s).get("LOCATOR_TEACH"))

    def test_before_and_after_pictures_for_an_inspection_while_the_grownup_is_away(self):
        # "cameras used when [a grown-up] isn't home and kids claim an untidy room, to take before and after pictures ...
        #  camera turns on just long enough to get a snap of each with bright white lights, open shades"
        self.tick(0.01)
        self.eye.cfg["inspect_shots"] = {"unless_home": ["sam"]}
        self.eye.cfg["cameras"]["cam1"]["shades"] = ["cover.k_shade"]
        self.on, self.shots = False, []                          # the camera is off: the picture wakes it
        self.s.adult_home = lambda who, tok: False
        vars(self.s)["adult_home"] = self.s.adult_home
        self.assertEqual(self.app.post("/api/start", {"kid": "zoe", "chore": "kitchen"})[0], 200)
        self.tick(0.01)
        self.assertEqual([x[0] for x in self.shots], ["inspection_before"])
        calls = [c[0] for c in self.app.calls]
        self.assertIn("cover.open_cover", calls)                 # shades up, lights white, then back from the snapshot
        self.assertIn("scene.turn_on", calls)
        ev = self.app.pass_report("zoe", "kitchen", points=4)
        self.tick(0.01)
        self.assertEqual([x[0] for x in self.shots], ["inspection_before", "inspection_after"])
        ph = self.eye.photos_for(ref=ev["id"])
        self.assertEqual(len(ph), 1)                              # one pair
        self.assertTrue(ph[0]["before"] and ph[0]["after"])
        # Inspection media stays in the private store. The LAN server has no photo routes.
        import urllib.error
        import urllib.request
        for path in ("/api/inspect_photos", "/api/inspect_photo/%s/after.jpg" % ph[0]["key"]):
            for headers in ({}, {"X-HA-User": "guest", "Referer": self.app.base + "/inspect"}):
                req = urllib.request.Request(self.app.base + path, headers=headers)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(req, timeout=10)
                self.assertEqual(error.exception.code, 404)
                error.exception.close()

    def test_no_pictures_while_the_named_grownup_is_home(self):
        self.tick(0.01)
        self.eye.cfg["inspect_shots"] = {"unless_home": ["sam"]}
        self.shots = []
        vars(self.s)["adult_home"] = lambda who, tok: who == "sam"
        self.app.post("/api/start", {"kid": "zoe", "chore": "kitchen"})
        self.tick(0.01)
        self.assertEqual(self.shots, [])


if __name__ == "__main__":
    unittest.main()
