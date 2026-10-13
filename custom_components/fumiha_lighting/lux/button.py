"""Button platform: kick off the automatic calibration sweep."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .runtime import ZoneRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: ZoneRuntime = hass.data[DOMAIN]["lux"][entry.entry_id]
    async_add_entities([CalibrateButton(runtime)])


class CalibrateButton(ButtonEntity):
    """Press to start the 0→100→0 sweep; takes about 15~25 minutes."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "calibrate"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:tune-vertical"

    def __init__(self, runtime: ZoneRuntime) -> None:
        self._runtime = runtime
        self._attr_unique_id = f"{runtime.entry_id}-calibrate"
        self._attr_device_info = runtime.device_info

    async def async_press(self) -> None:
        if not self._runtime.start_calibration():
            # 抛错让 UI 弹 toast，而不是静默写日志
            raise HomeAssistantError(
                f"{self._runtime.zone_name}: 校准已在进行中，等当前校准完成后再试"
            )

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
