"""V1.5.0 R2 复验修复批（wb 批 anl-v15-ci-20261019-r2）的回归钉。

覆盖 F-065（CI 生效判据含 day_enabled：manager/web/flow/快照四面）、
F-066（extra 起点屏障：带内计划步让位 + transition=0 落带告警）、
F-067（归还钩子跨条目检查：web/flow/删条目三挂点）、F-068（虚拟灯
可用性预检：缺失/不可用回落 + WARNING 节流）、F-069（extra 回调
try/finally 重排）、F-070（extra 同刻 start 去重，schema 面见
test_anl_config_schema）、F-071（计划步守卫用实际触发时刻）、
F-072（触发轮级虚拟灯调用 + extra 并发下发）。

桩复用 test_anl_transitions（make_manager 家族）与 test_web（FakeHass
家族）；F-073 的 domain 分支修正落在 test_web 原用例内。
"""

from __future__ import annotations

import logging
from datetime import datetime
from types import SimpleNamespace

import conftest as stubs
import pytest
from test_anl_transitions import (
    BAND_KEY,
    LIGHT,
    _FakeState,
    _record,
    _RecordingServices,
    make_ci_manager,
    make_manager,
)
from test_web import (
    FakeHass,
    FakeRequest,
    _anl_bucket,
    _config_entry,
    _config_manager,
    _recording_hass,
    fake_anl_manager,
    recorded,
    run,
)

import custom_components.fumiha_lighting as pkg
from custom_components.fumiha_lighting import web
from custom_components.fumiha_lighting.anl.const import (
    CONF_DAY_CI_LIGHTS,
    CONF_DAY_CI_VIRTUAL_LIGHT,
    CONF_DAY_ENABLED,
    CONF_EXTRAS,
    CONF_LIGHTS,
    CONF_START_TRANSITION,
)
from custom_components.fumiha_lighting.anl.flow import AnlGroupOptionsFlow
from custom_components.fumiha_lighting.anl.manager import LightState, PlanStep

VIRT = "light.virt"
MANAGER_LOGGER = "custom_components.fumiha_lighting.anl.manager"
WEB_LOGGER = "custom_components.fumiha_lighting.web"
PKG_LOGGER = "custom_components.fumiha_lighting"
# 双灯配置（F-072 轮级调用断言用）
CONF_LIGHTS_DUAL = {CONF_LIGHTS: [LIGHT, "light.second"]}

_TIMES = {
    "start_mode": "fixed",
    "end_mode": "fixed",
    "trigger_time": "22:00",
    "end_time": "06:00",
    "start_offset": 0,
    "end_offset": 0,
    "sun_entity": "sun.sun",
    "start_transition": 0,
    "end_transition": 0,
    "transition_interval": 5,
}
_SETTINGS = {
    "day_enabled": False,
    "brightness": 25,
    "color_temp_kelvin": 2200,
    "day_brightness": 100,
    "day_color_temp_kelvin": 4000,
    "day_ci_enabled": False,
    "day_ci_virtual_light": None,
    "tolerance_brightness": 4,
    "tolerance_kelvin": 150,
    "verify_delay": 5,
    "settle_delay": 1,
    "turn_on_listen": True,
    "only_when_on": False,
    "respect_manual": True,
}
_EXTRA_18 = [
    {"start": "18:00", "transition": 0, "brightness": 60, "color_temp_kelvin": 3000}
]


def _svc_recorder(events):
    async def _svc(domain, service, data, blocking=False):
        events.append(("svc", service, data.get("entity_id")))

    return _svc


def _ci_entry(**extra):
    """带 CI on+virtX+day_enabled 的 anl 条目（web 用例基座）。"""
    data = {
        "kind": "anl",
        "lights": ["light.a"],
        CONF_DAY_ENABLED: True,
        CONF_DAY_CI_LIGHTS: ["light.a"],
        CONF_DAY_CI_VIRTUAL_LIGHT: VIRT,
    }
    data.update(extra)
    return _config_entry(data=data)


