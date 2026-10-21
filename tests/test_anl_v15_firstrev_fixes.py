"""V1.5.0 首审修复批（wb 批 wbmcp-1021-184906-7024 / job-46a59136eb）的回归钉。

覆盖 F-080（监听注册与 turn_on_listen 解耦 + settle 门控）、F-081（启动/
重载归还对账）、F-082（listener 补 _stopped 早退，F-078 第四处）、F-083
（配置面三挂点归还统一走 async_release_virtual_light：lux mode 守卫钉）、
F-084（ensure(True) 校准中假成功防线）、F-085（CI 日间触发 only_when_on
准入）、F-086（运行面归还跨条目检查）。

F-087（trigger_now 文档三件套）与 F-088（_mode_at 挪进消费方守卫，行为
等价重构）无独立行为钉：F-088 由既有 F-066/F-075 extra 屏障钉与全量基线
覆盖等价性；F-087 由翻译三件套对账（键集 delta=0）覆盖。

桩复用 test_anl_transitions（make_manager 家族）与 test_web（_recording_
hass 家族）。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import conftest as stubs
from test_anl_transitions import (
    LIGHT,
    VIRT,
    _FakeState,
    _FakeStates,
    _record,
    _RecordingServices,
    make_ci_manager,
    make_entry,
    make_manager,
)
from test_web import (
    FakeRequest,
    _anl_bucket,
    _config_entry,
    _config_manager,
    _recording_hass,
    recorded,
    run,
)

import custom_components.fumiha_lighting.anl as anl_pkg
from custom_components.fumiha_lighting.anl import manager as anl_manager
from custom_components.fumiha_lighting import web
from custom_components.fumiha_lighting.anl.const import (
    CONF_DAY_CI_ENABLED,
    CONF_DAY_CI_VIRTUAL_LIGHT,
    CONF_DAY_ENABLED,
    CONF_TURN_ON_LISTEN,
)
from custom_components.fumiha_lighting.anl.manager import (
    LightState,
    async_release_virtual_light,
)

MANAGER_LOGGER = "custom_components.fumiha_lighting.anl.manager"


def _listener_event():
    """关→开事件桩（与 test_listener_* 家族同形状）。"""
    return SimpleNamespace(
        data={
            "old_state": _FakeState("off"),
            "new_state": _FakeState("on", 255, 4000),
            "entity_id": LIGHT,
        }
    )


def _no_listen_ci_manager():
    return make_ci_manager(**{CONF_TURN_ON_LISTEN: False})


# ---------------------------------------------------------------------------
# F-080：监听注册与 turn_on_listen 解耦 + settle 门控
# ---------------------------------------------------------------------------


def test_f080_ci_registers_listener_without_turn_on_listen():
    """turn_on_listen=False + CI 配置开 → start() 仍注册监听（接管链唯一入口）；
    对照：CI 关 + turn_on_listen=False → 不注册（老语义不回退）。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)
    m = _no_listen_ci_manager()
    m.start()
    assert m._unsub_state is not None  # CI 开：监听是日间接管链唯一事件入口
    m.stop()

    m2 = make_manager(**{CONF_TURN_ON_LISTEN: False})
    m2.start()
    assert m2._unsub_state is None  # CI 关：保持「不监听就不接管」老语义
    m2.stop()
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)


def test_f080_listener_day_ci_hands_over_without_listen():
    """turn_on_listen=False + CI 开 + 日间开灯 → 虚拟灯接管、真实灯零下发、
    无 settle（快照接管语义与监听面终于一致）。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)
    m = _no_listen_ci_manager()
    svc = []
    m.hass.services = _RecordingServices(svc)
    m.hass.states.mapping[VIRT] = _FakeState("off")
    calls = []
    m._async_process_light = _record(calls)
    stubs.CALL_LATER_CALLS.clear()
    run(m._async_light_state_changed(_listener_event()))
    assert svc == [("light", "turn_on", {"entity_id": VIRT})]
    assert calls == []
    assert stubs.CALL_LATER_CALLS == []  # 无 settle 延迟
    assert m.machines[LIGHT].state == LightState.IDLE
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)


def test_f080_listener_night_no_listen_release_only():
    """turn_on_listen=False + CI 开 + 夜间开灯 → 防御性归还应答，但 settle
    直控不发生（监听关闭的老语义：事件不驱动直控）。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)
    m = _no_listen_ci_manager()
    svc = []
    m.hass.services = _RecordingServices(svc)
    m.hass.states.mapping[VIRT] = _FakeState("on", mode="active")
    calls = []
    m._async_process_light = _record(calls)
    stubs.CALL_LATER_CALLS.clear()
    run(m._async_light_state_changed(_listener_event()))
    assert svc == [("light", "turn_off", {"entity_id": VIRT})]  # 归还仍应答
    assert calls == []  # 但不再 settle 直控
    assert stubs.CALL_LATER_CALLS == []
    machine = m.machines[LIGHT]
    assert machine.state != LightState.TURN_ON_PENDING  # 未进入监听接管态
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)


