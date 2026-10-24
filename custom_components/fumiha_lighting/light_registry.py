"""灯→恒照度区域反查表（V1.7.0 A 面单一来源）。

恒照度接管=控制权移交，移交对象由系统推导、不由用户指定。本模块是
「真实灯 entity_id → 所属 lux 区域」的反查表唯一构建点，三面共用：

- A1：anl 侧 CI 推导（day_ci_virtual_light 配置键废除，manager 按清单灯
  经本表推导 zone/虚拟灯）；
- A2：lux flow 保存查重（同灯换传感器再建 zone 的互抢在入口拒绝）；
- A3：anl 直控防护（对 ∈ 本表的灯直控前先归还其闭环）。

实时遍历构建（zone 数量级小，无缓存失效问题——消费端每次现查，lux 条
目 unload 后本表自然不再含其灯，与 A 面「清单保留+运行时跳过」语义一致）。
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# 虚拟灯 entity_id 反查失败的 WARNING 去重（每 entry 一次；恢复后再失败
# 可再响）——消费点含页面轮询与逐灯服务调用路径，逐次 WARNING 会刷屏
_WARNED_MISSING_VIRTUAL: set[str] = set()


def build_light_zone_index(hass: HomeAssistant) -> dict[str, dict]:
    """构建 light_entity_id → zone 反查表。

    返回 ``{真实灯 entity_id: {"entry_id", "zone_name", "virtual_entity_id"}}``。
    数据源 = ``hass.data[DOMAIN]["lux"]`` 桶 runtime（zone 配置即真实灯+
    传感器，lux/flow 单一事实）；虚拟灯 entity_id 经实体注册表按
    ``unique_id="{entry_id}-light"`` 反查（lux/light.py 的唯一来源，entity
    platform 分配的 entity_id 无法从 runtime 推导）。注册表反查失败（平台
    未挂载/实体已删）→ WARNING（每 entry 去重一次）+ 跳过该区域：消费端
    按「灯不属于任何恒照度区域」处理，不得静默给错映射。
    """
    index: dict[str, dict] = {}
    bucket = (hass.data.get(DOMAIN) or {}).get("lux") or {}
    if not bucket:
        _WARNED_MISSING_VIRTUAL.clear()
        return index
    registry = er.async_get(hass)
    for entry_id, runtime in bucket.items():
        virtual_entry = None
        for reg in er.async_entries_for_config_entry(registry, entry_id):
            if reg.unique_id == f"{entry_id}-light" and reg.domain == "light":
                virtual_entry = reg
                break
        if virtual_entry is None:
            if entry_id not in _WARNED_MISSING_VIRTUAL:
                _WARNED_MISSING_VIRTUAL.add(entry_id)
                _LOGGER.warning(
                    "恒照度区域 %s 的虚拟灯尚未注册（平台未挂载或实体已删），"
                    "反查表暂时跳过该区域",
                    getattr(runtime, "zone_name", entry_id),
                )
            continue
        _WARNED_MISSING_VIRTUAL.discard(entry_id)
        index[runtime.light_entity] = {
            "entry_id": entry_id,
            "zone_name": runtime.zone_name,
            "virtual_entity_id": virtual_entry.entity_id,
        }
    return index
