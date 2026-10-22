"""V1.6.0 日间恒照度单灯化回归钉（计划书 §3 步骤 2）。

语义面：day_ci_lights 逐灯接管清单（读入迁移、mixed 组 trigger/listener/
计划步、transitions_active 重定义、web 注册表身份拒绝与存量迁移引用镜像）。
组级守卫消费面（监听注册/夜间防御性归还/归还注册表判据/startup 对账）
由 test_anl_v15_* 既有钉覆盖，此处只钉逐灯化改动面。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import conftest as stubs
from test_anl_transitions import (
    LIGHT,
    VIRT,
    _FakeState,
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
    recorded,
    run,
)

import custom_components.fumiha_lighting as pkg
from custom_components.fumiha_lighting import web
from custom_components.fumiha_lighting.anl.const import (
    CONF_DAY_CI_ENABLED,
    CONF_DAY_CI_LIGHTS,
    CONF_DAY_CI_VIRTUAL_LIGHT,
    CONF_DAY_ENABLED,
    CONF_LIGHTS,
)
from custom_components.fumiha_lighting.anl.manager import (
    _NOT_IN_BAND,
    LightState,
    NightLightManager,
    PlanStep,
)

SECOND = "light.second"

# 计划步时刻（fixed 锚点 22:00/06:00 → 12:00 解析为 day）
WHEN_DAY = datetime(2026, 10, 2, 12, 0)


def make_mixed_manager(**extra_overrides) -> NightLightManager:
    """双灯组：light.bath 在接管清单、light.second 归日间直控。"""
    overrides = {
        CONF_LIGHTS: [LIGHT, SECOND],
        CONF_DAY_CI_LIGHTS: [LIGHT],
        CONF_DAY_CI_VIRTUAL_LIGHT: VIRT,
    }
    overrides.update(extra_overrides)
    return make_manager(**overrides)


def _record(calls):
    async def _fake(entity_id, brightness, kelvin, allow_turn_on=True,
                    transition_s=None):
        calls.append((entity_id, brightness, kelvin, transition_s))
        return True

    return _fake


def _listener_event(entity_id, state="on"):
    return {
        "old_state": _FakeState("off"),
        "new_state": _FakeState(state, 255, 4000),
        "entity_id": entity_id,
    }


# ---------------------------------------------------------------------------
# 读入迁移（零回写）
# ---------------------------------------------------------------------------


def test_migration_legacy_enabled_expands_to_full_light_set():
    """存量 day_ci_enabled=true 且 day_ci_lights 缺失 → 清单=灯集全集。"""
    m = make_manager(**{CONF_DAY_CI_ENABLED: True, CONF_DAY_CI_VIRTUAL_LIGHT: VIRT})
    assert m.day_ci_lights == [LIGHT]
    assert m.ci_configured() is True


def test_migration_legacy_disabled_yields_empty():
    m = make_manager(
        **{CONF_DAY_CI_ENABLED: False, CONF_DAY_CI_VIRTUAL_LIGHT: VIRT}
    )
    assert m.day_ci_lights == []
    assert m.ci_configured() is False


def test_migration_new_key_wins_over_legacy():
    """两键并存（重存后残留）→ day_ci_lights 生效，旧键不再回读。"""
    m = make_manager(
        **{
            CONF_DAY_CI_ENABLED: True,
            CONF_DAY_CI_LIGHTS: [],
            CONF_DAY_CI_VIRTUAL_LIGHT: VIRT,
        }
    )
    assert m.day_ci_lights == []
    assert m.ci_configured() is False


def test_migration_prunes_lights_not_in_group():
    """携带/手改残源：清单引用已裁撤灯 → 自愈过滤；全滤空 → CI 失效。"""
    m = make_manager(
        **{
            CONF_LIGHTS: [LIGHT],
            CONF_DAY_CI_LIGHTS: [LIGHT, "light.ghost"],
            CONF_DAY_CI_VIRTUAL_LIGHT: VIRT,
        }
    )
    assert m.day_ci_lights == [LIGHT]
    m2 = make_manager(
        **{
            CONF_LIGHTS: [LIGHT],
            CONF_DAY_CI_LIGHTS: ["light.ghost"],
            CONF_DAY_CI_VIRTUAL_LIGHT: VIRT,
        }
    )
    assert m2.day_ci_lights == []
    assert m2.ci_configured() is False


# ---------------------------------------------------------------------------
# async_trigger：mixed 组逐灯接管（F-072 轮级去重保留）
# ---------------------------------------------------------------------------


def test_trigger_day_mixed_group_takes_over_ci_light_only():
    """day 触发：CI 灯接管（零直控、machine 归位），非 CI 灯日间参数直控。"""
    stubs._FakeClock.now_dt = WHEN_DAY
    m = make_mixed_manager()
    svc = []
    m.hass.services = _RecordingServices(svc)
    m.hass.states.mapping[VIRT] = _FakeState("off")
    calls = []
    m._async_process_light = _record(calls)
    run(m.async_trigger("test"))
    # 轮级去重：虚拟灯 turn_on 恰 1 次、无亮度载荷
    assert svc == [("light", "turn_on", {"entity_id": VIRT})]
    # CI 灯零直控 + machine 归位；非 CI 灯复活日间参数直控
    assert LIGHT not in [c[0] for c in calls]
    assert calls == [(SECOND, 100, 4000, None)]
    assert m.machines[LIGHT].state == LightState.IDLE
    assert m.machines[LIGHT].expected is None
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)


def test_trigger_day_mixed_group_ensure_fail_falls_back_per_light():
    """ensure 失败（虚拟灯缺失 F-068）→ CI 灯回落日间参数直控。"""
    stubs._FakeClock.now_dt = WHEN_DAY
    m = make_mixed_manager()
    svc = []
    m.hass.services = _RecordingServices(svc)
    # VIRT 不在 states → F-068 预检失败（WARNING 节流旗标组级）
    calls = []
    m._async_process_light = _record(calls)
    run(m.async_trigger("test"))
    assert svc == []  # 未接管
    assert sorted(calls) == [(LIGHT, 100, 4000, None), (SECOND, 100, 4000, None)]
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)


def test_trigger_night_mixed_group_hands_back_then_drives_both():
    """夜间触发：归还虚拟灯 1 次，两灯（含 CI 灯）都按夜间参数直控。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)
    m = make_mixed_manager()
    svc = []
    m.hass.services = _RecordingServices(svc)
    m.hass.states.mapping[VIRT] = _FakeState("on", mode="active")
    calls = []
    m._async_process_light = _record(calls)
    run(m.async_trigger("test"))
    assert svc == [("light", "turn_off", {"entity_id": VIRT})]
    assert sorted(calls) == [(LIGHT, 25, 2200, None), (SECOND, 25, 2200, None)]
    stubs._FakeClock.now_dt = WHEN_DAY


