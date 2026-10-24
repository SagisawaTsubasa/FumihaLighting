"""Lux zone flow mixin (carried over from lux_balance's config flow).

Public step ids are prefixed ``lux_`` to share the merged translation
namespace; entry data gains ``kind="lux"`` so the merged integration can
dispatch setup, options and reconfigure.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from ..const import CONF_KIND, KIND_LUX
from ..light_registry import lux_light_owners
from .const import (
    CONF_AUTO_TURN_OFF,
    CONF_CAL_STEP_PCT,
    CONF_CROSS_FADE,
    CONF_DEADBAND_LUX,
    CONF_DEADBAND_PCT,
    CONF_LIGHT_ENTITY,
    CONF_LINK_GROUP,
    CONF_LUX_ENTITY,
    CONF_MANUAL_SLOPE,
    CONF_MAX_STEP_PCT,
    CONF_MIN_BRIGHTNESS_PCT,
    CONF_SCAN_INTERVAL_S,
    CONF_TARGET_LUX,
    CONF_ZONE_NAME,
    DOMAIN,
    merged_options,
)
from .store import ZoneStorage

# Color modes that imply the light can dim (anything but plain on/off).
_BRIGHTNESS_MODES = {
    "brightness",
    "color_temp",
    "hs",
    "rgb",
    "rgbw",
    "rgbww",
    "xy",
    "white",
}


def _user_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_ZONE_NAME): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
            ),
            vol.Required(CONF_LIGHT_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="light")
            ),
            vol.Required(CONF_LUX_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor", device_class="illuminance"
                )
            ),
        }
    )


def _validate_binding(
    hass: HomeAssistant, user_input: dict[str, Any] | None,
    exclude_entry_id: str | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Validate the light/lux binding shared by the user and reconfigure steps.

    V1.7.0 A2 查重：候选灯已属其他恒照度区域（命中且非自身条目）→
    light_in_other_zone 错误 + zone_name 占位（「同一盏灯换一个传感器即可
    再建 zone」的互抢在入口拒绝——查重防的是「重复控制」而非「重复配置」）。
    ANL-F-135：查重数据源=注册表配置真值（lux_light_owners），不是运行时
    反查表——旧区被禁用或 lux 处于 SETUP_RETRY 时不在运行时桶里，按桶判
    会放行同灯再建 zone，旧区恢复后双 ACTIVE 闭环互抢且配置期零提示。
    返回 (errors, description_placeholders)。
    """
    if user_input is None:
        return {}, {}
    light_entity: str = user_input[CONF_LIGHT_ENTITY]
    registry_entry = er.async_get(hass).async_get(light_entity)
    state = hass.states.get(light_entity)
    if registry_entry is None and state is None:
        return {"base": "entity_missing"}, {}
    if registry_entry is not None and registry_entry.platform == DOMAIN:
        return {"base": "light_is_virtual"}, {}
    owner = lux_light_owners(hass, exclude_entry_id=exclude_entry_id).get(
        light_entity
    )
    if owner is not None:
        return (
            {"base": "light_in_other_zone"},
            {"zone_name": owner[1]},
        )
    # BLE 灯经常离线：只有在线时才做亮度能力校验，离线不拦配置
    online = state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
    if online and not _BRIGHTNESS_MODES & set(
        state.attributes.get("supported_color_modes") or []
    ):
        return {"base": "light_no_brightness"}, {}
    return {}, {}