def test_f080_listener_day_ci_ensure_fail_no_listen_no_direct_control(caplog):
    """turn_on_listen=False + CI 日间 ensure 失败 → 不回落事件驱动直控
    （立即触发的 fallback 是 async_trigger 路径，不受门控影响）。"""
    import logging

    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)
    m = _no_listen_ci_manager()
    m.hass.services = _RecordingServices([], fail=True)
    m.hass.states.mapping[VIRT] = _FakeState("off")
    calls = []
    m._async_process_light = _record(calls)
    with caplog.at_level(logging.WARNING, logger=MANAGER_LOGGER):
        run(m._async_light_state_changed(_listener_event()))
    assert calls == []  # turn_on_listen=False：事件路径零直控
    assert any(
        r.levelno == logging.WARNING for r in caplog.records
    )  # 错误必须响（ensure 节流告警）
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)


# ---------------------------------------------------------------------------
# F-081：启动/重载归还对账
# ---------------------------------------------------------------------------


def test_f081_reconcile_night_active_returns_virtual():
    """CI 开 + 非日间 + lux 闭环在跑 → 对账归还（重启跨过夜锚残留治理）。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)
    m = make_ci_manager()
    svc = []
    m.hass.services = _RecordingServices(svc)
    m.hass.states.mapping[VIRT] = _FakeState("on", mode="active")
    run(m.async_ci_startup_reconcile())
    assert svc == [("light", "turn_off", {"entity_id": VIRT})]
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)


def test_f081_reconcile_daytime_untouched():
    """CI 开 + 日间 + 闭环在跑 → 不碰（「天亮不主动接管」红线；重启前接管
    的闭环继续跑正是期望状态）。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)
    m = make_ci_manager()
    svc = []
    m.hass.services = _RecordingServices(svc)
    m.hass.states.mapping[VIRT] = _FakeState("on", mode="active")
    run(m.async_ci_startup_reconcile())
    assert svc == []
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)


def test_f081_reconcile_ignores_idle_calibrating_missing():
    """idle/calibrating/属性缺失/实体缺失 → 一律不动（F-076 守卫集）。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)
    for mode in ("idle", "calibrating", None):
        m = make_ci_manager()
        svc = []
        m.hass.services = _RecordingServices(svc)
        if mode is None:
            m.hass.states.mapping[VIRT] = _FakeState("on")  # 属性缺失形态
        else:
            m.hass.states.mapping[VIRT] = _FakeState("on", mode=mode)
        run(m.async_ci_startup_reconcile())
        assert svc == [], f"mode={mode} 不应归还"
    # 实体缺失
    m = make_ci_manager()
    svc = []
    m.hass.services = _RecordingServices(svc)
    run(m.async_ci_startup_reconcile())
    assert svc == []
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)


def test_f081_setup_entry_runs_reconcile():
    """接线钉：anl_async_setup_entry 在 start() 后跑对账（reload 同路径）。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)
    svc = []
    hass = SimpleNamespace(
        data={"fumiha_lighting": {"lux": {}, "anl": {}}},
        states=_FakeStates(),
        services=_RecordingServices(svc),
        config_entries=SimpleNamespace(async_entries=lambda domain: []),
    )
    hass.states.mapping[VIRT] = _FakeState("on", mode="active")
    entry = make_entry(
        **{CONF_DAY_CI_ENABLED: True, CONF_DAY_CI_VIRTUAL_LIGHT: VIRT}
    )
    entry.async_on_unload = lambda _handler: None
    ok = run(anl_pkg.anl_async_setup_entry(hass, entry))
    assert ok is True
    assert svc == [("light", "turn_off", {"entity_id": VIRT})]  # 对账归还
    assert "anl" in hass.data["fumiha_lighting"]
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)


