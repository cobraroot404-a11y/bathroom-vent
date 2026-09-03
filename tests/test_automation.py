"""Regression tests for MQTT message handling and process startup."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

import automation  # noqa: E402


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, *args, **kwargs):
        self.published.append((args, kwargs))


def message(payload: bytes):
    return SimpleNamespace(topic=automation.TOPIC_TELEMETRY, payload=payload)


def test_non_object_json_telemetry_is_dropped_without_crashing(caplog):
    client = FakeClient()

    for payload in (b"null", b"[]", b'"text"'):
        automation.on_message(client, None, message(payload))

    assert client.published == []
    assert caplog.text.count("not a JSON object") == 3


def test_malformed_json_telemetry_is_dropped_without_crashing(caplog):
    client = FakeClient()

    automation.on_message(client, None, message(b"{not-json"))

    assert client.published == []
    assert "dropping malformed telemetry payload" in caplog.text


def test_main_retries_when_initial_broker_connection_fails(monkeypatch):
    calls = []

    class FakeMqttClient:
        def __init__(self, *args, **kwargs):
            calls.append(("init", args, kwargs))

        def username_pw_set(self, *args):
            calls.append(("auth", args))

        def reconnect_delay_set(self, **kwargs):
            calls.append(("backoff", kwargs))

        def connect_async(self, *args, **kwargs):
            calls.append(("connect_async", args, kwargs))

        def loop_forever(self, **kwargs):
            calls.append(("loop_forever", kwargs))

    monkeypatch.setattr(automation.mqtt, "Client", FakeMqttClient)
    automation.main()

    assert any(call[0] == "connect_async" for call in calls)
    assert ("loop_forever", {"retry_first_connection": True}) in calls
