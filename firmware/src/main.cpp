// Bathroom ventilation controller -- ESP32 + DHT22 + relay.
//
// This firmware is the authoritative, fail-safe implementation of the
// control law described in docs/. It keeps ventilating on its own local
// thresholds even with no Wi-Fi and no MQTT broker reachable; the network
// stack only adds telemetry/observability and an advisory `cmd` channel.
// service/rules.py implements the identical rule in Python for the
// automation service's own observability -- the two are intentionally
// redundant, not a single shared source, so this device never depends on
// the network to keep the room from staying damp. See README.md.
//
// No delay() anywhere below except inside setup(). loop() is fully
// non-blocking: Wi-Fi/MQTT (re)connection and sensor sampling are all
// driven by millis()-based timers.

#include <Arduino.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include <PubSubClient.h>
#include <DHT.h>
#include <ArduinoJson.h>

#include "config.h"
#if __has_include("secrets.h")
#include "secrets.h"
#else
// Keep a clean checkout buildable for CI and local control-law validation.
// The example values deliberately cannot expose real credentials; copy the
// example to secrets.h and edit it before expecting Wi-Fi/MQTT connectivity.
#include "secrets.h.example"
#endif

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

enum Phase : uint8_t { PHASE_IDLE, PHASE_VENTING, PHASE_COOLDOWN };
enum Mode : uint8_t { MODE_AUTO, MODE_MANUAL };
enum Action : uint8_t { ACTION_HOLD, ACTION_TURN_ON, ACTION_TURN_OFF };

struct Sample {
  float humidity;
  unsigned long tsSec;
};

struct TriggerResult {
  bool triggered;
  const char *reason;
};

struct VentState {
  Phase phase = PHASE_IDLE;
  bool fanOn = false;
  Mode mode = MODE_AUTO;
  unsigned long phaseEnteredAtS = 0;
  unsigned long fanOnSinceS = 0;
  bool fanIsTiming = false;
  unsigned long totalRuntimeS = 0;
  uint8_t rejectCount = 0;
  bool hasLastValid = false;
  float lastValidHumidity = 0.0f;
  String reason = "init";
  bool hasBaselineAtTrigger = false;
  float baselineAtTrigger = 0.0f;
  bool hasCmdFan = false;
  bool cmdFanOn = false;
  unsigned long cmdIssuedAtS = 0;
  unsigned long cmdTtlS = 0;
};

// ---------------------------------------------------------------------------
// Globals
// ---------------------------------------------------------------------------

WiFiClient wifiClient;
PubSubClient mqttClient(wifiClient);
DHT dht(PIN_DHT22, DHT_TYPE);

VentState state;

Sample history[HISTORY_CAPACITY];
size_t historyCount = 0;

unsigned long lastSampleMs = 0;
uint32_t seqCounter = 0;
bool hasPublishedTelemetry = false;
float lastPublishedHumidity = 0.0f;
unsigned long lastTelemetryPublishMs = 0;

unsigned long lastWifiAttemptMs = 0;
unsigned long wifiBackoffMs = 0; // 0 => attempt immediately on boot
bool wifiPsApplied = false;

unsigned long lastMqttAttemptMs = 0;
unsigned long mqttBackoffMs = 0; // 0 => attempt immediately once Wi-Fi is up

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

static inline unsigned long nowSeconds() { return millis() / 1000UL; }

void setFan(bool on) {
  digitalWrite(PIN_RELAY, on ? RELAY_ON_LEVEL : RELAY_OFF_LEVEL);
  digitalWrite(PIN_STATUS_LED, on ? HIGH : LOW);
}

// ---------------------------------------------------------------------------
// History buffer / baseline / rise-rate
// ---------------------------------------------------------------------------

void addToHistory(float humidity, unsigned long tsSec) {
  // Drop anything already outside the baseline window before appending, so
  // the buffer stays bounded without ever needing to grow past
  // HISTORY_CAPACITY under the normal (5-30 s) sampling cadence.
  size_t start = 0;
  while (start < historyCount && tsSec - history[start].tsSec > BASELINE_WINDOW_S) {
    start++;
  }
  if (start > 0) {
    memmove(history, history + start, (historyCount - start) * sizeof(Sample));
    historyCount -= start;
  }
  if (historyCount == HISTORY_CAPACITY) {
    // Pathological case (sustained 2 s sampling for 30+ minutes): degrade
    // gracefully by dropping the oldest sample rather than overflowing. The
    // baseline window just gets a little shorter/noisier, never unsafe.
    memmove(history, history + 1, (HISTORY_CAPACITY - 1) * sizeof(Sample));
    historyCount--;
  }
  history[historyCount].humidity = humidity;
  history[historyCount].tsSec = tsSec;
  historyCount++;
}

