# Configuration

Chore Time reads two files from its data folder (`$CHORE_DATA`, default `~/.local/share/chore-time`):

| File | What | Needed |
|---|---|---|
| `chores.json` | the family: kids, grown-ups, jobs, rewards | yes (copied from `example-config/` on first run) |
| `house.json` | the home: Home Assistant, TVs, Hubs, lights, vacuum, Grocy | no: leave it out for the core only |

The data folder also holds `ledger.jsonl` (every event, append-only) and some small state files. Back it up.

## chores.json

```jsonc
{
  "kids": [
    {"id": "ava", "name": "Ava", "color": "#d0103a", "icon": "1f430",   // icon = Twemoji code point
     "home_entity": "input_boolean.ava_home",        // optional: streaks skip days this was off
     "family_color": "#8e24aa",                       // optional: some signal bulbs show this beside her colour
     "toy_swap": true, "presents": 6}                 // optional: presents stash for trading old toys
  ],
  "inspectors": [{"id": "jordan", "name": "Jordan", "icon": "1f9d4", "color": "#00e676"}],   // grown-ups
  "rewards": [
    {"id": "show", "name": "TV show", "icon": "1f4fa", "cost": 5, "tv_minutes": 30},
    {"id": "vr", "name": "VR", "icon": "1f97d", "cost": 2, "minutes": 5, "break_min": 30}
  ],
  "bedtime": "20:30",                                 // or "off"
  "chores": [ ... ]
}
```

The reward ids `show`, `movie` and `games` unlock TVs: streaming/casting, streaming/casting, and HDMI (a console) respectively. A reward with `minutes` runs as an on-screen timer instead.

### A job

| Key | Meaning |
|---|---|
| `id`, `name`, `icon` | identity. `icon` is a Twemoji code point (`1f9f9` = 🧹) |
| `points` | stars. `null` = a grown-up picks at inspection |
| `room` + `cooldown_h` | a room: counts as clean for this many hours after it passes (default 72) |
| `only` | only these kid ids see it |
| `shared` | each kid does their own (organizing, "something else"): never "taken" by a sibling |
| `each_daily`, `each_days` | once per day per kid; `each_days: 2` = every other day per kid |
| `after`, `before` | open between these times of day, e.g. `"19:30"` – `"20:30"` |
| `bedtime_ok` | still allowed at bedtime (brush teeth) |
| `daily`, `weekly` | done for the day / week (`weekly: {"open_dow": 0, "due_dow": 4}`) |
| `daylight` | closed after sunset (needs Home Assistant's `sun`) |
| `once` | disappears after it passes once |
| `from`, `until` | ISO dates when it's available |
| `price_dates` | `{"2026-12-24": 8}`: a different price on one day |
| `slot` | `{"dow": 5, "time": "10:00"}`: one kid claims each week's slot ahead of time |
| `bundle` | ids of rooms. Priced as the sum of those still dirty. `points` makes it a fixed price, and `together` means team only |
| `boost` | `{"pct": 25, "min": 3}`: a bundle is worth 25% more with 3+ dirty rooms |
| `propose`, `propose_max` | the kid suggests their own price |
| `surface` | a table or desk: never makes its room show dirty on a map |
| `map_rooms`, `floor`, `where`, `desc`, `desc_for` | where it is, and a description (`desc_for` = per kid) |
| `hidden` | resting, not shown |

## house.json

Every section is optional. A missing section means that feature is off. `example-config/house.json` is a complete example.

| Section | Turns on |
|---|---|
| `home_assistant` | `url`, `token_file` (a long-lived token), plus entity names for the chore-time switch, the action relay and `sun`. `announce` = a TTS speaker for "five minutes left" |
| `adults` | per grown-up: `presence` entity + `home_state`, and `notify` services. The first one with `notify` gets the watch pings |
| `quiet_hours` | `[{"weekdays": [5, 6], "until": "08:00"}]`: no pings before then |
| `hours` | `chore_start`, `chore_end` (hours the TV guard and Hubs are active), `bed_end` |
| `tvs` | per TV: `name`, `icon`, `ip`, `kind` (`sony`, `vizio` or other), `television`, `players`, `remote`. `finish_show: true` if its player reports playback position. `home_first` = a remote that must press Home before a cast (Vizio) |
| `hubs` | per Hub: `ip`, `entity`, `from_h` (don't light it up before this hour, e.g. in a bedroom) |
| `launchers` | an HA `input_button` → `{ip, path}`: an on-demand "chore board" button |
| `casting` | `catt` path, `hub_volume` |
| `lights` | `chore_rooms`: job → `{modes: [input_select…], covers: [cover…]}`. `signal_bulbs`: room → `{bulbs, pick, pool, ref}` for the kid-colour bulbs |
| `vacuum` | the robot's entities, `segments` (robot room → `chore`/`chores`, `mop`), person `sensors` per segment, `entry_sensors`, `entry_doors`, `voice_volume`, night hours. Goals: `schedule` (segment → `{"nightly_after": "19:00", "needs_dinner": true}`), `every_h` (default 72), `dinner` (`sensor`, `from`, `until`, `min_on_min`) |
| `grocy` | `url`, `key_file`, `freezer_location`, `requesters`: Grocy as the master chore list |
| `dashboard_mirror` | keep a grown-up's own HA dashboard in step with chosen views |

### Home Assistant pieces you create

- `input_boolean.chore_time`, `input_number.chore_time_hours`: chore time, visible in HA.
- `input_text.chore_action` (max 255): HA dashboards send actions through it.
- Light-mode `input_select`s per room, if you use `lights` (your own lighting automations decide what "white" means).
- For the vacuum: `input_boolean.vacuum_autopilot` (the on/off switch) and `input_boolean.vacuum_autopilot_active` (on while it drives, so your own automations can stand down), plus a script that starts room cleaning by name (see `contrib/`).

Chore Time publishes `sensor.chore_state`, `sensor.chore_points_<kid>`, `sensor.chore_lights` (which bulbs carry a colour, so your lighting can skip them) and `sensor.vacuum_autopilot` (every room's vacuum decision and why).
