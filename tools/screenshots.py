"""Regenerate the README images from the demo family.

    python3 tools/demo.py --seed-only /tmp/ct-demo && CHORE_DATA=/tmp/ct-demo CHORE_DEMO=1 CHORE_PORT=8791 python3 server.py &
    uv run --with playwright --with pillow python tools/screenshots.py http://localhost:8791
"""
import json
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8791"
OUT = Path(__file__).resolve().parents[1] / "docs" / "img"
OUT.mkdir(parents=True, exist_ok=True)
CHROME = "/usr/bin/google-chrome" if Path("/usr/bin/google-chrome").exists() else None
HUB = {"width": 1280, "height": 800}      # Nest Hub Max


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())


def shot(pg, name):
    pg.wait_for_timeout(1800)
    pg.screenshot(path=str(OUT / name))
    print("wrote", name)


with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROME) if CHROME else p.chromium.launch()
    pg = b.new_page(viewport=HUB)
    pg.route("**/api/log*", lambda r: r.fulfill(status=200, body="{}"))
    frames = []

    def frame(name):
        shot(pg, name)
        frames.append(OUT / name)

    pg.goto(BASE + "/")
    frame("hub-login.png")
    pg.evaluate("openKid('max')")
    pg.wait_for_timeout(800)
    pg.evaluate("document.querySelectorAll('#m.on .btn, #nx, #newsok, #awok').forEach(function(b){})")
    pg.evaluate("if(typeof closeModal==='function')closeModal()")
    pg.add_style_tag(content="#tmrchip{display:none!important}")   # a sibling's VR timer chip would cover the balance
    frame("hub-kid.png")
    pg.evaluate("askChore('kitchen')")
    frame("hub-chore.png")
    pg.evaluate("closeModal();kid='ava';renderKid()")
    frame("hub-rewards.png")
    b2 = b.new_page(viewport={"width": 430, "height": 1600}, device_scale_factor=2)
    b2.goto(BASE + "/inspect?by=jordan")
    b2.wait_for_timeout(2500)
    b2.screenshot(path=str(OUT / "parents.png"), clip={"x": 0, "y": 0, "width": 430, "height": 1500})
    print("wrote parents.png")
    tv = b.new_page(viewport={"width": 1280, "height": 720})
    tv.goto(BASE + "/tv")
    shot(tv, "tv-lock.png")
    now = b.new_page(viewport=HUB)
    now.goto(BASE + "/now")
    shot(now, "right-now.png")
    post("/api/bedtime", {"by": "jordan", "on": True})
    bed = b.new_page(viewport=HUB)
    bed.route("**/api/log*", lambda r: r.fulfill(status=200, body="{}"))
    bed.goto(BASE + "/")
    shot(bed, "hub-bedtime.png")
    post("/api/bedtime", {"by": "jordan", "on": False})
    b.close()

try:   # a small animated tour for the top of the README
    from PIL import Image
    imgs = [Image.open(f).convert("RGB").resize((640, 400)) for f in frames]
    imgs[0].save(OUT / "tour.gif", save_all=True, append_images=imgs[1:], duration=1600, loop=0, optimize=True)
    print("wrote tour.gif")
except ImportError:
    pass
