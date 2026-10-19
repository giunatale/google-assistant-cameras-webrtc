"""Extension of Home Assistant's Google Assistant ``CameraStreamTrait``.

Home Assistant offers no hook for this, so the two trait methods are wrapped
on the class (the ``cloud`` integration uses the same class, so this covers
Nabu Casa as well as a self-hosted project). The wrappers always keep a
reference to the originals and fall back to them on any error, so the worst
case is stock behaviour.

Wrapping the methods (instead of swapping the class in ``trait.TRAITS``) is
deliberate: ``google_assistant.helpers.supported_traits_for_state`` caches the
trait classes per ``State`` object, so a swapped class would not be used for
cameras whose state has not changed since the cache was filled.
"""

from __future__ import annotations

import functools
import inspect
import logging
from types import ModuleType
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .const import (
    ATTR_SUPPORTED_PROTOCOLS,
    COMMAND_GET_CAMERA_STREAM,
    DATA_RUNTIME,
    PROTOCOL_WEBRTC,
    TRAIT_CAMERA_STREAM,
)

if TYPE_CHECKING:
    from .runtime import Runtime

_LOGGER = logging.getLogger(__name__)

_MARKER = "_google_assistant_cameras_webrtc_patch"


class ShapeMismatch(Exception):
    """HA's trait does not look like what this integration expects."""


class CoreHasWebRTC(Exception):
    """HA already advertises WebRTC itself; this integration is obsolete."""


class PatchHandle:
    """What was patched, so it can be undone."""

    def __init__(self, cls: type, orig_sync: Any, orig_execute: Any) -> None:
        """Store the originals."""
        self.cls = cls
        self.orig_sync = orig_sync
        self.orig_execute = orig_execute
        self.wrapper_sync: Any = None
        self.wrapper_execute: Any = None

    @property
    def active(self) -> bool:
        """Return True while our wrappers are the current methods."""
        return (
            self.cls.__dict__.get("sync_attributes") is self.wrapper_sync
            and self.cls.__dict__.get("execute") is self.wrapper_execute
        )


def _import_trait_module() -> ModuleType:
    from homeassistant.components.google_assistant import (
        trait as ga_trait,
    )

    return ga_trait


def verify_shape(ga_trait: ModuleType | None = None) -> type:
    """Return HA's CameraStreamTrait after checking everything we rely on.

    Raises ShapeMismatch when the class changed, CoreHasWebRTC when HA itself
    already advertises WebRTC.
    """
    if ga_trait is None:
        ga_trait = _import_trait_module()
    cls = getattr(ga_trait, "CameraStreamTrait", None)
    if not inspect.isclass(cls):
        raise ShapeMismatch("CameraStreamTrait class not found")
    if getattr(cls, "name", None) != TRAIT_CAMERA_STREAM:
        raise ShapeMismatch("trait name changed")
    if COMMAND_GET_CAMERA_STREAM not in (getattr(cls, "commands", None) or ()):
        raise ShapeMismatch("GetCameraStream command missing")
    traits = getattr(ga_trait, "TRAITS", None)
    if not isinstance(traits, list) or cls not in traits:
        raise ShapeMismatch("trait not registered in TRAITS")
    if not hasattr(cls, "stream_info"):
        raise ShapeMismatch("stream_info attribute missing")

    sync = _unwrapped(cls.__dict__.get("sync_attributes"))
    execute = _unwrapped(cls.__dict__.get("execute"))
    query = cls.__dict__.get("query_attributes")
    if not callable(sync) or not callable(execute) or not callable(query):
        raise ShapeMismatch("trait methods missing")
    if _params(sync) != ["self"]:
        raise ShapeMismatch("sync_attributes signature changed")
    if _params(execute) != ["self", "command", "data", "params", "challenge"]:
        raise ShapeMismatch("execute signature changed")
    if not inspect.iscoroutinefunction(execute):
        raise ShapeMismatch("execute is not a coroutine")

    # Probe the stock SYNC attributes. The stock implementation uses nothing
    # from the instance; if a future one does, the probe is skipped.
    try:
        attrs = sync(cls.__new__(cls))
    except Exception:
        attrs = None
    if isinstance(attrs, dict):
        protocols = attrs.get(ATTR_SUPPORTED_PROTOCOLS)
        if not isinstance(protocols, list):
            raise ShapeMismatch("cameraStreamSupportedProtocols is not a list")
        if PROTOCOL_WEBRTC in protocols:
            raise CoreHasWebRTC
    return cls


