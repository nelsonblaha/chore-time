"""Observe a moving robot against a surveyed floor patch. Camera access remains with Room Eye."""
import io
import json
import os
import math
import time
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter


def blobs(mask):
    """Bounding boxes and pixel coordinates of eight-connected foreground components."""
    pending = set(zip(*np.nonzero(mask)))
    while pending:
        seed = pending.pop()
        group, work = [seed], [seed]
        while work:
            y, x = work.pop()
            for yy in range(y-1, y+2):
                for xx in range(x-1, x+2):
                    point = (yy, xx)
                    if point in pending:
                        pending.remove(point)
                        work.append(point)
                        group.append(point)
        if len(group) >= 65:
            yy, xx = np.array(group).T
            yield xx, yy


class FloorView:
    def __init__(self, path):
        with np.load(path, allow_pickle=False) as data:
            self.size = tuple(data['frame_size'])
            self.box = tuple(data['box'])
            self.floor = data['mask'].astype(bool)
            self.empty = data['background'].astype(float)
            self.shape = data['silhouette'].astype(bool)
            self.area = tuple(data['area_range'])
            self.width = tuple(data['width_range'])
            self.height = tuple(data['height_range'])

    def locate(self, image):
        if image.size != self.size:
            return None
        pixels = np.asarray(image.crop(self.box).convert('L'), dtype=float)
        offset = np.median((pixels-self.empty)[self.floor])
        if abs(offset) > 22:
            return None
        loss = self.empty + offset - pixels
        # A displaced camera, a large shadow or a lighting/shade transition invalidates this view.
        if np.mean(np.abs(loss[self.floor]) > 23) > .24:
            return None
        dark = self.floor & (loss >= 19) & (pixels < 59+offset)
        filled = Image.fromarray(dark.astype('uint8')*255).filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))
        matches = []
        for xx, yy in blobs((np.asarray(filled) > 0) & self.floor):
            x, y = xx.min(), yy.min()
            w, h = xx.max()-x+1, yy.max()-y+1
            if not (self.area[0] <= len(xx) <= self.area[1] and self.width[0] <= w <= self.width[1]
                    and self.height[0] <= h <= self.height[1] and 2.0 <= w/h <= 6.6):
                continue
            obj = np.zeros((h,w), dtype='uint8')
            obj[yy-y,xx-x] = 255
            observed = np.asarray(Image.fromarray(obj).resize(self.shape.shape[::-1],Image.Resampling.BOX)) >= 128
            overlap = np.count_nonzero(observed & self.shape) / np.count_nonzero(observed | self.shape)
            if overlap >= .76 and np.median(loss[yy,xx]) >= 30:
                matches.append((float(x+w/2+self.box[0]),float(y+h/2+self.box[1])))
        return matches[0] if len(matches) == 1 else None


class MovingObject:
    def __init__(self):
        self.samples = deque(maxlen=3)

    def observe(self, t, point):
        if point is None:
            self.samples.clear()
            return False
        if self.samples:
            prev, last = self.samples[-1]
            dt = t-prev
            if not .15 <= dt <= 5 or math.dist(last,point) > 70*dt+5:
                self.samples.clear()
        self.samples.append((t,point))
        if len(self.samples) < 3:
            return False
        a,b,c = [p for _,p in self.samples]
        travelled = math.dist(a,b)+math.dist(b,c)
        return (math.dist(a,c) >= 6 and min(math.dist(a,b),math.dist(b,c)) >= 1
                and math.dist(a,c) >= .78*travelled)


def record(path,event):
    try:
        previous = json.loads(path.read_text())
    except FileNotFoundError:
        previous = []
    rows = [*previous[-255:],event]
    tmp = path.with_suffix('.tmp')
    with open(tmp,'w',opener=lambda name,flags: os.open(name,flags,0o600)) as output:
        json.dump(rows,output,allow_nan=False)
    os.replace(tmp,path)


