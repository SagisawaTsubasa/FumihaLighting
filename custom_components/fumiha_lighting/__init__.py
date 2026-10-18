"""Fumiha Lighting 文羽照明 — one integration, two entry kinds.

- ``lux`` entries: constant-illuminance zones (the former lux_balance).
- ``anl`` entries: scheduled night-light groups (the former auto_night_light).

Both kinds share one domain, one config flow (kind discriminator step) and
one web layer; each kind keeps its own runtime sub-package, page module and
Fumiha Hub page descriptor. Entities exist only for the HA-native ecosystem
(automations/dashboards) — the pages talk to the runtimes through this
integration's own REST views (web.py), never through entities.
"""

from __future__ import annotations

import logging

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_extract_entity_ids

from .anl.const import SERVICE_TRIGGER_NOW
from .const import CONF_KIND, DOMAIN, KIND_ANL, KIND_LUX
from .lux.const import SERVICE_CALIBRATE
from .lux.runtime import ZoneRuntime
from .lux.store import ZoneStorage
from .web import async_setup_web, async_unload_web

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_LOGGER = logging.getLogger(__name__)

_TARGET_KEYS = {"entity_id", "device_id", "area_id", "floor_id", "label_id"}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one entry of either kind, dispatching on entry.data["kind"]."""
    kind = entry.data.get(CONF_KIND, KIND_LUX)
    hass.data.setdefault(DOMAIN, {"lux": {}, "anl": {}})

    if kind == KIND_LUX:
        from .lux import lux_async_setup_entry

        await lux_async_setup_entry(hass, entry)
    elif kind == KIND_ANL:
        from .anl import anl_async_setup_entry

        await anl_async_setup_entry(hass, entry)
    else:
        _LOGGER.error("未知条目类型 %r（entry %s），跳过加载", kind, entry.entry_id)
        return False

    await async_setup_web(hass)
    _register_services(hass)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload one entry of either kind."""
    kind = entry.data.get(CONF_KIND, KIND_LUX)
    hass.data.setdefault(DOMAIN, {"lux": {}, "anl": {}})

    if kind == KIND_LUX:
        from .lux import lux_async_unload_entry

        ok = await lux_async_unload_entry(hass, entry)
    elif kind == KIND_ANL:
        from .anl import anl_async_unload_entry

        ok = await anl_async_unload_entry(hass, entry)
    else:
        # 未知 kind 永远进不了已加载态（setup 显式拒绝），此处不可达；
        # 保守返回成功让条目能被删除。
        _LOGGER.warning("未知条目类型 %r，按未加载处理", kind)
        ok = True

    # Web teardown 无条件调用（ANL-F-032）：函数内部已按 kind 各自判断
    # 清空与否；原"两 kind 双空才调用"的外层门控使"删最后一个 lux 条目
    # 而 anl 仍在"的页面拆除在生产路径不可达。views/static 由内部保留。
    async_unload_web(hass)
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop per-entry storage (the lux calibration curve) on delete."""
    if entry.data.get(CONF_KIND, KIND_LUX) == KIND_LUX:
        await ZoneStorage(hass, entry.entry_id).async_remove()


# ---------------------------------------------------------------------------
# Services (automations face; the pages use the REST views instead)
# ---------------------------------------------------------------------------


def _lux_zone_entities(hass: HomeAssistant, runtime: ZoneRuntime) -> set[str]:
    """Targeting set for one lux zone (registry entities + light + lux)."""
    registry = er.async_get(hass)
    own = {
        entry_entity.entity_id
        for entry_entity in er.async_entries_for_config_entry(
            registry, runtime.entry_id
        )
    }
    own |= {runtime.light_entity, runtime.lux_entity}
    return own


@callback
def _register_services(hass: HomeAssistant) -> None:
    """Register fumiha_lighting.calibrate and fumiha_lighting.trigger_now.

    Idempotent (has_service guards); services are NOT removed on the last
    unload — empty-bucket calls only warn, and re-setup re-uses them.
    """
    already = hass.services.has_service(DOMAIN, SERVICE_CALIBRATE)

    if not already:

        async def handle_calibrate(call: ServiceCall) -> None:
            unknown_keys = set(call.data) - _TARGET_KEYS
            has_target = bool(set(call.data) & _TARGET_KEYS)
            if unknown_keys and not has_target:
                _LOGGER.warning("fumiha_lighting.calibrate: 未识别到有效目标，放弃执行")
                return
            entity_ids: set[str] = set()
            if has_target:
                # HA 2025.10 changed the signature (leading hass deprecated
                # since then, shim removed in 2026.10); probe to support both.
                import inspect

                if "hass" in inspect.signature(async_extract_entity_ids).parameters:
                    entity_ids = set(await async_extract_entity_ids(hass, call))
                else:
                    entity_ids = set(await async_extract_entity_ids(call))
                if not entity_ids:
                    raise HomeAssistantError("目标未解析出任何实体，放弃执行")
            addressed = 0
            skipped: list[str] = []
            started: list[str] = []
            runtimes = list((hass.data.get(DOMAIN) or {}).get("lux", {}).values())
            for runtime in runtimes:
                if entity_ids and not entity_ids & _lux_zone_entities(hass, runtime):
                    continue
                addressed += 1
                if runtime.start_calibration():
                    started.append(runtime.zone_name)
                    _LOGGER.info("%s: 校准已启动", runtime.zone_name)
                else:
                    skipped.append(runtime.zone_name)
            if has_target and addressed == 0:
                raise HomeAssistantError("目标未命中任何恒照度区域")
            if skipped and has_target:
                # Loud, aligned with the calibrate button entity: a named
                # target that did not start must never look like success.
                raise HomeAssistantError(
                    "校准未启动："
                    + "、".join(skipped)
                    + "（本区或另一区域已有校准进行中，同一卫生间一次只能校准一个区）"
                )
            for zone in skipped:
                _LOGGER.warning(
                    "%s: 校准已在进行中，跳过（已启动：%s）",
                    zone,
                    "、".join(started) or "无",
                )

        hass.services.async_register(
            DOMAIN,
            SERVICE_CALIBRATE,
            handle_calibrate,
            schema=vol.Schema(
                {vol.Optional(ATTR_ENTITY_ID): vol.All(list, [str])},
                extra=vol.ALLOW_EXTRA,
            ),
        )

    if not hass.services.has_service(DOMAIN, SERVICE_TRIGGER_NOW):

        async def handle_trigger_now(call: ServiceCall) -> None:
            from .anl.manager import NightLightManager

            # Snapshot before awaiting (async_trigger awaits light services).
            managers: list[NightLightManager] = list(
                (hass.data.get(DOMAIN) or {}).get("anl", {}).values()
            )
            for mgr in managers:
                await mgr.async_trigger(reason="service")

        hass.services.async_register(
            DOMAIN, SERVICE_TRIGGER_NOW, handle_trigger_now, schema=vol.Schema({})
        )
