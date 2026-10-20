"""Config flow: a single instance, nothing to configure."""

from __future__ import annotations

import secrets
from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
import voluptuous as vol

from .const import CONF_WEBHOOK_ID, DOMAIN, NAME


class GoogleAssistantCamerasWebRTCConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the single entry on confirmation."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=vol.Schema({}))
        return self.async_create_entry(
            title=NAME, data={CONF_WEBHOOK_ID: secrets.token_hex(32)}
        )