# ---------------------------------------------------------------------------
# F-082：listener 补 _stopped 早退（F-078 同款第四处）
# ---------------------------------------------------------------------------


def test_f082_listener_stopped_guard():
    """stop() 后事件分派竞态 → 零服务、零 settle。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)
    m = make_ci_manager()
    svc = []
    m.hass.services = _RecordingServices(svc)
    m.hass.states.mapping[VIRT] = _FakeState("on", mode="active")
    stubs.CALL_LATER_CALLS.clear()
    m.stop()
    run(m._async_light_state_changed(_listener_event()))
    assert svc == []
    assert stubs.CALL_LATER_CALLS == []
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)


# ---------------------------------------------------------------------------
# F-083：配置面归还 lux mode 守卫（web/flow/删条目三挂点统一走助手）
# ---------------------------------------------------------------------------


def _virt_state(eid, mode):
    if eid != VIRT:
        return None
    state = SimpleNamespace(state="on")
    state.attributes = {"mode": mode} if mode is not None else {}
    return state


def test_f083_web_post_release_skipped_when_calibrating():
    """web POST 关 CI + 虚拟灯校准中 → 零 turn_off（校准收尾不被改写）、
    落盘照常。"""
    hass = _recording_hass()
    hass.states = SimpleNamespace(get=lambda eid: _virt_state(eid, "calibrating"))
    _anl_bucket(hass, _config_manager(_config_entry(
        entry_id="anl1",
        data={"kind": "anl", "lights": ["light.a"], CONF_DAY_ENABLED: True,
              CONF_DAY_CI_ENABLED: True, CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
    )))
    resp = run(web.AnlConfigView().post(
        FakeRequest(hass, {"entry_id": "anl1",
                           "patch": {"params": {"day_ci_enabled": False}}})
    ))
    assert resp.payload["ok"] is True
    assert hass.services.calls == []
    assert recorded[0][1]["options"]["day_ci_enabled"] is False


def test_f083_options_flow_release_skipped_when_calibrating():
    """options 流关 CI + 校准中 → 零 turn_off、保存照常。"""
    from test_anl_v15_r2_fixes import _SETTINGS, _TIMES

    from custom_components.fumiha_lighting.anl.flow import AnlGroupOptionsFlow

    events = []
    opts = AnlGroupOptionsFlow()
    opts.config_entry = SimpleNamespace(
        entry_id="anl1",
        data={"kind": "anl", "lights": ["light.a"],
              CONF_DAY_CI_ENABLED: True, CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
        options={},
    )
    opts.hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda eid: _virt_state(eid, "calibrating")),
        services=SimpleNamespace(async_call=_recorder(events)),
        config_entries=SimpleNamespace(async_entries=lambda domain: []),
    )
    run(opts.async_step_init(None))
    run(opts.async_step_anl_init(_TIMES))
    final = run(opts.async_step_settings({
        **_SETTINGS,
        "day_ci_enabled": False,
        "day_ci_virtual_light": VIRT,
    }))
    assert final["type"] == "create_entry"
    assert events == []  # 校准中不碰
    # F-096：配置侧挂点 retry_on_calibrating=True——校准中排 60s 延迟归还
    # （既有用例只钉「零 turn_off」，漏了「有 retry」）
    assert [d for d, _a in stubs.CALL_LATER_CALLS] == [60]


def test_f083_remove_entry_release_skipped_when_calibrating():
    """删条目 + 校准中 → 零 turn_off。"""
    import custom_components.fumiha_lighting as pkg

    hass = _recording_hass()
    hass.states = SimpleNamespace(get=lambda eid: _virt_state(eid, "calibrating"))
    entry = SimpleNamespace(
        entry_id="anl1",
        data={"kind": "anl", "lights": ["light.a"],
              CONF_DAY_CI_ENABLED: True, CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
        options={},
    )
    run(pkg.async_remove_entry(hass, entry))
    assert hass.services.calls == []
    # F-096：删条目挂点同上——校准中排 60s 延迟归还
    assert [d for d, _a in stubs.CALL_LATER_CALLS] == [60]


def test_f083_release_helper_direct_mode_guard():
    """助手直测：active/passthrough 才关；idle/calibrating/缺失不动（单一
    来源的判据面钉）。"""
    for mode, expect_off in (
        ("active", True),
        ("passthrough", True),
        ("idle", False),
        ("calibrating", False),
        (None, False),
    ):
        calls = []
        hass = SimpleNamespace(
            states=SimpleNamespace(get=lambda eid, _m=mode: _virt_state(eid, _m)),
            services=_RecordingServices(calls),
            # F-094：守卫单一来源——助手恒执行引用检查，桩补空表
            config_entries=SimpleNamespace(async_entries=lambda domain: []),
        )
        run(async_release_virtual_light(hass, VIRT, exclude_entry_id=None))
        expected = [("light", "turn_off", {"entity_id": VIRT})] if expect_off else []
        assert calls == expected, f"mode={mode}"


# ---------------------------------------------------------------------------
# F-084：ensure(True) 校准中假成功防线
# ---------------------------------------------------------------------------


def test_f084_day_trigger_calibrating_falls_back_to_day_params():
    """虚拟灯校准中 → ensure(True) 按未接管返回 False → 逐灯回落日间参数
    直控（不是假成功清 machine 什么都不做）。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)
    m = make_ci_manager()
    svc = []
    m.hass.services = _RecordingServices(svc)
    m.hass.states.mapping[VIRT] = _FakeState("on", mode="calibrating")
    calls = []
    m._async_process_light = _record(calls)
    run(m.async_trigger("test"))
    assert svc == []  # 校准中不发 turn_on（假成功防线在预检层）
    assert calls == [(LIGHT, 100, 4000, None)]  # 回落日间参数直控
    machine = m.machines[LIGHT]
    assert machine.expected == (100, 4000)
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)