# ---------------------------------------------------------------------------
# F-065：CI 生效判据含 day_enabled
# ---------------------------------------------------------------------------


def test_ci_configured_requires_day_enabled():
    """day_enabled=False + CI 键在 → _ci_configured False；夜间触发零虚拟灯调用。"""
    m = make_ci_manager(**{CONF_DAY_ENABLED: False})
    assert m._ci_configured() is False
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)
    svc = []
    m.hass.services = _RecordingServices(svc)
    calls = []
    m._async_process_light = _record(calls)
    run(m.async_trigger("test"))
    assert svc == []  # 不开虚拟灯、也不归还（CI 从未接管）
    assert calls == [(LIGHT, 25, 2200, None)]  # 纯夜间直控
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)


def test_extra_start_day_disabled_zero_virtual_calls():
    """day_enabled=False + CI 键在 → extra 起点零虚拟灯调用（不误关手开灯）。"""
    m = make_ci_manager(**{CONF_DAY_ENABLED: False, CONF_EXTRAS: _EXTRA_18})
    svc = []
    m.hass.services = _RecordingServices(svc)
    calls = []
    m._async_process_light = _record(calls)
    run(m._async_extra_start(0))
    assert svc == []
    assert calls == [(LIGHT, 60, 3000, None)]


def test_web_post_ci_on_day_disabled_400():
    """POST 时 CI 开而 day_enabled 缺失 → merged 级跨字段 400，零落盘零动灯。"""
    hass = _recording_hass()
    entry = _config_entry(
        data={
            "kind": "anl",
            "lights": ["light.a"],
            CONF_DAY_CI_LIGHTS: ["light.a"],
            CONF_DAY_CI_VIRTUAL_LIGHT: VIRT,
        }
    )  # 刻意不带 day_enabled 键 = 日间模式关
    _anl_bucket(hass, _config_manager(entry))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {"brightness": 30}}})
        )
    )
    assert resp.status == 400
    assert "day_enabled" in resp.payload["error"]
    assert recorded == []
    assert hass.services.calls == []


def test_options_flow_ci_without_day_enabled_reshows_form():
    """options 流 CI on + day_enabled False → 表单错误 base，不落 _settings。"""
    opts = AnlGroupOptionsFlow()
    opts.config_entry = SimpleNamespace(
        data={"kind": "anl", "lights": ["light.a"],
              CONF_DAY_CI_LIGHTS: ["light.a"], CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
        options={},
    )
    opts.hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda eid: None),
        config_entries=SimpleNamespace(async_entries=lambda domain: []),
    )
    run(opts.async_step_init(None))
    run(opts.async_step_anl_init(_TIMES))
    result = run(opts.async_step_settings({
        **_SETTINGS,  # day_enabled=False
        "day_ci_enabled": True,
        "day_ci_virtual_light": VIRT,
    }))
    assert result["type"] == "form"
    assert result["errors"] == {"base": "day_ci_needs_day_enabled"}


def test_snapshot_day_ci_active_false_when_day_disabled():
    """快照 day_ci_active 跟随含 day_enabled 的生效判据（enabled+灯在但日间关）。"""
    hass = FakeHass()
    _anl_bucket(
        hass,
        fake_anl_manager(
            day_enabled=False, day_ci_lights=["light.a"], day_ci_virtual_light=VIRT
        ),
    )
    resp = run(web.AnlSnapshotView().get(FakeRequest(hass)))
    assert resp.payload["managers"][0]["day_ci_active"] is False

    hass2 = FakeHass()
    _anl_bucket(
        hass2,
        fake_anl_manager(
            day_enabled=True, day_ci_lights=["light.a"], day_ci_virtual_light=VIRT
        ),
    )
    resp2 = run(web.AnlSnapshotView().get(FakeRequest(hass2)))
    assert resp2.payload["managers"][0]["day_ci_active"] is True


