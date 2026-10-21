"""Manager-level transition tests: takeover semantics, band-entry, plan build."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import conftest as stubs

from custom_components.fumiha_lighting.anl.const import (
    ANCHOR_FIXED,
    CONF_DAY_CI_ENABLED,
    CONF_DAY_CI_VIRTUAL_LIGHT,
    CONF_DAY_ENABLED,
    CONF_END_MODE,
    CONF_END_TIME,
    CONF_END_TRANSITION,
    CONF_EXTRAS,
    CONF_LIGHTS,
    CONF_START_MODE,
    CONF_START_TRANSITION,
    CONF_TRANSITION_ENABLED,
    CONF_TRANSITION_INTERVAL,
    CONF_TRIGGER_TIME,
)
from custom_components.fumiha_lighting.anl.manager import (
    LightState,
    NightLightManager,
    PlanStep,
)

LIGHT = "light.bath"


def make_entry(**overrides):
    data = {
        CONF_LIGHTS: [LIGHT],
        CONF_START_MODE: ANCHOR_FIXED,
        CONF_TRIGGER_TIME: "22:00",
        CONF_END_MODE: ANCHOR_FIXED,
        CONF_END_TIME: "06:00",
        CONF_EXTRAS: [],
        CONF_TRANSITION_ENABLED: True,
        CONF_START_TRANSITION: 60,
        CONF_END_TRANSITION: 60,
        CONF_TRANSITION_INTERVAL: 5,
        CONF_DAY_ENABLED: True,
    }
    data.update(overrides)
    # entry_id：F-086 归还助手读 entry.entry_id 排除自身——真机 ConfigEntry 必有
    # disabled_by/state：F-093 引用判据读这两个真机恒有属性，桩补默认形态
    return SimpleNamespace(
        data=data,
        options={},
        entry_id="anl-test",
        title="自动夜灯",
        disabled_by=None,
        state=stubs.ConfigEntryState.LOADED,
    )


def make_manager(**entry_overrides) -> NightLightManager:
    manager = NightLightManager(
        hass=SimpleNamespace(), entry=make_entry(**entry_overrides)
    )
    manager.hass = SimpleNamespace(
        states=_FakeStates(),
        async_create_task=lambda coro: asyncio.get_event_loop().create_task(coro),
        # F-086：归还统一走 async_release_virtual_light——跨条目检查读
        # config_entries.async_entries（真机 hass 必有；缺省空表=无他组引用）
        config_entries=SimpleNamespace(async_entries=lambda domain: []),
    )
    return manager


class _FakeState:
    def __init__(self, state, brightness=None, kelvin=None, mode=None):
        self.state = state
        self.attributes = {}
        if brightness is not None:
            self.attributes["brightness"] = brightness
        if kelvin is not None:
            self.attributes["color_temp_kelvin"] = kelvin
            self.attributes["color_mode"] = "color_temp"
        self.attributes["supported_color_modes"] = ["color_temp"]
        if mode is not None:
            # F-076：lux 虚拟灯 attributes["mode"]（light.py
            # extra_state_attributes 实测键名），归还判据消费
            self.attributes["mode"] = mode


class _FakeStates:
    def __init__(self):
        self.mapping = {}

    def get(self, entity_id):
        s = self.mapping.get(entity_id)
        return s() if callable(s) else s


# F-075：band_key 三元组 (target, date, from_mode)——本仓夜间带起点在日间
#（from=day），形状与 manager._rebuild_day_plan 产出一致
BAND_KEY = ("night", "2026-10-02", "day")

# 计划步回调的 when 参数（F-060 签名改动）：落在夜间带内的时刻——CI 关的
# 既有用例对该值不敏感（_ci_configured 短路），CI 用例单独传精确时刻
WHEN = datetime(2026, 10, 2, 21, 30)


def run(coro):
    return asyncio.run(coro)


def test_band_entry_offcurve_light_taken_over():
    """入带时已在夜间参数（不在曲线上）→ 判接管，整带不动、零下发。"""
    m = make_manager()
    m.hass.states.mapping[LIGHT] = _FakeState("on", 64, 2200)  # ≈25%/2200K
    step = PlanStep(LIGHT, 100, 4000, BAND_KEY, entrance=True)  # 带入口步
    calls = []
    m._async_process_light = _record(calls)
    run(m._async_run_steps([step], WHEN))
    assert m.machines[LIGHT].manual_override is True
    assert calls == []  # 未被拉回曲线


def test_oncurve_light_rides_with_native_transition():
    """在曲线上的灯正常跟随，且下发带原生 transition=间隔秒。"""
    m = make_manager()
    m.hass.states.mapping[LIGHT] = _FakeState("on", 255, 4000)  # 100%/4000K
    step = PlanStep(LIGHT, 100, 4000, BAND_KEY, entrance=True)
    calls = []
    m._async_process_light = _record(calls)
    run(m._async_run_steps([step], WHEN))
    assert calls == [(LIGHT, 100, 4000, 300)]  # transition = 5min*60
    assert m.machines[LIGHT].expected == (100, 4000)
    assert m.machines[LIGHT].manual_override is False


def test_manual_change_midband_taken_over():
    """带内连续 2 步偏离预期 → 判接管，后续步全跳过 (P2-1：单步不判)。"""
    m = make_manager()
    m.hass.states.mapping[LIGHT] = _FakeState("on", 153, 3100)  # 60%/3100K 手动值
    machine = m.machines[LIGHT]
    machine.band_key = BAND_KEY
    machine.expected = (94, 3915)  # 上一预期
    calls = []
    m._async_process_light = _record(calls)
    # 第 1 步偏离：只计数并观测，不纠正——手动值一次都不被拉回 (P1-2)
    run(m._async_run_steps([PlanStep(LIGHT, 88, 3830, BAND_KEY)], WHEN))
    assert machine.manual_override is False
    assert machine.mismatch_steps == 1
    assert calls == []
    # 第 2 步仍偏离：判接管
    run(m._async_run_steps([PlanStep(LIGHT, 81, 3745, BAND_KEY)], WHEN))
    assert machine.manual_override is True
    assert calls == []  # 全程零下发
    # 后续步继续跳过（全程零下发——首步观测不纠正，接管后更不干预）
    run(m._async_run_steps([PlanStep(LIGHT, 75, 3660, BAND_KEY)], WHEN))
    assert len(calls) == 0


def test_state_lag_self_heals_not_takeover():
    """单步滞后（回读慢一步）→ 拉回后恢复正常，不误判接管 (P2-1)。"""
    m = make_manager()
    machine = m.machines[LIGHT]
    machine.band_key = BAND_KEY
    machine.expected = (94, 3915)
    m.hass.states.mapping[LIGHT] = _FakeState("on", 255, 4000)  # 滞后一步
    calls = []
    m._async_process_light = _record(calls)
    run(m._async_run_steps([PlanStep(LIGHT, 88, 3830, BAND_KEY)], WHEN))
    assert machine.manual_override is False  # 单步不接管
    assert machine.expected == (94, 3915)  # 观测步不推进预期
    assert calls == []  # 首次偏离只观测，不纠正 (P1-2)
    # 灯的原生渐变追上曲线（94% 到位）→ 计数清零，恢复正常跟随
    m.hass.states.mapping[LIGHT] = _FakeState("on", 239, 3915)  # ≈94% 已跟上
    run(m._async_run_steps([PlanStep(LIGHT, 81, 3745, BAND_KEY)], WHEN))
    assert machine.mismatch_steps == 0
    assert machine.manual_override is False
    assert calls == [(LIGHT, 81, 3745, 300)]


def test_respect_manual_off_steers_anyway():
    """关闭「尊重手动调节」→ 回退旧行为，偏离也拉回曲线。"""
    m = make_manager(respect_manual=False)
    m.hass.states.mapping[LIGHT] = _FakeState("on", 153, 3100)
    machine = m.machines[LIGHT]
    machine.band_key = BAND_KEY
    machine.expected = (94, 3915)
    calls = []
    m._async_process_light = _record(calls)
    run(m._async_run_steps([PlanStep(LIGHT, 88, 3830, BAND_KEY)], WHEN))
    assert machine.manual_override is False
    assert calls == [(LIGHT, 88, 3830, 300)]


def test_listener_clears_override_on_off_on():
    """关→开重新加入曲线：接管标记清除、预期重置为当前插值。"""
    m = make_manager()
    machine = m.machines[LIGHT]
    machine.manual_override = True
    machine.band_key = BAND_KEY
    m.hass.states.mapping[LIGHT] = _FakeState("on", 255, 4000)
    # 把 now 固定在带内（21:30，dur=60 → 带 21:00~22:00）
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 21, 30)
    event = {
        "old_state": _FakeState("off"),
        "new_state": _FakeState("on", 255, 4000),
        "entity_id": LIGHT,
    }
    run(m._async_light_state_changed(SimpleNamespace(data=event)))
    assert machine.manual_override is False
    # 21:30 位于 21:00~22:00 带内 factor=0.5 → 预期 = (62, 3100)
    assert machine.expected == (62, 3100)
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)


def test_rebuild_schedules_aligned_step_points():
    """定点调度：步进点精确落在带起点 + k×间隔，末步=锚点。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)
    stubs.POINT_IN_TIME_CALLS.clear()
    m = make_manager()
    m._rebuild_day_plan()
    whens = sorted(w for w, _ in stubs.POINT_IN_TIME_CALLS)
    # 夜间开始带：22:00 锚点、60min → 21:00..21:55 步 + 22:00 锚点步
    # （幂等：async_trigger 先归位后，计划步 _matches 短路不会二次下发）
    night = [
        datetime(2026, 10, 2, 21, 0) + __import__("datetime").timedelta(minutes=k)
        for k in range(0, 65, 5)
    ]
    # 日间结束带：06:00 锚点、60min（今天已过 → 明天）
    from datetime import timedelta

    day = [datetime(2026, 10, 3, 5, 0) + timedelta(minutes=k) for k in range(0, 65, 5)]
    assert whens == sorted(night + day)
    # 每个时刻都有取消句柄配对
    assert len(stubs.POINT_IN_TIME_CALLS) == len(whens)


