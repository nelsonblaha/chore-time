"""Try Chore Time with a made-up family (Ava, Max, Zoe) - no Home Assistant needed.

    python3 tools/demo.py            # builds ./demo-data and starts the server on http://localhost:8790
    python3 tools/demo.py --seed-only DIR   (then: CHORE_DATA=DIR CHORE_DEMO=1 python3 server.py)

Open  /        the kids' touch screen (made for a Nest Hub Max, works in any browser)
      /inspect the grown-ups' page
      /tv      the TV lock screen
      /now     the "Right now" board
"""
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def seed(d: Path):
    d.mkdir(parents=True, exist_ok=True)
    cfg = json.loads((REPO / "example-config" / "chores.json").read_text())
    cfg["bedtime"] = "off"            # the demo should be usable at any hour
    (d / "chores.json").write_text(json.dumps(cfg, indent=2))
    house = json.loads((REPO / "example-config" / "house.json").read_text())
    for k in ("home_assistant", "vacuum", "grocy", "dashboard_mirror"):
        house.pop(k, None)            # demo = core only; TVs/Hubs are listed so the pages show them
    (d / "house.json").write_text(json.dumps(house, indent=2))
    now = time.time()
    evs = []

    def ev(ago_min, **e):
        e = {"id": uuid.uuid4().hex[:12], "ts": now - ago_min * 60, **e}
        evs.append(e)
        return e

    def done(ago, kid, chore, pts, feeling="fun", took=9, by="jordan", team=None):
        r = ev(ago, type="report", kid=kid, chore=chore, feeling=feeling, points=pts, started=now - (ago + took) * 60,
               **({"team": team} if team else {}))
        ev(ago - 4, type="inspect", ref=r["id"], by=by, ok=True, points=pts, label="")

    # yesterday and this morning
    done(1500, "ava", "kitchen", 4, took=14)
    done(1460, "max", "living_room", 3, took=11)
    done(1440, "zoe", "plants", 1, took=3)
    done(900, "ava", "homework", 2, "ok", took=25, by="casey")
    done(700, "ava", "letter", 6, took=20)
    done(600, "max", "mail", 1, took=2)
    done(420, "ava", "family_room", 3, took=12)
    done(400, "zoe", "toy_table", 2, took=6, by="casey")
    done(380, "max", "front_hall", 2, "ok", took=8)
    done(300, "ava", "kids_bath", 3, took=10, team=["ava", "zoe"])
    done(240, "max", "stairs", 2, "hard", took=7)
    r = ev(200, type="report", kid="zoe", chore="other", feeling="fun", points=None, note="FED THE CAT")
    ev(196, type="inspect", ref=r["id"], by="jordan", ok=True, points=1, label="Fed the cat")
    # spending
    ev(180, type="spend", kid="ava", reward="show", cost=5, tv="livingroom", until=now - 150 * 60, minutes=30,
       reward_name="TV show", reward_icon="1f4fa")
    # right now: Max is cleaning the kids' room, Zoe waits for a check, Ava is watching a show
    ev(12, type="start", kid="max", chore="kids_room")
    ev(3, type="report", kid="zoe", chore="laundry", feeling="ok", points=3, started=now - 9 * 60)
    s = ev(14, type="spend", kid="ava", reward="show", cost=5, tv="livingroom", until=now + 16 * 60, minutes=30,
           reward_name="TV show", reward_icon="1f4fa")
    ev(13, type="tv_started", ref=s["id"], tv="livingroom", what="stream")
    ev(1, type="spend", kid="zoe", reward="vr", cost=2, reward_name="VR", reward_icon="1f97d")
    with open(d / "ledger.jsonl", "w") as f:
        for e in sorted(evs, key=lambda e: e["ts"]):
            f.write(json.dumps(e) + "\n")
    print(f"demo data in {d}")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--seed-only":
        seed(Path(sys.argv[2]))
        sys.exit(0)
    d = REPO / "demo-data"
    shutil.rmtree(d, ignore_errors=True)
    seed(d)
    env = dict(os.environ, CHORE_DATA=str(d), CHORE_DEMO="1")
    print("Chore Time demo: http://localhost:%s  (kids)  /inspect  /tv  /now" % env.get("CHORE_PORT", "8790"))
    subprocess.run([sys.executable, str(REPO / "server.py")], env=env)
