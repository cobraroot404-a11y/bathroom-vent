# bathroom-vent

Automated, energy-efficient bathroom ventilation. An ESP32 + DHT22 reads
humidity, publishes telemetry over MQTT, a Python automation service
evaluates the same hysteresis-based rule for observability, and a relay
(standing in for a real exhaust fan) gets switched -- by the ESP32 itself,
autonomously, whether or not any of the network side is even reachable.

```
DHT22 --(GPIO4)--> ESP32 --(Wi-Fi/MQTT)--> Mosquitto <--> automation.py
                     |
                     +--(GPIO26, active-low)--> relay --> fan
```

## Contents

```
firmware/          PlatformIO project (Arduino framework, ESP32)
  src/main.cpp      control loop, MQTT, sampling, relay driver
  src/config.h      every tunable, named -- no magic numbers
  src/secrets.h.example   copy to secrets.h (gitignored) and fill in
service/
  rules.py          pure control-law functions, no I/O -- unit-testable
  automation.py     subscribes telemetry, runs rules.py, publishes cmd
tests/test_rules.py pytest suite for rules.py (no hardware, no broker)
scripts/simulate.py replays a 40-min shower curve at 20x, no hardware needed
broker/mosquitto.conf
docker-compose.yml  mosquitto + automation service
docs/WIRING.md      BOM + ASCII wiring diagram
docs/ENERGY.md      duty cycle and estimated savings
```

## Hardware (fixed)

| Item | Part |
|---|---|
| MCU | ESP32 DevKit V1 (30-pin, WROOM-32) |
| Sensor | DHT22 / AM2302 |
| Actuator | 1-channel 5V opto-isolated relay, ACTIVE-LOW |
| Pull-up | 10 kΩ, DHT22 DATA to 3V3 |
| Supply | 5V / ≥1A USB |

| Signal | GPIO |
|---|---|
| DHT22 DATA | 4 |
| Relay IN | 26 |
| Status LED | 2 |

Full wiring diagram and bill of materials: [docs/WIRING.md](docs/WIRING.md).

## MQTT contract

Base topic: `home/bathroom/vent`

| Topic | Direction | QoS | Retain | Payload |
|---|---|---|---|---|
| `.../status` | device -> broker | 1 | yes | `online` / `offline` (LWT) |
| `.../telemetry` | device -> broker | 1 | no | `{"humidity":68.4,"temperature":24.1,"rssi":-57,"uptime_s":1834,"seq":412}` |
| `.../state` | device -> broker | 1 | yes | `{"fan":"on","mode":"auto","reason":"rise_rate","runtime_s":96}` |
| `.../cmd` | service -> device | 1 | no | `{"fan":"on","mode":"auto","ttl_s":1200}` |

`fan` is `on`/`off`, `mode` is `auto`/`manual`. In `auto` mode the firmware
ignores the `fan` field of any `cmd` whose `ttl_s` has elapsed since it was
received -- a `cmd` is an *advisory, time-boxed* override, not a standing
instruction; once it expires, control reverts to the device's own rule. In
`manual` mode a `cmd`'s `fan` field is sticky until the next `cmd` changes
it.

The LWT (`status` -> `offline`, retained, QoS 1) is registered as part of
the MQTT `CONNECT` packet itself -- the only point in the protocol where a
will *can* be registered -- so a crashed node is detectable from the moment
the broker accepts the session. `online` (retained) is published
immediately after a successful connect.

## Control law

States: `IDLE -> VENTING -> COOLDOWN -> IDLE`.

A rolling 30-minute median of humidity is the ambient baseline `B`. `H` is
the current reading, `dH/dt` its rise rate in %RH/min.

**`IDLE -> VENTING`** on any of:
1. `H >= 70.0` (absolute)
2. `H - B >= 8.0` (relative spike -- the one that actually matters in a
   humid climate, where an absolute threshold alone would either never
   trigger or trigger permanently)
3. `dH/dt >= 2.0`, sustained over two consecutive samples

These are checked in that priority order: any sample with `H >= 70` is
always reported as `absolute_threshold`, even if the other two conditions
are also true for it. That's a documented contract, not an accident --
see `rules.py::_check_venting_trigger`.

**`VENTING -> COOLDOWN`** when *all* of:
1. `H <= 60.0` OR `H - B <= 3.0`
2. `MIN_RUN_S = 180s` has elapsed

