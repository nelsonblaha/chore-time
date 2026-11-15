"""Pattern for an AppDaemon lighting app: apply "white" to a room WITHOUT washing out Chore Time's kid-colour bulbs.

Call white_except_signals(self, "living_room", "light.living_room", transition) where your app would otherwise do
light/turn_on on the whole group.
"""


def chore_signal_bulbs(app, room):
    """{bulb: [r, g, b]} Chore Time holds in this room right now (sensor.chore_lights), else {}."""
    try:
        rooms = app.get_state("sensor.chore_lights", attribute="rooms") or {}
        bulbs = (rooms.get(room) or {}).get("bulbs") or {}
        return {b: list(rgb) for b, rgb in bulbs.items()
                if isinstance(b, str) and b.startswith("light.") and isinstance(rgb, (list, tuple)) and len(rgb) == 3}
    except Exception as exc:  # never let the chore signal break the room's lights
        app.log(f"chore signal lookup failed: {exc}", level="WARNING")
        return {}


def white_except_signals(app, room, group, transition=1, kelvin=4000):
    signal = chore_signal_bulbs(app, room)
    if not signal:
        app.call_service("light/turn_on", entity_id=group, brightness_pct=100, color_temp_kelvin=kelvin, transition=transition)
        return
    members = app.get_state(group, attribute="entity_id") or []
    rest = [m for m in members if m not in signal]
    if rest:
        app.call_service("light/turn_on", entity_id=rest, brightness_pct=100, color_temp_kelvin=kelvin, transition=transition)
    for bulb, rgb in signal.items():
        app.call_service("light/turn_on", entity_id=bulb, rgb_color=rgb, brightness_pct=100, transition=transition)
