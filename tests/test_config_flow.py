"""The (empty) config flow."""

from homeassistant.core import HomeAssistant

from custom_components.google_assistant_cameras_webrtc.const import DOMAIN


async def test_user_flow_creates_entry(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "form" and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    assert result["title"] == "Google Assistant WebRTC Cameras"
    assert result["data"] == {}
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(result["result"].entry_id)