def test_f084_listener_day_ci_calibrating_falls_back_to_settle():
    """监听路径同防：校准中 → ensure False → 落 settle 路径（turn_on_listen
    开时回落日间参数直控）。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)
    m = make_ci_manager()
    svc = []
    m.hass.services = _RecordingServices(svc)
    m.hass.states.mapping[VIRT] = _FakeState("on", mode="calibrating")
    stubs.CALL_LATER_CALLS.clear()
    run(m._async_light_state_changed(_listener_event()))
    assert svc == []
    assert len(stubs.CALL_LATER_CALLS) == 1  # settle 延迟已排（fallback 直控）
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)


# ---------------------------------------------------------------------------
# F-085：CI 日间触发 only_when_on 准入
# ---------------------------------------------------------------------------


def test_f085_ci_day_trigger_only_when_on_all_off_skips():
    """only_when_on + 组内灯全关 + 日间触发 → 不开虚拟灯（防经 lux 点亮
    全关灯组；夜间同语义=逐灯 SKIPPED_OFF）。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)
    m = make_ci_manager(**{"only_when_on": True})
    svc = []
    m.hass.services = _RecordingServices(svc)
    m.hass.states.mapping[VIRT] = _FakeState("off")
    m.hass.states.mapping[LIGHT] = _FakeState("off")
    calls = []
    m._async_process_light = _record(calls)
    run(m.async_trigger("test"))
    assert svc == []  # 零虚拟灯调用
    assert calls == []
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)


def test_f085_ci_day_trigger_only_when_on_any_on_takes_over():
    """only_when_on + 任一灯亮 → 正常接管（准入只拦全关组）。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)
    m = make_ci_manager(**{"only_when_on": True})
    svc = []
    m.hass.services = _RecordingServices(svc)
    m.hass.states.mapping[VIRT] = _FakeState("off")
    m.hass.states.mapping[LIGHT] = _FakeState("on", 255, 4000)
    calls = []
    m._async_process_light = _record(calls)
    run(m.async_trigger("test"))
    assert svc == [("light", "turn_on", {"entity_id": VIRT})]
    assert calls == []  # 接管成功：真实灯零下发
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)


# ---------------------------------------------------------------------------
# F-086：运行面归还跨条目检查
# ---------------------------------------------------------------------------


def test_f086_night_trigger_release_skipped_when_other_entry_shares():
    """夜间触发归还 + 其他 anl 条目仍引用同一虚拟灯 → 零 turn_off（不误关
    别组日间闭环）、本组夜间直控照常。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)
    m = make_ci_manager()
    other = SimpleNamespace(
        entry_id="anl2",
        data={"kind": "anl", "lights": ["light.z"], CONF_DAY_ENABLED: True,
              CONF_DAY_CI_ENABLED: True, CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
        options={},
        disabled_by=None,
        state="loaded",
    )
    m.hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [
            SimpleNamespace(entry_id=m.entry.entry_id, data=make_entry().data,
                            options={}, disabled_by=None, state="loaded"),
            other,
        ]
    )
    svc = []
    m.hass.services = _RecordingServices(svc)
    m.hass.states.mapping[VIRT] = _FakeState("on", mode="active")
    calls = []
    m._async_process_light = _record(calls)
    run(m.async_trigger("test"))
    assert svc == []  # 跨条目引用在：归还跳过
    assert calls == [(LIGHT, 25, 2200, None)]  # 夜间直控照常
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)


