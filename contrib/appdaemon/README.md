# Keeping kid-colour bulbs coloured (AppDaemon example)

Chore Time turns a few colour bulbs in the room being cleaned to the working kid's colour. If your own lighting
automation turns the *whole* room group white on motion, those bulbs flash white and back.

Chore Time publishes which bulbs it holds in `sensor.chore_lights`:

```yaml
rooms:
  living_room:
    kids: [max]
    colors: ["#1e88e5"]
    bulbs: {light.living_room_sconce_left: [142, 36, 170], light.living_room_chandelier_3: [30, 136, 229]}
```

`skip_signal_bulbs.py` shows the pattern for an AppDaemon room app: send white to the rest of the group, and
the held bulbs straight to their colour. It comes with a unit test you can adapt.
