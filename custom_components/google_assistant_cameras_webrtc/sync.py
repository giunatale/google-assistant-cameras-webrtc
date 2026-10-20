"""Ask Google to re-sync devices, through whichever link is configured.

- Nabu Casa: ``cloud``'s Google config (``async_sync_entities_all``).
- Self-hosted project: the ``google_assistant`` config entry's config object,
  which needs a ``service_account`` (the ``request_sync`` service exists only
  then; without it HA cannot talk to the HomeGraph API).

When neither works a fixable repair issue asks the user to say
"Hey Google, sync my devices"; it clears itself when the next SYNC arrives.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CALLBACK_TYPE, Event, HassJob, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import DOMAIN, ISSUE_SYNC_NEEDED, SYNC_DEBOUNCE

_LOGGER = logging.getLogger(__name__)

GOOGLE_DOMAIN = "google_assistant"
CLOUD_DOMAIN = "cloud"


async def async_request_google_sync(hass: HomeAssistant) -> tuple[bool, str]:
    """Request a SYNC from Google. Returns (ok, detail)."""
    attempts: list[str] = []

    # Nabu Casa
    cloud = hass.data.get(CLOUD_DOMAIN)
    if cloud is not None and getattr(cloud, "is_logged_in", False):
        try:
            gconf = await cloud.client.get_google_config()
            if getattr(gconf, "enabled", False):
                status = await gconf.async_sync_entities_all()
                if _ok(status):
                    return True, "Nabu Casa"
                attempts.append(f"Nabu Casa returned {status}")
            else:
                attempts.append("Nabu Casa Google Assistant disabled")
        except Exception as err:
            attempts.append(f"Nabu Casa error: {type(err).__name__}")

    # Self-hosted project
    entries = hass.config_entries.async_entries(GOOGLE_DOMAIN)
    if entries:
        if not hass.services.has_service(GOOGLE_DOMAIN, "request_sync"):
            attempts.append("google_assistant has no service_account")
        else:
            for entry in entries:
                gconf = getattr(entry, "runtime_data", None)
                sync_all = getattr(gconf, "async_sync_entities_all", None)
                if sync_all is None:
                    attempts.append("google_assistant config object unavailable")
                    continue
                try:
                    status = await sync_all()
                except Exception as err:
                    attempts.append(f"google_assistant error: {type(err).__name__}")
                    continue
                if _ok(status):
                    return True, "google_assistant service account"
                attempts.append(f"google_assistant returned {status}")
    elif cloud is None:
        attempts.append("no Google Assistant link found")

    return False, "; ".join(attempts) or "no Google Assistant link found"


def _ok(status: Any) -> bool:
    try:
        return 200 <= int(status) < 300
    except TypeError, ValueError:
        return False


class SyncRequester:
    """Debounced, boot-aware sync requests with a repair fallback."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise."""
        self._hass = hass
        self._unsub: CALLBACK_TYPE | None = None
        self._unsub_started: CALLBACK_TYPE | None = None
        self._reasons: list[str] = []
        self.last_request: str | None = None
        self.last_result: str | None = None
        self.issue_open = False
        self.on_result: Callable[[bool], None] | None = None

    @callback
    def request(self, reason: str) -> None:
        """Schedule a sync request (debounced; waits for HA to start)."""
        self._reasons.append(reason)
        if not self._hass.is_running:
            if self._unsub_started is None:
                self._unsub_started = self._hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STARTED, self._started
                )
            return
        self._schedule()

    @callback
    def _started(self, _event: Event) -> None:
        self._unsub_started = None
        self._schedule()

    @callback
    def _schedule(self) -> None:
        if self._unsub is not None:
            self._unsub()
        self._unsub = async_call_later(
            self._hass,
            SYNC_DEBOUNCE,
            HassJob(self._fire, f"{DOMAIN} sync request", cancel_on_shutdown=True),
        )

    async def _fire(self, _now: Any) -> None:
        self._unsub = None
        await self.async_run()

    async def async_run(self) -> bool:
        """Run a pending request now (cancels the debounce)."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        reasons = ", ".join(dict.fromkeys(self._reasons)) or "manual"
        self._reasons.clear()
        self.last_request = dt_util.utcnow().isoformat()
        ok, detail = await async_request_google_sync(self._hass)
        self.last_result = f"{'ok' if ok else 'failed'}: {detail}"
        if ok:
            _LOGGER.info("Requested Google device sync via %s (%s)", detail, reasons)
            self.clear_issue()
        else:
            _LOGGER.warning(
                "Could not ask Google to sync devices (%s); say "
                '"Hey Google, sync my devices" (%s)',
                detail,
                reasons,
            )
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                ISSUE_SYNC_NEEDED,
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_SYNC_NEEDED,
                translation_placeholders={"reason": detail},
            )
            self.issue_open = True
        if self.on_result:
            self.on_result(ok)
        return ok

    @callback
    def clear_issue(self) -> None:
        """Remove the "sync needed" repair (a sync happened or succeeded)."""
        if self.issue_open:
            self.issue_open = False
            ir.async_delete_issue(self._hass, DOMAIN, ISSUE_SYNC_NEEDED)

    @callback
    def cancel(self) -> None:
        """Cancel pending timers (HA shutdown)."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        if self._unsub_started is not None:
            self._unsub_started()
            self._unsub_started = None
