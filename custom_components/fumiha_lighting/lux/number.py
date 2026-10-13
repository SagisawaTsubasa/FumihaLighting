"""Number platform: the target illuminance setpoint."""

from __future__ import annotations

import logging

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import LIGHT_LUX, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, TARGET_MAX_LUX, TARGET_MIN_LUX, TARGET_STEP_LUX
from .runtime import ZoneRuntime

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: ZoneRuntime = hass.data[DOMAIN]["lux"][entry.entry_id]
    async_add_entities([TargetLuxNumber(runtime)])


class TargetLuxNumber(NumberEntity, RestoreEntity):
    """Target illuminance at the sensor, adjustable and automation-friendly."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "target_illuminance"
    _attr_native_min_value = TARGET_MIN_LUX
    _attr_native_max_value = TARGET_MAX_LUX
    _attr_native_step = TARGET_STEP_LUX
    _attr_native_unit_of_measurement = LIGHT_LUX
    _attr_device_class = NumberDeviceClass.ILLUMINANCE
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:brightness-percent"

    def __init__(self, runtime: ZoneRuntime) -> None:
        self._runtime = runtime
        self._attr_unique_id = f"{runtime.entry_id}-target"
        self._attr_device_info = runtime.device_info

    @property
    def native_value(self) -> float:
        return self._runtime.target_lux

    async def async_set_native_value(self, value: float) -> None:
        self._runtime.set_target(value)
        self._runtime.request_evaluate()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        # The number entity is the single source of truth for the target:
        # restore the last value across restarts (options no longer override).
        last = await self.async_get_last_state()
        if last is not None and last.state not in (
            STATE_UNKNOWN,
            STATE_UNAVAILABLE,
        ):
            try:
                self._runtime.set_target(float(last.state))
            except ValueError:
                _LOGGER.warning(
                    "%s: 恢复目标照度失败（last=%s）",
                    self._runtime.zone_name,
                    last.state,
                )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._runtime.signal, self._handle_update
            )
        )
        self.async_write_ha_state()

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
