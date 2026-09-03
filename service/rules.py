"""Pure control law for the bathroom ventilation system.

No I/O, no network, no hardware access, no wall-clock reads (the caller always
supplies `now`). Every function here is deterministic given its arguments, so
the whole module is unit-testable without a broker, a sensor, or an ESP32.

This is one of two intentionally redundant implementations of the same state
machine. The other lives in `firmware/src/main.cpp` (C++). The firmware copy
is authoritative for actually driving the relay and MUST keep working with no
network connection at all -- ventilation must never depend on this service or
on MQTT being reachable. This Python copy exists so the automation service can
apply the identical rule for observability (structured logs, metrics, a place
to extend the policy) and to advise the firmware via `cmd` messages. Keep the
two in sync: the constants below mirror `firmware/src/config.h` exactly, and
`tests/test_rules.py` is the executable spec for both.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, replace
from typing import Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Tunables -- MUST match firmware/src/config.h. No magic numbers below this
# block; every threshold used by evaluate() is named here.
# ---------------------------------------------------------------------------

HUM_ON = 70.0            # %RH absolute threshold -> VENTING
HUM_OFF = 60.0            # %RH absolute threshold -> eligible for COOLDOWN
DELTA_ON = 8.0            # H - B spike (%RH) -> VENTING
DELTA_OFF = 3.0           # H - B (%RH) -> eligible for COOLDOWN
RISE_ON = 2.0             # %RH per minute, sustained 2 samples -> VENTING

MIN_RUN_S = 180.0         # minimum VENTING dwell before COOLDOWN is allowed
MAX_RUN_S = 1800.0        # unconditional VENTING -> COOLDOWN guard
COOLDOWN_S = 120.0        # minimum COOLDOWN dwell before IDLE

BASELINE_WINDOW_S = 30 * 60.0   # rolling median window for baseline B
MAX_VALID_JUMP = 20.0            # %RH; bigger single-sample jump is rejected
REJECT_FAULT_COUNT = 3           # consecutive rejects -> sensor_fault

DEFAULT_CMD_TTL_S = 1200.0

PHASE_IDLE = "IDLE"
PHASE_VENTING = "VENTING"
PHASE_COOLDOWN = "COOLDOWN"

FAN_ON = "on"
FAN_OFF = "off"

MODE_AUTO = "auto"
MODE_MANUAL = "manual"

ACTION_TURN_ON = "turn_on"
ACTION_TURN_OFF = "turn_off"
ACTION_HOLD = "hold"

REASON_ABSOLUTE = "absolute_threshold"
REASON_RELATIVE = "relative_spike"
REASON_RISE_RATE = "rise_rate"
REASON_HUMIDITY_CLEARED = "humidity_cleared"
REASON_MAX_RUNTIME = "max_runtime_guard"
REASON_COOLDOWN_DONE = "cooldown_complete"
REASON_SENSOR_FAULT = "sensor_fault"
REASON_READING_REJECTED = "reading_rejected"
REASON_CMD_OVERRIDE = "cmd_override"
REASON_MANUAL = "manual"
REASON_INIT = "init"
REASON_NONE = "none"


@dataclass(frozen=True)
class Reading:
    """A single sensor sample. `humidity` may be NaN to represent a failed read."""

    humidity: float
    temperature: float
    ts: float


@dataclass(frozen=True)
class VentState:
    """Immutable control-law state. Construct once, thread through evaluate()."""

    phase: str = PHASE_IDLE
    fan: str = FAN_OFF
    mode: str = MODE_AUTO
    phase_entered_at: float = 0.0
    fan_on_since: Optional[float] = None
    total_runtime_s: float = 0.0
    reject_count: int = 0
    last_valid_humidity: Optional[float] = None
    reason: str = REASON_INIT
    baseline_at_trigger: Optional[float] = None
    cmd_fan: Optional[str] = None
    cmd_issued_at: Optional[float] = None
    cmd_ttl_s: Optional[float] = None


def is_valid_reading(humidity: Optional[float], last_valid_humidity: Optional[float]) -> bool:
    """Reject NaN, out-of-range, and implausible single-sample jumps."""
    if humidity is None or (isinstance(humidity, float) and math.isnan(humidity)):
        return False
    if humidity < 0.0 or humidity > 100.0:
        return False
    if last_valid_humidity is not None and abs(humidity - last_valid_humidity) > MAX_VALID_JUMP:
        return False
    return True


def _window(history: Sequence[Reading], now: float, window_s: float) -> list:
    return [r for r in history if 0.0 <= now - r.ts <= window_s]


def compute_baseline(history: Sequence[Reading], now: float) -> Optional[float]:
    """Rolling BASELINE_WINDOW_S median humidity. None if history is empty."""
    window = _window(history, now, BASELINE_WINDOW_S)
    if not window:
        return None
    return statistics.median(r.humidity for r in window)


def compute_rise_rate(a: Reading, b: Reading) -> Optional[float]:
    """%RH per minute from sample `a` to sample `b` (b must be later than a)."""
    dt_min = (b.ts - a.ts) / 60.0
    if dt_min <= 0.0:
        return None
    return (b.humidity - a.humidity) / dt_min


def rise_sustained(history_with_current: Sequence[Reading]) -> bool:
    """True when RISE_ON is met/exceeded on two consecutive sample-to-sample rates."""
    if len(history_with_current) < 3:
        return False
    a, b, c = history_with_current[-3], history_with_current[-2], history_with_current[-1]
    r1 = compute_rise_rate(a, b)
    r2 = compute_rise_rate(b, c)
    return r1 is not None and r2 is not None and r1 >= RISE_ON and r2 >= RISE_ON


def _check_venting_trigger(h: float, baseline: float, history_with_current: Sequence[Reading]) -> Tuple[bool, str]:
    """Priority order is a deliberate, documented contract: absolute threshold is
    checked first, so any sample with H >= HUM_ON always reports
    'absolute_threshold' even if the relative-spike or rise-rate conditions are
    also true for that same sample."""
    if h >= HUM_ON:
        return True, REASON_ABSOLUTE
    if (h - baseline) >= DELTA_ON:
        return True, REASON_RELATIVE
    if rise_sustained(history_with_current):
        return True, REASON_RISE_RATE
    return False, REASON_NONE


def apply_cmd(state: VentState, mode: str, fan: Optional[str], ttl_s: Optional[float], now: float) -> VentState:
    """Ingest an inbound `.../cmd` message. Pure: does not itself set the relay.

    `mode` switches the device between autonomous control and a sticky manual
    override. `fan`/`ttl_s` are only meaningful as a temporary advisory
    override while `mode == "auto"`; see `_resolve_override`.
    """
    return replace(
        state,
        mode=mode,
        cmd_fan=fan,
        cmd_issued_at=now,
        cmd_ttl_s=ttl_s,
    )


def _resolve_override(state: VentState, now: float) -> Tuple[Optional[str], Optional[str]]:
    """Returns (forced_fan, reason) if an inbound cmd should bypass the state
    machine this tick, or (None, None) if the state machine should run normally.
    """
    if state.cmd_fan is None:
        return None, None

    if state.mode == MODE_MANUAL:
        return state.cmd_fan, REASON_MANUAL

    if state.mode == MODE_AUTO:
        if state.cmd_issued_at is None or state.cmd_ttl_s is None:
            return state.cmd_fan, REASON_CMD_OVERRIDE
        if now - state.cmd_issued_at > state.cmd_ttl_s:
            # TTL elapsed: the override is ignored and control reverts to the
            # local rule. This is what keeps a lost/late cmd from pinning the
            # fan on or off forever.
            return None, None
        return state.cmd_fan, REASON_CMD_OVERRIDE

    return None, None


def evaluate(
    state: VentState,
    reading: Reading,
    history: Sequence[Reading],
    now: float,
) -> Tuple[VentState, str, str]:
    """One control-law tick.

    `history` is the caller-owned buffer of prior *valid* readings, oldest
    first, NOT including `reading`. Callers should trim it to roughly
    BASELINE_WINDOW_S before calling (evaluate() re-filters internally too, so
    a slightly larger buffer is harmless).

    Returns (new_state, action, reason) where action is one of
    ACTION_TURN_ON / ACTION_TURN_OFF / ACTION_HOLD.
    """
    if not is_valid_reading(reading.humidity, state.last_valid_humidity):
        reject_count = state.reject_count + 1
        if reject_count >= REJECT_FAULT_COUNT:
            reason = REASON_SENSOR_FAULT
        else:
            reason = REASON_READING_REJECTED
        new_state = replace(state, reject_count=reject_count, reason=reason)
        # Fail-safe: hold the last commanded fan state, never guess.
        return new_state, ACTION_HOLD, reason

    history_with_current = list(history) + [reading]
    live_baseline = compute_baseline(history_with_current, now)
    if live_baseline is None:
        live_baseline = reading.humidity
    h = reading.humidity

    phase = state.phase
    fan = state.fan
    fan_on_since = state.fan_on_since
    phase_entered_at = state.phase_entered_at
    total_runtime_s = state.total_runtime_s
    reason = state.reason
    baseline_at_trigger = state.baseline_at_trigger
    action = ACTION_HOLD

    forced_fan, forced_reason = _resolve_override(state, now)

    if forced_fan is not None:
        if forced_fan != fan:
            action = ACTION_TURN_ON if forced_fan == FAN_ON else ACTION_TURN_OFF
            if forced_fan == FAN_OFF and fan_on_since is not None:
                total_runtime_s += now - fan_on_since
            fan_on_since = now if forced_fan == FAN_ON else None
            phase = PHASE_VENTING if forced_fan == FAN_ON else PHASE_IDLE
            phase_entered_at = now
            fan = forced_fan
        reason = forced_reason
    else:
        if phase == PHASE_IDLE:
            triggered, trig_reason = _check_venting_trigger(h, live_baseline, history_with_current)
            if triggered:
                phase = PHASE_VENTING
                phase_entered_at = now
                fan_on_since = now
                fan = FAN_ON
                action = ACTION_TURN_ON
                reason = trig_reason
                # Freeze B at the ambient value it held the instant we started
                # venting. If we kept recomputing B from a window that now
                # also contains the shower itself, B would drift up toward H
                # and both the DELTA_OFF exit test and (eventually) the
                # MAX_RUN_S guard would be silently defeated by a sensor that
                # is simply pinned high. B resumes rolling once back in IDLE.
                baseline_at_trigger = live_baseline

        elif phase == PHASE_VENTING:
            run_elapsed = now - fan_on_since if fan_on_since is not None else 0.0
            if run_elapsed >= MAX_RUN_S:
                phase = PHASE_COOLDOWN
                phase_entered_at = now
                fan = FAN_OFF
                action = ACTION_TURN_OFF
                if fan_on_since is not None:
                    total_runtime_s += now - fan_on_since
                fan_on_since = None
                reason = REASON_MAX_RUNTIME
                baseline_at_trigger = None
            else:
                b = baseline_at_trigger if baseline_at_trigger is not None else live_baseline
                fall_ok = (h <= HUM_OFF) or ((h - b) <= DELTA_OFF)
                if fall_ok and run_elapsed >= MIN_RUN_S:
                    phase = PHASE_COOLDOWN
                    phase_entered_at = now
                    fan = FAN_OFF
                    action = ACTION_TURN_OFF
                    if fan_on_since is not None:
                        total_runtime_s += now - fan_on_since
                    fan_on_since = None
                    reason = REASON_HUMIDITY_CLEARED
                    baseline_at_trigger = None

        elif phase == PHASE_COOLDOWN:
            if now - phase_entered_at >= COOLDOWN_S:
                phase = PHASE_IDLE
                phase_entered_at = now
                reason = REASON_COOLDOWN_DONE

    new_state = replace(
        state,
        phase=phase,
        fan=fan,
        phase_entered_at=phase_entered_at,
        fan_on_since=fan_on_since,
        total_runtime_s=total_runtime_s,
        reject_count=0,
        last_valid_humidity=reading.humidity,
        reason=reason,
        baseline_at_trigger=baseline_at_trigger,
    )
    return new_state, action, reason
