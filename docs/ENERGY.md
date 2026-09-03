# Energy

Every number below is an order-of-magnitude engineering estimate derived
from the design's own parameters (`firmware/src/config.h` /
`service/rules.py`) and typical ESP32/DHT22 datasheet figures -- this repo
has no physical hardware attached to measure from. Treat them as a
justification for the design choices, not a calibration report; re-derive
them from real current-draw measurements before citing them anywhere that
matters.

## Assumed usage pattern

Two showers/day, ~10 minutes each, in a moderately humid climate (ambient
40-50 %RH). This is the scenario the hysteresis thresholds in `config.h`
(`HUM_ON=70`, `DELTA_ON=8`, `RISE_ON=2`) are tuned for.

## Fan duty cycle

| Trigger path | Typical time-to-trigger from shower start |
|---|---|
| `rise_rate` (dH/dt >= 2 %RH/min, 2 samples) | ~1-2 min -- usually the first to fire |
| `relative_spike` (H - B >= 8 %RH) | ~2-4 min if the rise is more gradual |
| `absolute_threshold` (H >= 70 %RH) | Whenever it's crossed, regardless of the other two |

Per shower, venting typically runs from trigger until humidity clears back
past `HUM_OFF`/`DELTA_OFF`, bounded below by `MIN_RUN_S=180s` (so a quick
in-and-out shower doesn't chatter the relay) and above by
`MAX_RUN_S=1800s` (so a stuck-high sensor can't run the fan forever). A
representative single-shower run: **~15-20 minutes**, dominated by how long
the room actually takes to clear, not by the constants themselves.

- **This design**: 2 showers x ~18 min = **~36 min/day** of fan runtime.
- **Naive baselines it improves on**:
  - A dumb humidistat with a single 70 %RH threshold and no hysteresis
    would relay-chatter every time the room hovers near 70 %RH (see the
    oscillation test in `tests/test_rules.py`) -- more relay wear and mostly
    the same runtime, but with far more switching events and the associated
    motor inrush.
  - A fixed wall-timer fan switch (common in rentals) is typically set to
    20-30 min *per press*, run manually or on every light-switch cycle
    regardless of whether the room is actually still humid -- easily
    2-3x this design's runtime for the same two showers, since it can't
    tell "still damp" from "already cleared."

Cumulative runtime is tracked on-device (`state.runtime_s` in every
`.../state` publish) specifically so this estimate can be replaced with a
measured one after a few days of real use.

## MCU / radio power

- **Wi-Fi modem sleep** (`WiFi.setSleep(true)` +
  `esp_wifi_set_ps(WIFI_PS_MIN_MODEM)`): the radio powers down between DTIM
  beacon intervals instead of staying associated at full power. Typical
  ESP32 current draw drops from ~80-120 mA (radio fully awake, no PS) to
  roughly 20-30 mA average with modem sleep and a stable AP association --
  call it a **60-70% reduction** in average Wi-Fi-related draw. The 60 s
  MQTT keepalive (`config.h::MQTT_KEEPALIVE_S`) is what makes it safe to
  leave the modem mostly asleep: without it, PubSubClient would need
  frequent traffic (or the broker would time the session out), forcing the
  radio awake more often than the sleep policy intends.
- **Adaptive sampling** (`SAMPLE_INTERVAL_IDLE_MS=30000` vs
  `SAMPLE_INTERVAL_ACTIVE_MS=5000`): the DHT22 read itself is cheap (a few
  ms of bit-banging), so the real saving here is fewer wake-ups of the
  sampling/telemetry code path, not sensor power -- most of the ESP32's
  budget is the radio, not the sensor. At ~22.5 hours/day in IDLE (30 s
  cadence) vs ~1.5 hours/day across two showers (5 s cadence), roughly
  **83%** of all sampling ticks happen at the slow cadence.
- **No deep sleep in this build** -- see README.md "Why not deep sleep" for
  the reasoning. This is the single biggest energy cost left on the table
  by design, traded deliberately for the ability to receive a `cmd` message
  within its latency budget.

## Network chatter: publish-on-change

Telemetry publishes only when humidity has moved >= `TELEMETRY_PUBLISH_DELTA
= 0.5 %RH` since the last publish, or `TELEMETRY_HEARTBEAT_MS = 5 min` has
elapsed with no change -- see `serviceSampling()` in `main.cpp`.

Rough daily message count for the two-shower pattern above:

| Policy | Messages/day (approx.) |
|---|---|
| Publish every sample, no change-gating | ~2,800 (IDLE, 30 s cadence) + ~480 (VENTING, 5 s cadence, 2x20 min) = **~3,280** |
| Publish-on-change (this design) | ~288 heartbeats (every 5 min, 24 h) + ~480 during active rises (nearly every active-cadence sample moves >=0.5 %RH while humidity is actually climbing/falling) = **~770** |

That's roughly a **75-80% reduction** in telemetry message volume, at zero
loss of information relevant to the control law -- IDLE periods where
humidity is flat are exactly the periods a naive fixed-cadence publisher
would otherwise be repeating the same number every 30 s.
