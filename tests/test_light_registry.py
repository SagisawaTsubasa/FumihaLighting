"""灯→恒照度区域反查表测试（V1.7.0 A 面，计划书 §3 步骤 2）。

覆盖：空桶/正常映射（registry 按 unique_id={entry_id}-light 反查）、虚拟
灯未注册 WARNING 去重与恢复、多区域、unload 边界（条目出桶即失效）。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import conftest as stubs
from test_web import FakeHass, run  # noqa: F401 — run 供后续异步用例复用

from custom_components.fumiha_lighting.const import DOMAIN
from custom_components.fumiha_lighting.light_registry import (
    _WARNED_MISSING_VIRTUAL,
    build_light_zone_index,
)

LOGGER = "custom_components.fumiha_lighting.light_registry"


def _lux_bucket(hass, *runtimes):
    bucket = hass.data.setdefault(DOMAIN, {}).setdefault("lux", {})
    for rt in runtimes:
        bucket[rt.entry_id] = rt
    return bucket


def _runtime(entry_id="lux1", light="light.bath", zone="卫生间"):
    return SimpleNamespace(
        entry_id=entry_id, light_entity=light, zone_name=zone
    )


def _registry_entry(entry_id, entity_id="light.bath_virt"):
    return SimpleNamespace(
        unique_id=f"{entry_id}-light", entity_id=entity_id, domain="light"
    )


def setup_function(_):
    _WARNED_MISSING_VIRTUAL.clear()
    # conftest 的 autouse 复位只清 _entities——本文件自理 _entries_for
    # （跨用例隔离：A 用例种下的虚拟灯注册不得泄漏给 B）
    stubs._ENTITY_REGISTRY._entries_for.clear()


def teardown_function(_):
    _WARNED_MISSING_VIRTUAL.clear()
    stubs._ENTITY_REGISTRY._entries_for.clear()


def test_empty_bucket_returns_empty_index():
    hass = FakeHass()
    assert build_light_zone_index(hass) == {}


def test_zone_mapped_with_registry_lookup():
    """正常面：灯 → {entry_id, zone_name, virtual_entity_id}（虚拟灯经
    注册表 unique_id={entry_id}-light 反查，lux/light.py 单一来源）。"""
    hass = FakeHass()
    _lux_bucket(hass, _runtime())
    stubs._ENTITY_REGISTRY._entries_for["lux1"] = [_registry_entry("lux1")]
    assert build_light_zone_index(hass) == {
        "light.bath": {
            "entry_id": "lux1",
            "zone_name": "卫生间",
            "virtual_entity_id": "light.bath_virt",
        }
    }


def test_missing_virtual_skipped_with_deduped_warning(caplog):
    """平台未挂载/实体已删：跳过该区域 + WARNING 每 entry 去重一次；恢复
    后再去掉去重标记（再失败可再响）。"""
    hass = FakeHass()
    _lux_bucket(hass, _runtime())
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert build_light_zone_index(hass) == {}
        assert build_light_zone_index(hass) == {}  # 第二次不再 WARNING
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1 and "尚未注册" in messages[0]

    # 恢复：注册表出现 → 映射恢复，去重标记清除
    stubs._ENTITY_REGISTRY._entries_for["lux1"] = [_registry_entry("lux1")]
    assert "light.bath" in build_light_zone_index(hass)
    assert "lux1" not in _WARNED_MISSING_VIRTUAL


def test_non_light_unique_id_ignored():
    """unique_id 匹配但 domain 不是 light 的注册表条目不算虚拟灯（防其他
    平台实体误配）。"""
    hass = FakeHass()
    _lux_bucket(hass, _runtime())
    stubs._ENTITY_REGISTRY._entries_for["lux1"] = [
        SimpleNamespace(unique_id="lux1-light", entity_id="sensor.x", domain="sensor"),
        _registry_entry("lux1"),
    ]
    index = build_light_zone_index(hass)
    assert index["light.bath"]["virtual_entity_id"] == "light.bath_virt"


def test_multiple_zones_and_unload_boundary():
    """多区域各自映射；条目出桶（unload/删除）后其灯即刻失效——无缓存。"""
    hass = FakeHass()
    _lux_bucket(
        hass,
        _runtime("lux1", "light.bath", "湿区"),
        _runtime("lux2", "light.dry", "干区"),
    )
    stubs._ENTITY_REGISTRY._entries_for["lux1"] = [_registry_entry("lux1")]
    stubs._ENTITY_REGISTRY._entries_for["lux2"] = [
        _registry_entry("lux2", "light.dry_virt")
    ]
    index = build_light_zone_index(hass)
    assert set(index) == {"light.bath", "light.dry"}
    assert index["light.dry"]["zone_name"] == "干区"

    # unload 边界：lux2 出桶 → 干区灯即刻不在表（消费端走「不属于任何
    # 恒照度区域」分支，A 面生效语义：清单保留+运行时跳过）
    hass.data[DOMAIN]["lux"].pop("lux2")
    assert set(build_light_zone_index(hass)) == {"light.bath"}
