# Install

Chore Time needs Python 3.11+ and nothing else for the core. For the Home Assistant extras:

```bash
pip install websockets catt     # watch "It's me" + notification buttons; casting to Hubs and TVs
```

## Run it as a service (systemd, user)

```ini
# ~/.config/systemd/user/chore-time.service
[Unit]
Description=Chore Time
After=network-online.target

[Service]
Environment=CHORE_DATA=%h/.local/share/chore-time
ExecStart=/usr/bin/python3 %h/chore-time/server.py
Restart=always

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now chore-time
loginctl enable-linger $USER     # keep it running when you're logged out
```

## Screens

| Where | URL |
|---|---|
| Kids' tablet or Nest Hub Max | `http://<server>:8790/` (the Hub keeper casts it for you if `hubs` is configured) |
| Grown-ups' phone | `http://<server>:8790/inspect`, or inside Home Assistant: copy `static/inspect.html` to `/config/www/chore-inspect.html` and run `tools/ha_iframe_view.py --page /local/chore-inspect.html --view chores` |
| TV lock screen | cast automatically by the TV guard (`/tv`) |
| Kitchen board | `http://<server>:8790/now` |

Inside Home Assistant the parents' page talks to Chore Time through `input_text.chore_action`, so it works from anywhere your HA does, without exposing Chore Time itself.

## Updating

`git pull` and restart. Pages on Hubs and TVs reload themselves when they notice the new version.
