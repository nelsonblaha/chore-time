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
    {"id": "movie", "name": "Movie", "icon": "1f3ac", "cost": 15, "tv_minutes": 100,
     "together_cost": {"2": 12, "3": 10}},          // optional: what EACH kid pays when 2 or 3 watch together
    {"id": "vr", "name": "VR", "icon": "1f97d", "cost": 2, "minutes": 5, "break_min": 30,
     "needs_ok": true}                               // optional: the kid ASKS; a grown-up accepts (it starts and is paid then) or says not now
  ],
  "bedtime": "20:30",                                 // or "off"
  "rewards_until": "19:30",                           // optional: no rewards from then until morning (brush-teeth time)
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
| `slot` | `{"dow": 5, "time": "10:00"}`: one kid claims each week's slot ahead of time. Add `"every_weeks": 2, "anchor": "YYYY-MM-DD"` for every other week (the shared calendar below overrides it), and `"certify_after": "12:00"` so a grown-up confirms it happened (the kid's "I did it" goes to a grown-up's inspection; the confirm card is the fallback), and `"claim_days": 4` so kids only see and claim it that many days ahead |
| `ask_title` | `true`: starting it asks the kid to name it ("organizing rocks"); any job can be named with ✏️ while doing it |
| `bundle` | ids of rooms. Priced as the sum of those still dirty. `points` makes it a fixed price, and `together` means team only |
| `boost` | `{"pct": 25, "min": 3}`: a bundle is worth 25% more with 3+ dirty rooms |
| `propose`, `propose_max` | the kid suggests their own price |
| `surface` | a table or desk: never makes its room show dirty on a map |
| `map_rooms`, `floor`, `where`, `desc`, `desc_for` | where it is, and a description (`desc_for` = per kid) |
| `household` | for two-home families: the household id it belongs to, or `"any"` (homework, brush teeth). Default: the first household |
| `hidden` | resting, not shown |

Most of these can be set without editing JSON: **＋ New chore** and **Edit** on the parents' page open a form for the name, subtitle (`desc`), icon, stars, how it repeats (one time, back after N days, once a day per kid, any time, or only when a grown-up opens it), where, which kids, which house, and the dates, times, daylight and length. The "Where" list comes from `house_tasks.floors` in house.json; picking a room also fills `floor`, and `map_rooms` when a robot-map room of that name is already in use.

### Envelopes (letters)

A job listed in `envelopes.chores` (e.g. a letter to a relative) gets extra steps before its "I'm done": the kid picks who it's
from and who it's to, puts an envelope in the printer (with an optional photo showing how), and taps **Print**. Chore Time
writes a one-page PDF and sends it to CUPS with `lp`. See the docstring at the top of `envelopes.py` for every key:

```json
"envelopes": {"printer": "Office", "chores": ["letter"],
  "lp_options": {"PageSize": "Env10", "InputSlot": "Manual", "MediaType": "Envelope", "orientation-requested": "4", "print-scaling": "none"},
  "return_address": ["1 Main St", "Springfield"], "senders": {"ava": "Ava Example"},
  "recipients": [{"id": "gran", "label": "Granny", "name": "Ann Example", "icon": "1f475"}],
  "to_address": ["2 Elm St", "Shelbyville"], "load_photo": "envelope-load.jpg"}
```

### Kids' calendar (which weekends they're here)

house.json `"kid_calendar": {"ics_url": "<private iCal address>", "with": "^Home"}`: an all-day event whose title
matches `with` means the kids are here that day. Slot chores (e.g. reading at a weekly service) follow it where it
has data, and their `every_weeks` rule elsewhere. Read every 6 hours into `kid_calendar.json` in the data folder.

The same calendar marks days with no school: an all-day event whose title matches `"no_school"` (default: holiday,
break, intersession, "no school", "off school", vacation) and not `"school_anyway"` (default: "last day", "resumes",
"first day"). An event that names a kid is that kid's day off only.

### School mornings ("Put on clothes!" by itself)

