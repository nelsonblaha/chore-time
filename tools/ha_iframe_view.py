"""Put a Chore Time page into a Home Assistant dashboard as a full-panel iframe view, or refresh its ?v= cache-buster.

  uv run --with websockets tools/ha_iframe_view.py --page /local/chore-inspect.html --view chores --title Chores \
      [--dashboard lovelace] [--after home] [--icon mdi:broom] [--users <ha-user-id> ...] [--version N]

Re-running with a new --version is how you make tablets and phones pick up a new copy of the page.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _house import ha_session  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--page", required=True)
ap.add_argument("--view", required=True, help="view path, e.g. chores")
ap.add_argument("--title")
ap.add_argument("--dashboard", default=None, help="dashboard url_path; default = the main Overview")
ap.add_argument("--after", help="insert after this view path (new views only)")
ap.add_argument("--icon", default="mdi:broom")
ap.add_argument("--users", nargs="*", help="only these HA user ids see it")
ap.add_argument("--version", default="1")
a = ap.parse_args()


async def main(call):
    sel = {"url_path": a.dashboard} if a.dashboard else {}
    cfg = (await call({"type": "lovelace/config", **sel}))["result"]
    url = f"{a.page}?v={a.version}"
    view = next((v for v in cfg["views"] if v.get("path") == a.view), None)
    if view:
        view["cards"] = [{"type": "iframe", "url": url, "aspect_ratio": "230%"}]
    else:
        view = {"title": a.title or a.view.title(), "path": a.view, "icon": a.icon, "type": "panel",
                "cards": [{"type": "iframe", "url": url, "aspect_ratio": "230%"}]}
        idx = next((i + 1 for i, v in enumerate(cfg["views"]) if v.get("path") == a.after), len(cfg["views"]))
        cfg["views"].insert(idx, view)
    if a.users:
        view["visible"] = [{"user": u} for u in a.users]
    r = await call({"type": "lovelace/config/save", "config": cfg, **sel})
    print("saved" if r.get("success") else r, url)

asyncio.run(ha_session(main))
