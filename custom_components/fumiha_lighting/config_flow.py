"""Config flow entry point: kind discriminator, then the kind's own wizard."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .anl.flow import AnlGroupFlowMixin, AnlGroupOptionsFlow
from .const import CONF_KIND, DOMAIN, KIND_ANL, KIND_LUX
from .lux.flow import LuxZoneFlowMixin, LuxZoneOptionsFlow

_KIND_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_KIND, default=KIND_LUX): SelectSelector(
            SelectSelectorConfig(
                options=[KIND_LUX, KIND_ANL],
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="entry_kind",
            )
        )
    }
)


class FumihaLightingConfigFlow(
    LuxZoneFlowMixin, AnlGroupFlowMixin, ConfigFlow, domain=DOMAIN
):
    """Discriminate the entry kind, then run that kind's wizard.

    Both mixins provide their own prefixed public steps; the shared ``user``
    step only picks the kind. HA's reconfigure entry point dispatches on
    ``entry.data["kind"]``.
    """

    VERSION = 1

    def __init__(self) -> None:
        """The anl wizard keeps per-step state; init both mixin halves."""
        AnlGroupFlowMixin.__init__(self)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: pick what this entry is going to be."""
        if user_input is not None:
            kind = user_input[CONF_KIND]
            if kind == KIND_LUX:
                return await self.async_step_lux_zone()
            return await self.async_step_anl_user()
        return self.async_show_form(step_id="user", data_schema=_KIND_SCHEMA)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Dispatch to the kind's own reconfigure wizard."""
        entry = self._get_reconfigure_entry()
        if entry.data.get(CONF_KIND, KIND_LUX) == KIND_LUX:
            return await self.async_step_lux_reconfigure(user_input)
        return await self.async_step_anl_reconfigure(user_input)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Dispatch the options flow on the entry kind.

        两侧都无参构造：OptionsFlow 基类无 ``__init__``，向基类传参会
        TypeError（HA 2025.10 源码核实）；entry 由 flow manager 经
        ``self.config_entry`` property 注入。
        """
        if config_entry.data.get(CONF_KIND, KIND_LUX) == KIND_LUX:
            return LuxZoneOptionsFlow()
        return AnlGroupOptionsFlow()