float computeBaseline(unsigned long nowS) {
  static float buf[HISTORY_CAPACITY];
  size_t n = 0;
  for (size_t i = 0; i < historyCount; i++) {
    if (nowS - history[i].tsSec <= BASELINE_WINDOW_S) {
      buf[n++] = history[i].humidity;
    }
  }
  if (n == 0) return NAN;
  for (size_t i = 1; i < n; i++) {
    float key = buf[i];
    long j = (long)i - 1;
    while (j >= 0 && buf[j] > key) {
      buf[j + 1] = buf[j];
      j--;
    }
    buf[j + 1] = key;
  }
  if (n % 2 == 1) return buf[n / 2];
  return (buf[n / 2 - 1] + buf[n / 2]) / 2.0f;
}

float rateBetween(const Sample &a, const Sample &b) {
  if (b.tsSec <= a.tsSec) return NAN;
  float dtMin = (float)(b.tsSec - a.tsSec) / 60.0f;
  return (b.humidity - a.humidity) / dtMin;
}

// dH/dt over the two most recent samples, used only to pick the sampling
// interval (see currentSampleIntervalMs). Separate from riseSustained(),
// which is the two-consecutive-samples VENTING trigger.
float recentRiseRate() {
  if (historyCount < 2) return NAN;
  return rateBetween(history[historyCount - 2], history[historyCount - 1]);
}

bool riseSustained() {
  if (historyCount < 3) return false;
  float r1 = rateBetween(history[historyCount - 3], history[historyCount - 2]);
  float r2 = rateBetween(history[historyCount - 2], history[historyCount - 1]);
  if (isnan(r1) || isnan(r2)) return false;
  return r1 >= RISE_ON && r2 >= RISE_ON;
}

// ---------------------------------------------------------------------------
// Control law -- mirrors service/rules.py evaluate() exactly. Priority order
// for the IDLE->VENTING trigger is a documented contract: absolute threshold
// is checked first, so H >= HUM_ON always reports "absolute_threshold" even
// when the relative-spike or rise-rate conditions are also true.
// ---------------------------------------------------------------------------

TriggerResult checkVentingTrigger(float h, float baseline) {
  if (h >= HUM_ON) return {true, "absolute_threshold"};
  if ((h - baseline) >= DELTA_ON) return {true, "relative_spike"};
  if (riseSustained()) return {true, "rise_rate"};
  return {false, ""};
}

bool isValidReading(float humidity) {
  if (isnan(humidity)) return false;
  if (humidity < 0.0f || humidity > 100.0f) return false;
  if (state.hasLastValid && fabsf(humidity - state.lastValidHumidity) > MAX_VALID_JUMP) return false;
  return true;
}

// Resolves whether an inbound cmd should bypass the state machine this tick.
// Mirrors rules.py's _resolve_override(): an auto-mode override expires
// after cmdTtlS and is then silently ignored, falling back to the local
// rule; a manual-mode override is sticky.
bool resolveOverride(bool *forcedFanOn, const char **forcedReason) {
  if (!state.hasCmdFan) return false;

  if (state.mode == MODE_MANUAL) {
    *forcedFanOn = state.cmdFanOn;
    *forcedReason = "manual";
    return true;
  }

  // MODE_AUTO
  unsigned long nowS = nowSeconds();
  if (nowS - state.cmdIssuedAtS > state.cmdTtlS) {
    return false; // expired -> ignored
  }
  *forcedFanOn = state.cmdFanOn;
  *forcedReason = "cmd_override";
  return true;
}