# ---------------------------------------------------------------------------
# F-066：extra 起点屏障 + transition=0 落带告警
# ---------------------------------------------------------------------------


def test_extra_in_band_plan_steps_yield_to_extra(caplog):
    """extra 21:00/transition=0 落在夜带（20:00-22:00）内：夜带插值步让位、
    extra 自带 band_key 步照跑、22:00 夜带锚点步照跑；配置面 WARNING。"""
    m = make_manager(
        **{
            CONF_START_TRANSITION: 120,
            CONF_EXTRAS: [
                {"start": "21:00", "transition": 0,
                 "brightness": 60, "color_temp_kelvin": 3000}
            ],
        }
    )
    m.hass.states.mapping[LIGHT] = _FakeState("on", 255, 4000)
    calls = []
    m._async_process_light = _record(calls)
    # F-075：band_key 三元组——夜带 from=day（20:00 时最近锚点是 06:00 日间）、
    # 假想 extra 带起点在夜间窗口内 from=night
    night_step = PlanStep(LIGHT, 94, 3915, ("night", "2026-10-02", "day"))
    extra_step = PlanStep(LIGHT, 60, 3000, ("extra_0", "2026-10-02", "night"))
    run(m._async_run_step(night_step, datetime(2026, 10, 2, 21, 30)))
    assert calls == []  # extra 时段接管中，夜带插值步让位
    run(m._async_run_step(extra_step, datetime(2026, 10, 2, 21, 30)))
    assert calls == [(LIGHT, 60, 3000, 300)]  # extra 自己带的步照跑
    calls.clear()
    run(m._async_run_step(night_step, datetime(2026, 10, 2, 22, 0)))
    assert calls == [(LIGHT, 94, 3915, 300)]  # 夜带锚点步正常归位
    with caplog.at_level(logging.WARNING, logger=MANAGER_LOGGER):
        m._warn_overlapping_bands()
    assert any(
        "落在过渡带内" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    )


# ---------------------------------------------------------------------------
# F-067：归还钩子跨条目检查
# ---------------------------------------------------------------------------


