"""Export the made-up family as a static, read-only demo (no server needed to host it).

    python3 tools/export_demo.py OUT_DIR

Writes OUT_DIR/index.html (redirects to the kids' screen), chore-kids.html, chore-inspect.html and `states`
(the demo family's state, as a visitor's demo copy). Serve the folder from any static web server and open
index.html. Times in the snapshot are shifted to "now" in the visitor's browser, so the afternoon stays live.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main(out):
    import demo
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as d:
        demo.seed(Path(d))
        port = free_port()
        house = json.loads((Path(d) / "house.json").read_text())
        house["port"] = port
        (Path(d) / "house.json").write_text(json.dumps(house))
        env = dict(os.environ, CHORE_DATA=d, CHORE_DEMO="1", CHORE_PORT=str(port))
        srv = subprocess.Popen([sys.executable, str(REPO / "server.py")], env=env, cwd=str(REPO),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            st = None
            for _ in range(60):
                try:
                    st = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/api/state" % port, timeout=2).read())
                    break
                except OSError:
                    time.sleep(0.5)
            if st is None:
                sys.exit("the demo server did not start")
        finally:
            srv.terminate()
            srv.wait(10)
    sys.path.insert(0, str(REPO))
    os.environ.setdefault("CHORE_DATA", tempfile.mkdtemp())
    from server import demo_attrs
    attrs = demo_attrs(st)
    attrs["demo_now"] = time.time()
    (out / "states").write_text(json.dumps({"states": {"sensor.chore_demo_state": {"state": "demo", "attributes": attrs}}}))
    shutil.copy(REPO / "static" / "kids.html", out / "chore-kids.html")
    shutil.copy(REPO / "static" / "inspect.html", out / "chore-inspect.html")
    (out / "index.html").write_text('<!doctype html><meta charset="utf-8"><title>Chore Time demo</title>'
                                    '<meta http-equiv="refresh" content="0;url=chore-kids.html?sandbox=states">'
                                    '<a href="chore-kids.html?sandbox=states">Open the Chore Time demo</a>\n')
    print("wrote", out)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
