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
  "households": [{"id": "dad", "name": "Dad's house"}, {"id": "mom", "name": "Mom's house"}],   // optional, see below
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
| `on_demand` | hidden until a grown-up opens it (a button on the parents' page), e.g. laundry after running the machines; hidden again once it passes |
| `from`, `until` | ISO dates when it's available |
| `price_dates` | `{"2026-12-24": 8}`: a different price on one day |
| `slot` | `{"dow": 5, "time": "10:00"}`: one kid claims each week's slot ahead of time |
| `bundle` | ids of rooms. Priced as the sum of those still dirty. `points` makes it a fixed price, and `together` means team only |
| `boost` | `{"pct": 25, "min": 3}`: a bundle is worth 25% more with 3+ dirty rooms |
| `propose`, `propose_max` | the kid suggests their own price |
| `surface` | a table or desk: never makes its room show dirty on a map |
| `map_rooms`, `floor`, `where`, `desc`, `desc_for` | where it is, and a description (`desc_for` = per kid) |
| `household` | for two-home families: the household id it belongs to, or `"any"` (homework, brush teeth). Default: the first household |
| `hidden` | resting, not shown |

### Two households

A child who lives in two homes can use Chore Time in both. Add `households` (the first is the default home of every job
and reward), give jobs and rewards a `household` (or `"any"`), and tell Chore Time where the child is:

```jsonc
"kids": [{"id": "zoe", ..., "where": {"entity": "input_boolean.casey_home", "on": "dad", "off": "mom"}}]
```

The child's board then shows that home's jobs and rewards plus the `"any"` ones, and the server refuses the others. Stars
are one balance across both homes. The other parent can sign in through Home Assistant: give their HA login a view with
the kids' screen locked to their child (`/local/<kids page>?kid=zoe`, served from Home Assistant's `www` folder) and limit
what that login may do with `home_assistant.user_kids` in house.json (`{"<HA user id>": ["zoe"]}`).

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
| `vacuum` | the robot's entities, `segments` (robot room → `chore`/`chores`, `mop`, `rooms`: the floor-plan rooms it really covers), person `sensors` per segment, `entry_sensors`, `entry_doors`, `voice_volume`, night hours, `night_carpet_boost_off` (sends the robot's own `set_carpet_mode`; not an official integration entity). Goals: `schedule` (segment → `{"nightly_after": "19:00", "needs_dinner": true}`), `every_h` (default 72), `dinner` (`sensor`, `from`, `until`, `min_block_min`, `merge_gap_s`: dinner = one continuous stretch with at least `min_block_min` inside the window; people passing through look like short blips; `reopens`: jobs a detected dinner makes dirty that evening) |
| `robot_locator` | per robot vacuum: `room_sensor` (the integration's current-room sensor) and `publish` (a sensor to create). Learns room outlines from the robot's position while it cleans, then reports its room any time, including while it heads home or is lost, when the integration's own sensor is frozen |
| `grocy` | `url`, `key_file`, `freezer_location`, `requesters`: Grocy as the master chore list. Also starts the grown-ups' side (`house_tasks.py` → `sensor.house_tasks`): due grown-up chores, open tasks and batteries to replace. Run `tools/grocy_userfields.py` for the userfields: on chores and tasks `ctx_show_on`, `ctx_show_when`, `ctx_done_when` (Home Assistant conditions such as `sensor.dryer = off and person.jordan = home`, or clock tests `now.hour >= 17`, `now.weekday >= 5`, or another task `task.12 = done`); on batteries `ha_entity` (the device's battery sensor(s)) and `ha_low` (replace at or below; negative = registered but not watched). `ctx_rooms` on chores and tasks: where it is done (a checklist built from house_tasks.floors). `review_category`: a task category used as an inbox (counted, not listed) |
| `house_tasks` | `floors` (floor → rooms), `displays` (screen id → its room), `per_display` (default 6): each screen ranks the grown-ups' jobs for its own room (overdue first, then due today, then undated; the screen's room, then its floor, ranks up). Published in `sensor.house_tasks` → `displays`. `todo_entity` (e.g. a Local To-do list `todo.house_jobs`): kept in step both ways, so ticking it in the HA app or by voice through Assist ("add … to house jobs", "complete …") reaches Grocy |
| `dashboard_mirror` | keep a grown-up's own HA dashboard in step with chosen views |

### Home Assistant pieces you create

- `input_boolean.chore_time`, `input_number.chore_time_hours`: chore time, visible in HA.
- `input_text.chore_action` (max 255): HA dashboards send actions through it.
- Light-mode `input_select`s per room, if you use `lights` (your own lighting automations decide what "white" means).
- For the vacuum: `input_boolean.vacuum_autopilot` (the on/off switch) and `input_boolean.vacuum_autopilot_active` (on while it drives, so your own automations can stand down), plus a script that starts room cleaning by name (see `contrib/`).

Chore Time publishes `sensor.chore_state`, `sensor.chore_points_<kid>`, `sensor.chore_lights` (which bulbs carry a colour, so your lighting can skip them) and `sensor.vacuum_autopilot` (every room's vacuum decision and why).