def test_web_post_release_skipped_when_other_entry_shares_virtual(caplog):
    """B 条目仍引用同一虚拟灯 → A 关 CI 保存：零 turn_off + INFO，落盘照常。"""
    entry_a = _ci_entry()
    entry_b = _config_entry(
        entry_id="anl2",
        data={"kind": "anl", "lights": ["light.z"],
              CONF_DAY_ENABLED: True, CONF_DAY_CI_LIGHTS: ["light.a"],
              CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
    )
    hass = _recording_hass()
    # F-083：归还走助手 lux mode 守卫——桩给出闭环在跑的真实形态
    hass.states = SimpleNamespace(
        get=lambda eid: SimpleNamespace(state="on", attributes={"mode": "active"})
        if eid == VIRT
        else None
    )
    hass.config_entries.async_entries = lambda domain: [entry_a, entry_b]
    _anl_bucket(hass, _config_manager(entry_a))
    # F-083：归还助手落 anl/manager.py——「跳过归还」INFO 由 manager logger 发出
    with caplog.at_level(logging.INFO, logger=MANAGER_LOGGER):
        resp = run(
            web.AnlConfigView().post(
                FakeRequest(hass, {"entry_id": "anl1",
                                   "patch": {"params": {"day_ci_lights": []}}})
            )
        )
    assert resp.payload["ok"] is True
    assert hass.services.calls == []  # 归还被跨条目检查跳过
    assert recorded[0][1]["options"]["day_ci_lights"] == []  # 保存不受影响
    assert any(
        "跳过归还" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO
    )


def test_remove_entry_release_skipped_when_other_entry_shares_virtual(caplog):
    """删除 A 条目时 B 仍引用同虚拟灯 → 零 turn_off + INFO。"""
    hass = FakeHass()
    entry_a = SimpleNamespace(
        entry_id="anl1",
        data={"kind": "anl", "lights": ["light.a"],
              CONF_DAY_CI_LIGHTS: ["light.a"], CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
        options={},
    )
    entry_b = SimpleNamespace(
        entry_id="anl2",
        data={"kind": "anl", "lights": ["light.z"],
              CONF_DAY_ENABLED: True, CONF_DAY_CI_LIGHTS: ["light.a"],
              CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
        options={},
        title="自动夜灯B",
        disabled_by=None,  # F-093：引用判据读真机 ConfigEntry 恒有的两属性
        state=stubs.ConfigEntryState.LOADED,
    )
    hass.states = SimpleNamespace(  # F-083：助手 mode 守卫，见 web 用例
        get=lambda eid: SimpleNamespace(state="on", attributes={"mode": "active"})
        if eid == VIRT
        else None
    )
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [entry_a, entry_b]
    )
    with caplog.at_level(logging.INFO, logger=PKG_LOGGER):
        run(pkg.async_remove_entry(hass, entry_a))
    assert hass.services.calls == []
    assert any(
        "跳过归还" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO
    )


def test_options_flow_release_skipped_when_other_entry_shares_virtual():
    """options 流归还前跨条目检查——他条目引用 → 跳过 turn_off，保存照常。"""
    events = []
    entry_a = SimpleNamespace(
        entry_id="anl1",
        data={"kind": "anl", "lights": ["light.a"],
              CONF_DAY_CI_LIGHTS: ["light.a"], CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
        options={},
    )
    entry_b = SimpleNamespace(
        entry_id="anl2",
        data={"kind": "anl", "lights": ["light.z"],
              CONF_DAY_ENABLED: True, CONF_DAY_CI_LIGHTS: ["light.a"],
              CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
        options={},
        title="自动夜灯B",
        disabled_by=None,  # F-093：引用判据读真机 ConfigEntry 恒有的两属性
        state=stubs.ConfigEntryState.LOADED,
    )
    opts = AnlGroupOptionsFlow()
    opts.config_entry = entry_a
    opts.hass = SimpleNamespace(
        # 虚拟灯可见（state=on）：预检放行，turn_off 跳过必须由跨条目检查达成
        # （F-083 助手读 attributes["mode"]——本桩不带 mode → 守卫不碰，零
        # turn_off 由 mode 守卫与跨条目检查双重达成）
        states=SimpleNamespace(
            get=lambda eid: SimpleNamespace(state="on", attributes={})
            if eid == VIRT
            else None
        ),
        services=SimpleNamespace(async_call=_svc_recorder(events)),
        config_entries=SimpleNamespace(async_entries=lambda domain: [entry_a, entry_b]),
    )
    real_create = opts.async_create_entry

    def _recording_create(title, data):
        events.append(("entry",))
        return real_create(title, data)

    opts.async_create_entry = _recording_create
    run(opts.async_step_init(None))
    run(opts.async_step_anl_init(_TIMES))
    final = run(opts.async_step_settings({
        **_SETTINGS,
        "day_ci_enabled": False,
        "day_ci_virtual_light": VIRT,
    }))
    assert final["type"] == "create_entry"
    assert events == [("entry",)]  # 无 turn_off——归还被跨条目检查跳过


def test_web_post_release_turns_off_when_reference_is_unique():
    """对照钉：唯一引用（无其他条目）→ turn_off 照旧（既有语义不回退）。"""
    hass = _recording_hass()
    hass.states = SimpleNamespace(  # F-083：助手 mode 守卫，闭环在跑
        get=lambda eid: SimpleNamespace(state="on", attributes={"mode": "active"})
        if eid == VIRT
        else None
    )
    _anl_bucket(hass, _config_manager(_ci_entry()))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {"day_ci_lights": []}}})
        )
    )
    assert resp.payload["ok"] is True
    assert hass.services.calls == [("light", "turn_off", {"entity_id": VIRT})]


# ---------------------------------------------------------------------------
# F-068：虚拟灯可用性预检
# ---------------------------------------------------------------------------