class LuxZoneFlowMixin:
    """Lux-zone steps mixed into the merged config flow."""

    VERSION = 1

    async def async_step_lux_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors, placeholders = _validate_binding(self.hass, user_input)
        if user_input is not None and not errors:
            light_entity: str = user_input[CONF_LIGHT_ENTITY]
            lux_entity: str = user_input[CONF_LUX_ENTITY]
            await self.async_set_unique_id(f"lux:{light_entity}::{lux_entity}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=str(user_input[CONF_ZONE_NAME]),
                data={CONF_KIND: KIND_LUX, **user_input},
            )
        return self.async_show_form(
            step_id="lux_zone",
            data_schema=self.add_suggested_values_to_schema(_user_schema(), user_input),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_lux_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Re-bind the zone's light/lux sensor or rename it."""
        entry = self._get_reconfigure_entry()
        # A2：改绑查重排除自身条目（纯改名/换传感器不触发跨区 400）
        errors, placeholders = _validate_binding(
            self.hass, user_input, exclude_entry_id=entry.entry_id
        )
        if user_input is not None and not errors:
            light_entity: str = user_input[CONF_LIGHT_ENTITY]
            lux_entity: str = user_input[CONF_LUX_ENTITY]
            new_unique_id = f"lux:{light_entity}::{lux_entity}"
            await self.async_set_unique_id(new_unique_id, raise_on_progress=False)
            if entry.unique_id != self.unique_id:
                # 组合真的变化时才查重；未变的纯改名会命中条目自身
                self._abort_if_unique_id_configured(error="unique_id_mismatch")
            if light_entity != entry.data.get(CONF_LIGHT_ENTITY) or (
                lux_entity != entry.data.get(CONF_LUX_ENTITY)
            ):
                # 校准曲线与旧灯/传感器绑定，改绑后不再对应——清除并回落
                # 到手动系数（改绑后需重新校准）
                old_light = entry.data.get(CONF_LIGHT_ENTITY)
                if light_entity != old_light and old_light:
                    # R1 发现 14：他区 store 里以旧灯实体为键的交叉曲线
                    # 成永久孤儿（键 miss + 同 entity_id 换灯具会静默复活
                    # 旧曲线）——改绑时逐区回收（内存+落盘，幂等）。
                    # ANL-F-135：回收面按注册表补全——桶循环只覆盖已加载
                    # 区，disabled/SETUP_RETRY 区 store 里的旧键原样留着；
                    # store 与 runtime 无关，未加载区直接构造 ZoneStorage
                    # 落盘回收（已加载区由上方 runtime 循环处理，此处经
                    # `in bucket` 跳过不重复 await）。
                    bucket = self.hass.data.get(DOMAIN, {}).get("lux", {})
                    for other in bucket.values():
                        if other.entry_id != entry.entry_id:
                            await other.async_drop_cross_curve(old_light)
                    for other_entry in self.hass.config_entries.async_entries(
                        DOMAIN
                    ):
                        if (
                            other_entry.data.get(CONF_KIND) != KIND_LUX
                            or other_entry.entry_id == entry.entry_id
                            or other_entry.entry_id in bucket
                        ):
                            continue
                        await ZoneStorage(
                            self.hass, other_entry.entry_id
                        ).async_remove_cross_curve(old_light)
                await ZoneStorage(self.hass, entry.entry_id).async_remove()
            return self.async_update_reload_and_abort(
                entry,
                title=str(user_input[CONF_ZONE_NAME]),
                unique_id=self.unique_id,
                data={CONF_KIND: KIND_LUX, **user_input},
            )
        return self.async_show_form(
            step_id="lux_reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _user_schema(), user_input or entry.data
            ),
            errors=errors,
            description_placeholders=placeholders,
        )


class LuxZoneOptionsFlow(config_entries.OptionsFlow):
    """Runtime-tunable options for one zone, split into two pages."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """HA options-flow entry point: init_step 固定为 "init"（HA 侧常量，
        缺本步真机打开即 UnknownStep——合并改名时的回归，此处修复）。"""
        return await self.async_step_lux_init(user_input)

    async def async_step_lux_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """闭环调节：扫描间隔 / 死区 / 最小亮度 / 自动关灯。"""
        current = merged_options(self.config_entry.options)
        if user_input is not None:
            self._cache = {**current, **user_input}
            return await self.async_step_lux_advanced()
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL_S): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=10, max=300, step=5, unit_of_measurement="s"
                    )
                ),
                vol.Required(CONF_DEADBAND_PCT): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=20, step=1, unit_of_measurement="%"
                    )
                ),
                vol.Required(CONF_DEADBAND_LUX): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=100,
                        step=1,
                        unit_of_measurement="lx",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(CONF_MIN_BRIGHTNESS_PCT): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=50, step=1, unit_of_measurement="%"
                    )
                ),
                vol.Required(CONF_AUTO_TURN_OFF): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="lux_init",
            data_schema=self.add_suggested_values_to_schema(schema, current),
        )

    async def async_step_lux_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """校准与降级：校准步长 / 手动斜率。"""
        if user_input is not None:
            self._cache.update(user_input)
            # R1 发现 9：link_group 写入侧 strip 归一——TextSelector 原样
            # 返回用户输入，「卫生间 」与「卫生间」尾空格静默不同组（三处
            # 精确 == 比较都看不见空格）；写入侧一次性归一（存量在下次保存
            # 时归一），运行时读取侧 runtime.__init__ 再 strip 一道兜底存量
            if CONF_LINK_GROUP in self._cache:
                self._cache[CONF_LINK_GROUP] = str(
                    self._cache.get(CONF_LINK_GROUP) or ""
                ).strip()
            data = {
                key: value
                for key, value in self._cache.items()
                if key != CONF_TARGET_LUX  # 目标照度由 number 实体唯一持有
            }
            return self.async_create_entry(title="", data=data)
        schema = vol.Schema(
            {
                vol.Required(CONF_CAL_STEP_PCT): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=2, max=20, step=1, unit_of_measurement="%"
                    )
                ),
                vol.Required(CONF_MANUAL_SLOPE): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=10,
                        step=0.1,
                        unit_of_measurement="lx/%",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                # V1.3.0 联动组：同标签条目互认，ambient 扣除组内其他灯贡献
                vol.Optional(CONF_LINK_GROUP): selector.TextSelector(),
                vol.Required(CONF_CROSS_FADE): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.1,
                        max=1.0,
                        step=0.05,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(CONF_MAX_STEP_PCT): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5, max=50, step=5, unit_of_measurement="%"
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="lux_advanced",
            data_schema=self.add_suggested_values_to_schema(schema, self._cache),
        )