def _recorder(events):
    async def _svc(domain, service, data, blocking=False):
        events.append(("svc", service, data.get("entity_id")))

    return _svc


# ---------------------------------------------------------------------------
# F-089：启动对账走共享助手（跨条目守卫）+ exclude_entry_id 必填
# ---------------------------------------------------------------------------


def test_f089_reconcile_skipped_when_other_entry_shares():
    """启动对账 + 其他 anl 条目仍引用同一虚拟灯 → 零 turn_off（对账不再
    绕过跨条目守卫，F-089）。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)
    m = make_ci_manager()
    other = SimpleNamespace(
        entry_id="anl2",
        data={"kind": "anl", "lights": ["light.z"], CONF_DAY_ENABLED: True,
              CONF_DAY_CI_ENABLED: True, CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
        options={},
        disabled_by=None,
        state="loaded",
    )
    m.hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [m.entry, other]
    )
    svc = []
    m.hass.services = _RecordingServices(svc)
    m.hass.states.mapping[VIRT] = _FakeState("on", mode="active")
    run(m.async_ci_startup_reconcile())
    assert svc == []  # 跨条目守卫命中：归还跳过
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)


def test_f089_reconcile_turns_off_when_reference_is_unique():
    """对照钉：无他条目引用 → 对账照常 turn_off（F-081 语义不回退）。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)
    m = make_ci_manager()
    svc = []
    m.hass.services = _RecordingServices(svc)
    m.hass.states.mapping[VIRT] = _FakeState("on", mode="active")
    run(m.async_ci_startup_reconcile())
    assert svc == [("light", "turn_off", {"entity_id": VIRT})]
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)


# ---------------------------------------------------------------------------
# F-090：配置侧归还的 calibrating 有界延迟归还
# ---------------------------------------------------------------------------


def _calib_states_map():
    return {VIRT: SimpleNamespace(
        state="on", attributes={"mode": "calibrating"})}


def _calib_hass(states_map):
    """web POST 用例基座：VIRT 校准中 + async_create_task 收集桩。"""
    hass = _recording_hass()
    hass.states = SimpleNamespace(get=lambda eid: states_map.get(eid))
    tasks = []
    hass.async_create_task = tasks.append
    return hass, tasks


