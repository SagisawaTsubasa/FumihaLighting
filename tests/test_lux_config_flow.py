"""Shell smoke tests: config flow render/validation/options chain.

These run against stubbed Home Assistant modules (see conftest) and pin the
0.1.6-class regressions (initial render crashing on user_input=None).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import custom_components.fumiha_lighting.config_flow as cf
import custom_components.fumiha_lighting.lux.flow as lux_flow
from custom_components.fumiha_lighting.lux.const import DOMAIN

LIGHT = "light.bathroom"
LUX = "sensor.bathroom_lux"
BRIGHT_MODES = {"supported_color_modes": ["brightness"]}


class FakeStates:
    def __init__(self, mapping):
        self.mapping = mapping

    def get(self, entity_id):
        data = self.mapping.get(entity_id)
        if data is None:
            return None
        state = SimpleNamespace()
        state.state = data.get("state", "on")
        state.attributes = data.get("attributes", {})
        return state


def make_hass(states):
    return SimpleNamespace(states=FakeStates(states))


def registry(entry_or_none):
    """er.async_get(hass) → 注册表对象；其 .async_get(entity) → entry_or_none。"""
    return SimpleNamespace(async_get=lambda eid: entry_or_none)


def make_user_flow(hass, er_registry=None, monkeypatch=None):
    flow = cf.FumihaLightingConfigFlow()
    flow.hass = hass
    if er_registry is not None:
        assert monkeypatch is not None
        monkeypatch.setattr(lux_flow.er, "async_get", lambda hass: er_registry)
    return flow


def _schema_keys(form):
    """从桩返回的表单里提取字段名集合（vol Marker.schema 即字段名）。"""
    return {marker.schema for marker in form["data_schema"].schema}


USER_INPUT = {"zone_name": "卫生间", "light_entity": LIGHT, "lux_entity": LUX}


def run(coro):
    return asyncio.run(coro)


def test_user_initial_render_no_crash():
    """0.1.6 回归钉：首渲染 user_input=None 必须返回表单而不是崩溃。"""
    flow = make_user_flow(make_hass({}))
    result = run(flow.async_step_lux_zone(None))
    assert result["type"] == "form"
    assert result["step_id"] == "lux_zone"


def test_user_missing_entity(monkeypatch):
    flow = make_user_flow(make_hass({}), registry(None), monkeypatch)
    result = run(flow.async_step_lux_zone(USER_INPUT))
    assert result["errors"] == {"base": "entity_missing"}


def test_user_rejects_virtual_light(monkeypatch):
    virtual = SimpleNamespace(platform=DOMAIN)
    flow = make_user_flow(
        make_hass({LIGHT: {"state": "on", "attributes": BRIGHT_MODES}}),
        registry(virtual),
        monkeypatch,
    )
    result = run(flow.async_step_lux_zone(USER_INPUT))
    assert result["errors"] == {"base": "light_is_virtual"}


def test_user_online_light_without_brightness_rejected(monkeypatch):
    no_brightness = {"state": "on", "attributes": {"supported_color_modes": ["onoff"]}}
    flow = make_user_flow(
        make_hass({LIGHT: no_brightness}), registry(None), monkeypatch
    )
    result = run(flow.async_step_lux_zone(USER_INPUT))
    assert result["errors"] == {"base": "light_no_brightness"}


def test_user_offline_light_accepted(monkeypatch):
    """BLE 灯离线是常态：离线时跳过亮度校验，不拦配置。"""
    flow = make_user_flow(
        make_hass({LIGHT: {"state": "unavailable", "attributes": {}}}),
        registry(None),
        monkeypatch,
    )
    result = run(flow.async_step_lux_zone(USER_INPUT))
    assert result["type"] == "create_entry"
    assert result["title"] == "卫生间"
    assert result["data"] == {"kind": "lux", **USER_INPUT}


def test_user_ok_creates_entry(monkeypatch):
    flow = make_user_flow(
        make_hass({LIGHT: {"state": "off", "attributes": BRIGHT_MODES}}),
        registry(None),
        monkeypatch,
    )
    result = run(flow.async_step_lux_zone(USER_INPUT))
    assert result["type"] == "create_entry"
    assert result["data"] == {"kind": "lux", **USER_INPUT}


def test_reconfigure_initial_render_no_crash():
    flow = make_user_flow(make_hass({}))
    flow.context = {"entry_id": "e1"}
    flow._reconfigure_entry = SimpleNamespace(
        data=USER_INPUT, unique_id=f"lux:{LIGHT}::{LUX}", title="卫生间"
    )
    result = run(flow.async_step_lux_reconfigure(None))
    assert result["type"] == "form"
    assert result["step_id"] == "lux_reconfigure"


def test_reconfigure_rebind_updates_entry(monkeypatch):
    """改绑（unique_id 变化）必须成功并同步 data/unique_id/title。"""
    flow = make_user_flow(
        make_hass({LIGHT: {"state": "off", "attributes": BRIGHT_MODES}}),
        registry(None),
        monkeypatch,
    )
    flow.context = {"entry_id": "e1"}
    entry = SimpleNamespace(
        entry_id="e1",
        data=dict(USER_INPUT),
        unique_id=f"lux:{LIGHT}::{LUX}",
        title="卫生间",
    )
    flow._reconfigure_entry = entry
    new_lux = "sensor.other_lux"
    result = run(
        flow.async_step_lux_reconfigure(
            {"zone_name": "卫生间", "light_entity": LIGHT, "lux_entity": new_lux}
        )
    )
    assert result == {"type": "abort", "reason": "reconfigure_successful"}
    expected_uid = f"lux:{LIGHT}::{new_lux}"
    assert entry.unique_id == expected_uid
    assert entry.data["lux_entity"] == new_lux
    assert entry.title == "卫生间"


def test_options_chain_without_target_lux():
    """options 两步链：闭环 4 项 → 校准 2 项；target_lux 不在 options。"""
    flow = cf.LuxZoneOptionsFlow()
    flow.config_entry = SimpleNamespace(options={})

    first = run(flow.async_step_lux_init(None))
    assert first["type"] == "form" and first["step_id"] == "lux_init"
    assert _schema_keys(first) == {
        "scan_interval_s",
        "deadband_pct",
        "deadband_lux",
        "min_brightness_pct",
        "auto_turn_off",
    }

    second = run(
        flow.async_step_lux_init(
            {
                "scan_interval_s": 45,
                "deadband_pct": 5.0,
                "deadband_lux": 10.0,
                "min_brightness_pct": 5.0,
                "auto_turn_off": False,
            }
        )
    )
    assert second["type"] == "form" and second["step_id"] == "lux_advanced"
    assert _schema_keys(second) == {"calibrate_step_pct", "manual_slope"}

    final = run(
        flow.async_step_lux_advanced(
            {
                "calibrate_step_pct": 5,
                "manual_slope": 0.0,
                "deadband_lux": 10.0,
                "link_group": "卫生间",
                "cross_fade": 0.5,
                "max_step_pct": 20.0,
            }
        )
    )
    assert final["type"] == "create_entry"
    assert set(final["data"]) == {
        "scan_interval_s",
        "deadband_pct",
        "deadband_lux",
        "min_brightness_pct",
        "auto_turn_off",
        "calibrate_step_pct",
        "manual_slope",
        "link_group",
        "cross_fade",
        "max_step_pct",
    }
    assert "target_lux" not in final["data"]
