"""Repair flows: confirm a manual Google sync, retry WebRTC for a camera."""

from __future__ import annotations

from typing import Any

from homeassistant import data_entry_flow
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant

from .const import (
    DATA_RUNTIME,
    DATA_SYNC,
    ISSUE_CAMERA_UNSUPPORTED_PREFIX,
    ISSUE_SYNC_NEEDED,
)


class SyncNeededFlow(ConfirmRepairFlow):
    """The user says they synced; close the issue."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Keep hass."""
        self._hass = hass

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Close the issue on submit."""
        if user_input is not None:
            sync = self._hass.data.get(DATA_SYNC)
            if sync is not None:
                sync.issue_open = False
        return await super().async_step_confirm(user_input)


class RetryCameraFlow(RepairsFlow):
    """Forget a camera's "unsupported" state and re-sync."""

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        """Keep hass and the camera."""
        self._hass = hass
        self._entity_id = entity_id

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Show the confirmation."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Reset the camera on submit."""
        if user_input is not None:
            runtime = self._hass.data.get(DATA_RUNTIME)
            if runtime is not None:
                runtime.retry_camera(self._entity_id)
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"entity_id": self._entity_id},
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Return the flow for an issue."""
    if issue_id == ISSUE_SYNC_NEEDED:
        return SyncNeededFlow(hass)
    if issue_id.startswith(ISSUE_CAMERA_UNSUPPORTED_PREFIX):
        return RetryCameraFlow(hass, issue_id[len(ISSUE_CAMERA_UNSUPPORTED_PREFIX) :])
    return ConfirmRepairFlow()