def test_midband_rebuild_no_false_takeover():
    """带中途重建后首观测步非入口 → 不误判接管，拉回一次后正常 (P1 钉)。"""
    m = make_manager()
    m.hass.states.mapping[LIGHT] = _FakeState("on", 240, 3850)  # ≈94%/3850K
    machine = m.machines[LIGHT]
    machine.band_key = None  # 重建后尚未观测
    mid_step = PlanStep(LIGHT, 88, 3830, BAND_KEY, entrance=False)
    calls = []
    m._async_process_light = _record(calls)
    run(m._async_run_steps([mid_step], WHEN))
    assert machine.manual_override is False
    assert calls == [(LIGHT, 88, 3830, 300)]
    assert machine.expected == (88, 3830)


def test_off_light_band_entry_not_overridden():
    """入带时灯是关的 → 不判接管 (P2a)。"""
    m = make_manager()
    m.hass.states.mapping[LIGHT] = _FakeState("off")
    step = PlanStep(LIGHT, 100, 4000, BAND_KEY, entrance=True)
    calls = []
    m._async_process_light = _record(calls)
    run(m._async_run_steps([step], WHEN))
    assert m.machines[LIGHT].manual_override is False


def test_service_failure_keeps_expected_and_retries():
    """下发失败 → 预期不推进，下一步不误判接管并重试 (P2b 钉)。"""
    m = make_manager()
    m.hass.states.mapping[LIGHT] = _FakeState("on", 255, 4000)
    machine = m.machines[LIGHT]
    machine.band_key = BAND_KEY
    machine.expected = (100, 4000)
    calls = []
    m._async_process_light = _record(calls, ok=False)
    run(m._async_run_steps([PlanStep(LIGHT, 94, 3915, BAND_KEY)], WHEN))
    assert len(calls) == 1
    assert machine.manual_override is False
    assert machine.expected == (100, 4000)
    run(m._async_run_steps([PlanStep(LIGHT, 88, 3830, BAND_KEY)], WHEN))
    assert len(calls) == 2


