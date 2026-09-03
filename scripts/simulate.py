#!/usr/bin/env python3
"""Replays a synthetic 40-minute shower humidity curve into the broker at
20x speed (~120 s of wall-clock time), so the whole pipeline -- broker,
automation service, control law -- can be demonstrated with no ESP32
attached.

Each published telemetry message carries `uptime_s` = the *simulated*
device clock (0..2400), not wall-clock time. automation.py times its state
machine off that field precisely so that MIN_RUN_S / MAX_RUN_S / COOLDOWN_S
gate correctly against 2400 s of simulated device time even though the
whole run only takes ~120 s of real time. See service/automation.py
"Time base".

Usage:
    python scripts/simulate.py [--host localhost] [--port 1883] [--speed 20]

What to watch, in a second terminal, while this runs:

    mosquitto_sub -t 'home/bathroom/vent/#' -v

You'll see the retained `status: online` this script publishes on connect,
a stream of `telemetry` messages tracing the humidity curve, and -- once the
automation service (docker compose up) is also running and reacting to that
stream -- `cmd` messages appearing (`{"fan": "on", ...}`) as the curve
crosses the VENTING trigger and disappearing/flipping back
(`{"fan": "off", ...}`) once it clears. That cmd on/off transition is the
no-hardware equivalent of the relay clicking / status LED changing on real
firmware.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time

import paho.mqtt.client as mqtt

TOPIC_BASE = "home/bathroom/vent"
TOPIC_STATUS = f"{TOPIC_BASE}/status"
TOPIC_TELEMETRY = f"{TOPIC_BASE}/telemetry"

SIM_DURATION_S = 40 * 60      # 2400 s of simulated device time
SAMPLE_PERIOD_S = 15          # simulated seconds between telemetry samples
AMBIENT_HUMIDITY = 45.0
AMBIENT_TEMPERATURE = 23.0


def humidity_at(t: float) -> float:
    """A plausible 40-minute bathroom humidity curve:

      0-5 min   ambient, gentle noise
      5-9 min   shower starts: fast rise to a peak around 90 %RH
      9-22 min  shower running: plateau/slow creep near the peak
      22-40 min shower off, door/window/fan clears the room back to ambient
    """
    if t < 300:  # 0-5 min: ambient
        # Amplitude/period are chosen so the noise's own rate of change
        # (d/dt[A*sin(t/T)] peaks at A/T) stays well under RISE_ON
        # (2 %RH/min = 0.033 %RH/s) -- otherwise the "ambient" phase can
        # spuriously sustain a rise-rate trigger across two consecutive
        # samples. 0.8/60 = 0.013 %RH/s, a comfortable margin under that.
        return AMBIENT_HUMIDITY + 0.8 * math.sin(t / 60.0)
    if t < 540:  # 5-9 min: fast rise
        frac = (t - 300) / 240.0
        return AMBIENT_HUMIDITY + frac * (90.0 - AMBIENT_HUMIDITY)
    if t < 1320:  # 9-22 min: plateau with light noise
        return 90.0 + 2.0 * math.sin(t / 90.0)
    # 22-40 min: exponential-ish decay back to ambient
    frac = (t - 1320) / (SIM_DURATION_S - 1320)
    peak = 90.0 + 2.0 * math.sin(1320 / 90.0)
    return AMBIENT_HUMIDITY + (peak - AMBIENT_HUMIDITY) * math.exp(-3.0 * frac)


def temperature_at(t: float) -> float:
    # Bathroom warms a couple degrees during the shower, then cools back off.
    h = humidity_at(t)
    return AMBIENT_TEMPERATURE + (h - AMBIENT_HUMIDITY) * 0.04


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--speed", type=float, default=20.0, help="playback speed multiplier")
    args = parser.parse_args()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="bathroom-vent-simulator")
    client.will_set(TOPIC_STATUS, payload="offline", qos=1, retain=True)
    client.connect(args.host, args.port, keepalive=30)
    client.loop_start()
    client.publish(TOPIC_STATUS, "online", qos=1, retain=True)

    print(f"simulate.py: replaying a {SIM_DURATION_S // 60}-minute shower at {args.speed:.0f}x "
          f"({SIM_DURATION_S / args.speed:.0f}s wall-clock) -> {args.host}:{args.port}")
    print(f"simulate.py: watch it with: mosquitto_sub -h {args.host} -p {args.port} -t '{TOPIC_BASE}/#' -v")

    seq = 0
    t = 0.0
    wall_start = time.monotonic()
    last_phase_label = None

    while t <= SIM_DURATION_S:
        humidity = round(humidity_at(t), 1)
        temperature = round(temperature_at(t), 1)
        payload = {
            "humidity": humidity,
            "temperature": temperature,
            "rssi": -55,
            "uptime_s": int(t),
            "seq": seq,
        }
        client.publish(TOPIC_TELEMETRY, json.dumps(payload), qos=1, retain=False)

        phase_label = "RISING/PEAK" if 300 <= t < 1320 else "ambient/clearing"
        if phase_label != last_phase_label:
            print(f"  t={int(t):5d}s sim  H={humidity:5.1f}%RH  [{phase_label}]")
            last_phase_label = phase_label

        seq += 1
        t += SAMPLE_PERIOD_S

        target_wall_elapsed = t / args.speed
        actual_wall_elapsed = time.monotonic() - wall_start
        sleep_s = target_wall_elapsed - actual_wall_elapsed
        if sleep_s > 0:
            time.sleep(sleep_s)

    print("simulate.py: replay complete. If the automation service is running, "
          "check its logs / `mosquitto_sub` for the ON -> OFF cmd transition "
          "(OFF follows once MIN_RUN_S + COOLDOWN_S of simulated time has elapsed).")

    client.publish(TOPIC_STATUS, "offline", qos=1, retain=True)
    time.sleep(0.5)  # let the last publishes flush before disconnecting
    client.loop_stop()
    client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