**`VENTING -> COOLDOWN` unconditionally** at `MAX_RUN_S = 1800s`, reason
`max_runtime_guard` -- a stuck-high sensor must never run the fan forever.

**`COOLDOWN` holds the fan off** for `COOLDOWN_S = 120s` before `IDLE` can
re-trigger.

The 10 %RH gap between the 70% ON and 60% OFF thresholds is the hysteresis
band and is never collapsed to one number -- see
`tests/test_rules.py::test_oscillation_across_threshold_does_not_chatter`.

### Baseline freezing (a deliberate addition beyond the literal spec)

`B` is a *rolling* median while `IDLE`, but is **frozen** at the value it
held the instant `VENTING` starts, and held constant for the rest of that
VENTING episode. If `B` kept rolling during VENTING, it would include the
shower itself in its own 30-minute window and drift upward toward `H` --
which would silently satisfy `H - B <= 3.0` (the relative "cleared" exit)
for a sensor that is simply pinned high, defeating both the hysteresis exit
condition and, eventually, the `MAX_RUN_S` guard's ability to be the thing
that actually stops it. `B` resumes rolling once back in `IDLE`. This is
exercised directly by
`tests/test_rules.py::test_stuck_high_sensor_forces_off_at_max_runtime`,
which asserts the *only* OFF transition in that scenario is the
`max_runtime_guard` one, not an earlier false `humidity_cleared`.

### Two control laws, intentionally

`service/rules.py` (Python, pure functions, no I/O) and
`firmware/src/main.cpp` (C++, same constants, same state machine) implement
the *same* rule twice, deliberately:

- **`main.cpp` is authoritative and fail-safe.** It drives the relay
  directly from its own local evaluation of `H`, `B`, and the phase timers.
  It does this whether or not Wi-Fi is up, whether or not the broker is
  reachable, whether or not `automation.py` is even running. Ventilation
  must never depend on the network -- see "Reliability" below.
- **`rules.py` exists for observability and policy**: the automation
  service logs every transition with a reason, and is the place to extend
  the policy later (schedules, forecasts, per-bathroom tuning) without ever
  touching firmware that has to keep working standalone.

`tests/test_rules.py` is the executable spec for the state machine; keep
the constants in `config.h` and `rules.py` in sync by hand (there's no
shared build step between an Arduino/C++ project and a Python one here) --
they're both under 20 lines to eyeball-diff.

## Energy efficiency

- **Adaptive sampling**: 30s in `IDLE`; 5s in `VENTING` or whenever
  `dH/dt > 0.5 %RH/min`; never faster than 2s (the DHT22 floor).
- **Wi-Fi modem sleep**: `WiFi.setSleep(true)` +
  `esp_wifi_set_ps(WIFI_PS_MIN_MODEM)`, kept viable by a 60s MQTT keepalive
  instead of reconnecting every cycle.
- **No deep sleep in this build** -- see below.
- **Publish-on-change**: telemetry only goes out when humidity has moved
  `>= 0.5 %RH` since the last publish, or after a 5-minute heartbeat.
- **Cumulative fan runtime** is tracked on-device and included in every
  `.../state` publish (`runtime_s`).

Estimated duty cycle, message-rate reduction, and the reasoning behind the
numbers: [docs/ENERGY.md](docs/ENERGY.md).

### Why not deep sleep

Deep sleep tears down the TCP/MQTT session. A sleeping node cannot receive
a `.../cmd` message within any useful latency budget -- it would only see
it after its next scheduled wake, which for a device meant to respond to
"someone just turned on the shower" defeats the purpose. This build keeps
the radio associated (with modem sleep, not deep sleep) specifically so the
device stays reachable.

**Future work**: a deep-sleep variant is possible if the device wakes on a
short fixed interval (e.g. every 30-60s) to sample, publish, and briefly
listen for a retained `cmd`, then sleeps again -- accepting that latency
instead of always-on reachability. The relay pin (GPIO26) was chosen
specifically because it's RTC-capable: on such a variant, the pin level set
before deep sleep must be preserved with `gpio_hold_en(GPIO_NUM_26)` +
`gpio_deep_sleep_hold_en()`, or the relay drops out (reverts to its
pull/float state) the instant deep sleep engages, momentarily cutting power
to whatever's mid-cycle. This build does not implement that; it's flagged
here so nobody adds deep sleep later without also adding the hold.