# ---------------------------------------------------------------------------
# 开灯监听：逐灯判定 + 日间非 CI 灯不得触发防御性归还
# ---------------------------------------------------------------------------


def test_listener_day_non_ci_light_settles_without_virtual_calls():
    """day + CI 配置开 + 非 CI 灯开灯 → settle 直控（零虚拟灯调用、零归还
    ——同组 CI 灯的虚拟灯可能正被接管，归还分支不得在日间触发）。"""
    stubs._FakeClock.now_dt = WHEN_DAY
    m = make_mixed_manager()
    svc = []
    m.hass.services = _RecordingServices(svc)
    m.hass.states.mapping[VIRT] = _FakeState("on", mode="active")
    stubs.CALL_LATER_CALLS.clear()
    machine = m.machines[SECOND]
    machine.manual_override = True
    run(m._async_light_state_changed(SimpleNamespace(data=_listener_event(SECOND))))
    assert svc == []  # 不接管、不归还
    assert machine.state == LightState.TURN_ON_PENDING  # 走既有 settle 路径
    assert len(stubs.CALL_LATER_CALLS) == 1  # settle 延迟已排
    stubs.CALL_LATER_CALLS.clear()
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)


def test_listener_day_ci_light_takes_over():
    stubs._FakeClock.now_dt = WHEN_DAY
    m = make_mixed_manager()
    svc = []
    m.hass.services = _RecordingServices(svc)
    m.hass.states.mapping[VIRT] = _FakeState("off")
    machine = m.machines[LIGHT]
    machine.expected = (1, 1)
    run(m._async_light_state_changed(SimpleNamespace(data=_listener_event(LIGHT))))
    assert svc == [("light", "turn_on", {"entity_id": VIRT})]
    assert machine.state == LightState.IDLE and machine.expected is None
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)


