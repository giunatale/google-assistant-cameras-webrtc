"""Per-camera circuit breaker.

Google chooses the protocol at SYNC time, so the fallback to HLS has to happen
in what we advertise, not per request. Each camera is in one of three states:

- ``ok``: WebRTC is advertised;
- ``tripped``: HLS only until ``until`` (exponential back-off on repeats);
- ``unsupported``: HLS only until the user retries from the repair issue
  (deterministic failures such as a video codec Google cannot play).

The breaker is a pure state machine; the runtime persists it and asks Google
to re-sync whenever the advertised set changes.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .const import TRIP_AFTER_FAILURES, TRIP_BASE, TRIP_MAX

STATE_OK = "ok"
STATE_TRIPPED = "tripped"
STATE_UNSUPPORTED = "unsupported"


@dataclass
class CameraHealth:
    """Breaker state of one camera."""

    state: str = STATE_OK
    failures: int = 0  # consecutive failures while ok
    trips: int = 0  # consecutive trips (drives the back-off)
    until: datetime | None = None
    reason: str | None = None
    last_failure: str | None = None
    last_success: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "state": self.state,
            "failures": self.failures,
            "trips": self.trips,
            "until": self.until.isoformat() if self.until else None,
            "reason": self.reason,
            "last_failure": self.last_failure,
            "last_success": self.last_success.isoformat()
            if self.last_success
            else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CameraHealth:
        """Restore from storage; ignore anything malformed."""
        health = cls()
        state = data.get("state")
        if state in (STATE_OK, STATE_TRIPPED, STATE_UNSUPPORTED):
            health.state = state
        health.failures = int(data.get("failures") or 0)
        health.trips = int(data.get("trips") or 0)
        health.reason = data.get("reason") or None
        health.last_failure = data.get("last_failure") or None
        for attr in ("until", "last_success"):
            raw = data.get(attr)
            if isinstance(raw, str):
                with suppress(ValueError):
                    setattr(health, attr, datetime.fromisoformat(raw))
        if health.state == STATE_TRIPPED and health.until is None:
            health.state = STATE_OK
        return health


@dataclass
class Breaker:
    """Breaker states for all cameras."""

    now: Callable[[], datetime]
    cameras: dict[str, CameraHealth] = field(default_factory=dict)

    def health(self, entity_id: str) -> CameraHealth:
        """Return the (possibly fresh) health of a camera."""
        return self.cameras.setdefault(entity_id, CameraHealth())

    def allows(self, entity_id: str) -> tuple[bool, str]:
        """Return whether WebRTC may be advertised and why not otherwise.

        An expired trip is cleared here; callers check ``changed`` via the
        return of ``expire`` to know if a re-sync is due.
        """
        health = self.cameras.get(entity_id)
        if health is None:
            return True, ""
        if health.state == STATE_UNSUPPORTED:
            return False, f"unsupported ({health.reason})"
        if health.state == STATE_TRIPPED:
            if health.until is not None and self.now() >= health.until:
                health.state = STATE_OK
                health.until = None
                health.failures = 0
                return True, ""
            return False, f"tripped until {health.until:%H:%M:%S} ({health.reason})"
        return True, ""

    def expired_trips(self) -> list[str]:
        """Return cameras whose trip expired (and clear them)."""
        cleared: list[str] = []
        for entity_id, health in self.cameras.items():
            if (
                health.state == STATE_TRIPPED
                and health.until is not None
                and self.now() >= health.until
            ):
                health.state = STATE_OK
                health.until = None
                health.failures = 0
                cleared.append(entity_id)
        return cleared

    def next_expiry(self) -> datetime | None:
        """Return the earliest trip expiry, if any."""
        expiries = [
            health.until
            for health in self.cameras.values()
            if health.state == STATE_TRIPPED and health.until is not None
        ]
        return min(expiries) if expiries else None

    def record_success(self, entity_id: str) -> bool:
        """Record a working stream. Returns True if the state changed."""
        health = self.health(entity_id)
        changed = health.state == STATE_TRIPPED
        health.state = STATE_OK
        health.failures = 0
        health.trips = 0
        health.until = None
        health.reason = None
        health.last_success = self.now()
        return changed

    def record_failure(self, entity_id: str, reason: str) -> bool:
        """Record a failure. Returns True if the camera just tripped."""
        health = self.health(entity_id)
        health.last_failure = reason
        if health.state != STATE_OK:
            return False
        health.failures += 1
        if health.failures < TRIP_AFTER_FAILURES:
            return False
        health.trips += 1
        backoff = min(TRIP_BASE * (2 ** (health.trips - 1)), TRIP_MAX)
        health.state = STATE_TRIPPED
        health.until = self.now() + backoff
        health.reason = reason
        health.failures = 0
        return True

    def mark_unsupported(self, entity_id: str, reason: str) -> bool:
        """Disable WebRTC until the user retries. Returns True if changed."""
        health = self.health(entity_id)
        changed = health.state != STATE_UNSUPPORTED
        health.state = STATE_UNSUPPORTED
        health.until = None
        health.reason = reason
        health.last_failure = reason
        health.failures = 0
        return changed

    def reset(self, entity_id: str) -> bool:
        """Forget a camera's state (user retry). Returns True if changed."""
        health = self.cameras.pop(entity_id, None)
        return health is not None and health.state != STATE_OK

    def trip_duration(self, entity_id: str) -> timedelta | None:
        """Return the remaining trip time for diagnostics."""
        health = self.cameras.get(entity_id)
        if health and health.state == STATE_TRIPPED and health.until:
            return max(health.until - self.now(), timedelta(0))
        return None

    def as_dict(self) -> dict[str, Any]:
        """Serialise non-default states only."""
        return {
            entity_id: health.as_dict()
            for entity_id, health in self.cameras.items()
            if health.state != STATE_OK or health.last_failure or health.last_success
        }

    def load(self, data: dict[str, Any]) -> None:
        """Restore from storage."""
        self.cameras = {
            entity_id: CameraHealth.from_dict(raw)
            for entity_id, raw in data.items()
            if isinstance(raw, dict)
        }