def test_rebuild_dedupes_overlapping_bands():
    """重叠带同刻同灯只留一条步进，extra 带胜出 (P2c 钉)。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)
    stubs.POINT_IN_TIME_CALLS.clear()
    m = make_manager(
        **{
            CONF_EXTRAS: [
                {
                    "start": "21:30",
                    "transition": 60,
                    "brightness": 60,
                    "color_temp_kelvin": 3000,
                }
            ]
        }
    )
    m._rebuild_day_plan()
    seen = {}
    dup = 0
    for when, steps in m._plan.items():
        for step in steps:
            key = (when, step.entity_id)
            if key in seen:
                dup += 1
            seen[key] = step.band_key
    assert dup == 0
    # 重叠区 21:00~21:25：extra 带胜出（列表序靠前）
    assert seen[(datetime(2026, 10, 2, 21, 15), LIGHT)][0] == "extra_0"
    # extra 带自己的 21:30 锚点步被夜间带窗口覆盖 → 不生成；
    # 21:30 的条目来自夜间带（其内部步），无重复
    assert seen[(datetime(2026, 10, 2, 21, 30), LIGHT)][0] == "night"
    assert seen[(datetime(2026, 10, 2, 21, 45), LIGHT)][0] == "night"


def test_entry_accepts_last_applied_params():
    """常亮灯从未收到 from-mode 参数 → 入口判定接受我方最后下发值 (P2-3)。"""
    m = make_manager()
    m.hass.states.mapping[LIGHT] = _FakeState("on", 64, 2200)  # 25%/2200K
    machine = m.machines[LIGHT]
    machine.applied = (25, 2200)  # 我方此前下发过夜间参数（跨日界常亮）
    step = PlanStep(LIGHT, 100, 4000, BAND_KEY, entrance=True)  # 入口预期=日间
    calls = []
    m._async_process_light = _record(calls)
    run(m._async_run_steps([step], WHEN))
    assert machine.manual_override is False  # 匹配 applied → 在曲线
    assert calls == [(LIGHT, 100, 4000, 300)]
    assert machine.expected == (100, 4000)


def test_settle_failure_clears_seed():
    """开灯 settle 下发失败 → 播种值清空，下一步不误判接管 (P2-2)。"""
    m = make_manager()
    machine = m.machines[LIGHT]
    machine.band_key = BAND_KEY
    machine.expected = (62, 3100)  # 监听播种值
    m._async_process_light = _record([], ok=False)  # 下发失败
    run(m._async_settle_apply(LIGHT, 62, 3100))
    assert machine.expected is None
    assert machine.manual_override is False


def test_real_process_light_payload_and_contract():
    """真实 _async_process_light：服务载荷含 brightness/kelvin/transition，
    契约 = 匹配返回 True 不下发、离线返回 False (P3-6.3)。"""
    from homeassistant.const import SERVICE_TURN_ON

    sent = []

    class _Services:
        async def async_call(self, domain, service, data, blocking):
            assert domain == "light" and service == SERVICE_TURN_ON
            sent.append(data)

    m = make_manager()
    m.hass.services = _Services()
    m.hass.states.mapping[LIGHT] = _FakeState("on", 255, 4000)
    ok = run(
        m._async_process_light(LIGHT, 25, 2200, allow_turn_on=False, transition_s=300)
    )
    assert ok is True
    assert sent[-1]["brightness"] == 64  # _to_byte(25)
    assert sent[-1]["color_temp_kelvin"] == 2200
    assert sent[-1]["transition"] == 300
    m.hass.states.mapping[LIGHT] = _FakeState("on", 64, 2200)  # 已在目标
    ok = run(m._async_process_light(LIGHT, 25, 2200, allow_turn_on=False))
    assert ok is True
    assert len(sent) == 1  # 已匹配不下发
    m.hass.states.mapping[LIGHT] = _FakeState("unavailable")
    ok = run(m._async_process_light(LIGHT, 25, 2200, allow_turn_on=False))
    assert ok is False


def test_rebuild_twice_no_handle_leak():
    """连续两次重建：句柄取消生效，POINT_IN_TIME_CALLS 不累积 (P3-6.4)。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)
    stubs.POINT_IN_TIME_CALLS.clear()
    m = make_manager()
    m._rebuild_day_plan()
    first = len(stubs.POINT_IN_TIME_CALLS)
    assert first > 0
    m._rebuild_day_plan()
    assert len(stubs.POINT_IN_TIME_CALLS) == first  # 旧句柄已取消移除