def test_day_trigger_ci_virtual_missing_falls_back(caplog):
    """虚拟灯不在 states → 预检失败回落日间参数直控；WARNING 恰 1 条后节流。"""
    m = make_ci_manager()
    svc = []
    m.hass.services = _RecordingServices(svc)  # states 空 → 预检必败
    calls = []
    m._async_process_light = _record(calls)
    with caplog.at_level(logging.WARNING, logger=MANAGER_LOGGER):
        run(m.async_trigger("test"))
    assert svc == []  # 未发出 turn_on（假成功防线）
    assert calls == [(LIGHT, 100, 4000, None)]  # 日间参数 fallback 直控
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=MANAGER_LOGGER):
        run(m.async_trigger("test"))
    assert len(calls) == 2  # 仍回落直控
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 0
    assert any(
        "仍不可用" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.DEBUG
    )


def test_day_trigger_ci_virtual_unavailable_falls_back(caplog):
    """虚拟灯 state=unavailable → 同预检回落路径，恰 1 条 WARNING。"""
    m = make_ci_manager()
    m.hass.states.mapping[VIRT] = _FakeState("unavailable")
    svc = []
    m.hass.services = _RecordingServices(svc)
    calls = []
    m._async_process_light = _record(calls)
    with caplog.at_level(logging.WARNING, logger=MANAGER_LOGGER):
        run(m.async_trigger("test"))
    assert svc == []
    assert calls == [(LIGHT, 100, 4000, None)]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_night_trigger_ci_virtual_missing_zero_calls():
    """夜间触发归还路径同样过预检：虚拟灯缺失 → 零服务调用、夜间直控照常。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)
    m = make_ci_manager()
    svc = []
    m.hass.services = _RecordingServices(svc)  # 虚拟灯不在 states
    calls = []
    m._async_process_light = _record(calls)
    run(m.async_trigger("test"))
    assert svc == []  # 归还预检失败，不发 turn_off
    assert calls == [(LIGHT, 25, 2200, None)]
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)


# ---------------------------------------------------------------------------
# F-069 / F-070（运行时面）：extra 回调重排 + 同刻去重
# ---------------------------------------------------------------------------


def test_extra_start_callback_exception_still_rearms():
    """回调抛异常 → try/finally 重排：次日句柄仍然注册。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)
    stubs.POINT_IN_TIME_CALLS.clear()
    m = make_ci_manager(**{CONF_EXTRAS: _EXTRA_18})
    m.start()
    when, action = next(
        (w, a)
        for w, a in stubs.POINT_IN_TIME_CALLS
        if w == datetime(2026, 10, 2, 18, 0)
    )

    async def _boom(index):
        raise RuntimeError("extra start exploded")

    m._async_extra_start = _boom
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 18, 0)  # 触发时刻的 now
    with pytest.raises(RuntimeError):
        run(action(None))
    assert datetime(2026, 10, 3, 18, 0) in [w for w, _ in stubs.POINT_IN_TIME_CALLS]


