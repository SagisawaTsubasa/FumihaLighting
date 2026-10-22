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
    # V1.6.0 P3（R11 报告发现 4）：setup 失败必须回滚入桶的 runtime——
    # HA 对 SETUP_ERROR 条目不调 async_unload_entry（该路径只在 loaded 后
    # 触发），异常后桶里残留的 runtime 会被 lux 页面当正常区域序列化，
    # 出现「从不工作、点击校准却真能起扫」的幻影区域（检查单 17：setup
    # 失败回滚要覆盖内嵌组件自身的注册面；卸载侧对称性 ANL-F-037a 已有，
    # 这是 setup 侧的另一半）。
    try:
        await runtime.async_load()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        if hass.data[DOMAIN].get("lux", {}).get(entry.entry_id) is runtime:
            hass.data[DOMAIN]["lux"].pop(entry.entry_id, None)
        _LOGGER.exception("%s: 区域加载失败，已回滚运行时", entry.title)
        raise
    runtime.start()
    return True


async def lux_async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload one zone: stop listeners, timers and the calibration task."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    # 平台卸载失败时条目仍处于 loaded 态：runtime 留桶（页面继续可用），
    # 不提前拆 —— 否则 web 层按空桶拆页而条目还活着（ANL-F-037a）
    if unload_ok:
        data = hass.data.get(DOMAIN, {}).get("lux", {})
        runtime: ZoneRuntime | None = data.pop(entry.entry_id, None)
        if runtime is not None:
            await runtime.async_stop()
    return unload_ok