def test_shared_anchor_minute_deduped():
    """两带共享锚点分钟（半开窗口都不含）→ 兜底去重保留列表序靠前带 (P1-1)。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)
    stubs.POINT_IN_TIME_CALLS.clear()
    m = make_manager(
        **{
            CONF_EXTRAS: [
                {
                    "start": "22:00",
                    "transition": 60,
                    "brightness": 60,
                    "color_temp_kelvin": 3000,
                }
            ]
        }
    )
    m._rebuild_day_plan()
    for when, steps in m._plan.items():
        entities = [st.entity_id for st in steps]
        assert len(entities) == len(set(entities)), f"同刻重复步进: {when}"
    # 22:00 平局归 extra_0（列表序靠前，与 _mode_at 一致）
    seen = {
        when: steps[0].band_key[0]
        for when, steps in m._plan.items()
        if when == datetime(2026, 10, 2, 22, 0)
    }
    assert seen[datetime(2026, 10, 2, 22, 0)] == "extra_0"


def test_sun_entity_datetime_attribute():
    """sun.sun 的 next_* 属性是原生 datetime 对象——不抛错、直接采用 (P1-3)。"""
    m = make_manager(sun_entity="sensor.custom_sun", start_mode="sunset")
    dt_value = datetime(2026, 10, 2, 18, 30)
    m.hass.states = SimpleNamespace(
        get=lambda eid: SimpleNamespace(
            state="above_horizon",
            attributes={"next_setting": dt_value, "next_rising": dt_value},
        )
    )
    result = m._sun_dt("next_setting", "sunset", stubs._FakeClock.now_dt.date())
    assert result == dt_value


def _record(calls, ok=True):
    async def _fake_process(
        entity_id, brightness, kelvin, allow_turn_on=True, transition_s=None
    ):
        calls.append((entity_id, brightness, kelvin, transition_s))
        return ok  # 模拟下发成功（P2b：失败时预期不推进）

    return _fake_process


# ---------------------------------------------------------------------------
# 日间恒照度联动（V1.5.0 CI）
# ---------------------------------------------------------------------------

VIRT = "light.virt"


class _RecordingServices:
    """services.async_call 记录桩：记录 (domain, service, data) 序列。"""

    def __init__(self, calls, fail=False):
        self.calls = calls
        self.fail = fail

    async def async_call(self, domain, service, data, blocking=False):
        if self.fail:
            raise RuntimeError("virtual light service failed")
        self.calls.append((domain, service, dict(data)))


def make_ci_manager(**extra_overrides):
    overrides = {
        CONF_DAY_CI_ENABLED: True,
        CONF_DAY_CI_VIRTUAL_LIGHT: VIRT,
    }
    overrides.update(extra_overrides)
    return make_manager(**overrides)


def test_day_trigger_ci_hands_over_to_virtual_light():
    """day trigger + CI：只开虚拟灯（无亮度载荷），真实灯零下发、machine 归零。"""
    m = make_ci_manager()
    svc = []
    m.hass.services = _RecordingServices(svc)
    # F-068：ensure 现带状态预检——虚拟灯须在 states 中可见才走服务路径
    m.hass.states.mapping[VIRT] = _FakeState("off")
    calls = []
    m._async_process_light = _record(calls)
    machine = m.machines[LIGHT]
    machine.state = LightState.SETTING  # 非初值，验证被置回 IDLE
    machine.expected = (1, 1)
    machine.applied = (1, 1)
    run(m.async_trigger("test"))
    assert svc == [("light", "turn_on", {"entity_id": VIRT})]  # 无亮度/亮度百分比键
    assert calls == []  # 真实灯零下发
    assert machine.state == LightState.IDLE
    assert machine.expected is None


def test_night_trigger_ci_returns_virtual_then_drives_real():
    """night trigger + CI：先关虚拟灯归还（lux 纯放手），再按夜间参数直控。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)
    m = make_ci_manager()
    events = []
    # F-068：夜间归还路径同样过状态预检——虚拟灯须可见（本场景=CI 开着）
    m.hass.states.mapping[VIRT] = _FakeState("on", mode="active")

    class _Svc:
        async def async_call(self, domain, service, data, blocking=False):
            events.append(("svc", domain, service, data.get("entity_id")))

    async def _fake_process(
        entity_id, brightness, kelvin, allow_turn_on=True, transition_s=None
    ):
        events.append(("process", entity_id, brightness, kelvin, transition_s))
        return True

    m.hass.services = _Svc()
    m._async_process_light = _fake_process
    run(m.async_trigger("test"))
    assert events == [
        ("svc", "light", "turn_off", VIRT),
        ("process", LIGHT, 25, 2200, None),
    ]
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)


