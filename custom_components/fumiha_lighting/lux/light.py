"""Virtual light platform: on/off/brightness translated to the real light."""

from __future__ import annotations

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
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
    async_add_entities([LuxBalanceLight(runtime)])


class LuxBalanceLight(LightEntity):
    """Mirror of the real light whose brightness is curve-compensated."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "adaptive_light"
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_icon = "mdi:lightbulb-auto"

    def __init__(self, runtime: ZoneRuntime) -> None:
        self._runtime = runtime
        self._attr_unique_id = f"{runtime.entry_id}-light"
        self._attr_device_info = runtime.device_info

    @property
    def available(self) -> bool:
        # 虚拟灯是控制接口：条目在即保持可用，自动化不因真灯瞬断而断；
        # 真灯离线反映在 real_light 属性与运行状态里（runtime 的 stale 逻辑）
        return True

    @property
    def is_on(self) -> bool | None:
        state = self.hass.states.get(self._runtime.light_entity)
        if state is None:
            return None
        return state.state == STATE_ON

    @property
    def brightness(self) -> int | None:
        state = self.hass.states.get(self._runtime.light_entity)
        real = state.attributes.get(ATTR_BRIGHTNESS) if state else None
        if isinstance(real, (int, float)) and real > 0:
            return int(real)
        pct = self._runtime.commanded_pct
        return int(round(pct / 100.0 * 255)) if pct else None

    @property
    def extra_state_attributes(self) -> dict:
        real = self.hass.states.get(self._runtime.light_entity)
        return {
            "mode": self._runtime.mode,
            "commanded_pct": self._runtime.commanded_pct,
            "real_light": real.state if real else "missing",
        }

    async def async_turn_on(self, **kwargs) -> None:
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        pct = None if brightness is None else brightness / 255.0 * 100.0
        await self._runtime.async_turn_on_requested(pct)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self._runtime.async_turn_off_requested()
        self.async_write_ha_state()

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
