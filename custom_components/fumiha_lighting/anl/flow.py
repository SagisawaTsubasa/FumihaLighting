"""Anl group flow mixin (carried over from auto_night_light's config flow).

Public entry steps are prefixed ``anl_``; the inner wizard step ids
(time_details / extra_count / ... ) stay unique in the merged namespace.
Entry data gains ``kind="anl"``; unique_id becomes ``anl:<lights>``.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigFlowResult,
    OptionsFlow,
)
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
    TextSelector,
    TimeSelector,
)

from ..const import CONF_KIND, KIND_ANL
from .const import (
    ANCHOR_FIXED,
    ANCHOR_MODES,
    ANCHOR_SUNRISE,
    ANCHOR_SUNSET,
    CONF_BRIGHTNESS,
    CONF_COLOR_TEMP_KELVIN,
    CONF_CUSTOM_PER_LIGHT,
    CONF_DAY_BRIGHTNESS,
    CONF_DAY_COLOR_TEMP_KELVIN,
    CONF_DAY_ENABLED,
    CONF_END_MODE,
    CONF_END_OFFSET,
    CONF_END_TIME,
    CONF_END_TRANSITION,
    CONF_EXTRA_COUNT,
    CONF_EXTRA_ENABLED,
    CONF_EXTRAS,
    CONF_LIGHTS,
    CONF_ONLY_WHEN_ON,
    CONF_OVERRIDES,
    CONF_RESPECT_MANUAL,
    CONF_SETTLE_DELAY,
    CONF_START_MODE,
    CONF_START_OFFSET,
    CONF_START_TRANSITION,
    CONF_SUN_ENTITY,
    CONF_TOLERANCE_BRIGHTNESS,
    CONF_TOLERANCE_KELVIN,
    CONF_TRANSITION_ENABLED,
    CONF_TRANSITION_INTERVAL,
    CONF_TRIGGER_TIME,
    CONF_TURN_ON_LISTEN,
    CONF_VERIFY_DELAY,
    DEFAULT_BRIGHTNESS,
    DEFAULT_COLOR_TEMP_KELVIN,
    DEFAULT_DAY_BRIGHTNESS,
    DEFAULT_DAY_COLOR_TEMP_KELVIN,
    DEFAULT_END_OFFSET,
    DEFAULT_END_TIME,
    DEFAULT_EXTRA_BRIGHTNESS,
    DEFAULT_EXTRA_COLOR_TEMP_KELVIN,
    DEFAULT_EXTRA_START,
    DEFAULT_RESPECT_MANUAL,
    DEFAULT_SETTLE_DELAY,
    DEFAULT_START_OFFSET,
    DEFAULT_SUN_ENTITY,
    DEFAULT_TOLERANCE_BRIGHTNESS,
    DEFAULT_TOLERANCE_KELVIN,
    DEFAULT_TRANSITION_INTERVAL,
    DEFAULT_TURN_ON_LISTEN,
    DEFAULT_VERIFY_DELAY,
    EXTRA_BRIGHTNESS,
    EXTRA_COLOR_TEMP_KELVIN,
    EXTRA_NAME,
    EXTRA_START,
    EXTRA_TRANSITION,
    MAX_EXTRA_PERIODS,
    MAX_TRANSITION_MINUTES,
    OVR_BRIGHTNESS,
    OVR_COLOR_TEMP_KELVIN,
    OVR_DAY_BRIGHTNESS,
    OVR_DAY_COLOR_TEMP_KELVIN,
    OVR_EXTRA_BRIGHTNESS,
    OVR_EXTRA_COLOR_TEMP_KELVIN,
    OVR_EXTRAS,
)


def _pct_selector(key: str, default: int) -> tuple:
    return vol.Required(key, default=default), NumberSelector(
        NumberSelectorConfig(
            min=1,
            max=100,
            unit_of_measurement="%",
            mode=NumberSelectorMode.SLIDER,
        )
    )


def _kelvin_selector(key: str, default: int) -> tuple:
    return vol.Required(key, default=default), NumberSelector(
        NumberSelectorConfig(
            min=1500,
            max=6500,
            step=100,
            unit_of_measurement="K",
            mode=NumberSelectorMode.SLIDER,
        )
    )


def _offset_selector(key: str, default: int) -> tuple:
    return vol.Required(key, default=default), NumberSelector(
        NumberSelectorConfig(
            min=-120,
            max=120,
            step=5,
            unit_of_measurement="min",
            mode=NumberSelectorMode.SLIDER,
        )
    )


def _add_period_fields(
    schema: dict,
    b_key: str,
    k_key: str,
    defaults: dict,
    def_b: int,
    def_k: int,
) -> None:
    """Add brightness/kelvin fields for one period to a schema dict."""
    b_marker, b_sel = _pct_selector(b_key, defaults.get(b_key, def_b))
    k_marker, k_sel = _kelvin_selector(k_key, defaults.get(k_key, def_k))
    schema[b_marker] = b_sel
    schema[k_marker] = k_sel


def _anchor_select(key: str, default: str) -> tuple:
    return vol.Required(key, default=default), SelectSelector(
        SelectSelectorConfig(
            options=ANCHOR_MODES,
            mode=SelectSelectorMode.DROPDOWN,
            translation_key="anchor_mode",
        )
    )


def _time_schema(defaults: dict) -> vol.Schema:
    """Step 1: anchor source choices and feature toggles only."""
    start_mode_marker, start_mode_sel = _anchor_select(
        CONF_START_MODE, defaults.get(CONF_START_MODE, ANCHOR_SUNSET)
    )
    end_mode_marker, end_mode_sel = _anchor_select(
        CONF_END_MODE, defaults.get(CONF_END_MODE, ANCHOR_SUNRISE)
    )
    return vol.Schema(
        {
            start_mode_marker: start_mode_sel,
            end_mode_marker: end_mode_sel,
            vol.Required(
                CONF_TRANSITION_ENABLED,
                default=defaults.get(CONF_TRANSITION_ENABLED, False),
            ): BooleanSelector(),
            vol.Required(
                CONF_DAY_ENABLED, default=defaults.get(CONF_DAY_ENABLED, False)
            ): BooleanSelector(),
            vol.Required(
                CONF_EXTRA_ENABLED,
                default=defaults.get(
                    CONF_EXTRA_ENABLED, bool(defaults.get(CONF_EXTRAS))
                ),
            ): BooleanSelector(),
        }
    )


def _transition_slider(key: str, default: int) -> tuple:
    return vol.Required(key, default=default), NumberSelector(
        NumberSelectorConfig(
            min=0,
            max=MAX_TRANSITION_MINUTES,
            step=5,
            unit_of_measurement="min",
            mode=NumberSelectorMode.SLIDER,
        )
    )


def _time_details_schema(times: dict, defaults: dict) -> vol.Schema:
    """Step 2: only the fields relevant to the choices made in step 1."""
    schema: dict = {}
    any_sun = False
    if times.get(CONF_START_MODE) == ANCHOR_FIXED:
        schema[
            vol.Required(
                CONF_TRIGGER_TIME, default=defaults.get(CONF_TRIGGER_TIME, "22:00:00")
            )
        ] = TimeSelector()
    else:
        any_sun = True
        marker, sel = _offset_selector(
            CONF_START_OFFSET, defaults.get(CONF_START_OFFSET, DEFAULT_START_OFFSET)
        )
        schema[marker] = sel
    if times.get(CONF_END_MODE) == ANCHOR_FIXED:
        schema[
            vol.Required(
                CONF_END_TIME, default=defaults.get(CONF_END_TIME, DEFAULT_END_TIME)
            )
        ] = TimeSelector()
    else:
        any_sun = True
        marker, sel = _offset_selector(
            CONF_END_OFFSET, defaults.get(CONF_END_OFFSET, DEFAULT_END_OFFSET)
        )
        schema[marker] = sel
    if any_sun:
        schema[
            vol.Required(
                CONF_SUN_ENTITY,
                default=defaults.get(CONF_SUN_ENTITY, DEFAULT_SUN_ENTITY),
            )
        ] = EntitySelector(EntitySelectorConfig(domain=["sun", "sensor"]))
    if times.get(CONF_TRANSITION_ENABLED):
        st_marker, st_sel = _transition_slider(
            CONF_START_TRANSITION, defaults.get(CONF_START_TRANSITION, 0)
        )
        schema[st_marker] = st_sel
        if times.get(CONF_DAY_ENABLED):
            et_marker, et_sel = _transition_slider(
                CONF_END_TRANSITION, defaults.get(CONF_END_TRANSITION, 0)
            )
            schema[et_marker] = et_sel
        schema[
            vol.Required(
                CONF_TRANSITION_INTERVAL,
                default=defaults.get(
                    CONF_TRANSITION_INTERVAL, DEFAULT_TRANSITION_INTERVAL
                ),
            )
        ] = NumberSelector(
            NumberSelectorConfig(
                min=1,
                max=15,
                unit_of_measurement="min",
                mode=NumberSelectorMode.SLIDER,
            )
        )
    return vol.Schema(schema)


def _extra_count_schema(defaults: dict) -> vol.Schema:
    """Extra periods: how many."""
    return vol.Schema(
        {
            vol.Required(
                CONF_EXTRA_COUNT,
                default=defaults.get(
                    CONF_EXTRA_COUNT, max(1, len(defaults.get(CONF_EXTRAS, [])))
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1, max=MAX_EXTRA_PERIODS, mode=NumberSelectorMode.SLIDER
                )
            ),
        }
    )


def _extra_period_schema(defaults: dict) -> vol.Schema:
    """One extra period: optional name, anchor start time, brightness/kelvin."""
    schema: dict = {
        vol.Optional(EXTRA_NAME, default=defaults.get(EXTRA_NAME, "")): TextSelector(),
        vol.Required(
            EXTRA_START, default=defaults.get(EXTRA_START, DEFAULT_EXTRA_START)
        ): TimeSelector(),
    }
    _add_period_fields(
        schema,
        EXTRA_BRIGHTNESS,
        EXTRA_COLOR_TEMP_KELVIN,
        defaults,
        DEFAULT_EXTRA_BRIGHTNESS,
        DEFAULT_EXTRA_COLOR_TEMP_KELVIN,
    )
    t_marker, t_sel = _transition_slider(
        EXTRA_TRANSITION, defaults.get(EXTRA_TRANSITION, 0)
    )
    schema[t_marker] = t_sel
    return vol.Schema(schema)


def _lights_schema(defaults: dict) -> vol.Schema:
    """Light selection."""
    return vol.Schema(
        {
            vol.Required(
                CONF_LIGHTS, default=defaults.get(CONF_LIGHTS, [])
            ): EntitySelector(EntitySelectorConfig(domain="light", multiple=True)),
        }
    )


def _lights_extra_schema(lights: list[str], custom: list[str]) -> vol.Schema:
    """One toggle per selected light: configure it separately or not."""
    return vol.Schema(
        {
            vol.Required(light, default=light in custom): BooleanSelector()
            for light in lights
        }
    )


def _settings_schema(defaults: dict, day_enabled: bool) -> vol.Schema:
    """Base period brightness/kelvin plus advanced parameters."""
    schema: dict = {}
    _add_period_fields(
        schema,
        CONF_BRIGHTNESS,
        CONF_COLOR_TEMP_KELVIN,
        defaults,
        DEFAULT_BRIGHTNESS,
        DEFAULT_COLOR_TEMP_KELVIN,
    )
    if day_enabled:
        _add_period_fields(
            schema,
            CONF_DAY_BRIGHTNESS,
            CONF_DAY_COLOR_TEMP_KELVIN,
            defaults,
            DEFAULT_DAY_BRIGHTNESS,
            DEFAULT_DAY_COLOR_TEMP_KELVIN,
        )
    schema.update(
        {
            vol.Required(
                CONF_TOLERANCE_BRIGHTNESS,
                default=defaults.get(
                    CONF_TOLERANCE_BRIGHTNESS, DEFAULT_TOLERANCE_BRIGHTNESS
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=25,
                    unit_of_measurement="%",
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
            vol.Required(
                CONF_TOLERANCE_KELVIN,
                default=defaults.get(CONF_TOLERANCE_KELVIN, DEFAULT_TOLERANCE_KELVIN),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=1000,
                    step=50,
                    unit_of_measurement="K",
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
            vol.Required(
                CONF_VERIFY_DELAY,
                default=defaults.get(CONF_VERIFY_DELAY, DEFAULT_VERIFY_DELAY),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=60,
                    unit_of_measurement="s",
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
            vol.Required(
                CONF_SETTLE_DELAY,
                default=defaults.get(CONF_SETTLE_DELAY, DEFAULT_SETTLE_DELAY),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=10,
                    step=0.5,
                    unit_of_measurement="s",
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
            vol.Required(
                CONF_TURN_ON_LISTEN,
                default=defaults.get(CONF_TURN_ON_LISTEN, DEFAULT_TURN_ON_LISTEN),
            ): BooleanSelector(),
            vol.Required(
                CONF_ONLY_WHEN_ON, default=defaults.get(CONF_ONLY_WHEN_ON, False)
            ): BooleanSelector(),
            vol.Required(
                CONF_RESPECT_MANUAL,
                default=defaults.get(CONF_RESPECT_MANUAL, DEFAULT_RESPECT_MANUAL),
            ): BooleanSelector(),
        }
    )
    return vol.Schema(schema)


def _override_schema(
    settings: dict, current: dict | None, extras: list[dict], day_enabled: bool
) -> vol.Schema:
    """Per-light override schema: night + day + every extra period."""
    current = current or {}
    schema: dict = {}
    _add_period_fields(
        schema,
        OVR_BRIGHTNESS,
        OVR_COLOR_TEMP_KELVIN,
        current,
        settings[CONF_BRIGHTNESS],
        settings[CONF_COLOR_TEMP_KELVIN],
    )
    if day_enabled:
        _add_period_fields(
            schema,
            OVR_DAY_BRIGHTNESS,
            OVR_DAY_COLOR_TEMP_KELVIN,
            current,
            settings.get(CONF_DAY_BRIGHTNESS, DEFAULT_DAY_BRIGHTNESS),
            settings.get(CONF_DAY_COLOR_TEMP_KELVIN, DEFAULT_DAY_COLOR_TEMP_KELVIN),
        )
    ovr_extras = current.get(OVR_EXTRAS, {})
    for i, extra in enumerate(extras):
        cur = ovr_extras.get(str(i), {})
        # 存储键是 brightness/color_temp_kelvin，需映射到表单键 extra_{i}_*
        cur_mapped = {}
        if OVR_EXTRA_BRIGHTNESS in cur:
            cur_mapped[f"extra_{i}_brightness"] = cur[OVR_EXTRA_BRIGHTNESS]
        if OVR_EXTRA_COLOR_TEMP_KELVIN in cur:
            cur_mapped[f"extra_{i}_kelvin"] = cur[OVR_EXTRA_COLOR_TEMP_KELVIN]
        _add_period_fields(
            schema,
            f"extra_{i}_brightness",
            f"extra_{i}_kelvin",
            cur_mapped,
            extra.get(EXTRA_BRIGHTNESS, DEFAULT_EXTRA_BRIGHTNESS),
            extra.get(EXTRA_COLOR_TEMP_KELVIN, DEFAULT_EXTRA_COLOR_TEMP_KELVIN),
        )
    return vol.Schema(schema)


def _extra_label(extra: dict, idx: int) -> str:
    """Display label for an extra period."""
    return extra.get(EXTRA_NAME) or f"{idx + 1}"


async def _extra_period_step(flow, user_input, next_step):
    """Shared loop over extra period pages for config and options flows."""
    if user_input is not None:
        extra = {
            EXTRA_NAME: user_input.get(EXTRA_NAME, ""),
            EXTRA_START: user_input[EXTRA_START],
            EXTRA_BRIGHTNESS: user_input[EXTRA_BRIGHTNESS],
            EXTRA_COLOR_TEMP_KELVIN: user_input[EXTRA_COLOR_TEMP_KELVIN],
            EXTRA_TRANSITION: int(user_input.get(EXTRA_TRANSITION, 0)),
        }
        if flow._idx < len(flow._extras):
            flow._extras[flow._idx] = extra
        else:
            flow._extras.append(extra)
        flow._idx += 1

    if flow._idx >= flow._extra_count:
        return await next_step()

    current = flow._extras[flow._idx] if flow._idx < len(flow._extras) else {}
    return flow.async_show_form(
        step_id="extra_period",
        data_schema=_extra_period_schema(current),
        description_placeholders={
            "position": f"{flow._idx + 1}/{flow._extra_count}",
        },
    )


async def _per_light_step(flow, user_input, finish):
    """Shared per-light override step logic for config and options flows."""
    if user_input is not None:
        entity = flow._custom_lights[flow._idx]
        ovr = {
            OVR_BRIGHTNESS: user_input[OVR_BRIGHTNESS],
            OVR_COLOR_TEMP_KELVIN: user_input[OVR_COLOR_TEMP_KELVIN],
        }
        if flow._day_enabled:
            ovr[OVR_DAY_BRIGHTNESS] = user_input[OVR_DAY_BRIGHTNESS]
            ovr[OVR_DAY_COLOR_TEMP_KELVIN] = user_input[OVR_DAY_COLOR_TEMP_KELVIN]
        ovr_extras = {}
        for i in range(len(flow._extras)):
            ovr_extras[str(i)] = {
                OVR_EXTRA_BRIGHTNESS: user_input[f"extra_{i}_brightness"],
                OVR_EXTRA_COLOR_TEMP_KELVIN: user_input[f"extra_{i}_kelvin"],
            }
        if ovr_extras:
            ovr[OVR_EXTRAS] = ovr_extras
        flow._overrides[entity] = ovr
        flow._idx += 1

    if flow._idx >= len(flow._custom_lights):
        return await finish()

    entity = flow._custom_lights[flow._idx]
    state = flow.hass.states.get(entity)
    name = (
        state.attributes.get("friendly_name", entity) if state is not None else entity
    )
    return flow.async_show_form(
        step_id="per_light",
        data_schema=_override_schema(
            flow._settings,
            flow._overrides.get(entity),
            flow._extras,
            flow._day_enabled,
        ),
        description_placeholders={
            "entity": entity,
            "name": name,
            "position": f"{flow._idx + 1}/{len(flow._custom_lights)}",
        },
    )


class _FlowMixin:
    """Shared step logic for the config and options flows."""

    _times: dict
    _extras: list[dict]
    _extra_count: int
    _lights: list[str]
    _custom_lights: list[str]
    _settings: dict
    _overrides: dict[str, dict]
    _idx: int

    @property
    def _day_enabled(self) -> bool:
        return bool(self._times.get(CONF_DAY_ENABLED))

    def _init_state(self) -> None:
        self._times = {}
        self._extras = []
        self._extra_count = 0
        self._lights = []
        self._custom_lights = []
        self._settings = {}
        self._overrides = {}
        self._idx = 0
        # 预填来源：options/reconfigure 流为当前条目数据，新建流为空
        self._defaults: dict = {}
        self._suggest: dict | None = None

    async def _handle_time_step(self, user_input, step_id):
        """Step 1: anchor source choices and toggles."""
        if user_input is not None:
            self._times = user_input
            return await self._async_step_time_details()
        schema = _time_schema(self._defaults)
        if self._suggest is not None:
            schema = self.add_suggested_values_to_schema(schema, self._suggest)
        return self.async_show_form(step_id=step_id, data_schema=schema)

    async def _async_step_time_details(self, user_input=None):
        """Step 2: only the fields relevant to the step-1 choices."""
        if user_input is not None:
            self._times.update(user_input)
            if self._times.get(CONF_EXTRA_ENABLED):
                return await self._async_step_extra_count()
            self._extras = []
            return await self._async_step_lights()
        schema = _time_details_schema(self._times, self._defaults)
        if self._suggest is not None:
            schema = self.add_suggested_values_to_schema(schema, self._suggest)
        return self.async_show_form(step_id="time_details", data_schema=schema)

    async def _async_step_extra_count(self, user_input=None):
        """Step 2 (optional): how many extra periods."""
        if user_input is not None:
            self._extra_count = int(user_input[CONF_EXTRA_COUNT])
            self._extras = self._extras[: self._extra_count]
            self._idx = 0
            return await self._async_step_extra_period()
        return self.async_show_form(
            step_id="extra_count",
            data_schema=_extra_count_schema(self._defaults),
        )

    async def _async_step_extra_period(self, user_input=None):
        """Step 3 (optional, loop): one page per extra period."""
        return await _extra_period_step(self, user_input, self._async_step_lights)

    async def _async_step_lights(self, user_input=None):
        """Step 4: pick light entities."""
        errors = {}
        if user_input is not None:
            if user_input.get(CONF_LIGHTS):
                self._lights = user_input[CONF_LIGHTS]
                self._custom_lights = [
                    light for light in self._custom_lights if light in self._lights
                ]
                return await self._async_step_lights_extra()
            errors["base"] = "no_lights"
        schema = _lights_schema(self._defaults)
        if self._suggest is not None:
            schema = self.add_suggested_values_to_schema(schema, self._suggest)
        return self.async_show_form(
            step_id="lights",
            data_schema=schema,
            errors=errors,
        )

    async def _async_step_lights_extra(self, user_input=None):
        """Step 5: per-light toggle for custom parameters."""
        if user_input is not None:
            self._custom_lights = [
                light for light in self._lights if user_input.get(light)
            ]
            return await self._async_step_settings()
        mapping = []
        for light in self._lights:
            state = self.hass.states.get(light)
            name = (
                state.attributes.get("friendly_name", light)
                if state is not None
                else light
            )
            mapping.append(f"{name} = `{light}`")
        return self.async_show_form(
            step_id="lights_extra",
            data_schema=_lights_extra_schema(self._lights, self._custom_lights),
            description_placeholders={"lights": "\n\n".join(mapping)},
        )

    async def _async_step_settings(self, user_input=None):
        """Step 6: base period parameters and advanced settings."""
        if user_input is not None:
            self._settings = user_input
            self._idx = 0
            if self._custom_lights:
                return await self._async_step_per_light()
            return await self._finish()
        return self.async_show_form(
            step_id="settings",
            data_schema=_settings_schema(self._defaults, self._day_enabled),
        )

    async def _async_step_per_light(self, user_input=None):
        """Step 7 (loop): per-light overrides for toggled lights only."""
        return await _per_light_step(self, user_input, self._finish)

    def _entry_data(self) -> dict:
        return {
            **self._times,
            CONF_EXTRA_COUNT: len(self._extras),
            CONF_EXTRAS: self._extras,
            CONF_LIGHTS: self._lights,
            CONF_CUSTOM_PER_LIGHT: self._custom_lights,
            **self._settings,
            CONF_OVERRIDES: {
                k: v for k, v in self._overrides.items() if k in self._lights
            },
        }


class AnlGroupFlowMixin(_FlowMixin):
    """Handle the initial config flow."""

    def __init__(self) -> None:
        """Store intermediate data between steps."""
        self._init_state()

    async def async_step_anl_user(self, user_input=None) -> ConfigFlowResult:
        """Step 1: time settings."""
        return await self._handle_time_step(user_input, "anl_user")

    async def async_step_anl_reconfigure(self, user_input=None) -> ConfigFlowResult:
        """Reconfigure: same pages as adding, prefilled from the entry."""
        if self._suggest is None:
            entry = self._get_reconfigure_entry()
            current = {**entry.data, **entry.options}
            self._extras = [dict(e) for e in current.get(CONF_EXTRAS, [])]
            self._extra_count = len(self._extras)
            self._custom_lights = list(current.get(CONF_CUSTOM_PER_LIGHT, []))
            self._overrides = dict(current.get(CONF_OVERRIDES, {}))
            self._defaults = current
            self._suggest = current
        return await self._handle_time_step(user_input, "anl_reconfigure")

    async def async_step_time_details(self, user_input=None) -> ConfigFlowResult:
        return await self._async_step_time_details(user_input)

    async def async_step_extra_count(self, user_input=None) -> ConfigFlowResult:
        return await self._async_step_extra_count(user_input)

    async def async_step_extra_period(self, user_input=None) -> ConfigFlowResult:
        return await self._async_step_extra_period(user_input)

    async def async_step_lights(self, user_input=None) -> ConfigFlowResult:
        return await self._async_step_lights(user_input)

    async def async_step_lights_extra(self, user_input=None) -> ConfigFlowResult:
        return await self._async_step_lights_extra(user_input)

    async def async_step_settings(self, user_input=None) -> ConfigFlowResult:
        return await self._async_step_settings(user_input)

    async def async_step_per_light(self, user_input=None) -> ConfigFlowResult:
        return await self._async_step_per_light(user_input)

    async def _finish(self) -> ConfigFlowResult:
        if self.source == SOURCE_RECONFIGURE:
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(),
                data={CONF_KIND: KIND_ANL, **self._entry_data()},
                options={},
            )
        await self.async_set_unique_id("anl:" + ",".join(sorted(self._lights)))
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="自动夜灯", data={CONF_KIND: KIND_ANL, **self._entry_data()}
        )


class AnlGroupOptionsFlow(_FlowMixin, OptionsFlow):
    """Allow re-editing times, extras, lights, settings and per-light overrides."""

    def __init__(self) -> None:
        """Initialize flow state; the entry is prefilled lazily (L6)."""
        self._init_state()

    @property
    def _current(self) -> dict:
        return {**self.config_entry.data, **self.config_entry.options}

    async def async_step_anl_init(self, user_input=None) -> ConfigFlowResult:
        """Step 1: time settings, prefilled with current values."""
        if self._suggest is None:
            current = self._current
            self._extras = [dict(e) for e in current.get(CONF_EXTRAS, [])]
            self._extra_count = len(self._extras)
            self._custom_lights = list(current.get(CONF_CUSTOM_PER_LIGHT, []))
            self._overrides = dict(current.get(CONF_OVERRIDES, {}))
            self._defaults = current
            self._suggest = current
        return await self._handle_time_step(user_input, "anl_init")

    async def async_step_time_details(self, user_input=None) -> ConfigFlowResult:
        return await self._async_step_time_details(user_input)

    async def async_step_extra_count(self, user_input=None) -> ConfigFlowResult:
        return await self._async_step_extra_count(user_input)

    async def async_step_extra_period(self, user_input=None) -> ConfigFlowResult:
        return await self._async_step_extra_period(user_input)

    async def async_step_lights(self, user_input=None) -> ConfigFlowResult:
        return await self._async_step_lights(user_input)

    async def async_step_lights_extra(self, user_input=None) -> ConfigFlowResult:
        return await self._async_step_lights_extra(user_input)

    async def async_step_settings(self, user_input=None) -> ConfigFlowResult:
        return await self._async_step_settings(user_input)

    async def async_step_per_light(self, user_input=None) -> ConfigFlowResult:
        return await self._async_step_per_light(user_input)

    async def _finish(self) -> ConfigFlowResult:
        return self.async_create_entry(
            title="", data={CONF_KIND: KIND_ANL, **self._entry_data()}
        )
