"""V1.5.0 首审修复批（wb 批 anl-v15-ci-20261019）的回归钉。

覆盖 F-057（CI 停用/改指/删条目归还旧虚拟灯）、F-058（存量值放行 +
注册表身份校验）、F-059（额外时段起点定时器）、F-060（CI 下日间窗口
步进跳过）、F-061（CI 开无灯 400/表单拦截 + day_ci_active 序列化）、
F-062（transitions_active 按生效带）、F-064（灯组拒绝自家虚拟灯）。

复用 test_web 的 REST/flow 桩（FakeHass/FakeRequest/_recording_hass、
_make_flow）与 test_anl_transitions 的 manager 桩（make_manager 家族），
不重复造桩；既有用例的签名适配（_async_run_steps 带 when）留在原文件。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import conftest as stubs
from test_anl_transitions import (
    BAND_KEY,
    LIGHT,
    _FakeState,
    _record,
    _RecordingServices,
    make_ci_manager,
    make_manager,
)
from test_anl_transitions import VIRT as MGR_VIRT  # noqa: I001 — isort 别名分块
from test_web import (
    FakeHass,
    FakeRequest,
    _anl_bucket,
    _config_entry,
    _config_manager,
    _make_flow,
    _recording_hass,
    fake_anl_manager,
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
    CONF_EXTRAS,
)
from custom_components.fumiha_lighting.anl.flow import AnlGroupOptionsFlow
from custom_components.fumiha_lighting.anl.manager import PlanStep
from custom_components.fumiha_lighting.const import CONF_KIND, KIND_ANL

VIRT = "light.virtX"
VIRT2 = "light.virt2"

# 共享表单输入（与 test_web 既有链式用例同形状）
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
    "tolerance_brightness": 4,
    "tolerance_kelvin": 150,
    "verify_delay": 5,
    "settle_delay": 1,
    "turn_on_listen": True,
    "only_when_on": False,
    "respect_manual": True,
}

# GUI 新增行的默认参数（DEFAULT_EXTRA_BRIGHTNESS/COLOR_TEMP_KELVIN）
_EXTRA_18 = [
    {"start": "18:00", "transition": 0, "brightness": 60, "color_temp_kelvin": 3000}
]


def _register_virtual(entity_id, platform="fumiha_lighting", domain="light"):
    """往注册表桩塞一个条目（autouse fixture 每测试后清空）。"""
    stubs._ENTITY_REGISTRY._entities[entity_id] = SimpleNamespace(
        entity_id=entity_id, platform=platform, domain=domain
    )


def _ci_entry(**extra):
    """带 CI on+virtX 的 anl 条目（web 用例基座；day_enabled 开——F-065
    生效判据要求日间模式开，否则 web 跨字段校验会 400）。"""
    data = {
        "kind": "anl",
        "lights": ["light.a"],
        CONF_DAY_ENABLED: True,
        CONF_DAY_CI_LIGHTS: ["light.a"],
        CONF_DAY_CI_VIRTUAL_LIGHT: VIRT,
    }
    data.update(extra)
    return _config_entry(data=data)


def _svc_recorder(events):
    async def _svc(domain, service, data, blocking=False):
        events.append(("svc", service, data.get("entity_id")))

    return _svc


# ---------------------------------------------------------------------------
# F-057：CI 停用/改指/删条目 → 归还旧虚拟灯
# ---------------------------------------------------------------------------


def test_web_post_ci_disabled_releases_old_virtual():
    """①存 CI on+virtX → POST 关闭 CI → 200 且 services 收到 turn_off。"""
    hass = _recording_hass()
    # F-083：归还走助手 lux mode 守卫——桩给出闭环在跑的真实形态
    hass.states = SimpleNamespace(
        get=lambda eid: SimpleNamespace(state="on", attributes={"mode": "active"})
        if eid == VIRT
        else None
    )
    # V1.7.0 反查推导：待归还虚拟灯由生效清单经反查表推导
    stubs.seed_light_zone(hass, "lux-x", "light.a", "卫生间", VIRT)
    _anl_bucket(hass, _config_manager(_ci_entry()))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {"day_ci_lights": []}}})
        )
    )
    assert resp.payload["ok"] is True
    assert hass.services.calls == [("light", "turn_off", {"entity_id": VIRT})]
    assert recorded[0][1]["options"]["day_ci_lights"] == []


def test_web_post_unchanged_ci_zero_calls():
    """②CI on 同实体再存无关键 → 零 turn_off（reload 等价场景不动灯）。"""
    hass = _recording_hass()
    _anl_bucket(hass, _config_manager(_ci_entry()))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {"brightness": 30}}})
        )
    )
    assert resp.payload["ok"] is True
    assert hass.services.calls == []


def test_web_post_repoint_releases_old_only():
    """③zone 改指（清单 light.a→light.b，V1.7.0 语义：虚拟灯无配置键，
    改指=改接管清单）→ turn_off 旧 zone 虚拟灯；新 zone 虚拟灯不动。"""
    hass = _recording_hass()
    hass.states = SimpleNamespace(  # F-083：助手 mode 守卫，见①
        get=lambda eid: SimpleNamespace(state="on", attributes={"mode": "active"})
        if eid in (VIRT, VIRT2)
        else None
    )
    stubs.seed_light_zone(hass, "lux-a", "light.a", "卫生间", VIRT)
    stubs.seed_light_zone(hass, "lux-b", "light.b", "干区", VIRT2)
    entry = _ci_entry()
    entry.data["lights"] = ["light.a", "light.b"]  # 灯集须含新接管灯
    _anl_bucket(hass, _config_manager(entry))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {
                                   "day_ci_lights": ["light.b"]}}})
        )
    )
    assert resp.payload["ok"] is True
    assert hass.services.calls == [("light", "turn_off", {"entity_id": VIRT})]
    assert recorded[0][1]["options"]["day_ci_lights"] == ["light.b"]


def test_web_post_validation_400_never_touches_lights():
    """④POST 校验 400 路径 → 零 turn_off（校验失败绝不动灯）。"""
    hass = _recording_hass()
    _anl_bucket(hass, _config_manager(_ci_entry()))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {"brightness": 0}}})
        )
    )
    assert resp.status == 400
    assert hass.services.calls == []
    assert recorded == []


def test_options_flow_releases_before_entry_update():
    """⑤options 流 CI on→关：先 turn_off 旧虚拟灯，后 create entry（顺序钉）。"""
    events = []
    opts = AnlGroupOptionsFlow()
    opts.config_entry = SimpleNamespace(
        entry_id="anl1",
        data={"kind": "anl", "lights": ["light.a"],
              CONF_DAY_CI_ENABLED: True, CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
        options={},
    )
    opts.hass = SimpleNamespace(
        # F-083：归还走助手 lux mode 守卫——桩给出闭环在跑的真实形态
        data={},  # 反查表读 hass.data 桶（真机 hass 恒有）
        states=SimpleNamespace(
            get=lambda eid: SimpleNamespace(
                state="on", attributes={"mode": "active"}
            )
            if eid == VIRT
            else None
        ),
        services=SimpleNamespace(async_call=_svc_recorder(events)),
        # F-067：flow 归还前跨条目检查读 config_entries.async_entries
        config_entries=SimpleNamespace(async_entries=lambda domain: []),
    )
    # V1.7.0 反查推导：旧生效清单（存量 day_ci_enabled=true → [light.a]）
    # 经反查表映射到 VIRT——桩喂 lux 桶 + 注册表条目
    stubs.seed_light_zone(opts.hass, "lux-x", "light.a", "卫生间", VIRT)
    real_create = opts.async_create_entry

    def _recording_create(title, data):
        events.append(("entry",))
        return real_create(title, data)

    opts.async_create_entry = _recording_create
    run(opts.async_step_init(None))
    run(opts.async_step_anl_init(_TIMES))
    final = run(opts.async_step_settings({**_SETTINGS}))
    assert final["type"] == "create_entry"
    assert events == [("svc", "turn_off", VIRT), ("entry",)]


def test_reconfigure_flow_releases_old_virtual_on_ci_off():
    """补钉：reconfigure 关 CI → turn_off 旧虚拟灯后才落条目（F-057.2）。"""
    flow = _make_flow()
    events = []
    flow.hass = SimpleNamespace(
        # F-083：归还走助手 lux mode 守卫——桩给出闭环在跑的真实形态
        data={},  # 反查表读 hass.data 桶（真机 hass 恒有）
        states=SimpleNamespace(
            get=lambda eid: SimpleNamespace(
                state="on", attributes={"mode": "active"}
            )
            if eid == VIRT
            else None
        ),
        services=SimpleNamespace(async_call=_svc_recorder(events)),
        # F-067：flow 归还前跨条目检查读 config_entries.async_entries
        config_entries=SimpleNamespace(async_entries=lambda domain: []),
    )
    # V1.7.0 反查推导：旧生效清单（存量形状 → [light.a]）映射到 VIRT
    stubs.seed_light_zone(flow.hass, "lux-x", "light.a", "卫生间", VIRT)
    flow.source = "reconfigure"
    entry = SimpleNamespace(
        entry_id="anl1",
        data={"kind": "anl", "lights": ["light.a"],
              CONF_DAY_CI_ENABLED: True, CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
        options={},
        unique_id="anl:light.a",
    )
    flow._reconfigure_entry = entry
    run(flow.async_step_reconfigure(None))
    run(flow.async_step_anl_reconfigure({"lights": ["light.a"]}))
    run(flow.async_step_time_details(_TIMES))
    result = run(flow.async_step_settings({**_SETTINGS}))
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert events == [("svc", "turn_off", VIRT)]


def test_remove_entry_anl_releases_virtual_lux_does_not():
    """⑥async_remove_entry：anl 条目真删除 → turn_off；lux 条目 → 零调用。"""
    hass = FakeHass()
    # F-083：归还走助手 lux mode 守卫——桩给出闭环在跑的真实形态
    hass.states = SimpleNamespace(
        get=lambda eid: SimpleNamespace(state="on", attributes={"mode": "active"})
        if eid == VIRT
        else None
    )
    # V1.7.0 反查推导：待归还虚拟灯由生效清单经反查表推导
    stubs.seed_light_zone(hass, "lux-x", "light.a", "卫生间", VIRT)
    anl_entry = SimpleNamespace(
        entry_id="anl1",
        data={"kind": "anl", "lights": ["light.a"],
              CONF_DAY_CI_ENABLED: True, CONF_DAY_CI_VIRTUAL_LIGHT: VIRT},
        options={},
    )
    run(pkg.async_remove_entry(hass, anl_entry))
    assert hass.services.calls == [("light", "turn_off", {"entity_id": VIRT})]

    hass2 = FakeHass()
    lux_entry = SimpleNamespace(entry_id="lux1", data={"kind": "lux"}, options={})
    run(pkg.async_remove_entry(hass2, lux_entry))
    assert hass2.services.calls == []


# ---------------------------------------------------------------------------
# F-058：存量值放行 + 注册表身份校验
# ---------------------------------------------------------------------------


def test_web_post_stored_ci_lights_resubmit_zero_release():
    """存量值放行（F-058 新形态）：清单原样重存（zone 有效）→ 200、零归还
    （reload 等价场景不动灯；虚拟灯注册表校验随键废除消失）。"""
    hass = _recording_hass()
    stubs.seed_light_zone(hass, "lux-x", "light.a", "卫生间", VIRT)
    _anl_bucket(hass, _config_manager(_ci_entry()))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {
                                   "day_ci_lights": ["light.a"]}}})
        )
    )
    assert resp.payload["ok"] is True
    assert hass.services.calls == []


def test_web_post_existing_virtual_registry_present_states_absent_ok():
    """F-058：存量灯注册表在、states 无、patch 只改亮度 → 200 不复查存量。"""
    hass = _recording_hass()
    _anl_bucket(hass, _config_manager(_ci_entry()))
    _register_virtual(VIRT)
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {"brightness": 30}}})
        )
    )
    assert resp.payload["ok"] is True


def test_web_post_ci_light_outside_any_zone_400():
    """zone 归属校验（V1.7.0 A1/A2 同源）：组内灯 ∉ 反查表 → 400 逐灯报名、
    零落盘（校验失败绝不动灯）。"""
    hass = _recording_hass()
    entry = _ci_entry()
    entry.data["lights"] = ["light.a", "light.b"]  # light.b 在组内、无区域
    _anl_bucket(hass, _config_manager(entry))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {
                                   "day_ci_lights": ["light.b"]}}})
        )
    )
    assert resp.status == 400
    assert "灯 light.b 不属于任何恒照度区域" in resp.payload["error"]
    assert recorded == []


def test_web_post_new_virtual_wrong_platform_400():
    """F-064 身份面：注册表在但 platform 不是本集成 → 400。"""
    hass = _recording_hass()
    _anl_bucket(hass, _config_manager(_ci_entry()))
    _register_virtual("light.other", platform="hue", domain="light")
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {
                                   "day_ci_virtual_light": "light.other"}}})
        )
    )
    assert resp.status == 400


# ---------------------------------------------------------------------------
# F-059：额外时段起点定时器
# ---------------------------------------------------------------------------


def test_extra_start_timer_scheduled_on_start():
    """①start() 带 extra 18:00（now 12:00）→ 定点注册今天 18:00（+22:00 触发）。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)
    stubs.POINT_IN_TIME_CALLS.clear()
    m = make_ci_manager(**{CONF_EXTRAS: _EXTRA_18})
    m.start()
    whens = [w for w, _ in stubs.POINT_IN_TIME_CALLS]
    assert datetime(2026, 10, 2, 18, 0) in whens
    assert datetime(2026, 10, 2, 22, 0) in whens  # 既有夜间触发不受影响


