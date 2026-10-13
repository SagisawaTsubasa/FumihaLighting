"""Sensor platform: runtime status with diagnostic attributes."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .runtime import ZoneRuntime

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: ZoneRuntime = hass.data[DOMAIN]["lux"][entry.entry_id]
    async_add_entities([ZoneStatusSensor(runtime)])


class ZoneStatusSensor(SensorEntity):
    """idle / active / calibrating / passthrough, plus diagnostics attributes."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:information-outline"

    def __init__(self, runtime: ZoneRuntime) -> None:
        self._runtime = runtime
        self._attr_unique_id = f"{runtime.entry_id}-status"
        self._attr_device_info = runtime.device_info

    @property
    def native_value(self) -> str:
        return self._runtime.mode

    @property
    def extra_state_attributes(self) -> dict:
        return self._runtime.diagnostics()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._runtime.signal, self._handle_update
            )
        )
        self.async_write_ha_state()

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
