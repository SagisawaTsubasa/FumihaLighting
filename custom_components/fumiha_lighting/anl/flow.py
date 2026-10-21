"""Anl group flow mixin — three-step wizard + options flow.

向导 3 步：灯具选择 → 时间锚点（含过渡）→ 全局参数。逐灯覆盖与额外
时段**不在向导里**——它们由夜灯助手页面的 GUI 设置弹窗经
/api/fumiha_lighting/anl/config 读写（唯一编辑入口），向导产物不含
extras/overrides（manager 对这些键按缺省值消费，GUI 后补）。

reconfigure/options 保存时把条目里 GUI 管理的键（extras/extra_count/
overrides）携带过去，避免向导保存把 GUI 改动冲掉；overrides 同时按当前
灯集裁剪，不留死键。CI 停用或改指时在创建/更新条目之前 best-effort
归还旧虚拟灯（F-057；被其他 anl 条目引用时跳过——F-067）；CI 开而虚拟
灯为空在参数步就地拦截（F-061）；CI 开而日间模式关在参数步就地拦截
（F-065）；灯集里混入本集成的虚拟恒照度灯在灯具步就地拦截（F-064）。

Entry data gains ``kind="anl"``; unique_id is ``anl:<lights>``，reconfigure
改灯时同步更新并查重（lux 同款）。
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TimeSelector,
)

from ..const import CONF_KIND, KIND_ANL
from .config_schema import (
    PARAMS_NUMERIC,
    TIMES_ENUM,
    TIMES_NUMERIC,
    TIMES_TIME,
    normalize_time,
)
from .const import (
    ANCHOR_SUNRISE,
    ANCHOR_SUNSET,
    CONF_BRIGHTNESS,
    CONF_COLOR_TEMP_KELVIN,
    CONF_DAY_BRIGHTNESS,
    CONF_DAY_CI_ENABLED,
    CONF_DAY_CI_VIRTUAL_LIGHT,
    CONF_DAY_COLOR_TEMP_KELVIN,
    CONF_DAY_ENABLED,
    CONF_END_TIME,
    CONF_END_TRANSITION,
    CONF_EXTRAS,
    CONF_LIGHTS,
    CONF_ONLY_WHEN_ON,
    CONF_OVERRIDES,
    CONF_RESPECT_MANUAL,
    CONF_SETTLE_DELAY,
    CONF_START_MODE,
    CONF_START_TRANSITION,
    CONF_SUN_ENTITY,
    CONF_TOLERANCE_BRIGHTNESS,
    CONF_TOLERANCE_KELVIN,
    CONF_TRANSITION_ENABLED,
    CONF_TRIGGER_TIME,
    CONF_TURN_ON_LISTEN,
    CONF_VERIFY_DELAY,
    DEFAULT_SUN_ENTITY,
    DOMAIN,
)
from .manager import async_release_virtual_light

_LOGGER = logging.getLogger(__name__)

# 向导保存时原样携带的 GUI 管理键（防 reconfigure 清 options 丢数据）。
# custom_per_light/extra_count 不在内：manager 用 len(self.extras) 自算数量，
# 两者均为零消费死键（ANL-F-022/029），旧条目残留值不再向 options 固化。
_GUI_MANAGED_KEYS = (
    CONF_EXTRAS,
    CONF_OVERRIDES,
)

# 向导只建 times/params 字段（extras 移入 GUI 编辑器），无需 extras 规格
_UNIT_OF = {
    key: spec[4]
    for key, spec in (*TIMES_NUMERIC.items(), *PARAMS_NUMERIC.items())
}


def _num_field(key: str, spec_by_key: dict, defaults: dict) -> tuple:
    """vol marker + NumberSelector from the shared spec (single source)."""
    lo, hi, step, default, _unit = spec_by_key[key]
    return vol.Required(key, default=defaults.get(key, default)), NumberSelector(
        NumberSelectorConfig(
            min=lo,
            max=hi,
            step=step,
            unit_of_measurement=_UNIT_OF[key],
            mode=NumberSelectorMode.SLIDER,
        )
    )


def _anchor_select(key: str, defaults: dict) -> tuple:
    default = defaults.get(
        key, ANCHOR_SUNSET if key == CONF_START_MODE else ANCHOR_SUNRISE
    )
    return vol.Required(key, default=default), SelectSelector(
        SelectSelectorConfig(
            options=list(TIMES_ENUM[key]),
            mode=SelectSelectorMode.DROPDOWN,
            translation_key="anchor_mode",
        )
    )


def _add_period_fields(schema: dict, b_key: str, k_key: str, defaults: dict) -> None:
    """亮度/色温一对字段（夜间或日间）。"""
    for key in (b_key, k_key):
        marker, sel = _num_field(key, PARAMS_NUMERIC, defaults)
        schema[marker] = sel


def _lights_schema(defaults: dict) -> vol.Schema:
    """Step 1: light selection."""
    return vol.Schema(
        {
            vol.Required(
                CONF_LIGHTS, default=defaults.get(CONF_LIGHTS, [])
            ): EntitySelector(EntitySelectorConfig(domain="light", multiple=True)),
        }
    )


def _time_details_schema(defaults: dict) -> vol.Schema:
    """Step 2: anchors + fixed times + offsets + sun entity + transitions.

    固定时间与偏移同页展示：mode=fixed 用固定时间字段，sunset/sunrise 用
    偏移滑条，无关侧由 manager 忽略（消费端已核对）。
    """
    schema: dict = {}
    for key in TIMES_ENUM:
        marker, sel = _anchor_select(key, defaults)
        schema[marker] = sel
    for key in TIMES_TIME:
        schema[
            vol.Required(key, default=defaults.get(key, TIMES_TIME[key]))
        ] = TimeSelector()
    for key in TIMES_NUMERIC:
        marker, sel = _num_field(key, TIMES_NUMERIC, defaults)
        schema[marker] = sel
    schema[
        vol.Required(
            CONF_SUN_ENTITY, default=defaults.get(CONF_SUN_ENTITY, DEFAULT_SUN_ENTITY)
        )
    ] = EntitySelector(EntitySelectorConfig(domain=["sun", "sensor"]))
    return vol.Schema(schema)


def _settings_schema(defaults: dict) -> vol.Schema:
    """Step 3: day toggle + period params + tolerances + delays + toggles.

    日间参数字段恒显示：day_enabled 关闭时 manager 不消费（消费端已核对）。
    CI 两字段同样平铺（day_ci_enabled 关时虚拟灯实体服务端忽略）。
    """
    schema: dict = {
        vol.Required(
            CONF_DAY_ENABLED, default=defaults.get(CONF_DAY_ENABLED, False)
        ): BooleanSelector()
    }
    _add_period_fields(schema, CONF_BRIGHTNESS, CONF_COLOR_TEMP_KELVIN, defaults)
    _add_period_fields(
        schema, CONF_DAY_BRIGHTNESS, CONF_DAY_COLOR_TEMP_KELVIN, defaults
    )
    # 日间恒照度联动（V1.5.0）：向导平铺无条件显隐是原生表单的既定约束，
    # 条件显隐由夜灯助手页面 GUI 设置弹窗承担；CI 关时实体字段留空
    # （None）→ _entry_data 归一 ""，服务端按「未配置」消费。
    schema[
        vol.Required(
            CONF_DAY_CI_ENABLED, default=defaults.get(CONF_DAY_CI_ENABLED, False)
        )
    ] = BooleanSelector()
    schema[
        vol.Optional(
            CONF_DAY_CI_VIRTUAL_LIGHT,
            default=defaults.get(CONF_DAY_CI_VIRTUAL_LIGHT) or None,
        )
    ] = EntitySelector(EntitySelectorConfig(domain="light", integration=DOMAIN))
    for key in (
        CONF_TOLERANCE_BRIGHTNESS,
        CONF_TOLERANCE_KELVIN,
        CONF_VERIFY_DELAY,
        CONF_SETTLE_DELAY,
    ):
        marker, sel = _num_field(key, PARAMS_NUMERIC, defaults)
        schema[marker] = sel
    schema[
        vol.Required(
            CONF_TURN_ON_LISTEN,
            default=defaults.get(CONF_TURN_ON_LISTEN, True),
        )
    ] = BooleanSelector()
    schema[
        vol.Required(
            CONF_ONLY_WHEN_ON, default=defaults.get(CONF_ONLY_WHEN_ON, False)
        )
    ] = BooleanSelector()
    schema[
        vol.Required(
            CONF_RESPECT_MANUAL, default=defaults.get(CONF_RESPECT_MANUAL, True)
        )
    ] = BooleanSelector()
    return vol.Schema(schema)


class _FlowMixin:
    """Shared step logic for the config and options flows."""

    _times: dict
    _lights: list[str]
    _settings: dict

    def _init_state(self) -> None:
        self._times = {}
        self._lights = []
        self._settings = {}
        # 预填来源：options/reconfigure 流为当前条目数据，新建流为空
        self._defaults: dict = {}
        self._suggest: dict | None = None

    def _virtual_light_in_pick(self, lights: list[str]) -> str | None:
        """灯集里第一个本集成生成的虚拟恒照度灯（F-064）。

        注册表身份判据：platform==DOMAIN 且 domain=="light"。把自家虚拟灯
        再选进灯组会让 ANL 直控与 lux 闭环抢同一盏灯——manager 侧不做
        静默过滤（错误必须响），在灯具步就地拦截。返回 None 表示无虚拟灯。
        """
        registry = er.async_get(self.hass)
        for entity_id in lights:
            reg_entry = registry.async_get(entity_id)
            if (
                reg_entry is not None
                and reg_entry.platform == DOMAIN
                and reg_entry.domain == "light"
            ):
                return entity_id
        return None

    async def _handle_lights_step(self, user_input, step_id):
        """Step 1: pick light entities."""
        errors = {}
        if user_input is not None:
            if user_input.get(CONF_LIGHTS):
                if self._virtual_light_in_pick(user_input[CONF_LIGHTS]) is not None:
                    errors["base"] = "light_is_virtual"
                else:
                    self._lights = user_input[CONF_LIGHTS]
                    return await self._async_step_time_details()
            else:
                errors["base"] = "no_lights"
        schema = _lights_schema(self._defaults)
        if self._suggest is not None:
            schema = self.add_suggested_values_to_schema(schema, self._suggest)
        return self.async_show_form(step_id=step_id, data_schema=schema, errors=errors)

    async def _async_step_time_details(self, user_input=None):
        """Step 2: anchors + times + transitions."""
        if user_input is not None:
            self._times = user_input
            return await self._async_step_settings()
        schema = _time_details_schema(self._defaults)
        if self._suggest is not None:
            schema = self.add_suggested_values_to_schema(schema, self._suggest)
        return self.async_show_form(step_id="time_details", data_schema=schema)

    async def _async_step_settings(self, user_input=None):
        """Step 3: parameters."""
        errors: dict | None = None
        if user_input is not None:
            if user_input.get(CONF_DAY_CI_ENABLED) and not user_input.get(
                CONF_DAY_ENABLED
            ):
                # F-065：CI 是日间子功能——日间模式关时 CI 完全不生效
                #（运行时零虚拟灯调用、快照不报接管），向导入口就拦不落盘
                errors = {"base": "day_ci_needs_day_enabled"}
            elif user_input.get(CONF_DAY_CI_ENABLED) and not (
                user_input.get(CONF_DAY_CI_VIRTUAL_LIGHT) or ""
            ):
                # F-061：CI 开但虚拟灯为空——静默放行会让页面谎报恒照度
                # 已接管；就地拦截重显表单，不落 _settings
                errors = {"base": "day_ci_needs_virtual_light"}
            else:
                self._settings = user_input
                return await self._finish()
        schema = _settings_schema(self._defaults)
        if self._suggest is not None:
            schema = self.add_suggested_values_to_schema(schema, self._suggest)
        return self.async_show_form(
            step_id="settings", data_schema=schema, errors=errors
        )

    def _entry_data(self, include_lights: bool = True) -> dict:
        # transition_enabled 恒等派生：任一过渡时长 > 0（与 manager 门控语义一致）
        transitions_on = bool(
            self._times.get(CONF_START_TRANSITION, 0)
            or self._times.get(CONF_END_TRANSITION, 0)
        )
        times = dict(self._times)
        # TimeSelector 提交值是 datetime.time（HA 公开契约）——归一化为
        # "HH:MM:SS" 字符串存储形状，防 GET /anl/config 序列化 500（ANL-F-015）
        for key in (CONF_TRIGGER_TIME, CONF_END_TIME):
            if key in times:
                normalized = normalize_time(times[key])
                if normalized is not None:
                    times[key] = normalized
        data = {
            **times,
            CONF_TRANSITION_ENABLED: transitions_on,
            **self._settings,
        }
        # CI 实体字段存储形状归一：EntitySelector Optional 留空提交 None，
        # 统一落 ""（未配置），防 None 入库
        if data.get(CONF_DAY_CI_VIRTUAL_LIGHT) is None:
            data[CONF_DAY_CI_VIRTUAL_LIGHT] = ""
        if include_lights:
            data[CONF_LIGHTS] = self._lights
        # include_lights=False：options 流没有灯具步，_lights 恒空——不写
        # lights 键，防空列表镜像进 options 覆盖 data 的灯表（manager 变空）
        return data

    async def _async_release_stale_virtual(
        self, old: dict, new: dict, exclude_entry_id: str | None = None
    ) -> None:
        """CI 停用或改指时归还旧虚拟灯（F-057 双闭环互纠）。

        旧配置 CI 开且配了灯、新配置关闭或改指 → best-effort turn_off 旧灯
        （lux off=纯放手，对真实灯零下发）；失败 WARNING 不阻断保存。只在
        创建/更新条目之前调用（reconfigure/options 的 _finish）；新建流没有
        旧条目，不调。reload/卸载路径不归还——正常 reload 会把 CI 关掉、
        日间闭环还在，归还只属于「配置真的放弃该灯」的边界。
        F-067：归还前跨条目检查——其他 anl 条目仍引用同一虚拟灯时跳过
        turn_off（关掉会静默终止别组的日间闭环），INFO 可观测。排除自身
        条目：reconfigure 流传被改条目 id，options 流传当前条目 id。
        F-083：归还动作统一走 async_release_virtual_light（lux mode 守卫 +
        跨条目检查 + best-effort turn_off，anl/manager.py 单一来源）。
        """
        old_virtual = old.get(CONF_DAY_CI_VIRTUAL_LIGHT) or ""
        if not (old.get(CONF_DAY_CI_ENABLED) and old_virtual):
            return
        if new.get(CONF_DAY_CI_ENABLED) and (
            new.get(CONF_DAY_CI_VIRTUAL_LIGHT) or ""
        ) == old_virtual:
            return
        # F-090：配置侧挂点启用 calibrating 有界延迟归还
        await async_release_virtual_light(
            self.hass,
            old_virtual,
            exclude_entry_id=exclude_entry_id,
            retry_on_calibrating=True,
        )

    def _carry_gui_keys(self, current: dict, lights=None) -> dict:
        """GUI 管理键携带（reconfigure 收敛 data / options 全镜像都需要）。

        overrides 按灯集裁剪：灯具被移除后其覆盖一并丢弃，不留死键
        （ANL-F-017 灯集面）。lights 缺省取向导灯集（reconfigure 改灯场景）；
        options 流没有灯具步，须显式传条目现有灯集。
        """
        carry = {k: current[k] for k in _GUI_MANAGED_KEYS if k in current}
        overrides = carry.get(CONF_OVERRIDES)
        if isinstance(overrides, dict):
            keep = set(lights if lights is not None else self._lights)
            carry[CONF_OVERRIDES] = {
                k: v for k, v in overrides.items() if k in keep
            }
        return carry


class AnlGroupFlowMixin(_FlowMixin):
    """Handle the initial config flow."""

    def __init__(self) -> None:
        """Store intermediate data between steps."""
        self._init_state()

    async def async_step_anl_user(self, user_input=None) -> ConfigFlowResult:
        """Step 1: select lights."""
        return await self._handle_lights_step(user_input, "anl_user")

    async def async_step_anl_reconfigure(self, user_input=None) -> ConfigFlowResult:
        """Reconfigure: same three pages, prefilled from the entry."""
        if self._suggest is None:
            entry = self._get_reconfigure_entry()
            current = {**entry.data, **entry.options}
            self._defaults = current
            self._suggest = current
        return await self._handle_lights_step(user_input, "anl_reconfigure")

    async def async_step_time_details(self, user_input=None) -> ConfigFlowResult:
        return await self._async_step_time_details(user_input)

    async def async_step_settings(self, user_input=None) -> ConfigFlowResult:
        return await self._async_step_settings(user_input)

    async def _finish(self) -> ConfigFlowResult:
        if self.source == SOURCE_RECONFIGURE:
            entry = self._get_reconfigure_entry()
            current = {**entry.data, **entry.options}
            # unique_id 绑定灯集：灯具变更时同步更新并查重（lux 同款三步，
            # ANL-F-016——否则旧 id 残留导致重复条目/误报 conflict）
            new_uid = "anl:" + ",".join(sorted(self._lights))
            await self.async_set_unique_id(new_uid, raise_on_progress=False)
            if entry.unique_id != self.unique_id:
                self._abort_if_unique_id_configured(error="anl_unique_id_mismatch")
            # F-057：改指/停用 CI 时归还旧虚拟灯——abort 路径之后才动灯；
            # F-067：排除自身条目做跨条目引用检查
            await self._async_release_stale_virtual(
                current, self._entry_data(), entry.entry_id
            )
            # options 清空 + GUI 管理键并入 data：全量收敛进 data，GUI 改动不丢
            return self.async_update_reload_and_abort(
                entry,
                data={
                    CONF_KIND: KIND_ANL,
                    **self._entry_data(),
                    **self._carry_gui_keys(current),
                },
                options={},
                unique_id=self.unique_id,
            )
        await self.async_set_unique_id("anl:" + ",".join(sorted(self._lights)))
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="自动夜灯", data={CONF_KIND: KIND_ANL, **self._entry_data()}
        )


class AnlGroupOptionsFlow(_FlowMixin, OptionsFlow):
    """Re-edit times and parameters; lights are NOT editable here (unique_id
    is bound to the light set — change it via 重新配置), and extras/overrides
    stay GUI-managed."""

    def __init__(self) -> None:
        """Initialize flow state; the entry is prefilled lazily."""
        self._init_state()

    @property
    def _current(self) -> dict:
        return {**self.config_entry.data, **self.config_entry.options}

    def _prefill(self) -> None:
        if self._suggest is None:
            current = self._current
            self._defaults = current
            self._suggest = current

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        """HA options-flow entry point: init_step 固定为 "init"（HA 侧常量，
        缺本步真机打开即 UnknownStep——合并时的回归，此处修复）。

        首屏直接进时间锚点：灯具不改（unique_id 绑定灯集，改灯走重新配置，
        ANL-F-016 的 options 面）。
        """
        self._prefill()
        return await self._async_step_time_options(user_input)

    async def async_step_anl_init(self, user_input=None) -> ConfigFlowResult:
        """首屏表单 step_id 是 "anl_init"，cur_step 跟随表单 step_id——
        第一次提交派发到这里而非 async_step_init（ANL-F-014）。"""
        self._prefill()
        return await self._async_step_time_options(user_input)

    async def _async_step_time_options(self, user_input=None):
        """时间锚点页（options 版）：提交后进参数页。"""
        if user_input is not None:
            self._times = user_input
            return await self._async_step_settings()
        schema = _time_details_schema(self._defaults)
        if self._suggest is not None:
            schema = self.add_suggested_values_to_schema(schema, self._suggest)
        return self.async_show_form(step_id="anl_init", data_schema=schema)

    async def async_step_settings(self, user_input=None) -> ConfigFlowResult:
        return await self._async_step_settings(user_input)

    async def _finish(self) -> ConfigFlowResult:
        # options 全镜像约定：向导键 + GUI 管理键（data 里的 extras/overrides
        # 带进 options，防旧版全镜像 options 被薄 options 替换后丢失）
        new_data = {
            CONF_KIND: KIND_ANL,
            **self._entry_data(include_lights=False),
            **self._carry_gui_keys(
                self._current, lights=self._current.get(CONF_LIGHTS, [])
            ),
        }
        # F-057：CI 停用/改指 → 先归还旧虚拟灯，再创建 options 条目；
        # F-067：排除当前条目做跨条目引用检查
        await self._async_release_stale_virtual(
            self._current, new_data, self.config_entry.entry_id
        )
        return self.async_create_entry(title="", data=new_data)