## Reliability

- `loop()` is fully non-blocking; the only `delay()` in the firmware is a
  50ms serial-settle call inside `setup()`.
- The relay pin's level is set *before* its direction
  (`digitalWrite(...); pinMode(..., OUTPUT);`) -- the reverse order produces
  a ~10ms fan pulse on every reset, because `pinMode(OUTPUT)` briefly drives
  the pin from whatever its default floating/LOW state was.
- Wi-Fi and MQTT reconnect independently with non-blocking exponential
  backoff (1, 2, 4, 8s ... capped at 30s), timed off `millis()`.
- **Fail-safe on link loss**: `setFan()` is called directly from the local
  control-law evaluation in `serviceSampling()`, unconditionally on whether
  MQTT is connected. Losing the network does not stop ventilation; it only
  stops telemetry/state publishes (which are individually guarded by
  `mqttClient.connected()` and silently skipped, not queued or retried,
  when down).
- Readings that are NaN, `< 0`, `> 100`, or that jump `> 20 %RH` from the
  previous valid sample are rejected. Three consecutive rejects publish
  `reason: "sensor_fault"` and hold the last commanded fan state -- the
  firmware does not guess.
- Every tunable lives in `firmware/src/config.h` / the constants block at
  the top of `service/rules.py`, named. No magic numbers in the control-law
  logic in either file.
- `firmware/src/secrets.h` is gitignored; `secrets.h.example` ships instead.

## Known limitations

- **PubSubClient publishes are QoS 0 on the wire.** The MQTT contract above
  specifies QoS 1 for every topic, and `subscribe(TOPIC_CMD, 1)` does
  request/honour QoS 1 for *inbound* delivery bookkeeping -- but
  `knolleary/PubSubClient` (the fixed, required firmware library) does not
  implement a publisher-side PUBACK handshake or retry; every
  `mqttClient.publish(...)` call it makes is QoS 0 regardless of the topic
  contract's intent. This is a real constraint of the specified library,
  documented rather than silently papered over. If true QoS 1 publishing
  from the device matters for your deployment, it requires swapping
  PubSubClient for a library that implements it (e.g. `esp-mqtt` on
  ESP-IDF, or `arduino-mqtt`).
- **`broker/mosquitto.conf` allows anonymous connections** on a plain
  (non-TLS) listener. That's deliberate for a zero-setup prototype on a
  private LAN in a private repo; add `password_file` and TLS before
  exposing this broker beyond your own network.
- **`automation.py`'s mirrored state times itself off the device's own
  `uptime_s`** (from telemetry), not wall-clock message-arrival time -- see
  the `Automation` class docstring in `service/automation.py` for why (it's
  what makes `scripts/simulate.py`'s 20x-speed replay gate `MIN_RUN_S` /
  `MAX_RUN_S` / `COOLDOWN_S` correctly in compressed real time, and it's
  also more correct in general: the rule shouldn't be sensitive to network
  delivery jitter). A device reboot resets `uptime_s` to 0; the service
  detects the backward jump and resets its own mirrored state rather than
  computing garbage elapsed times. This only affects the service's
  *observability* copy of the state machine -- the firmware itself never
  reads this field for its own (authoritative) control law.
- The firmware's history buffer times everything in whole seconds via
  `millis()/1000`, which wraps at ~49.7 days of continuous uptime. Not
  handled (would need an explicit epoch/wrap check); acceptable for a
  prototype, worth fixing before a permanent install.

## Running the tests

```bash
cd service && pip install -r requirements.txt
pip install pytest
pytest ../tests/test_rules.py -v
```

All 7 required scenarios (plus one extra covering `is_valid_reading`
directly) pass. See the top of `tests/test_rules.py` for how each one is
constructed and why (in particular #1, #4 and #5, where the exact shape of
the input series matters).

## Bring-up order

1. **Flash the firmware.**
   ```bash
   cd firmware
   cp src/secrets.h.example src/secrets.h   # then edit it: Wi-Fi + broker details
   pio run -t upload
   ```
