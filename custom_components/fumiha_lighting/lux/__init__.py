"""Constant-illuminance zone kind: setup/unload wrappers + platforms."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS
from .runtime import ZoneRuntime

_LOGGER = logging.getLogger(__name__)


async def lux_async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one constant-illuminance zone into the merged data layout."""
    hass.data.setdefault(DOMAIN, {})
    runtime = ZoneRuntime(hass, entry)
    hass.data[DOMAIN].setdefault("lux", {})[entry.entry_id] = runtime
    await runtime.async_load()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    runtime.start()
    return True


async def lux_async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload one zone: stop listeners, timers and the calibration task."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    data = hass.data.get(DOMAIN, {}).get("lux", {})
    runtime: ZoneRuntime | None = data.pop(entry.entry_id, None)
    if runtime is not None:
        await runtime.async_stop()
    return unload_ok
