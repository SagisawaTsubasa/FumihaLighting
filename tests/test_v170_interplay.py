"""V1.7.0 交互消解组合钉（计划书 §交互面：link_group × 反查表 × B1/A3）。

三个交叉面的设计确认与回归钉：
1. A3 归还 → 组内重评估：anl 归还虚拟灯后 lux 侧 IDLE 放手、真实灯直控
   落地不遭反扑；组内对方区经 _on_linked_event 重评估一次且死区收敛
   （与 test_lux_linkgroup 乒乓钉共用收敛依据，此处钉组合链路）。
2. B1 色温一次性下发不扰 ambient 扣除：载荷无亮度键 → 灯亮度属性不变 →
   _external_contribution 按亮度查曲线恒定 → 对方区决策不变。
3. 对称化监听 × light_registry 实时构建：无缓存失效面——lux 条目卸载后
   反查表即刻失效（manager 侧失效告警+跳过、lux 侧联动订阅重建摘除）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from types import SimpleNamespace

import conftest as stubs
from test_anl_transitions import (
    LIGHT,
    VIRT,
    _FakeState,
    _FakeStates,
    _RecordingServices,
    make_ci_manager,
)
from test_lux_linkgroup import _make_hass, _pair_with_states, _state
from test_web import run

from custom_components.fumiha_lighting.const import DOMAIN
from custom_components.fumiha_lighting.lux import runtime as rt_mod
from custom_components.fumiha_lighting.lux.logic import curve_from_manual

MANAGER_LOGGER = "custom_components.fumiha_lighting.anl.manager"


# ---------------------------------------------------------------------------
# 1. A3 归还 → 组内重评估（组合链路钉）
# ---------------------------------------------------------------------------


def test_a3_release_then_direct_control_not_fought_back():
    """A3 组合链：anl 夜间直控 ∈反查表的灯 → 先归还虚拟灯（lux 侧 IDLE
    放手）→ 真实灯直控落地；lux 侧收到灯态变化后零反写下发（mode 门），
    组内对方区重评估一次即收敛（扣除后决策不变，无乒乓）。"""
    # —— lux 侧布景：a（受控区，灯=LIGHT）与 b（同组对方区）——
    states = {
        "sensor.a": SimpleNamespace(state="155.49", attributes={}, last_changed=None),
        "sensor.b": SimpleNamespace(state="155.49", attributes={}, last_changed=None),
        LIGHT: _state(brightness=128),
        "light.b": _state(brightness=128),
    }
    hass, a, b = _pair_with_states(states)
    a.light_entity = LIGHT  # a 区的真实灯 = anl 组里的 LIGHT
    a.curve = curve_from_manual(2.0)
    b.curve = curve_from_manual(1.0)
    b.mode = "active"
    b.target_lux = 30.0
    lux_writes: list[tuple[str, str]] = []
    for rt in (a, b):

        def _call(service, data, _owner=rt):
            lux_writes.append((_owner.entry_id, service))
            return _async_true()

        rt._async_call_light = _call

    # —— anl 侧：夜间直控 LIGHT（make_ci_manager 已喂 LIGHT→VIRT 种子）——
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)
    m = make_ci_manager()
    m.hass.states = _FakeStates()
    # 灯在日间参数（偏离夜间目标）→ 夜锚直控落地
    m.hass.states.mapping[LIGHT] = _FakeState("on", 255, 4000)
    anl_svc = []
    released = {"v": False}

    class _FlipOnRelease(_RecordingServices):
        """桩忠实度：turn_off VIRT 后其 attributes["mode"] 翻为 idle（真机
        虚拟灯状态随归还刷新）——否则 A3 防护在直控前读到 active 会二次
        归还，测不出生产时序。"""

        async def async_call(self, domain, service, data, blocking=False):
            await super().async_call(domain, service, data, blocking)
            if service == "turn_off" and data.get("entity_id") == VIRT:
                released["v"] = True

    m.hass.services = _FlipOnRelease(anl_svc)

    def _virt_state(_eid):
        if _eid == VIRT:
            return _FakeState("on", mode="idle" if released["v"] else "active")
        return m.hass.states.mapping.get(_eid)

    m.hass.states.get = _virt_state

    run(m.async_trigger("test"))
    # 归还恰一次 turn_off VIRT（先于直控），LIGHT 按夜间参数直控
    assert anl_svc == [
        ("light", "turn_off", {"entity_id": VIRT}),
        ("light", "turn_on", {"entity_id": LIGHT, "brightness": 64,
                              "color_temp_kelvin": 2200}),
    ]
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)

    # —— lux 侧回放：归还后 a 区 mode=IDLE；anl 的直控落成灯态变化事件
    #（_on_light_event）与对方区联动事件（_on_linked_event）——
    a.mode = "idle"  # async_release_virtual_light turn_off → runtime IDLE

    async def _replay():
        a._on_light_event(SimpleNamespace(data={"new_state": _state("on", 64)}))
        b._on_linked_event(SimpleNamespace(data={"new_state": _state("on", 64)}))
        for _ in range(10):
            await asyncio.sleep(0)

    run(_replay())
    # a 区 IDLE 不反写下发（anl 直控不被反扑）；b 区重评估过但死区内零写
    assert ("a", "turn_on") not in lux_writes and ("a", "turn_off") not in lux_writes
    assert ("b", "turn_on") not in lux_writes
    assert len(b.decisions) >= 1  # b 确实重评估过（组合链路非哑转）


# ---------------------------------------------------------------------------
# 2. B1 色温下发不扰 ambient 扣除
# ---------------------------------------------------------------------------


def test_b1_color_temp_write_does_not_disturb_ambient_deduction():
    """B1 组合钉：色温补发载荷只带 color_temp_kelvin（无亮度键）——灯亮度
    属性不变 → _external_contribution 恒定 → 对方区决策不变（闭环零感知）。"""
    states = {
        "sensor.a": SimpleNamespace(state="130.39", attributes={}, last_changed=None),
        "light.b": _state(brightness=128),  # b 区灯：贡献 1.0×50.2×0.5
        "light.a": _state(brightness=128),
    }
    hass, a, b = _pair_with_states(states)
    b.curve = curve_from_manual(1.0)
    before = a._external_contribution()
    decision_before, lux_before = run(a._async_compute_decision())

    # B1 补发落地（模拟）：b 区灯色温 2700→4000，亮度不变
    states["light.b"] = SimpleNamespace(
        state="on",
        attributes={"brightness": 128, "color_temp_kelvin": 4000,
                    "color_mode": "color_temp"},
        last_changed=datetime(2026, 10, 24, 13, 0, 0),
    )
    after = a._external_contribution()
    decision_after, lux_after = run(a._async_compute_decision())
    assert after == pytest_approx(before)
    assert decision_after.action == decision_before.action
    assert decision_after.pct == decision_before.pct
    assert lux_after == pytest_approx(lux_before)


def pytest_approx(value):
    import pytest

    return pytest.approx(value)


def test_b1_payload_carries_no_brightness_key():
    """manager 面钉：B1 服务载荷恰为 {entity_id, color_temp_kelvin}——多一
    个亮度键就会落 lux passthrough 直控（抢闭环写亮度）。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)
    m = make_ci_manager()
    svc = []
    m.hass.services = _RecordingServices(svc)
    m.hass.states.mapping[VIRT] = _FakeState("off")
    m.hass.states.mapping[LIGHT] = _FakeState("on", 255, 4000)
    calls = []

    def _record(calls):
        async def _fake(entity_id, brightness, kelvin, allow_turn_on=True,
                        transition_s=None):
            calls.append((entity_id, brightness, kelvin))
            return True

        return _fake

    m._async_process_light = _record(calls)
    run(m.async_trigger("test"))
    kelvin_calls = [c for c in svc if c[2].get("entity_id") == LIGHT]
    assert kelvin_calls == [
        ("light", "turn_on", {"entity_id": LIGHT, "color_temp_kelvin": 4000})
    ]