def test_extra_start_applies_period_params_and_seeds():
    """②_async_extra_start(0) 灯已亮 → 下发 60/3000、播种 expected、清接管。"""
    m = make_ci_manager(**{CONF_EXTRAS: _EXTRA_18})
    m.hass.states.mapping[LIGHT] = _FakeState("on", 255, 4000)
    # F-076：归还预检读虚拟灯 lux mode 属性——本夹具不带 mode（视 lux 未
    # 在跑）→ 归还分支跳过，本用例只钉参数下发与播种；manager 侧虚拟灯名
    # 来自 test_anl_transitions 的 MGR_VIRT（"light.virt"）
    m.hass.states.mapping[MGR_VIRT] = _FakeState("on")
    m.machines[LIGHT].manual_override = True
    calls = []
    m._async_process_light = _record(calls)
    run(m._async_extra_start(0))
    assert calls == [(LIGHT, 60, 3000, None)]
    assert m.machines[LIGHT].expected == (60, 3000)
    assert m.machines[LIGHT].applied == (60, 3000)
    assert m.machines[LIGHT].manual_override is False


def test_extra_start_ci_hands_back_virtual_first():
    """③CI 开+虚拟灯亮 → 事件序先 turn_off 虚拟灯，后 extra 参数直控。"""
    m = make_ci_manager(**{CONF_EXTRAS: _EXTRA_18})
    events = []
    # F-068+F-076：归还过双预检——虚拟灯可见且 lux mode 在 handback 集合
    # （本场景=CI 开着且闭环 active）
    m.hass.states.mapping[MGR_VIRT] = _FakeState("on", mode="active")

    class _Svc:
        async def async_call(self, domain, service, data, blocking=False):
            events.append(("svc", service, data.get("entity_id")))

    async def _fake_process(
        entity_id, brightness, kelvin, allow_turn_on=True, transition_s=None
    ):
        events.append(("process", entity_id, brightness, kelvin))
        return True

    m.hass.services = _Svc()
    m._async_process_light = _fake_process
    run(m._async_extra_start(0))
    assert events == [
        ("svc", "turn_off", "light.virt"),
        ("process", LIGHT, 60, 3000),
    ]


