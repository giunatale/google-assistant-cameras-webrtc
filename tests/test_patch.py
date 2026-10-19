"""Trait patching: shape checks, idempotence, chain-aware removal."""

import types

from homeassistant.components.google_assistant import trait as ga_trait
from homeassistant.core import HomeAssistant
import pytest

from custom_components.google_assistant_cameras_webrtc import patch


def test_verify_shape_ok():
    assert patch.verify_shape() is ga_trait.CameraStreamTrait


def test_verify_shape_detects_native_webrtc(monkeypatch):
    def sync_attributes(self):
        return {
            "cameraStreamSupportedProtocols": ["webrtc", "hls"],
            "cameraStreamNeedAuthToken": False,
        }

    monkeypatch.setattr(ga_trait.CameraStreamTrait, "sync_attributes", sync_attributes)
    with pytest.raises(patch.CoreHasWebRTC):
        patch.verify_shape()


@pytest.mark.parametrize(
    ("attr", "value", "detail"),
    [
        ("name", "action.devices.traits.Other", "trait name"),
        ("commands", [], "GetCameraStream"),
        ("sync_attributes", lambda self, extra: {}, "sync_attributes signature"),
        ("execute", lambda self, command: None, "execute"),
    ],
)
def test_verify_shape_mismatches(monkeypatch, attr, value, detail):
    monkeypatch.setattr(ga_trait.CameraStreamTrait, attr, value)
    with pytest.raises(patch.ShapeMismatch, match=detail):
        patch.verify_shape()


def test_verify_shape_requires_registration(monkeypatch):
    monkeypatch.setattr(
        ga_trait,
        "TRAITS",
        [t for t in ga_trait.TRAITS if t is not ga_trait.CameraStreamTrait],
    )
    with pytest.raises(patch.ShapeMismatch, match="TRAITS"):
        patch.verify_shape()


def test_verify_shape_without_class():
    fake = types.SimpleNamespace(TRAITS=[])
    with pytest.raises(patch.ShapeMismatch, match="not found"):
        patch.verify_shape(fake)


async def test_apply_is_idempotent_and_removable(hass: HomeAssistant):
    cls = ga_trait.CameraStreamTrait
    orig_sync, orig_execute = cls.__dict__["sync_attributes"], cls.__dict__["execute"]
    handle = patch.apply(hass)
    assert handle.active
    assert cls.__dict__["sync_attributes"] is handle.wrapper_sync
    assert cls.sync_attributes.__wrapped__ is orig_sync
    assert patch.apply(hass) is handle  # second apply reuses
    assert patch.verify_shape() is cls  # shape check sees through our wrapper
    assert patch.remove(handle)
    assert cls.__dict__["sync_attributes"] is orig_sync
    assert cls.__dict__["execute"] is orig_execute
    assert not handle.active


async def test_remove_leaves_foreign_wrapper(hass: HomeAssistant, caplog):
    cls = ga_trait.CameraStreamTrait
    handle = patch.apply(hass)
    ours = cls.sync_attributes

    def foreign(self):
        return ours(self)

    cls.sync_attributes = foreign
    assert not patch.remove(handle)
    assert cls.__dict__["sync_attributes"] is foreign
    assert "wrapped again" in caplog.text


async def test_wrappers_are_transparent_without_runtime(hass: HomeAssistant):
    """With no runtime in hass.data the wrappers behave exactly like stock."""
    patch.apply(hass)
    hass.states.async_set("camera.x", "idle", {"supported_features": 2})
    trt = ga_trait.CameraStreamTrait(hass, hass.states.get("camera.x"), None)
    assert trt.sync_attributes() == {
        "cameraStreamSupportedProtocols": ["hls"],
        "cameraStreamNeedAuthToken": False,
        "cameraStreamNeedDrmEncryption": False,
    }
