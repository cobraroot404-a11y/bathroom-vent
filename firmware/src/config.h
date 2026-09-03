// config.h -- every tunable used by the control law and the sampling /
// networking policy lives here as a named constant. main.cpp must not
// contain magic numbers for anything declared in this file.
//
// These values MUST match service/rules.py. The two implementations of the
// state machine are intentionally redundant: this one (C++, on the ESP32)
// is authoritative and fail-safe -- it keeps venting the bathroom even with
// no Wi-Fi and no broker. service/rules.py exists for observability and to
// issue advisory `cmd` messages; see README.md "Two control laws" section.

#pragma once

// ---------------------------------------------------------------------------
// Pin map (fixed -- do not change without re-reading docs/WIRING.md)
// ---------------------------------------------------------------------------
#define PIN_DHT22 4        // DHT22 DATA, 10k pull-up to 3V3, not a strapping pin
#define PIN_RELAY 26       // Relay IN, ACTIVE-LOW, RTC-capable for future deep-sleep hold
#define PIN_STATUS_LED 2   // Onboard LED, mirrors fan state

#define DHT_TYPE DHT22

// ---------------------------------------------------------------------------
// Relay polarity
// ---------------------------------------------------------------------------
#define RELAY_ACTIVE_LOW true
#define RELAY_ON_LEVEL   (RELAY_ACTIVE_LOW ? LOW : HIGH)
#define RELAY_OFF_LEVEL  (RELAY_ACTIVE_LOW ? HIGH : LOW)

// ---------------------------------------------------------------------------
// Control law thresholds -- mirror service/rules.py exactly
// ---------------------------------------------------------------------------
static const float HUM_ON = 70.0f;          // %RH absolute threshold -> VENTING
static const float HUM_OFF = 60.0f;         // %RH absolute threshold -> eligible for COOLDOWN
static const float DELTA_ON = 8.0f;         // H - B spike (%RH) -> VENTING
static const float DELTA_OFF = 3.0f;        // H - B (%RH) -> eligible for COOLDOWN
static const float RISE_ON = 2.0f;          // %RH per minute, sustained 2 samples -> VENTING

static const unsigned long MIN_RUN_S = 180UL;       // minimum VENTING dwell before COOLDOWN allowed
static const unsigned long MAX_RUN_S = 1800UL;      // unconditional VENTING -> COOLDOWN guard
static const unsigned long COOLDOWN_S = 120UL;      // minimum COOLDOWN dwell before IDLE

static const unsigned long BASELINE_WINDOW_S = 30UL * 60UL;  // rolling median window for baseline B
static const float MAX_VALID_JUMP = 20.0f;                    // %RH; bigger single-sample jump rejected
static const uint8_t REJECT_FAULT_COUNT = 3;                  // consecutive rejects -> sensor_fault

static const unsigned long DEFAULT_CMD_TTL_S = 1200UL;

// ---------------------------------------------------------------------------
// Adaptive sampling
// ---------------------------------------------------------------------------
static const unsigned long SAMPLE_INTERVAL_IDLE_MS = 30000UL;   // 30 s in IDLE
static const unsigned long SAMPLE_INTERVAL_ACTIVE_MS = 5000UL;  // 5 s in VENTING or fast rise
static const unsigned long SAMPLE_INTERVAL_MIN_MS = 2000UL;     // DHT22 hard floor
static const float FAST_RISE_RATE_THRESHOLD = 0.5f;             // %RH/min -> switch to active sampling

// ---------------------------------------------------------------------------
// Publish-on-change policy
// ---------------------------------------------------------------------------
static const float TELEMETRY_PUBLISH_DELTA = 0.5f;              // %RH change that forces a publish
static const unsigned long TELEMETRY_HEARTBEAT_MS = 5UL * 60UL * 1000UL; // 5 min heartbeat

// ---------------------------------------------------------------------------
// Networking
// ---------------------------------------------------------------------------
static const unsigned long MQTT_KEEPALIVE_S = 60UL;
static const unsigned long RECONNECT_BACKOFF_MIN_MS = 1000UL;
static const unsigned long RECONNECT_BACKOFF_MAX_MS = 30000UL;

// ---------------------------------------------------------------------------
// MQTT topics -- base "home/bathroom/vent"
// ---------------------------------------------------------------------------
#define TOPIC_BASE     "home/bathroom/vent"
#define TOPIC_STATUS   TOPIC_BASE "/status"
#define TOPIC_TELEMETRY TOPIC_BASE "/telemetry"
#define TOPIC_STATE    TOPIC_BASE "/state"
#define TOPIC_CMD      TOPIC_BASE "/cmd"

// History buffer sized for the worst case: 5 s sampling for the full 30 min
// baseline window = 360 samples. Rounded up with headroom.
static const size_t HISTORY_CAPACITY = 400;
