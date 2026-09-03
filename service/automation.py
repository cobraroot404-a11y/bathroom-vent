"""Automation service.

Subscribes to `home/bathroom/vent/telemetry`, runs the same control law as
the firmware (service/rules.py) for observability, and publishes advisory
`.../cmd` messages that the ESP32 may act on.

This service is NOT in the safety-critical path: the firmware is fail-safe
and keeps ventilating on its own local thresholds even if this process, the
broker, or the network is down. This service exists to add structured
logging and a place to extend the policy (schedules, external humidity
forecasts, etc.) without touching the firmware. See README.md and
firmware/src/main.cpp.

Stdlib + paho-mqtt only, per the project's fixed stack.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import List, Optional

import paho.mqtt.client as mqtt

import rules

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER") or None
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD") or None
MQTT_KEEPALIVE_S = int(os.environ.get("MQTT_KEEPALIVE_S", "60"))

TOPIC_BASE = "home/bathroom/vent"
TOPIC_STATUS = f"{TOPIC_BASE}/status"
TOPIC_TELEMETRY = f"{TOPIC_BASE}/telemetry"
TOPIC_STATE = f"{TOPIC_BASE}/state"
TOPIC_CMD = f"{TOPIC_BASE}/cmd"

CMD_TTL_S = int(float(os.environ.get("CMD_TTL_S", rules.DEFAULT_CMD_TTL_S)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("automation")


class Automation:
    """Owns the mirrored control-law state for one bathroom node.

    The control law is timed off the *device's* clock (telemetry's
    `uptime_s`), not wall-clock message-arrival time. This makes the rule's
    notion of "now" immune to MQTT/network delivery jitter, and is what lets
    scripts/simulate.py replay a 40-minute curve at 20x speed and still see
    MIN_RUN_S/MAX_RUN_S/COOLDOWN_S gate correctly in compressed real time --
    those constants are seconds of simulated device time, not seconds of
    wall-clock waiting. See README.md "Time base" for the reboot caveat this
    implies (handled below via a monotonicity check).
    """

    def __init__(self) -> None:
        self.state = rules.VentState(mode=rules.MODE_AUTO)
        self.history: List[rules.Reading] = []
        self.last_device_time: Optional[float] = None

    def handle_telemetry(self, payload: dict, now: float) -> str:
        if self.last_device_time is not None and now < self.last_device_time - 5.0:
            # Device clock jumped backward -- almost certainly a reboot
            # (uptime_s resets to 0). Start this mirror over cleanly rather
            # than computing nonsensical negative elapsed times. The
            # firmware itself is unaffected by this; it never depended on
            # this service's state.
            log.warning("device uptime went backward (reboot?); resetting mirrored state")
            self.state = rules.VentState(mode=rules.MODE_AUTO)
            self.history = []
        self.last_device_time = now

        raw_humidity = payload.get("humidity")
        try:
            humidity = float(raw_humidity)
        except (TypeError, ValueError):
            humidity = float("nan")

        raw_temperature = payload.get("temperature")
        try:
            temperature = float(raw_temperature)
        except (TypeError, ValueError):
            temperature = float("nan")

        reading = rules.Reading(humidity=humidity, temperature=temperature, ts=now)
        was_valid = rules.is_valid_reading(humidity, self.state.last_valid_humidity)

        self.state, action, reason = rules.evaluate(self.state, reading, self.history, now)

        if was_valid:
            self.history.append(reading)
            self.history = [r for r in self.history if now - r.ts <= rules.BASELINE_WINDOW_S]

        log.info(
            "telemetry humidity=%s phase=%s fan=%s action=%s reason=%s",
            f"{humidity:.1f}" if was_valid else "invalid",
            self.state.phase,
            self.state.fan,
            action,
            reason,
        )
        return action

    @staticmethod
    def build_cmd(action: str) -> Optional[dict]:
        if action == rules.ACTION_TURN_ON:
            return {"fan": rules.FAN_ON, "mode": rules.MODE_AUTO, "ttl_s": CMD_TTL_S}
        if action == rules.ACTION_TURN_OFF:
            return {"fan": rules.FAN_OFF, "mode": rules.MODE_AUTO, "ttl_s": CMD_TTL_S}
        return None


automation = Automation()


def on_connect(client: mqtt.Client, userdata, flags, reason_code, properties=None) -> None:
    if reason_code == 0:
        log.info("connected to broker %s:%s", MQTT_HOST, MQTT_PORT)
        client.subscribe(TOPIC_TELEMETRY, qos=1)
        client.subscribe(TOPIC_STATUS, qos=1)
        client.subscribe(TOPIC_STATE, qos=1)
    else:
        log.error("connection to broker failed: %s", reason_code)


def on_disconnect(client: mqtt.Client, userdata, flags, reason_code, properties=None) -> None:
    log.warning("disconnected from broker (reason=%s); paho will retry with backoff", reason_code)


def _device_now(payload: dict) -> float:
    """The control law's time base: the device's own uptime_s if present
    (see Automation docstring), falling back to wall-clock arrival time for
    a malformed/missing field."""
    try:
        return float(payload["uptime_s"])
    except (KeyError, TypeError, ValueError):
        return time.time()


def on_message(client: mqtt.Client, userdata, msg: mqtt.MQTTMessage) -> None:
    if msg.topic == TOPIC_TELEMETRY:
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            log.warning("dropping malformed telemetry payload: %s", exc)
            return
        if not isinstance(payload, dict):
            log.warning(
                "dropping telemetry payload that is not a JSON object: %s",
                type(payload).__name__,
            )
            return
        now = _device_now(payload)
        action = automation.handle_telemetry(payload, now)
        cmd = automation.build_cmd(action)
        if cmd is not None:
            client.publish(TOPIC_CMD, json.dumps(cmd), qos=1, retain=False)
            log.info("published cmd -> %s", cmd)

    elif msg.topic == TOPIC_STATUS:
        log.info("device status: %s", msg.payload.decode("utf-8", "replace"))

    elif msg.topic == TOPIC_STATE:
        log.info("device state: %s", msg.payload.decode("utf-8", "replace"))


def main() -> None:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="bathroom-vent-automation")
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    log.info("connecting to %s:%s ...", MQTT_HOST, MQTT_PORT)
    # connect_async() lets the network loop own both the initial connection
    # and later reconnects.  With retry_first_connection enabled, starting
    # this process before the broker is ready no longer terminates it with an
    # uncaught ConnectionRefusedError.
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=MQTT_KEEPALIVE_S)
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