def test_listener_day_ci_hands_over_without_settle():
    """turn-on 监听 day+CI：开虚拟灯（无亮度）、零 settle 延迟、machine 归零。"""
    m = make_ci_manager()
    svc = []
    m.hass.services = _RecordingServices(svc)
    # F-068：日间接管开虚拟灯——虚拟灯须在 states 中可见
    m.hass.states.mapping[VIRT] = _FakeState("off")
    machine = m.machines[LIGHT]
    machine.manual_override = True
    machine.expected = (1, 1)
    machine.applied = (1, 1)
    machine.state = LightState.MISMATCH
    stubs.CALL_LATER_CALLS.clear()
    event = {
        "old_state": _FakeState("off"),
        "new_state": _FakeState("on", 255, 4000),
        "entity_id": LIGHT,
    }
    run(m._async_light_state_changed(SimpleNamespace(data=event)))
    assert svc == [("light", "turn_on", {"entity_id": VIRT})]
    assert stubs.CALL_LATER_CALLS == []  # 无 settle 延迟调度
    assert machine.state == LightState.IDLE
    assert machine.expected is None


def test_rebuild_day_plan_ci_drops_day_side_bands():
    """CI 时 from/target 含 day 的带不生成步进；night↔extra 带不受影响。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)
    stubs.POINT_IN_TIME_CALLS.clear()
    m = make_ci_manager()
    m._rebuild_day_plan()
    # start 带 from=day、end 带 target=day → 全部跳过
    assert stubs.POINT_IN_TIME_CALLS == []
    assert m._plan == {}

    # 夜间窗口内的 extra 带（from=night, target=extra_0）不受 CI 影响
    stubs.POINT_IN_TIME_CALLS.clear()
    m2 = make_ci_manager(
        **{
            CONF_EXTRAS: [
                {
                    "start": "23:30",
                    "transition": 60,
                    "brightness": 60,
                    "color_temp_kelvin": 3000,
                }
            ]
        }
    )
    m2._rebuild_day_plan()
    assert stubs.POINT_IN_TIME_CALLS  # 只有 extra 带生成步进
    for when, _action in stubs.POINT_IN_TIME_CALLS:
        assert datetime(2026, 10, 2, 22, 30) <= when <= datetime(2026, 10, 2, 23, 30)
    assert m2._plan

    # 回归钉：CI 关（键仍在）→ 恢复原有两带步进
    stubs.POINT_IN_TIME_CALLS.clear()
    m3 = make_manager(**{CONF_DAY_CI_ENABLED: False, CONF_DAY_CI_VIRTUAL_LIGHT: VIRT})
    m3._rebuild_day_plan()
    whens = sorted(w for w, _ in stubs.POINT_IN_TIME_CALLS)
    assert datetime(2026, 10, 2, 21, 0) in whens  # 夜间开始带（今日 21:00–22:00）
    assert datetime(2026, 10, 3, 5, 0) in whens  # 日间结束带（次日 05:00–06:00）


def test_ci_virtual_service_failure_falls_back_to_day_params(caplog):
    """虚拟灯服务失败 → WARNING 节流一次 + 回落日间参数直控并播种。"""
    import logging

    m = make_ci_manager()
    svc = []
    m.hass.services = _RecordingServices(svc, fail=True)
    # F-068：预检须放行（虚拟灯可见），才能打到既有服务失败分支
    m.hass.states.mapping[VIRT] = _FakeState("off")
    calls = []
    m._async_process_light = _record(calls)
    logger_name = "custom_components.fumiha_lighting.anl.manager"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        run(m.async_trigger("test"))
    assert svc == []
    assert calls == [(LIGHT, 100, 4000, None)]  # 日间参数直控
    machine = m.machines[LIGHT]
    assert machine.expected == (100, 4000)
    assert machine.applied == (100, 4000)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    # 同 manager 实例第二次触发：WARNING 不再重复（节流），仍回落直控
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=logger_name):
        run(m.async_trigger("test"))
    assert len(calls) == 2
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 0


def test_listener_night_ci_returns_left_on_virtual_light():
    """turn-on 监听 night+CI 且虚拟灯亮着 → 先关虚拟灯再走既有 settle 路径。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)
    m = make_ci_manager()
    svc = []
    m.hass.services = _RecordingServices(svc)
    # 虚拟灯亮（lux mode=active）→ 防御性归还
    m.hass.states.mapping[VIRT] = _FakeState("on", mode="active")
    stubs.CALL_LATER_CALLS.clear()
    event = {
        "old_state": _FakeState("off"),
        "new_state": _FakeState("on", 255, 4000),
        "entity_id": LIGHT,
    }
    run(m._async_light_state_changed(SimpleNamespace(data=event)))
    assert svc == [("light", "turn_off", {"entity_id": VIRT})]
    assert len(stubs.CALL_LATER_CALLS) == 1  # 既有 settle 延迟恰好一条
    machine = m.machines[LIGHT]
    assert machine.state == LightState.TURN_ON_PENDING
    assert machine.expected == (25, 2200)  # 夜间参数播种
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)


def test_day_trigger_ci_disabled_direct_control():
    """CI 关（键在但 enabled=False）→ day trigger 走既有直控，零虚拟灯调用。"""
    m = make_manager(**{CONF_DAY_CI_ENABLED: False, CONF_DAY_CI_VIRTUAL_LIGHT: VIRT})
    svc = []
    m.hass.services = _RecordingServices(svc)
    calls = []
    m._async_process_light = _record(calls)
    run(m.async_trigger("test"))
    assert svc == []  # 零虚拟灯调用
    assert calls == [(LIGHT, 100, 4000, None)]
    assert m.machines[LIGHT].expected == (100, 4000)