def test_listener_night_ci_light_defensive_handback():
    """夜间 CI 灯开灯 → 防御性归还虚拟灯后走 settle（组级消费面保留）。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)
    m = make_mixed_manager()
    svc = []
    m.hass.services = _RecordingServices(svc)
    m.hass.states.mapping[VIRT] = _FakeState("on", mode="active")
    stubs.CALL_LATER_CALLS.clear()
    run(m._async_light_state_changed(SimpleNamespace(data=_listener_event(LIGHT))))
    assert svc == [("light", "turn_off", {"entity_id": VIRT})]
    stubs.CALL_LATER_CALLS.clear()
    stubs._FakeClock.now_dt = WHEN_DAY


# ---------------------------------------------------------------------------
# 计划步：F-060 守卫逐灯化 + 日侧带只对非 CI 灯生成步进
# ---------------------------------------------------------------------------


def test_run_step_day_ci_light_skipped_non_ci_processed():
    """日间落点步：CI 灯跳过（零直控），非 CI 灯照常接管判定+下发。"""
    stubs._FakeClock.now_dt = WHEN_DAY
    m = make_mixed_manager()
    m.hass.states.mapping[LIGHT] = _FakeState("on", 255, 4000)
    m.hass.states.mapping[SECOND] = _FakeState("on", 255, 4000)
    calls = []
    m._async_process_light = _record(calls)
    step = PlanStep(LIGHT, 100, 4000, ("day", "2026-10-02", "night"), True)
    run(m._async_run_step(step, WHEN_DAY))
    assert calls == []  # CI 灯跳过
    step2 = PlanStep(SECOND, 100, 4000, ("day", "2026-10-02", "night"), True)
    run(m._async_run_step(step2, WHEN_DAY))
    assert calls == [(SECOND, 100, 4000, 300)]  # 非 CI 灯照常（带过渡时长）
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)


def test_rebuild_plan_day_side_band_steps_only_for_non_ci_lights():
    stubs._FakeClock.now_dt = WHEN_DAY
    stubs.POINT_IN_TIME_CALLS.clear()
    m = make_mixed_manager()
    m._rebuild_day_plan()
    steps = [s for steps in m._plan.values() for s in steps]
    assert steps  # 日侧带步进存在
    assert {s.entity_id for s in steps} == {SECOND}  # 仅非 CI 灯
    stubs.POINT_IN_TIME_CALLS.clear()
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)


def test_transitions_active_mixed_group_day_side_band_alive():
    """mixed 组：日侧带对非 CI 灯复活 → chip True（全 CI 组 False 由
    test_anl_v15_r2_fixes 既有钉覆盖）。"""
    stubs._FakeClock.now_dt = WHEN_DAY
    m = make_mixed_manager()
    assert m.transitions_active is True
    # 全部灯被接管 → 仅剩日侧带不谎报
    m2 = make_ci_manager(
        **{CONF_LIGHTS: [LIGHT, SECOND], CONF_DAY_CI_LIGHTS: [LIGHT, SECOND]}
    )
    assert m2.transitions_active is False
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 23, 0)


def test_band_params_day_side_alive_for_non_ci_light():
    """_band_params 逐灯：非 CI 灯在日侧带内拿到插值，CI 灯拿到锚点参数。"""
    stubs._FakeClock.now_dt = WHEN_DAY
    m = make_mixed_manager()
    # 入夜过渡带 21:00→22:00（start_transition 60）：21:30 落带内
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 21, 30)
    non_ci = m._band_params(SECOND, 21 * 60 + 30)
    ci = m._band_params(LIGHT, 21 * 60 + 30)
    assert non_ci != (25, 2200) and non_ci is not _NOT_IN_BAND  # 插值中
    assert ci is _NOT_IN_BAND  # CI 灯日侧带被滤 → 不在带内
    stubs._FakeClock.now_dt = WHEN_DAY


# ---------------------------------------------------------------------------
# web：day_ci_lights 注册表身份拒绝 + 存量迁移引用镜像
# ---------------------------------------------------------------------------


def test_web_post_day_ci_lights_rejects_own_virtual_light():
    """清单混入本集成虚拟灯 → 400（F-064 同语义；挡 F-064 前存量灯集）。"""
    hass = _recording_hass()
    entry = _config_entry(
        data={"kind": "anl", "lights": [LIGHT, "light.virtX"],
              CONF_DAY_ENABLED: True,
              CONF_DAY_CI_VIRTUAL_LIGHT: "light.virtX"}
    )
    _anl_bucket(hass, _config_manager(entry))
    import conftest as stubs_mod

    stubs_mod._ENTITY_REGISTRY._entities["light.virtX"] = SimpleNamespace(
        entity_id="light.virtX", platform="fumiha_lighting", domain="light"
    )
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {
                                   "day_ci_lights": ["light.virtX"]}}})
        )
    )
    assert resp.status == 400 and "虚拟恒照度灯" in resp.payload["error"]
    assert recorded == []  # 校验失败绝不落盘


def test_web_post_day_ci_lights_subset_enforced():
    hass = _recording_hass()
    entry = _config_entry(data={"kind": "anl", "lights": [LIGHT]})
    _anl_bucket(hass, _config_manager(entry))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {
                                   "day_ci_lights": [SECOND]}}})
        )
    )
    assert resp.status == 400 and "未知灯具" in resp.payload["error"]


def test_web_post_legacy_other_entry_still_counts_as_reference():
    """归还注册表判据迁移镜像：他条目仍是存量 day_ci_enabled=true 形状
    （运行时已按灯集全集接管）→ 构成生效引用，归还被跳过（F-086 防线
    在读入迁移下不破）。"""
    entry_a = _config_entry(
        entry_id="anl-a",
        data={"kind": "anl", "lights": [LIGHT], CONF_DAY_ENABLED: True,
              CONF_DAY_CI_LIGHTS: [LIGHT], CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
    )
    entry_b = _config_entry(
        entry_id="anl-b",
        data={"kind": "anl", "lights": ["light.b"], CONF_DAY_ENABLED: True,
              CONF_DAY_CI_ENABLED: True, CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
    )
    hass = _recording_hass()
    hass.states = SimpleNamespace(
        get=lambda eid: SimpleNamespace(state="on", attributes={"mode": "active"})
        if eid == VIRT
        else None
    )
    hass.config_entries.async_entries = lambda domain: [entry_a, entry_b]
    _anl_bucket(hass, _config_manager(entry_a))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl-a",
                               "patch": {"params": {"day_ci_lights": []}}})
        )
    )
    assert resp.payload["ok"] is True
    assert hass.services.calls == []  # 归还被存量条目引用检查跳过


def test_remove_entry_legacy_shape_releases_virtual():
    """删条目归还判据走读入迁移（day_ci_enabled=true 存量形状 → 归还）。"""
    hass = FakeHass()
    hass.states = SimpleNamespace(
        get=lambda eid: SimpleNamespace(state="on", attributes={"mode": "active"})
        if eid == VIRT
        else None
    )
    anl_entry = SimpleNamespace(
        entry_id="anl1",
        data={"kind": "anl", "lights": [LIGHT], CONF_DAY_ENABLED: True,
              CONF_DAY_CI_ENABLED: True, CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
        options={},
    )
    run(pkg.async_remove_entry(hass, anl_entry))
    assert hass.services.calls == [("light", "turn_off", {"entity_id": VIRT})]

# ---------------------------------------------------------------------------
# flow：GUI 键携带 + 存量展开（读入迁移的写侧对偶）
# ---------------------------------------------------------------------------


def _reconfigure_flow_to_settings(entry, lights, settings):
    """驱动 reconfigure 流到 settings 步（返回 flow，_finish 交给用例）。"""
    from test_web import _make_flow

    flow = _make_flow()
    flow.source = "reconfigure"
    flow._reconfigure_entry = entry
    flow.hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda eid: None),
        config_entries=SimpleNamespace(async_entries=lambda domain: []),
    )
    run(flow.async_step_reconfigure(None))
    run(flow.async_step_anl_reconfigure({"lights": lights}))
    run(flow.async_step_time_details({
        "start_mode": "fixed", "end_mode": "fixed",
        "trigger_time": "22:00", "end_time": "06:00",
        "start_offset": 0, "end_offset": 0, "sun_entity": "sun.sun",
        "start_transition": 0, "end_transition": 0, "transition_interval": 5,
    }))
    result = run(flow.async_step_settings(settings))
    return flow, result


_SETTINGS = {
    "day_enabled": True, "brightness": 25, "color_temp_kelvin": 2200,
    "day_brightness": 100, "day_color_temp_kelvin": 4000,
    "tolerance_brightness": 4, "tolerance_kelvin": 150,
    "verify_delay": 5, "settle_delay": 1,
    "turn_on_listen": True, "only_when_on": False, "respect_manual": True,
}


def test_reconfigure_carries_day_ci_lights_new_shape():
    """新形状条目 reconfigure → day_ci_lights 携带（改灯时按灯集裁剪）。"""
    entry = SimpleNamespace(
        entry_id="anl1",
        data={"kind": "anl", "lights": [LIGHT, SECOND], CONF_DAY_ENABLED: True,
              CONF_DAY_CI_LIGHTS: [LIGHT], CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
        options={},
        unique_id="anl:light.bath,light.second",
    )
    flow, result = _reconfigure_flow_to_settings(
        entry, [LIGHT, SECOND], {**_SETTINGS}
    )
    assert result["type"] == "abort"
    assert flow._reconfigure_entry.data[CONF_DAY_CI_LIGHTS] == [LIGHT]
    assert flow._reconfigure_entry.data[CONF_DAY_CI_VIRTUAL_LIGHT] == VIRT


def test_reconfigure_prunes_carried_ci_lights_by_new_light_set():
    """改灯撤掉 CI 接管灯 → 携带时裁剪（清单永远 ⊆ 灯集）。"""
    entry = SimpleNamespace(
        entry_id="anl1",
        data={"kind": "anl", "lights": [LIGHT, SECOND], CONF_DAY_ENABLED: True,
              CONF_DAY_CI_LIGHTS: [LIGHT], CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
        options={},
        unique_id="anl:light.bath,light.second",
    )
    flow, result = _reconfigure_flow_to_settings(entry, [SECOND], {**_SETTINGS})
    assert result["type"] == "abort"
    assert flow._reconfigure_entry.data[CONF_DAY_CI_LIGHTS] == []


def test_reconfigure_legacy_ci_entry_survives_without_silent_reset():
    """存量 day_ci_enabled=true 条目 reconfigure：携带时展开为当时灯集，
    CI 不被向导保存静默清零（读入迁移的写侧对偶）。"""
    entry = SimpleNamespace(
        entry_id="anl1",
        data={"kind": "anl", "lights": [LIGHT], CONF_DAY_ENABLED: True,
              CONF_DAY_CI_ENABLED: True, CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
        options={},
        unique_id="anl:light.bath",
    )
    flow, result = _reconfigure_flow_to_settings(entry, [LIGHT], {**_SETTINGS})
    assert result["type"] == "abort"
    saved = flow._reconfigure_entry.data
    # 旧键不固化（死键），新键以展开形态落盘
    assert CONF_DAY_CI_ENABLED not in saved
    assert saved[CONF_DAY_CI_LIGHTS] == [LIGHT]
    assert saved[CONF_DAY_CI_VIRTUAL_LIGHT] == VIRT
    # 运行时判据：迁移语义下 CI 仍配置开
    m = NightLightManager(SimpleNamespace(), flow._reconfigure_entry)
    assert m.ci_configured() is True


def test_reconfigure_legacy_ci_disabled_stays_off():
    """存量 day_ci_enabled=false 条目 reconfigure → 不携带清单键（CI 关）。"""
    entry = SimpleNamespace(
        entry_id="anl1",
        data={"kind": "anl", "lights": [LIGHT], CONF_DAY_ENABLED: True,
              CONF_DAY_CI_ENABLED: False, CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
        options={},
        unique_id="anl:light.bath",
    )
    flow, result = _reconfigure_flow_to_settings(entry, [LIGHT], {**_SETTINGS})
    assert result["type"] == "abort"
    saved = flow._reconfigure_entry.data
    assert CONF_DAY_CI_LIGHTS not in saved
    assert CONF_DAY_CI_ENABLED not in saved  # 旧键死键不固化
