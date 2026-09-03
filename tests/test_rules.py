"""Executable spec for the ventilation control law in service/rules.py.

Pure unit tests: no hardware, no MQTT broker, no wall clock. `now` is always
supplied explicitly by the test data.

Run with: pytest tests/test_rules.py -v
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

import rules  # noqa: E402


def run_series(points, mode=rules.MODE_AUTO, state=None):
    """Feed (t, humidity, temperature) points through evaluate() in order.

    Mirrors how a real caller (firmware or automation.py) would maintain its
    own history buffer: only readings that pass is_valid_reading() are kept,
    and the buffer is trimmed to the baseline window.

    Returns the list of (t, state, action, reason) tuples, one per point.
    """
    state = state or rules.VentState(mode=mode)
    history = []
    results = []
    for t, humidity, temperature in points:
        reading = rules.Reading(humidity=humidity, temperature=temperature, ts=t)
        was_valid = rules.is_valid_reading(humidity, state.last_valid_humidity)
        state, action, reason = rules.evaluate(state, reading, history, t)
        if was_valid:
            history.append(reading)
            history = [r for r in history if t - r.ts <= rules.BASELINE_WINDOW_S]
        results.append((t, state, action, reason))
    return results


def actions(results):
    return [a for (_, _, a, _) in results]


# ---------------------------------------------------------------------------
# 1. Rising humidity 45 -> 72 %RH turns the fan ON and reports reason correctly.
# ---------------------------------------------------------------------------

def test_rising_humidity_turns_fan_on_with_valid_reason():
    # A gentle, realistic ramp (2 %RH / 5 min) so the trigger is driven by the
    # absolute threshold crossing near the top of the climb rather than the
    # faster rise-rate/relative-spike rules -- this test is about the basic
    # ON transition, not about which specific rule fires first (that's what
    # the other, more targeted tests below isolate).
    humidities = list(range(45, 74, 2))
    points = [(t, float(h), 24.0) for t, h in zip(range(0, 300 * len(humidities), 300), humidities)]
    results = run_series(points)

    on_events = [(t, reason) for (t, _, action, reason) in results if action == rules.ACTION_TURN_ON]
    assert len(on_events) == 1, "shower ramp should produce exactly one ON transition"

    _, reason = on_events[0]
    assert reason in {rules.REASON_ABSOLUTE, rules.REASON_RELATIVE, rules.REASON_RISE_RATE}

    final_state = results[-1][1]
    assert final_state.fan == rules.FAN_ON
    assert final_state.phase == rules.PHASE_VENTING


# ---------------------------------------------------------------------------
# 2. Falling humidity 72 -> 58 %RH does NOT turn the fan off before MIN_RUN_S.
# ---------------------------------------------------------------------------

def test_falling_humidity_respects_min_run_time():
    points = [
        (0, 72.0, 24.0),    # triggers VENTING (absolute threshold)
        (30, 58.0, 23.5),   # well below HUM_OFF, but only 30s into the run
    ]
    results = run_series(points)

    early_state = results[-1][1]
    assert early_state.fan == rules.FAN_ON, "must not switch off before MIN_RUN_S has elapsed"
    assert early_state.phase == rules.PHASE_VENTING
    assert results[-1][2] == rules.ACTION_HOLD

    # Once MIN_RUN_S has actually elapsed with humidity still low, it must turn off.
    late_results = run_series(points + [(200, 58.0, 23.5)])
    late_state = late_results[-1][1]
    assert late_state.fan == rules.FAN_OFF
    assert late_state.phase == rules.PHASE_COOLDOWN
    assert late_results[-1][3] == rules.REASON_HUMIDITY_CLEARED


# ---------------------------------------------------------------------------
# 3. Humidity oscillating 69 <-> 71 %RH produces exactly one ON transition.
# ---------------------------------------------------------------------------

def test_oscillation_across_threshold_does_not_chatter():
    points = [
        (0, 71.0, 24.0),
        (5, 69.0, 24.0),
        (10, 71.0, 24.0),
        (15, 69.0, 24.0),
    ]
    results = run_series(points)

    on_count = sum(1 for a in actions(results) if a == rules.ACTION_TURN_ON)
    off_count = sum(1 for a in actions(results) if a == rules.ACTION_TURN_OFF)
    assert on_count == 1, "hysteresis band must prevent relay chatter across 70/60"
    assert off_count == 0
    assert results[-1][1].fan == rules.FAN_ON


# ---------------------------------------------------------------------------
# 4. Humidity pinned at 95 %RH forces OFF at MAX_RUN_S with max_runtime_guard.
# ---------------------------------------------------------------------------

def test_stuck_high_sensor_forces_off_at_max_runtime():
    ramp = [
        (0, 45.0, 24.0),
        (60, 60.0, 24.0),
        (120, 75.0, 24.0),   # crosses HUM_ON -> VENTING starts here, fan_on_since=120
        (180, 90.0, 24.0),
        (240, 95.0, 24.0),
    ]
    trigger_t = 120
    pinned = ramp + [
        (600, 95.0, 24.0),
        (1200, 95.0, 24.0),
        (trigger_t + rules.MAX_RUN_S, 95.0, 24.0),  # run_elapsed == MAX_RUN_S exactly
    ]
    results = run_series(pinned)

    final_t, final_state, final_action, final_reason = results[-1]
    assert final_action == rules.ACTION_TURN_OFF
    assert final_reason == rules.REASON_MAX_RUNTIME
    assert final_state.fan == rules.FAN_OFF
    assert final_state.phase == rules.PHASE_COOLDOWN
    assert final_state.total_runtime_s == rules.MAX_RUN_S

    # And it must not have been switched off early by the baseline "catching up"
    # to the pinned value -- that would defeat the whole point of this guard.
    off_events = [t for (t, _, a, _) in results if a == rules.ACTION_TURN_OFF]
    assert off_events == [trigger_t + rules.MAX_RUN_S]


# ---------------------------------------------------------------------------
# 5. A slow ambient drift does NOT trigger, because B drifts with it.
# ---------------------------------------------------------------------------

def test_slow_ambient_drift_does_not_trigger():
    # 45 -> 69 %RH in 2 %RH / 5 min steps: rise rate (~0.4 %RH/min) is far
    # under RISE_ON, the ceiling stays below HUM_ON, and the rolling 30-minute
    # median baseline B tracks the ramp closely enough that H - B never
    # reaches DELTA_ON.
    humidities = list(range(45, 70, 2))
    points = [(t, float(h), 22.0) for t, h in zip(range(0, 300 * len(humidities), 300), humidities)]
    results = run_series(points)

    assert all(a == rules.ACTION_HOLD for a in actions(results))
    final_state = results[-1][1]
    assert final_state.phase == rules.PHASE_IDLE
    assert final_state.fan == rules.FAN_OFF
    assert final_state.last_valid_humidity < rules.HUM_ON


# ---------------------------------------------------------------------------
# 6. A NaN burst of three readings yields sensor_fault and holds fan state.
# ---------------------------------------------------------------------------

def test_nan_burst_yields_sensor_fault_and_holds_state():
    points = [
        (0, 40.0, 23.0),
        (30, float("nan"), None),
        (60, float("nan"), None),
        (90, float("nan"), None),
    ]
    results = run_series(points)

    assert results[-1][3] == rules.REASON_SENSOR_FAULT
    assert results[-1][2] == rules.ACTION_HOLD

    final_state = results[-1][1]
    assert final_state.fan == rules.FAN_OFF  # unchanged from before the burst
    assert final_state.phase == rules.PHASE_IDLE
    assert final_state.reject_count == 3

    reject_reasons = [r for (_, _, _, r) in results[1:]]
    assert reject_reasons == [
        rules.REASON_READING_REJECTED,
        rules.REASON_READING_REJECTED,
        rules.REASON_SENSOR_FAULT,
    ]


# ---------------------------------------------------------------------------
# 7. A cmd with an expired ttl_s is ignored in auto mode.
# ---------------------------------------------------------------------------

def test_expired_cmd_ttl_is_ignored_in_auto_mode():
    base = rules.VentState(mode=rules.MODE_AUTO)

    # Not expired: the advisory override applies.
    fresh = rules.apply_cmd(base, mode=rules.MODE_AUTO, fan=rules.FAN_ON, ttl_s=10.0, now=0.0)
    reading = rules.Reading(humidity=40.0, temperature=22.0, ts=5.0)  # below every trigger
    new_state, action, reason = rules.evaluate(fresh, reading, [], now=5.0)
    assert action == rules.ACTION_TURN_ON
    assert reason == rules.REASON_CMD_OVERRIDE
    assert new_state.fan == rules.FAN_ON

    # Expired: the same fan directive must be ignored, and the local rule
    # (which would not trigger on 40 %RH) is what actually decides the state.
    stale = rules.apply_cmd(base, mode=rules.MODE_AUTO, fan=rules.FAN_ON, ttl_s=10.0, now=0.0)
    late_reading = rules.Reading(humidity=40.0, temperature=22.0, ts=50.0)
    late_state, late_action, late_reason = rules.evaluate(stale, late_reading, [], now=50.0)
    assert late_action == rules.ACTION_HOLD
    assert late_reason != rules.REASON_CMD_OVERRIDE
    assert late_state.fan == rules.FAN_OFF
    assert late_state.phase == rules.PHASE_IDLE


# ---------------------------------------------------------------------------
# Extra coverage: reading validation itself, exercised directly.
# ---------------------------------------------------------------------------

def test_is_valid_reading_rejects_nan_out_of_range_and_big_jumps():
    assert not rules.is_valid_reading(float("nan"), 50.0)
    assert not rules.is_valid_reading(-1.0, 50.0)
    assert not rules.is_valid_reading(101.0, 50.0)
    assert not rules.is_valid_reading(75.0, 50.0)  # 25 %RH jump > MAX_VALID_JUMP
    assert rules.is_valid_reading(55.0, 50.0)
    assert rules.is_valid_reading(50.0, None)