def _unwrapped(func: Any) -> Any:
    """Return the function underneath our own wrapper (if it is ours)."""
    while (
        func is not None
        and getattr(func, "__wrapped__", None) is not None
        and getattr(func, _MARKER, False)
    ):
        func = func.__wrapped__
    return func


def _params(func: Any) -> list[str]:
    try:
        return list(inspect.signature(func).parameters)
    except TypeError, ValueError:
        return []


def _runtime(hass: HomeAssistant | None) -> Runtime | None:
    if hass is None:
        return None
    runtime = hass.data.get(DATA_RUNTIME)
    if runtime is None or not getattr(runtime, "active", False):
        return None
    return runtime


def apply(hass: HomeAssistant) -> PatchHandle:
    """Wrap the trait methods. Idempotent: returns the existing handle."""
    ga_trait = _import_trait_module()
    cls = verify_shape(ga_trait)
    existing: PatchHandle | None = cls.__dict__.get(_MARKER)
    if existing is not None and existing.active:
        _LOGGER.debug("Trait already patched; reusing the existing wrappers")
        return existing

    orig_sync = cls.__dict__["sync_attributes"]
    orig_execute = cls.__dict__["execute"]
    handle = PatchHandle(cls, orig_sync, orig_execute)

    @functools.wraps(orig_sync)
    def sync_attributes(self: Any) -> dict[str, Any]:
        attrs = orig_sync(self)
        runtime = _runtime(getattr(self, "hass", None))
        if runtime is None:
            return attrs
        try:
            return runtime.decorate_sync_attributes(self.state.entity_id, attrs)
        except Exception:
            _LOGGER.exception(
                "Error extending SYNC attributes for %s; using stock attributes",
                getattr(getattr(self, "state", None), "entity_id", "?"),
            )
            return attrs

    @functools.wraps(orig_execute)
    async def execute(
        self: Any, command: str, data: Any, params: Any, challenge: Any
    ) -> None:
        runtime = _runtime(getattr(self, "hass", None))
        entity_id = getattr(getattr(self, "state", None), "entity_id", "?")
        if runtime is not None and command == COMMAND_GET_CAMERA_STREAM:
            try:
                info = runtime.webrtc_execute(entity_id, params)
            except Exception:
                _LOGGER.exception(
                    "Error preparing WebRTC for %s; using stock HLS", entity_id
                )
                info = None
            if info is not None:
                self.stream_info = info
                return
        await orig_execute(self, command, data, params, challenge)
        if runtime is not None and command == COMMAND_GET_CAMERA_STREAM:
            try:
                self.stream_info = runtime.decorate_hls_result(
                    entity_id, params, self.stream_info
                )
            except Exception:
                _LOGGER.exception("Error decorating HLS result for %s", entity_id)

    setattr(sync_attributes, _MARKER, True)
    setattr(execute, _MARKER, True)
    handle.wrapper_sync = sync_attributes
    handle.wrapper_execute = execute
    cls.sync_attributes = sync_attributes  # type: ignore[method-assign]
    cls.execute = execute  # type: ignore[method-assign]
    setattr(cls, _MARKER, handle)
    return handle


def remove(handle: PatchHandle) -> bool:
    """Restore the original methods if ours are still in place.

    Returns False (and leaves everything as is) when something else wrapped
    the methods after us; our wrappers then become transparent because the
    runtime is gone.
    """
    cls = handle.cls
    if not handle.active:
        _LOGGER.warning(
            "Google Assistant camera trait was wrapped again after this "
            "integration; leaving the chain intact (our wrappers are inert now)"
        )
        return False
    cls.sync_attributes = handle.orig_sync  # type: ignore[method-assign]
    cls.execute = handle.orig_execute  # type: ignore[method-assign]
    if cls.__dict__.get(_MARKER) is handle:
        delattr(cls, _MARKER)
    return True