2. **Verify serial telemetry** before touching the network at all:
   ```bash
   pio device monitor
   ```
   You should see `bathroom-vent: boot complete, entering non-blocking control loop`,
   then periodic `fan ON`/`fan OFF` lines once humidity crosses a threshold
   (or immediately, if your DHT22 happens to read >=70% RH in open air near
   your breath -- that's a real trigger, not a bug).
3. **Start the broker** (and automation service):
   ```bash
   docker compose up -d
   ```
4. **Confirm MQTT end-to-end**, in a separate terminal:
   ```bash
   mosquitto_sub -h <broker-host> -t 'home/bathroom/vent/#' -v
   ```
   Expect a retained `status: online`, then live `telemetry` every
   30s/5s, and `state` on every phase transition.
5. **Connect the relay last.** Bring up DHT22 + Wi-Fi + MQTT first and
   confirm the serial log's `fan ON`/`fan OFF` lines look right; only then
   wire the relay module in, per `docs/WIRING.md`. This way a wiring mistake
   on the relay side can't be confused with a firmware/logic bug.

No hardware on hand? Skip straight to:
```bash
docker compose up -d
python scripts/simulate.py
```
and watch `mosquitto_sub -t 'home/bathroom/vent/#' -v` (or the
`automation` container's logs) for the ON -> OFF cmd cycle -- see
`scripts/simulate.py`'s docstring for exactly what to expect and when.

## Three most likely first-run failures

1. **`pio run` fails to find the board / libraries on first build.**
   PlatformIO downloads the `espressif32` platform and the four libraries
   in `platformio.ini` on first use, which needs internet access and can
   take a few minutes the very first time. If it fails partway, re-run
   `pio run` -- it resumes rather than starting over. If a library version
   pin has since been yanked from the registry, loosen the `@^x.y.z`
   constraint in `platformio.ini` and retry.
2. **DHT22 reads come back `nan` (which the firmware correctly logs as
   `reading_rejected`, then `sensor_fault` after three in a row).**
   Almost always one of: the 10kΩ pull-up is missing or wired to 5V instead
   of 3V3, `DATA` and `VCC`/`GND` are swapped, or the sensor is being
   sampled faster than its ~0.5 Hz limit (shouldn't happen with this
   firmware's 2s floor, but double-check if you've edited `config.h`).
   Reseat the wiring and watch the serial monitor.
3. **The device never shows `status: online`, or the broker connection
   spins in backoff forever.** Check `secrets.h` first (typo'd SSID/password
   is the most common cause), then confirm the broker's `MQTT_HOST`/`PORT`
   in `secrets.h` are reachable from the ESP32's Wi-Fi network specifically
   -- e.g. a broker bound to `docker compose`'s internal network only
   (rather than published to `1883` on the host, as `docker-compose.yml`
   here already does) won't be reachable from a physical device on your
   LAN. `mosquitto_sub -h <broker-host> -t '$SYS/#' -C 1` from another
   machine on the same LAN is a quick way to confirm the broker itself is
   reachable before blaming the ESP32.

## Assumptions made where the spec left a value unspecified

- **PlatformIO board id**: `esp32doit-devkit-v1` -- the standard PlatformIO
  board definition for a 30-pin ESP32 DevKit V1 / WROOM-32 module.
- **Section 9, test 5** ("a slow ambient drift to 71 %RH does NOT
  trigger...") is internally inconsistent with the state machine as
  specified: `H >= 70.0` (rule 1, absolute threshold) fires unconditionally
  regardless of baseline, so *any* drift reaching 71% must trigger by that
  rule alone -- the "B drifts with it" reasoning given only explains why
  the *relative-spike* rule (rule 2) wouldn't fire, not why the absolute
  one wouldn't. Implemented the test's actual intent (verifying rule 2's
  baseline-tracking behavior in isolation) with a drift ceiling of 69 %RH,
  just under the absolute threshold, so the test isolates what it's
  actually meant to isolate. See
  `tests/test_rules.py::test_slow_ambient_drift_does_not_trigger`.
- **`cmd` TTL default**: `DEFAULT_CMD_TTL_S = 1200` (20 min), matching the
  worked example in the spec's own `cmd` payload. Used whenever a `cmd`
  omits `ttl_s`.
- **MQTT credentials**: `secrets.h.example` ships with empty
  user/password, matching `mosquitto.conf`'s `allow_anonymous true`. Fill
  both in together if you add authentication.
- **`automation.py`'s advisory `cmd`s** are published with `mode: "auto"`
  (never override into manual) -- the service only ever nudges the
  autonomous rule's own conclusion, on the theory that a service crash
  mid-manual-override should never be able to strand the fan in a state the
  firmware's own local rule wouldn't have chosen.
