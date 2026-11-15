"""Shared bits for the helper tools: read house.json (CHORE_HOUSE, else $CHORE_DATA/house.json)."""
import json
import os
from pathlib import Path

DATA = Path(os.environ.get("CHORE_DATA", Path.home() / ".local/share/chore-time")).expanduser()
HOUSE = json.loads(Path(os.environ.get("CHORE_HOUSE", DATA / "house.json")).expanduser().read_text())


def ha():
    """(websocket url, token) for Home Assistant."""
    h = HOUSE["home_assistant"]
    url = h["url"].rstrip("/").replace("http", "ws", 1) + "/api/websocket"
    return url, Path(h["token_file"]).expanduser().read_text().strip()


async def ha_session(fn):
    """Run fn(call) inside an authenticated HA websocket session; call(msg) -> result."""
    import itertools
    import websockets
    url, tok = ha()
    async with websockets.connect(url, max_size=None) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": tok}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            raise SystemExit("Home Assistant refused the token")
        ids = itertools.count(1)

        async def call(msg):
            i = next(ids)
            await ws.send(json.dumps({"id": i, **msg}))
            while True:
                r = json.loads(await ws.recv())
                if r.get("id") == i:
                    return r
        return await fn(call)


def grocy():
    """(api base, key) for Grocy."""
    g = HOUSE["grocy"]
    return g["url"].rstrip("/") + "/api", Path(g["key_file"]).expanduser().read_text().strip()