def test_extra_start_ci_disabled_zero_virtual_calls():
    """④CI 关 → 零虚拟灯调用（test_day_trigger_ci_disabled 的 extra 面）。"""
    m = make_manager(
        **{CONF_DAY_CI_LIGHTS: [],
           CONF_DAY_CI_VIRTUAL_LIGHT: "light.virt",
           CONF_EXTRAS: _EXTRA_18}
    )
    svc = []
    m.hass.services = _RecordingServices(svc)
    calls = []
    m._async_process_light = _record(calls)
    run(m._async_extra_start(0))
    assert svc == []
    assert calls == [(LIGHT, 60, 3000, None)]


def test_extra_start_timers_cancelled_on_stop():
    """⑤stop() 后 POINT_IN_TIME_CALLS 注册项被移除（unsub 生效）。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)
    stubs.POINT_IN_TIME_CALLS.clear()
    m = make_ci_manager(**{CONF_EXTRAS: _EXTRA_18})
    m.start()
    assert stubs.POINT_IN_TIME_CALLS  # trigger + extra 起点
    m.stop()
    assert stubs.POINT_IN_TIME_CALLS == []


def test_extra_start_callback_rearms_for_next_day():
    """补钉：起点回调执行后自循环重排次日（未停止时）。"""
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 12, 0)
    stubs.POINT_IN_TIME_CALLS.clear()
    m = make_ci_manager(**{CONF_EXTRAS: _EXTRA_18})
    m.start()
    fired = [
        (w, a)
        for w, a in stubs.POINT_IN_TIME_CALLS
        if w == datetime(2026, 10, 2, 18, 0)
    ]
    assert len(fired) == 1
    when, action = fired[0]
    calls = []
    m._async_process_light = _record(calls)
    stubs._FakeClock.now_dt = datetime(2026, 10, 2, 18, 0)  # 触发时刻的 now
    run(action(None))
    assert calls == [(LIGHT, 60, 3000, None)]
    assert datetime(2026, 10, 3, 18, 0) in [w for w, _ in stubs.POINT_IN_TIME_CALLS]


# ---------------------------------------------------------------------------
# F-060：CI 下跨入日间窗口的计划步跳过
# ---------------------------------------------------------------------------


def test_plan_step_in_ci_daytime_window_skipped():
    """CI 开 + extra 06:30/60（带 05:30-06:30，from=night 保留）：带内夜间
    时刻（05:45）正常下发，跨过日间锚点后的同带步进（06:05）零下发。"""
    m = make_ci_manager(
        **{CONF_EXTRAS: [
            {"start": "06:30", "transition": 60,
             "brightness": 60, "color_temp_kelvin": 3000}
        ]}
    )
    m.hass.states.mapping[LIGHT] = _FakeState("on", 255, 4000)
    calls = []
    m._async_process_light = _record(calls)
    step = PlanStep(LIGHT, 88, 3830, BAND_KEY)
    run(m._async_run_step(step, datetime(2026, 10, 2, 5, 45)))
    assert calls == [(LIGHT, 88, 3830, 300)]
    calls.clear()
    run(m._async_run_step(step, datetime(2026, 10, 2, 6, 5)))
    assert calls == []


# ---------------------------------------------------------------------------
# F-061：CI 开无虚拟灯拦截 + day_ci_active 序列化
# ---------------------------------------------------------------------------


def test_web_post_ci_on_empty_virtual_400():
    """①POST CI on+空串 → 400（页面谎报防线），零落盘零动灯。"""
    hass = _recording_hass()
    _anl_bucket(
        hass,
        _config_manager(_config_entry(data={"kind": "anl", "lights": ["light.a"]})),
    )
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {
                                   "day_enabled": True,
                                   "day_ci_lights": ["light.a"],
                                   "day_ci_virtual_light": ""}}})
        )
    )
    assert resp.status == 400
    assert "day_ci_virtual_light" in resp.payload["error"]
    assert recorded == []
    assert hass.services.calls == []


def test_snapshot_day_ci_active_follows_configured():
    """④快照序列化：configured 假（enabled+空灯）→ False；真 → True。"""
    hass = FakeHass()
    _anl_bucket(hass, fake_anl_manager(day_ci_lights=[], day_ci_virtual_light=""))
    resp = run(web.AnlSnapshotView().get(FakeRequest(hass)))
    assert resp.payload["managers"][0]["day_ci_active"] is False

    hass2 = FakeHass()
    _anl_bucket(
        hass2,
        fake_anl_manager(
            day_enabled=True,
            day_ci_lights=["light.a"],
            day_ci_virtual_light="light.virt",
        ),
    )
    resp2 = run(web.AnlSnapshotView().get(FakeRequest(hass2)))
    assert resp2.payload["managers"][0]["day_ci_active"] is True


# ---------------------------------------------------------------------------
# F-062：transitions_active 按生效带计算
# ---------------------------------------------------------------------------


def test_transitions_active_false_when_ci_filters_all_day_side_bands():
    """CI 开 + 仅 start/end 过渡（day 侧带全滤）→ transitions_active False。"""
    m = make_ci_manager()
    assert m.transitions_active is False


def test_transitions_active_true_without_ci():
    """同配置 CI 关 → True（生效带==原始带，语义不变）。"""
    m = make_manager(
        **{CONF_DAY_CI_LIGHTS: [], CONF_DAY_CI_VIRTUAL_LIGHT: "light.virt"}
    )
    assert m.transitions_active is True


def test_transitions_active_true_with_night_extra_band():
    """CI 开 + 夜间 extra 带（night↔extra 不受 CI 过滤）→ True。"""
    m = make_ci_manager(
        **{CONF_EXTRAS: [
            {"start": "23:30", "transition": 60,
             "brightness": 60, "color_temp_kelvin": 3000}
        ]}
    )
    assert m.transitions_active is True


# ---------------------------------------------------------------------------
# F-064：灯组拒绝本集成的虚拟恒照度灯
# ---------------------------------------------------------------------------


def test_wizard_lights_step_rejects_own_virtual_light():
    """向导灯组含本集成虚拟灯 → 表单错误 light_is_virtual；剔除后正常。"""
    flow = _make_flow()
    flow.hass = SimpleNamespace(states=SimpleNamespace(get=lambda eid: None))
    _register_virtual("light.virt")
    run(flow.async_step_user({CONF_KIND: KIND_ANL}))
    errored = run(flow.async_step_anl_user({"lights": ["light.a", "light.virt"]}))
    assert errored["errors"] == {"base": "light_is_virtual"}
    ok = run(flow.async_step_anl_user({"lights": ["light.a"]}))
    assert ok["step_id"] == "time_details"


def test_wizard_lights_step_allows_real_light_only():
    """反向钉：他平台的真实灯（platform 非本集成）零误伤。"""
    flow = _make_flow()
    flow.hass = SimpleNamespace(states=SimpleNamespace(get=lambda eid: None))
    _register_virtual("light.hue", platform="hue", domain="light")
    run(flow.async_step_user({CONF_KIND: KIND_ANL}))
    result = run(flow.async_step_anl_user({"lights": ["light.hue"]}))
    assert result["step_id"] == "time_details"