def test_schedule_extra_starts_dedupes_same_start():
    """同刻双 extra 只注册首个 index（_mode_at 列表序平局语义）；重排不泄漏。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)
    stubs.POINT_IN_TIME_CALLS.clear()
    dup = [
        {"start": "18:00", "transition": 0,
         "brightness": 60, "color_temp_kelvin": 3000},
        {"start": "18:00", "transition": 0,
         "brightness": 80, "color_temp_kelvin": 3500},
    ]
    m = make_manager(**{CONF_EXTRAS: dup})
    m.start()
    fired = [
        (w, a)
        for w, a in stubs.POINT_IN_TIME_CALLS
        if w == datetime(2026, 10, 2, 18, 0)
    ]
    assert len(fired) == 1  # 同刻只注册首个 index
    calls = []
    m._async_process_light = _record(calls)
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 18, 0)
    run(fired[0][1](None))
    assert calls == [(LIGHT, 60, 3000, None)]  # index 0 胜出
    rearm = [
        (w, a)
        for w, a in stubs.POINT_IN_TIME_CALLS
        if w == datetime(2026, 10, 3, 18, 0)
    ]
    assert len(rearm) == 1  # 次日恰好一条，无重复无泄漏


# ---------------------------------------------------------------------------
# F-071：计划步守卫用实际触发时刻
# ---------------------------------------------------------------------------


def test_plan_step_callback_guard_uses_actual_now():
    """计划时刻在夜带、实际时钟在 CI 日间 → 守卫按实际时刻跳过（迟到步防绕过）。"""
    m = make_ci_manager()
    m.hass.states.mapping[LIGHT] = _FakeState("on", 255, 4000)
    calls = []
    m._async_process_light = _record(calls)
    tasks = []
    m.hass.async_create_task = lambda coro: tasks.append(coro)
    planned = datetime(2026, 10, 2, 23, 0)  # 计划时刻：夜间
    step = PlanStep(LIGHT, 94, 3915, BAND_KEY)
    cb = m._make_plan_step_callback(planned, [step])
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)  # 实际触发：CI 日间
    cb(None)
    assert len(tasks) == 1
    run(tasks[0])
    assert calls == []  # 旧实现按 when=23:00 会直控——实际日间必须跳过
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)  # 实际触发在夜间
    tasks.clear()
    cb2 = m._make_plan_step_callback(planned, [step])
    cb2(None)
    run(tasks[0])
    assert calls == [(LIGHT, 94, 3915, 300)]


# ---------------------------------------------------------------------------
# F-072：触发轮级虚拟灯调用 + extra 并发下发
# ---------------------------------------------------------------------------


def test_day_trigger_ci_dual_lights_single_turn_on():
    """双灯 day+CI → 虚拟灯 turn_on 恰 1 次（轮级接管），双 machine 归位。"""
    m = make_ci_manager(**CONF_LIGHTS_DUAL)
    svc = []
    m.hass.services = _RecordingServices(svc)
    m.hass.states.mapping[VIRT] = _FakeState("off")
    calls = []
    m._async_process_light = _record(calls)
    run(m.async_trigger("test"))
    assert svc == [("light", "turn_on", {"entity_id": VIRT})]
    assert calls == []  # 真实灯零下发
    assert all(mm.state == LightState.IDLE for mm in m.machines.values())


def test_night_trigger_ci_dual_lights_single_turn_off():
    """双灯夜间触发 → 虚拟灯 turn_off 恰 1 次（gather 之前轮级归还）。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)
    m = make_ci_manager(**CONF_LIGHTS_DUAL)
    svc = []
    m.hass.services = _RecordingServices(svc)
    m.hass.states.mapping[VIRT] = _FakeState("on", mode="active")
    calls = []
    m._async_process_light = _record(calls)
    run(m.async_trigger("test"))
    assert svc == [("light", "turn_off", {"entity_id": VIRT})]
    assert calls == [(LIGHT, 25, 2200, None), ("light.second", 25, 2200, None)]
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)


def test_extra_start_dual_lights_concurrent_apply():
    """extra 起点双灯并发下发各自参数；ensure(False) 每轮一次在前。"""
    m = make_ci_manager(**{**CONF_LIGHTS_DUAL, CONF_EXTRAS: _EXTRA_18})
    svc = []
    m.hass.services = _RecordingServices(svc)
    m.hass.states.mapping[VIRT] = _FakeState("on", mode="active")
    calls = []
    m._async_process_light = _record(calls)
    run(m._async_extra_start(0))
    assert svc == [("light", "turn_off", {"entity_id": VIRT})]
    assert sorted(calls) == [
        (LIGHT, 60, 3000, None),
        ("light.second", 60, 3000, None),
    ]
    assert m.machines[LIGHT].expected == (60, 3000)
    assert m.machines["light.second"].expected == (60, 3000)