house.json `"dress": {"school_mornings": {"at": "05:30", "until": "07:30", "weekdays": [0, 1, 2, 3, 4],
"awake": "input_boolean.kids_awake"}}`: on a school day (one of `weekdays`, Mon=0, that the kids' calendar doesn't
mark as no school) the "Put on clothes!" switch turns on at `at` for the kids at this house (home switch on), and
off at `until` if a grown-up hasn't passed everyone yet. It is quiet: no spoken reminders, no chimes, and the
Hubs and TVs aren't taken over until the kids are up (`awake`: an optional Home Assistant switch that turns on
when the kids get up; otherwise when bedtime ends). Each kid starts their own race clock with Start on their
screen. Once a morning: a grown-up who
switches it off keeps it off until the next school day.

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
| `arrival_grace_hour` | streaks skip days a kid was away (`home_entity` off). The day they come home after a day away also counts as away when they got home at or after this hour (default 19): too late for a job. A job done that day still counts |
| `demo_state` | a sensor name (e.g. `sensor.chore_demo_state`): publish a copy of the state for visitors' read-only demos, without free text, other households, where the kids are, letter addresses or other grown-ups. Open `kids.html?sandbox=<states url>` (a JSON of `{states: {sensor.chore_demo_state: ...}}`): the real screens, taps change only the page, and the grown-up button pretends to ask the watch, then opens the grown-ups' page in the same mode |
| `vacuum` | the robot's entities, `segments` (robot room → `chore`/`chores`, `mop`, `rooms`: the floor-plan rooms it really covers), person `sensors` per segment, `held_sensors` (those whose On holds for many minutes, like a thermostat's motion: they count from when they turned on, and not at all during a run, when the robot trips them), `entry_sensors`, `entry_doors`, `voice_volume`, night hours, `night_carpet_boost_off` (sends the robot's own `set_carpet_mode`; not an official integration entity). Goals: `schedule` (segment → `{"nightly_after": "19:00", "needs_dinner": true}`), `every_h` (default 72), `dinner` (`sensor`, `from`, `until`, `min_block_min`, `merge_gap_s`: dinner = one continuous stretch with at least `min_block_min` inside the window; people passing through look like short blips; `reopens`: jobs a detected dinner makes dirty that evening). Optional `map_select` + `map_name`: the robot's map picker and the map these segments are on; the status shows `map_selected` and `map_other` (set while the robot is on another map, whose room names differ). Your clean script should load the right map before sending rooms. `pin_map`: true puts a docked robot back on `map_name` whenever it sits on another map. `failed_start_backoff`: a run whose robot cleans for under a minute is a failed start (reported with the map it went back to); the autopilot then waits 2 h, 4 h, 8 h, then a day (false: retry every quiet window, e.g. while you fix the robot). `progress`, `clean_time`: the robot's own % done and seconds-this-clean sensors, shown in the status with the battery. While the robot reports `mapping`, the autopilot sends it nothing at all. The status sensor's `why` is always a sentence (what it waits for, what it's cleaning and how many rooms are left, why it paused or won't start, a clean started by hand), with `last_run`, `backoff_until` and a `heartbeat` so its timestamp moves every 5 minutes. `explore` (`{school_from_h: 15, free_from_h: 9, until_h: 19}`): a tidiness check. While the kids are here today and tomorrow (kid calendar), at least one is home, and it's the afternoon on a school day or from the morning on a day off, not past `until_h` or bedtime, the robot explores the rooms marked untidy that nothing else holds back, at most once a day, under the same empty-floor rule. If it gets stuck, the room is confirmed untidy and the kids rescue it (`vacuum_help`). The status and the room panel say why. `room_requests`: a helper holding `{segment: epoch}` for rooms a grown-up queued to clean when vacant; these run at the next vacancy even with the autopilot switch off, which governs only the autopilot's own runs |
| `robot_locator` | per robot vacuum: `room_sensor` (the integration's current-room sensor) and `publish` (a sensor to create). Learns room outlines from the robot's position while it cleans, then reports its room any time, including while it heads home or is lost, when the integration's own sensor is frozen. Off-cleaning it reads `unknown` unless a position from the last minute places it in one learned room (never the frozen map room, an old fix, an unlearned spot or a tie); attributes `observed_at`/`expires_at`/`why`, re-sent every 30 s. Optional `dock_room`: the room while docked. Fusion: every live source is evidence of the robot's room: its map room, its learned position, room `motion` sensors (`{sensor: robot room}`) and `cameras` motion/sound events (`{event entity: robot room}`) while the presence tracker (`occupancy: {sensor, floor}`) says nobody is on that floor, and a grown-up's word (`POST /api/robot_seen {vacuum, room, by}`). The estimate is a vote weighted by each source's learned trust (graded whenever the map room and the position agree, or a grown-up says where it is) and by age (half every 30 s). A confident estimate teaches the outlines. Published: `fused_from`, `confidence`, `evidence`, `reliability`. `motion_when` (the autopilot's status sensor): without a tracker, motion during an autopilot run still teaches the outlines, confirmed a minute later |
| `vacuum_more` | more robots, each its own autopilot: a list of house.json section names shaped like `vacuum` (e.g. an upstairs robot), each with its own `publish` sensor, `log_src` and state file. Optional on any robot section: `occupancy` (`sensor`, `floor`, `seal`: floor vacancy from a people tracker instead of motion sensors; fails closed when stale or the roster is incomplete; an occupied room whose door contacts all read closed is skipped instead of waited for), `guest_switches` (segment → switch: skip while a guest stays), `untidy_blocks` (rooms marked untidy wait until tidy), `smart_mode` (`always` or `day`: the robot's own automatic vacuum/mop choice per room; nights keep the quiet tiers), `tvs` (which TVs count as someone here; `[]` none) |
| `vacuum_help` | robot hero jobs that open by themselves (`vacuum_help.py`). Per robot: `id`, `name`, `floor`, `error` (the vacuum-error sensor), optional `dock_error` and `water` (binary sensors, On = change the water), `room` (sensors to read the robot's room from, first known wins) and `rooms` (robot room → `tidy_spaces` ids). A stuck robot (brush or wheels jammed, trapped, stranded...) opens "Rescue *name*!": free it, press Home (or carry it to its dock), and, when it got stuck on something, tidy at least part of that room; the room is marked untidy at once; the inspector adds stars for the part tidied, which come off the room's own job. An empty clean tank or full dirty tank opens "Fresh water for *name*". Worn parts, filters, the dust bag and other dock upkeep never open a job. Each opens with a one-time price (`points.rescue`, `points.water`), so it arrives as BIG NEWS; none open at bedtime; a job nobody took closes 15 min after the robot is fine again. Per robot, optional `sensors_left` (the robot's sensor-cleaning time-left sensor), `sensors_reset` (its reset button) and `sensors_photo` (a picture in data/photos showing what to wipe): when the reminder reaches 0 a trusted *Clean the robot's sensors* job opens (`points.sensors`, default 2). The kid confirms they wiped every spot, gets the stars without an inspection, and the robot's reminder is reset. A reminder reset somewhere else closes the job and ends an in-progress copy without awarding stars. Any chore can be trusted like this (`trust: true`, optional `confirm` question) |
| `room_eye` | cameras that learn what each room looks like tidy (`room_eye.py`, needs numpy + Pillow). `capture_url`: a camera service (`GET /cameras` → which are on; `POST /capture {camera, purpose}` → one JPEG with an `X-People` count, or 409 when that camera is off; it never turns a camera on). `cameras`: per camera, `spaces` (tidy_spaces it sees), `lights` (lights snapshotted, turned full white for the picture, then restored from the snapshot), optional `motion` (a binary sensor) and `robots` (vacuum → the robot's room name for this view). Only while a camera is on: every `every_h` (4) it takes a picture and, once it has tidy references, marks its rooms untidy when enough changed (live); a grown-up's tidy/untidy click on those rooms adds a labelled picture (now, or at the camera's next on-window); its first 3 pictures of each kind are labelled by the map. Changed spots queue for a grown-up's label (`GET /api/room_eye`, `POST /api/room_eye/label {id, name, hazard, by}`); "not a hazard" spots are ignored from then on, hazards are named in the room's note. Motion while a floor's robot is out teaches the robot locator. Pictures with people are never kept; only grown-ups' labels are used as evidence. Optional `inspect_shots` (`{unless_home: [grown-up ids], keep_days: 14, media_dir, media_source}`): when a kid claims a job in a room a camera sees, and again when they say it's done, the camera service is asked to `wake` the camera for one picture, with the room's lights full white and its `shades` (per camera) open, all restored after. Never while a grown-up in `unless_home` is home. The grown-ups' inspection shows the pair. `media_dir`/`media_source`: a Home Assistant media folder for the HA copy of that page. Pictures are deleted after `keep_days` |
| `grocy` | `url`, `key_file`, `freezer_location`, `requesters`: Grocy as the master chore list. Also starts the grown-ups' side (`house_tasks.py` → `sensor.house_tasks`): due grown-up chores, open tasks and batteries to replace. Run `tools/grocy_userfields.py` for the userfields: on chores and tasks `ctx_show_on`, `ctx_show_when`, `ctx_done_when` (Home Assistant conditions such as `sensor.dryer = off and person.jordan = home`, or clock tests `now.hour >= 17`, `now.weekday >= 5`, or another task `task.12 = done`); on batteries `ha_entity` (the device's battery sensor(s)) and `ha_low` (replace at or below; negative = registered but not watched). `ctx_rooms` on chores and tasks: where it is done (a checklist built from house_tasks.floors). `review_category`: a task category used as an inbox (counted, not listed) |
| `house_tasks` | `floors` (floor → rooms), `displays` (screen id → its room), `per_display` (default 6): each screen ranks the grown-ups' jobs for its own room (overdue first, then due today, then undated; the screen's room, then its floor, ranks up). Published in `sensor.house_tasks` → `displays`. `todo_entity` (e.g. a Local To-do list `todo.house_jobs`): kept in step both ways, so ticking it in the HA app or by voice through Assist ("add … to house jobs", "complete …") reaches Grocy |
| `dashboard_mirror` | keep a grown-up's own HA dashboard in step with chosen views |

### Home Assistant pieces you create

- `input_boolean.chore_time`, `input_number.chore_time_hours`: chore time, visible in HA.
- `input_text.chore_action` (max 255): HA dashboards send actions through it.
- Light-mode `input_select`s per room, if you use `lights` (your own lighting automations decide what "white" means).
- For the vacuum: `input_boolean.vacuum_autopilot` (the on/off switch) and `input_boolean.vacuum_autopilot_active` (on while it drives, so your own automations can stand down), plus a script that starts room cleaning by name (see `contrib/`).

Chore Time publishes `sensor.chore_state`, `sensor.chore_points_<kid>`, `sensor.chore_lights` (which bulbs carry a colour, so your lighting can skip them) and `sensor.vacuum_autopilot` (every room's vacuum decision and why).

### Physical vacuum buttons

Home Assistant can append a physical button selection to a robot's durable, per-floor FIFO with
`POST /api/vacuum/button` and JSON `{floor, gesture, rooms, request_id}`. `floor` is the configured
vacuum floor, `gesture` is `single`, `double` or `long`, `rooms` contains exact segment names, and
`request_id` makes retries idempotent. Send the contents of `vacuum-button.token` in the
`X-Vacuum-Button` header; requests fail closed when the local token file is missing. The response is
`{ok, rooms, why}` and always lists the exact accepted segments, including for whole-floor mode. The queue never preempts a
clean already in progress, a paused/returning robot, or a recharge or mop-wash dock break; it waits for
confirmed completion before starting the next item and survives an app restart. Rooms already active
or queued are deduplicated while still appearing in the response. When any accessible room is marked
untidy, a long press queues the remaining accessible allowed segments in order, one room at a time,
and skips untidy or blocked ones. With no accessible untidy room, it uses the robot's genuine
whole-floor start unless that would enter a reachable room under a standing exclusion. That decision
is checked again when the request reaches the head of the queue.

### Spoken announcements

`house.json` → `home_assistant.announce`: `tts` (a TTS entity) and `speaker` (a media player). If the speaker
plays only a chime and no voice (it can't fetch Home Assistant's own TTS links, e.g. a Music Assistant player
that can't resolve HA's internal hostname), add `"media_base": "http://<HA address the speaker can reach>:8123"`:
Chore Time then makes the audio itself and plays it from there. VR warnings are said twice.