Action evaluateControlLaw(float humidity, unsigned long nowS) {
  addToHistory(humidity, nowS);
  float liveBaseline = computeBaseline(nowS);
  if (isnan(liveBaseline)) liveBaseline = humidity;

  Action action = ACTION_HOLD;
  String reason = state.reason;

  bool forcedFanOn = false;
  const char *forcedReason = "";
  bool hasForced = resolveOverride(&forcedFanOn, &forcedReason);

  if (hasForced) {
    if (forcedFanOn != state.fanOn) {
      action = forcedFanOn ? ACTION_TURN_ON : ACTION_TURN_OFF;
      if (!forcedFanOn && state.fanIsTiming) {
        state.totalRuntimeS += nowS - state.fanOnSinceS;
        state.fanIsTiming = false;
      }
      if (forcedFanOn) {
        state.fanOnSinceS = nowS;
        state.fanIsTiming = true;
        state.phase = PHASE_VENTING;
      } else {
        state.phase = PHASE_IDLE;
      }
      state.phaseEnteredAtS = nowS;
      state.fanOn = forcedFanOn;
    }
    reason = forcedReason;
  } else {
    if (state.phase == PHASE_IDLE) {
      TriggerResult t = checkVentingTrigger(humidity, liveBaseline);
      if (t.triggered) {
        state.phase = PHASE_VENTING;
        state.phaseEnteredAtS = nowS;
        state.fanOnSinceS = nowS;
        state.fanIsTiming = true;
        state.fanOn = true;
        action = ACTION_TURN_ON;
        reason = t.reason;
        state.hasBaselineAtTrigger = true;
        // Freeze B at the ambient value it held the instant venting started.
        // If we kept recomputing B from a window that now also contains the
        // shower itself, B would drift up toward H and both the DELTA_OFF
        // exit test and (eventually) the MAX_RUN_S guard would be silently
        // defeated by a sensor that is simply pinned high. B resumes
        // rolling once we're back in IDLE.
        state.baselineAtTrigger = liveBaseline;
      }
    } else if (state.phase == PHASE_VENTING) {
      unsigned long runElapsedS = nowS - state.fanOnSinceS;
      if (runElapsedS >= MAX_RUN_S) {
        state.phase = PHASE_COOLDOWN;
        state.phaseEnteredAtS = nowS;
        state.fanOn = false;
        action = ACTION_TURN_OFF;
        if (state.fanIsTiming) {
          state.totalRuntimeS += nowS - state.fanOnSinceS;
          state.fanIsTiming = false;
        }
        reason = "max_runtime_guard";
        state.hasBaselineAtTrigger = false;
      } else {
        float b = state.hasBaselineAtTrigger ? state.baselineAtTrigger : liveBaseline;
        bool fallOk = (humidity <= HUM_OFF) || ((humidity - b) <= DELTA_OFF);
        if (fallOk && runElapsedS >= MIN_RUN_S) {
          state.phase = PHASE_COOLDOWN;
          state.phaseEnteredAtS = nowS;
          state.fanOn = false;
          action = ACTION_TURN_OFF;
          if (state.fanIsTiming) {
            state.totalRuntimeS += nowS - state.fanOnSinceS;
            state.fanIsTiming = false;
          }
          reason = "humidity_cleared";
          state.hasBaselineAtTrigger = false;
        }
      }
    } else { // PHASE_COOLDOWN
      unsigned long coolElapsedS = nowS - state.phaseEnteredAtS;
      if (coolElapsedS >= COOLDOWN_S) {
        state.phase = PHASE_IDLE;
        state.phaseEnteredAtS = nowS;
        reason = "cooldown_complete";
      }
    }
  }

  state.reason = reason;
  state.rejectCount = 0;
  state.hasLastValid = true;
  state.lastValidHumidity = humidity;
  return action;
}

// ---------------------------------------------------------------------------
// MQTT publish helpers
//
// NOTE (known library limitation, documented rather than silently ignored):
// PubSubClient only ever emits QoS 0 PUBLISH packets -- it does not
// implement a publisher-side PUBACK handshake/retry for QoS 1, even though
// this is the fixed, required client library. subscribe() below still
// requests QoS 1 for the inbound cmd topic, which PubSubClient does honour
// for delivery accounting. See README.md "Known limitations".
// ---------------------------------------------------------------------------

void publishState(bool retained) {
  if (!mqttClient.connected()) return;
  JsonDocument doc;
  doc["fan"] = state.fanOn ? "on" : "off";
  doc["mode"] = state.mode == MODE_MANUAL ? "manual" : "auto";
  doc["reason"] = state.reason;
  unsigned long runtimeS = state.totalRuntimeS;
  if (state.fanIsTiming) runtimeS += nowSeconds() - state.fanOnSinceS;
  doc["runtime_s"] = runtimeS;
  char buf[192];
  serializeJson(doc, buf, sizeof(buf));
  mqttClient.publish(TOPIC_STATE, buf, retained);
}