def _calib_entry(entry_id="anl1"):
    return _config_entry(
        entry_id=entry_id,
        data={"kind": "anl", "lights": ["light.a"], CONF_DAY_ENABLED: True,
              CONF_DAY_CI_ENABLED: True, CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
    )


def _post_disable_ci(hass):
    return run(web.AnlConfigView().post(
        FakeRequest(hass, {"entry_id": "anl1",
                           "patch": {"params": {"day_ci_enabled": False}}})
    ))


def test_f090_web_post_release_schedules_retry_on_calibrating():
    """web POST 关 CI + 校准中 → 零 turn_off + 排 60s 延迟归还（一次性
    挂点不再永久丢失，F-090）。"""
    states_map = _calib_states_map()
    hass, _tasks = _calib_hass(states_map)
    _anl_bucket(hass, _config_manager(_calib_entry()))
    stubs.CALL_LATER_CALLS.clear()
    resp = _post_disable_ci(hass)
    assert resp.payload["ok"] is True
    assert hass.services.calls == []
    assert [d for d, _a in stubs.CALL_LATER_CALLS] == [60]  # 60s 复查


def test_f090_retry_completes_after_calibration_ends():
    """校准结束（mode→active）后重试到点 → 补做 turn_off（无人引用）。"""
    states_map = _calib_states_map()
    hass, tasks = _calib_hass(states_map)
    _anl_bucket(hass, _config_manager(_calib_entry()))
    stubs.CALL_LATER_CALLS.clear()
    _post_disable_ci(hass)
    assert hass.services.calls == []
    states_map[VIRT] = SimpleNamespace(  # 校准结束
        state="on", attributes={"mode": "active"})
    # F-096：走 flush 入口——_stub_callback 结构断言对 retry 回调生效
    stubs.flush_deferred_callbacks()
    assert tasks, "校准结束应补做归还"
    run(tasks[0])
    assert hass.services.calls == [("light", "turn_off", {"entity_id": VIRT})]


def test_f090_retry_yields_to_reconfigured_reference():
    """重试到点时虚拟灯已被新配置重新引用 → 放弃归还（不误杀新接管）。"""
    states_map = _calib_states_map()
    hass, tasks = _calib_hass(states_map)
    _anl_bucket(hass, _config_manager(_calib_entry()))
    stubs.CALL_LATER_CALLS.clear()
    _post_disable_ci(hass)  # 此时 async_entries 空表 → retry 排上
    reconfigured = SimpleNamespace(
        entry_id="anl2",
        data={"kind": "anl", "lights": ["light.z"], CONF_DAY_ENABLED: True,
              CONF_DAY_CI_ENABLED: True, CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
        options={},
        disabled_by=None,
        state="loaded",
    )
    hass.config_entries.async_entries = lambda domain: [reconfigured]
    states_map[VIRT] = SimpleNamespace(
        state="on", attributes={"mode": "active"})
    # F-096：flush 结构门禁；F-094：守卫单一来源——补做协程在助手内
    # 命中引用判据后跳过（retry 不再自带 pre-check）
    stubs.flush_deferred_callbacks()
    assert tasks, "补做已投递，守卫在助手内命中"
    run(tasks[0])
    assert hass.services.calls == []


def test_f090_runtime_release_no_retry_on_calibrating():
    """运行面（夜间触发归还）+ 校准中 → 零服务零 retry timer（下一次
    trigger/extra/开灯事件是天然重试，F-090 不堆 timer）。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)
    m = make_ci_manager()
    svc = []
    m.hass.services = _RecordingServices(svc)
    m.hass.states.mapping[VIRT] = _FakeState("on", mode="calibrating")
    calls = []
    m._async_process_light = _record(calls)
    stubs.CALL_LATER_CALLS.clear()
    run(m.async_trigger("test"))
    assert svc == []  # calibrating 不碰
    assert stubs.CALL_LATER_CALLS == []  # 不排延迟归还
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)


# ---------------------------------------------------------------------------
# R7 修复批（F-092~F-096）回归钉
# ---------------------------------------------------------------------------


def test_f092_retry_survives_31_rechecks():
    """F-092：~31 分钟（31 次 60s 复查）仍在校准 → 仍再排未放弃。旧上限
    30 分钟在此处已放弃且此后无任何归还路径（trigger/extra/监听/对账全被
    _ci_configured 门挡）——新上限 90 分钟覆盖 README 最坏校准口径。"""
    states_map = _calib_states_map()
    hass, tasks = _calib_hass(states_map)
    _anl_bucket(hass, _config_manager(_calib_entry()))
    stubs.CALL_LATER_CALLS.clear()
    _post_disable_ci(hass)
    for _ in range(31):
        stubs.flush_deferred_callbacks()  # 每次复查仍校准 → 再排 60s
    assert [d for d, _a in stubs.CALL_LATER_CALLS] == [60]  # 链仍在
    assert tasks == [] and hass.services.calls == []  # 未提前归还


def test_f092_gives_up_with_warning_after_budget(monkeypatch, caplog):
    """F-092：预算耗尽才放弃，且 WARNING 可观测（含虚拟灯实体与手动归还
    指引）——旧实现 DEBUG 放弃，用户无从得知归还已永久丢失。"""
    import logging

    states_map = _calib_states_map()
    hass, tasks = _calib_hass(states_map)
    _anl_bucket(hass, _config_manager(_calib_entry()))
    monkeypatch.setattr(anl_manager, "_CI_RELEASE_RETRY_MAX", 2)
    stubs.CALL_LATER_CALLS.clear()
    _post_disable_ci(hass)
    with caplog.at_level(logging.WARNING, logger=MANAGER_LOGGER):
        stubs.flush_deferred_callbacks()  # 复查#1：仍校准 → 再排
        assert [d for d, _a in stubs.CALL_LATER_CALLS] == [60]
        stubs.flush_deferred_callbacks()  # 复查#2：预算耗尽 → 放弃
    assert stubs.CALL_LATER_CALLS == []  # 不再排链
    assert tasks == [] and hass.services.calls == []  # 放弃≠误归还
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warns, "放弃必须 WARNING 落日志"
    assert any(VIRT in r.getMessage() and "手动" in r.getMessage()
               for r in warns)


def test_f090_retry_requeues_each_recheck():
    """F-096 补钉：校准中连续两次复查 → 每次再排 60s、零服务（重试链
    不断；既有用例只钉首次排程）。"""
    states_map = _calib_states_map()
    hass, _tasks = _calib_hass(states_map)
    _anl_bucket(hass, _config_manager(_calib_entry()))
    stubs.CALL_LATER_CALLS.clear()
    _post_disable_ci(hass)
    stubs.flush_deferred_callbacks()  # 复查#1
    assert [d for d, _a in stubs.CALL_LATER_CALLS] == [60]
    stubs.flush_deferred_callbacks()  # 复查#2
    assert [d for d, _a in stubs.CALL_LATER_CALLS] == [60]
    assert hass.services.calls == []


def test_f093_broken_reference_does_not_block_retry_release():
    """F-093：停用（disabled_by 非 None）或未运行（state 白名单外：
    setup_error/not_loaded）的其他条目不构成生效引用——重试补做照常
    turn_off。旧判据把它们当生效引用，其 manager 永不接手 → V 恒
    active + 双闭环互纠、归还永久丢失。"""
    for broken in ("disabled", "setup_error", "not_loaded"):
        states_map = _calib_states_map()
        hass, tasks = _calib_hass(states_map)
        _anl_bucket(hass, _config_manager(_calib_entry()))
        stubs.CALL_LATER_CALLS.clear()
        _post_disable_ci(hass)
        hass.config_entries.async_entries = lambda domain, _b=broken: [
            SimpleNamespace(
                entry_id="anl2",
                data={"kind": "anl", "lights": ["light.z"],
                      CONF_DAY_ENABLED: True, CONF_DAY_CI_ENABLED: True,
                      CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
                options={},
                disabled_by="user" if _b == "disabled" else None,
                state="loaded" if _b == "disabled" else _b,
            )
        ]
        states_map[VIRT] = SimpleNamespace(
            state="on", attributes={"mode": "active"})
        stubs.flush_deferred_callbacks()  # F-096：flush 结构门禁
        assert tasks, f"broken={broken} 应仍投递补做"
        run(tasks[0])
        assert hass.services.calls == [
            ("light", "turn_off", {"entity_id": VIRT})
        ], f"broken={broken} 应照常归还"


def test_f094_backfill_requeues_when_calibration_reenters():
    """F-094：补做协程运行前校准重新进入（mode 又回 calibrating）→ 助手
    经 retry_on_calibrating=True 重新排链而非丢归还（补做与普通挂点共用
    单一助手，守卫/重排不再各写一份）。"""
    states_map = _calib_states_map()
    hass, tasks = _calib_hass(states_map)
    _anl_bucket(hass, _config_manager(_calib_entry()))
    stubs.CALL_LATER_CALLS.clear()
    _post_disable_ci(hass)
    states_map[VIRT] = SimpleNamespace(  # 校准结束
        state="on", attributes={"mode": "active"})
    stubs.flush_deferred_callbacks()
    assert tasks, "校准结束应投递补做"
    assert stubs.CALL_LATER_CALLS == []  # _attempt 已消费重试链
    states_map[VIRT] = SimpleNamespace(  # 补做运行前重新进入校准
        state="on", attributes={"mode": "calibrating"})
    run(tasks[0])
    assert [d for d, _a in stubs.CALL_LATER_CALLS] == [60]  # 重排非丢弃
    assert hass.services.calls == []