def record_calibration(directory, camera, event, jpeg, limit=96):
    """Keep a bounded private set of people-free frames paired with native navigation.

    Calibration samples deliberately use a different journal from verified sightings, so the locator can never
    learn a room from an unsurveyed view.
    """
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    journal = directory / (camera + '.json')
    try:
        previous = json.loads(journal.read_text())
        if not isinstance(previous, list):
            previous = []
    except (OSError, ValueError):
        previous = []
    name = f"{camera}-{int(event['at'] * 1000)}.jpg"
    image = directory / name
    tmp_image = image.with_suffix('.tmp')
    with open(tmp_image, 'wb', opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
        output.write(jpeg)
    os.replace(tmp_image, image)
    rows = [*previous, {**event, 'image': name}][-limit:]
    tmp_journal = journal.with_suffix('.tmp')
    with open(tmp_journal, 'w', opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
        json.dump(rows, output, allow_nan=False)
    os.replace(tmp_journal, journal)
    keep = {row.get('image') for row in rows if isinstance(row, dict)}
    for old in directory.glob(camera + '-*.jpg'):
        if old.name not in keep:
            old.unlink(missing_ok=True)
    return len(rows)


def run(g, camera, config):
    import room_eye
    from locator import robot_position
    reference = config.get('floor_reference')
    view = FloorView(Path(reference).expanduser()) if reference else None
    calibration_dir = Path(config.get('calibration_dir') or
                           (g['DATA'] / 'robot-camera-calibration')).expanduser()
    calibration_every = max(1, float(config.get('calibration_every_s', 2)))
    motion = MovingObject()
    vacuum, room = config['vacuum'], config['room']
    last_seen, last_report, state, last_calibration = None, 0, None, 0
    calibration_count = 0
    journal = g['DATA'] / ('robot-camera-' + camera + '.json')
    while True:
        active = False
        try:
            eye = room_eye.EYE
            tok = g['ha_token']()
            active = g['ha_get'](vacuum,tok).get('state') in ('cleaning','returning')
            map_state = g['ha_get'](config['map_select'],tok).get('state') if active else None
            allowed = active and map_state == config['map_name'] and eye and \
                eye.capture_allowed(camera, purpose='robot_position')[0]
            status = ('calibrating' if view is None else 'watching') if allowed else 'idle'
            point = None
            should_capture = allowed and (view is not None or time.time()-last_calibration >= calibration_every)
            if should_capture:
                started = time.time()
                data, people = eye.capture(camera,'robot_position')
                captured = time.time()
                if data and people == 0 and captured-started < 3:
                    if view is not None:
                        with Image.open(io.BytesIO(data)) as image:
                            point = view.locate(image)
                    else:
                        xy = robot_position(g,tok,vacuum,timeout=3)
                        paired = xy if time.time()-started < 4 else None
                        # A calibration frame has the same strict second permission check as a verified sighting.
                        still_allowed = eye.capture_allowed(camera, purpose='robot_position')[0]
                        still_active = g['ha_get'](vacuum,tok).get('state') in ('cleaning','returning')
                        same_map = g['ha_get'](config['map_select'],tok).get('state') == config['map_name']
                        if paired and still_allowed and still_active and same_map:
                            event = dict(at=captured, room=room, camera=camera, vacuum=vacuum,
                                         xy=list(paired), map=map_state)
                            calibration_count = record_calibration(calibration_dir,camera,event,data,
                                                                   int(config.get('calibration_limit',96)))
                            last_calibration = captured
                elif not data or people != 0:
                    status = 'waiting'
            else:
                captured = time.time()
            matched = view is not None and motion.observe(captured,point)
            if matched and (last_seen is None or captured-last_seen['at'] >= 6):
                xy = robot_position(g,tok,vacuum,timeout=3)
                paired = xy if time.time()-started < 4 else None
                # Recheck after coordinate retrieval. Occupancy or privacy changes discard the sample.
                if eye.capture_allowed(camera, purpose='robot_position')[0] and \
                        g['ha_get'](vacuum,tok).get('state') in ('cleaning','returning'):
                    last_seen = dict(at=captured, room=room, camera=camera, vacuum=vacuum,
                                     pixel=list(point), xy=paired, map=map_state)
                    record(journal,last_seen)
            now = time.time()
            if allowed and last_seen and now-last_seen['at'] < 9:
                status = 'seen'
            if state != status or now-last_report >= 15:
                attrs = dict(friendly_name=config['name'], room=room, updated_at=now, valid_until=now+30,
                             last_sighting=last_seen, calibration_only=view is None,
                             calibration_samples=calibration_count,
                             source=('people-free frames paired with independent native navigation; no locator output'
                                     if view is None else 'moving robot on surveyed camera floor patch'))
                g['ha_post_state'](config['publish'],status,attrs,tok)
                state,last_report = status,now
        except Exception as error:
            motion.samples.clear()
            print('Robot camera observation unavailable:',type(error).__name__,flush=True)
        time.sleep(.65 if active else 5)