void publishTelemetry(float humidity, float temperature) {
  if (!mqttClient.connected()) return;
  JsonDocument doc;
  doc["humidity"] = humidity;
  if (isnan(temperature)) {
    doc["temperature"] = nullptr;
  } else {
    doc["temperature"] = temperature;
  }
  doc["rssi"] = WiFi.RSSI();
  doc["uptime_s"] = nowSeconds();
  doc["seq"] = seqCounter++;
  char buf[192];
  serializeJson(doc, buf, sizeof(buf));
  mqttClient.publish(TOPIC_TELEMETRY, buf, false);
}

// ---------------------------------------------------------------------------
// Inbound cmd handling
// ---------------------------------------------------------------------------

void applyCmd(const char *fanStr, const char *modeStr, long ttlS) {
  state.mode = (strcmp(modeStr, "manual") == 0) ? MODE_MANUAL : MODE_AUTO;
  if (fanStr != nullptr && strlen(fanStr) > 0) {
    state.hasCmdFan = true;
    state.cmdFanOn = (strcmp(fanStr, "on") == 0);
    state.cmdIssuedAtS = nowSeconds();
    state.cmdTtlS = (ttlS > 0) ? (unsigned long)ttlS : DEFAULT_CMD_TTL_S;
  }
}

void onMqttMessage(char *topic, byte *payload, unsigned int length) {
  if (strcmp(topic, TOPIC_CMD) != 0) return;

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, payload, length);
  if (err) {
    Serial.print("cmd: JSON parse failed: ");
    Serial.println(err.c_str());
    return;
  }

  const char *fanStr = doc["fan"] | "";
  const char *modeStr = doc["mode"] | "auto";
  long ttlS = doc["ttl_s"] | (long)DEFAULT_CMD_TTL_S;
  applyCmd(fanStr, modeStr, ttlS);

  // Re-evaluate immediately so a manual/override command takes effect
  // without waiting for the next sample tick.
  if (state.hasLastValid) {
    Action action = evaluateControlLaw(state.lastValidHumidity, nowSeconds());
    if (action == ACTION_TURN_ON) setFan(true);
    else if (action == ACTION_TURN_OFF) setFan(false);
    if (action != ACTION_HOLD) publishState(true);
  }
}

// ---------------------------------------------------------------------------
// Wi-Fi / MQTT connection management -- non-blocking, exponential backoff
// capped at RECONNECT_BACKOFF_MAX_MS. Ventilation itself never waits on any
// of this: setFan() is driven directly from evaluateControlLaw() in
// serviceSampling(), regardless of link state.
// ---------------------------------------------------------------------------

void serviceWifi(unsigned long nowMs) {
  if (WiFi.status() == WL_CONNECTED) {
    if (!wifiPsApplied) {
      // Modem-sleep power saving is only meaningful once associated; keep
      // the MQTT session alive via keepalive rather than reconnecting each
      // cycle (see mqttClient.setKeepAlive in setup()).
      WiFi.setSleep(true);
      esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
      wifiPsApplied = true;
    }
    wifiBackoffMs = RECONNECT_BACKOFF_MIN_MS;
    return;
  }

  wifiPsApplied = false;
  if (nowMs - lastWifiAttemptMs < wifiBackoffMs) return;

  lastWifiAttemptMs = nowMs;
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  wifiBackoffMs = (wifiBackoffMs == 0) ? RECONNECT_BACKOFF_MIN_MS
                                       : min(wifiBackoffMs * 2, RECONNECT_BACKOFF_MAX_MS);
}

bool mqttConnect() {
  String clientId = String(DEVICE_ID);
  bool ok;
  if (strlen(MQTT_USER) > 0) {
    ok = mqttClient.connect(clientId.c_str(), MQTT_USER, MQTT_PASSWORD,
                             TOPIC_STATUS, 1, true, "offline");
  } else {
    // LWT is registered as part of this CONNECT call -- the only point in
    // the MQTT protocol where a will can be registered -- so a crashed node
    // is detectable from the moment the broker accepts the session, before
    // we ever publish anything ourselves.
    ok = mqttClient.connect(clientId.c_str(), TOPIC_STATUS, 1, true, "offline");
  }
  if (ok) {
    mqttClient.publish(TOPIC_STATUS, "online", true);
    mqttClient.subscribe(TOPIC_CMD, 1);
    publishState(true);
    Serial.println("mqtt: connected");
  }
  return ok;
}

