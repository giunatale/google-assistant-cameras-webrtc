"""Repair flow: confirm a manual Google device sync."""

from __future__ import annotations

from typing import Any

from homeassistant import data_entry_flow
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant

from .const import DATA_SYNC


class SyncNeededFlow(ConfirmRepairFlow):
    """The user says they synced; close the issue."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Keep hass."""
        self._hass = hass

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Close the issue on submit."""
        if (
            user_input is not None
            and (sync := self._hass.data.get(DATA_SYNC)) is not None
        ):
            sync.issue_open = False
        return await super().async_step_confirm(user_input)


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Return the flow for an issue."""
    return SyncNeededFlow(hass)
