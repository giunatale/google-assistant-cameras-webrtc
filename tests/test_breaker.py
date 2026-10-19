"""Breaker state machine."""

from datetime import UTC, datetime

from custom_components.google_assistant_cameras_webrtc.breaker import (
    STATE_OK,
    STATE_TRIPPED,
    STATE_UNSUPPORTED,
    Breaker,
    CameraHealth,
)
from custom_components.google_assistant_cameras_webrtc.const import TRIP_BASE, TRIP_MAX

CAM = "camera.x"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now


def test_two_failures_trip_then_backoff():
    clock = Clock()
    b = Breaker(now=clock)
    assert b.allows(CAM) == (True, "")
    assert not b.record_failure(CAM, "x")
    assert b.allows(CAM)[0]
    assert b.record_failure(CAM, "y")
    allowed, why = b.allows(CAM)
    assert not allowed and "tripped" in why and "y" in why
    assert b.health(CAM).until == clock.now + TRIP_BASE
    assert b.next_expiry() == clock.now + TRIP_BASE
    assert b.trip_duration(CAM) == TRIP_BASE
    # expiry clears
    clock.now += TRIP_BASE
    assert b.allows(CAM) == (True, "")
    assert b.health(CAM).state == STATE_OK
    # second trip doubles
    b.record_failure(CAM, "a")
    b.record_failure(CAM, "b")
    assert b.health(CAM).until == clock.now + TRIP_BASE * 2
    # success resets the back-off ladder
    assert b.record_success(CAM)  # state changed (tripped -> ok)
    assert b.health(CAM).trips == 0
    assert not b.record_success(CAM)


def test_backoff_is_capped():
    clock = Clock()
    b = Breaker(now=clock)
    for _ in range(12):
        b.record_failure(CAM, "f")
        b.record_failure(CAM, "f")
        clock.now = b.health(CAM).until
        b.expired_trips()
    b.record_failure(CAM, "f")
    b.record_failure(CAM, "f")
    assert b.health(CAM).until - clock.now == TRIP_MAX


def test_unsupported_and_reset():
    b = Breaker(now=Clock())
    assert b.mark_unsupported(CAM, "codec")
    assert not b.mark_unsupported(CAM, "codec")
    allowed, why = b.allows(CAM)
    assert not allowed and "unsupported" in why
    assert not b.record_failure(CAM, "later")  # no effect while unsupported
    assert b.reset(CAM)
    assert b.allows(CAM) == (True, "")
    assert not b.reset(CAM)


def test_expired_trips_bulk_and_timer_helpers():
    clock = Clock()
    b = Breaker(now=clock)
    b.record_failure(CAM, "f")
    b.record_failure(CAM, "f")
    b.record_failure("camera.y", "f")
    b.record_failure("camera.y", "f")
    assert b.expired_trips() == []
    clock.now += TRIP_BASE
    assert sorted(b.expired_trips()) == [CAM, "camera.y"]
    assert b.next_expiry() is None and b.trip_duration(CAM) is None


def test_serialisation_roundtrip_and_tolerance():
    clock = Clock()
    b = Breaker(now=clock)
    b.record_failure(CAM, "f")
    b.record_failure(CAM, "f")
    b.mark_unsupported("camera.y", "codec")
    b.record_success("camera.z")
    data = b.as_dict()
    assert set(data) == {CAM, "camera.y", "camera.z"}
    restored = Breaker(now=clock)
    restored.load(data)
    assert restored.health(CAM).state == STATE_TRIPPED
    assert restored.health(CAM).until == b.health(CAM).until
    assert restored.health("camera.y").state == STATE_UNSUPPORTED
    assert restored.health("camera.z").last_success == clock.now
    # malformed input
    h = CameraHealth.from_dict(
        {"state": "weird", "until": "not-a-date", "failures": None}
    )
    assert h.state == STATE_OK and h.until is None and h.failures == 0
    h = CameraHealth.from_dict({"state": STATE_TRIPPED})  # tripped without until -> ok
    assert h.state == STATE_OK
    restored.load({"camera.bad": "garbage"})
    assert restored.cameras == {}