void serviceMqtt(unsigned long nowMs) {
  if (WiFi.status() != WL_CONNECTED) return;
  if (mqttClient.connected()) {
    mqttBackoffMs = RECONNECT_BACKOFF_MIN_MS;
    return;
  }
  if (nowMs - lastMqttAttemptMs < mqttBackoffMs) return;

  lastMqttAttemptMs = nowMs;
  if (mqttConnect()) {
    mqttBackoffMs = RECONNECT_BACKOFF_MIN_MS;
  } else {
    mqttBackoffMs = (mqttBackoffMs == 0) ? RECONNECT_BACKOFF_MIN_MS
                                          : min(mqttBackoffMs * 2, RECONNECT_BACKOFF_MAX_MS);
  }
}

// ---------------------------------------------------------------------------
// Sampling
// ---------------------------------------------------------------------------

unsigned long currentSampleIntervalMs() {
  if (state.phase == PHASE_VENTING) return SAMPLE_INTERVAL_ACTIVE_MS;
  float rate = recentRiseRate();
  if (!isnan(rate) && rate > FAST_RISE_RATE_THRESHOLD) return SAMPLE_INTERVAL_ACTIVE_MS;
  return SAMPLE_INTERVAL_IDLE_MS;
}

void serviceSampling(unsigned long nowMs) {
  unsigned long interval = currentSampleIntervalMs();
  if (interval < SAMPLE_INTERVAL_MIN_MS) interval = SAMPLE_INTERVAL_MIN_MS;
  if (nowMs - lastSampleMs < interval) return;
  lastSampleMs = nowMs;

  // The DHT22's bit-banged one-wire-like read blocks for a few ms inside the
  // library call itself -- that is inherent to the sensor protocol, not a
  // delay() in our own control flow, and is unavoidable with this
  // sensor/library combination.
  float humidity = dht.readHumidity();
  float temperature = dht.readTemperature();
  unsigned long nowS = nowSeconds();

  if (!isValidReading(humidity)) {
    state.rejectCount++;
    if (state.rejectCount >= REJECT_FAULT_COUNT) {
      state.reason = "sensor_fault";
      Serial.println("sensor: fault (3 consecutive rejected readings), holding last fan state");
      publishState(true);
    } else {
      state.reason = "reading_rejected";
    }
    return; // fan state is held exactly as-is -- no relay action taken
  }

  Action action = evaluateControlLaw(humidity, nowS);

  if (action == ACTION_TURN_ON) {
    setFan(true);
    Serial.printf("fan ON  (reason=%s, H=%.1f)\n", state.reason.c_str(), humidity);
  } else if (action == ACTION_TURN_OFF) {
    setFan(false);
    Serial.printf("fan OFF (reason=%s, H=%.1f)\n", state.reason.c_str(), humidity);
  }
  if (action != ACTION_HOLD) publishState(true);

  bool shouldPublish = !hasPublishedTelemetry ||
                        fabsf(humidity - lastPublishedHumidity) >= TELEMETRY_PUBLISH_DELTA ||
                        (nowMs - lastTelemetryPublishMs) >= TELEMETRY_HEARTBEAT_MS;
  if (shouldPublish) {
    publishTelemetry(humidity, temperature);
    lastPublishedHumidity = humidity;
    lastTelemetryPublishMs = nowMs;
    hasPublishedTelemetry = true;
  }
}

// ---------------------------------------------------------------------------
// setup() / loop()
// ---------------------------------------------------------------------------

void setup() {
  Serial.begin(115200);
  delay(50); // only delay() in this firmware; lets the USB-serial link settle

  // Relay pin level MUST be set before its direction: the reverse order
  // produces a ~10 ms fan pulse on every reset because pinMode(OUTPUT)
  // briefly drives the pin from its default (floating/LOW) state.
  digitalWrite(PIN_RELAY, RELAY_OFF_LEVEL);
  pinMode(PIN_RELAY, OUTPUT);

  pinMode(PIN_STATUS_LED, OUTPUT);
  digitalWrite(PIN_STATUS_LED, LOW);

  dht.begin();

  WiFi.mode(WIFI_STA);

  mqttClient.setServer(MQTT_HOST, MQTT_PORT);
  mqttClient.setKeepAlive(MQTT_KEEPALIVE_S);
  mqttClient.setCallback(onMqttMessage);
  mqttClient.setBufferSize(256);

  Serial.println("bathroom-vent: boot complete, entering non-blocking control loop");
}

void loop() {
  unsigned long nowMs = millis();
  serviceWifi(nowMs);
  serviceMqtt(nowMs);
  if (mqttClient.connected()) {
    mqttClient.loop();
  }
  serviceSampling(nowMs);
}