# ---------------------------------------------------------------------------
# 3. 对称监听 × registry 实时构建（无缓存失效面）
# ---------------------------------------------------------------------------


def test_registry_rebuilt_live_manager_drops_unloaded_zone(caplog):
    """unload 边界（manager 侧）：lux 条目卸载出桶 → 反查表即刻失效 →
    _ci_light_set 不再含该灯；下一次动作路径失效告警每 manager 恰一次
    （WARNING+跳过，保留存储），第二次不再响。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)
    m = make_ci_manager()
    assert m._ci_light_set() == {LIGHT}  # 在表：接管有效

    # 卸载：出桶 + 注册表条目清除（unload 全链的桩等价物）
    del m.hass.data[DOMAIN]["lux"]["lux-test"]
    stubs._ENTITY_REGISTRY._entries_for.pop("lux-test", None)
    assert m._ci_light_set() == set()  # 实时构建，无缓存残留

    with caplog.at_level(logging.WARNING, logger=MANAGER_LOGGER):
        m._warn_unmapped_ci_lights()
        m._warn_unmapped_ci_lights()  # 第二次不再响（每 manager 一次）
    warnings = [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "不属于任何恒照度区域" in warnings[0] and LIGHT in warnings[0]
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)


def test_link_listeners_refresh_drops_unloaded_peer(monkeypatch):
    """unload 边界（lux 侧）：组员出桶后 _refresh_link_listeners 重建——
    对已消失组员的订阅被摘除（幂等重建=先撤旧再按当前桶重订）。"""
    subs: list[tuple[str, tuple]] = []

    def fake_track(hass, entities, action):
        entry = (action.__self__.entry_id, tuple(entities))
        subs.append(entry)
        return lambda: subs.remove(entry)

    monkeypatch.setattr(rt_mod, "async_track_state_change_event", fake_track)

    hass = _make_hass({})

    def _real(entry_id, light, lux):
        entry = SimpleNamespace(
            entry_id=entry_id,
            data={
                "zone_name": entry_id,
                "light_entity": light,
                "lux_entity": lux,
            },
            options={},
        )
        rt = rt_mod.ZoneRuntime(hass, entry)
        hass.data[DOMAIN]["lux"][entry_id] = rt
        return rt

    a = _real("a", "light.a", "sensor.a")
    b = _real("b", "light.b", "sensor.b")
    c = _real("c", "light.c", "sensor.c")
    a.link_group = b.link_group = c.link_group = "卫"
    for rt in (a, b, c):
        rt._refresh_link_listeners()
    a_links = sorted(t for s, t in subs if s == "a")
    assert a_links == [("light.b",), ("light.c",)]

    # b 出桶（unload）→ a 重建联动订阅 → light.b 订阅摘除、light.c 保留
    hass.data[DOMAIN]["lux"].pop("b")
    a._refresh_link_listeners()
    a_links = sorted(t for s, t in subs if s == "a")
    assert a_links == [("light.c",)]


def test_on_linked_event_ignores_stale_mode_after_a3_release():
    """时序钉：A3 归还后 lux runtime 已 IDLE——紧随的灯态变化事件不再排
    评估（_schedule_evaluate 的 mode 门），零任务派生。"""
    states = {LIGHT: _state(brightness=128)}
    hass, a, b = _pair_with_states(states)
    a.mode = "idle"  # 归还后放手态
    created = []
    a.hass.async_create_task = lambda coro: created.append(coro)
    a._on_light_event(SimpleNamespace(data={"new_state": _state("on", 64)}))
    assert created == []  # IDLE 不评估（anl 直控后 lux 零反扑的机制面）


async def _async_true():
    return True
