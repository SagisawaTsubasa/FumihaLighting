"""Merged web-layer tests: two page REST surfaces, dual descriptors,
REST write endpoints and the kind-discriminating config flow."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from datetime import time as _time
from types import SimpleNamespace

from custom_components.fumiha_lighting import web
from custom_components.fumiha_lighting.const import (
    CONF_KIND,
    KIND_ANL,
    KIND_LUX,
)


def _json_body(data):
    """FakeRequest.json 的桩：返回 async callable。"""

    async def _read():
        return data

    return _read


class FakeBus:
    def __init__(self):
        self.fired = []
        self.listen_once = []

    def async_fire(self, event_type, data=None):
        self.fired.append((event_type, data))

    def async_listen_once(self, event_type, cb):
        self.listen_once.append((event_type, cb))
        return lambda: None


class FakeHttp:
    def __init__(self):
        self.views = []
        self.static = []

    def register_view(self, view):
        self.views.append(view)

    async def async_register_static_paths(self, configs):
        self.static.extend(configs)


class FakeServices:
    def __init__(self):
        self.registered = {}

    def async_register(self, domain, service, handler, schema=None):
        self.registered[(domain, service)] = handler

    def has_service(self, domain, service):
        return (domain, service) in self.registered

    def async_remove(self, domain, service):
        self.registered.pop((domain, service), None)


class FakeHass:
    def __init__(self):
        self.data = {}
        self.bus = FakeBus()
        self.http = FakeHttp()
        self.services = FakeServices()
        self.config_entries = SimpleNamespace(async_update_entry=lambda *a, **k: None)


class FakeRequest:
    def __init__(self, hass, body=None):
        self.app = {"hass": hass}
        self._body = body or {}

    async def json(self):
        return self._body


def fake_lux_runtime(entry_id="lux1", zone="卫生间", **overrides):
    curve = SimpleNamespace(
        points=[(0.0, 0.0), (50.25, 111.234), (100.0, 239.8)],
        source="calibrated",
        max_lux=239.8,
        slope_20_80=2.412,
    )
    rt = SimpleNamespace(
        entry_id=entry_id,
        zone_name=zone,
        mode="active",
        cal_progress="",
        target_lux=150.0,
        commanded_pct=45.0,
        lux_stale=False,
        cal_last_error=None,
        async_cancel_calibration=None,  # 序列化不读方法；cancel 端点测试用真 runtime
        curve=curve,
        decisions=["22:41:03 [lux] → set 45%"],
        last_decision=SimpleNamespace(ambient_lux=42.5, needed_lux=107.5),
        last_adjustment=None,
        light_entity="light.bath",
        lux_entity="sensor.bath_lux",
    )
    for k, v in overrides.items():
        setattr(rt, k, v)
    return rt


def fake_anl_manager(entry_id="anl1", title="自动夜灯", **overrides):
    from custom_components.fumiha_lighting.anl.manager import (
        LightMachine,
        LightState,
    )

    mgr = SimpleNamespace(
        entry=SimpleNamespace(entry_id=entry_id, title=title),
        machines={
            "light.a": LightMachine(entity_id="light.a"),
            "light.b": LightMachine(
                entity_id="light.b",
                state=LightState.MISMATCH,
                manual_override=True,
                mismatch_steps=2,
                target=(12, 2700),
            ),
        },
        start_mode="sunset",
        end_mode="sunrise",
        transition_enabled=False,
        day_enabled=True,
        respect_manual=True,
    )
    mgr._anchor_modes = lambda: [
        (_time(23, 30), "night"),
        (_time(6, 56), "day"),
    ]
    mgr._next_start_dt = lambda: None
    mgr._mode_at = lambda minutes: "night"
    mgr.anchor_snapshot = lambda: {
        "anchors": [(_time(23, 30), "night"), (_time(6, 56), "day")],
        "current_mode": "night",
        "next_anchor": None,
    }
    for k, v in overrides.items():
        setattr(mgr, k, v)
    return mgr


def _lux_bucket(hass, *runtimes):
    buckets = hass.data.setdefault(web.DOMAIN, {"lux": {}, "anl": {}})
    for rt in runtimes:
        buckets["lux"][rt.entry_id] = rt
    return buckets


def _anl_bucket(hass, *managers):
    buckets = hass.data.setdefault(web.DOMAIN, {"lux": {}, "anl": {}})
    for mgr in managers:
        buckets["anl"][mgr.entry.entry_id] = mgr
    return buckets


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# lux overview
# ---------------------------------------------------------------------------


def test_lux_overview_success():
    hass = FakeHass()
    _lux_bucket(hass, fake_lux_runtime("lux1", "卫生间"))
    resp = run(web.LuxOverviewView().get(FakeRequest(hass)))
    payload = json.loads(json.dumps(resp.payload, allow_nan=False))
    z = payload["zones"][0]
    assert z["entry_id"] == "lux1" and z["mode"] == "active"
    assert z["curve"]["points"][-1] == [100.0, 239.8]
    assert "kind" not in z
    # FL-IMP-05: 校准错误通道与陈旧窗口随载荷下发（页面 chip/中止提示的数据源）
    assert z["cal_last_error"] is None
    assert z["stale_limit_min"] == 30


def test_lux_overview_empty():
    hass = FakeHass()
    run(web.async_setup_web(hass))
    resp = run(web.LuxOverviewView().get(FakeRequest(hass)))
    assert resp.payload == {"zones": []}


# ---------------------------------------------------------------------------
# REST write endpoints (GUI → runtime, no entity detour)
# ---------------------------------------------------------------------------


def test_target_write_success():
    hass = FakeHass()
    from custom_components.fumiha_lighting.lux.runtime import ZoneRuntime

    rt = fake_lux_runtime("lux1")
    calls = []
    rt.notify = lambda: None  # 真 set_target 会 notify（dispatcher 桩已静默）
    rt.set_target = lambda v: (calls.append(v), ZoneRuntime.set_target(rt, v))
    _lux_bucket(hass, rt)
    req = FakeRequest(hass)
    req.json = _json_body({"entry_id": "lux1", "value": 200})
    resp = run(web.LuxTargetView().post(req))
    assert resp.payload == {"ok": True, "target_lux": 200.0}
    assert calls == [200.0]


def test_target_write_out_of_range():
    hass = FakeHass()
    rt = fake_lux_runtime("lux1")
    rt.set_target = lambda v: (_ for _ in ()).throw(AssertionError("must not write"))
    _lux_bucket(hass, rt)
    req = FakeRequest(hass)
    req.json = _json_body({"entry_id": "lux1", "value": 9999})
    resp = run(web.LuxTargetView().post(req))
    assert resp.status == 400 and "lx 之间" in resp.payload["error"]


def test_target_write_unknown_entry():
    hass = FakeHass()
    _lux_bucket(hass)
    req = FakeRequest(hass)
    req.json = _json_body({"entry_id": "nope", "value": 200})
    resp = run(web.LuxTargetView().post(req))
    assert resp.status == 404


def test_calibrate_write_started_true():
    hass = FakeHass()
    rt = fake_lux_runtime("lux1")
    rt.start_calibration = lambda: True
    _lux_bucket(hass, rt)
    req = FakeRequest(hass)
    req.json = _json_body({"entry_id": "lux1"})
    resp = run(web.LuxCalibrateView().post(req))
    assert resp.payload == {"ok": True, "started": True}


def test_calibrate_write_started_false_with_reason():
    """GUI reads the boolean + reason — no exception channel needed."""
    hass = FakeHass()
    rt = fake_lux_runtime("lux1")
    rt.start_calibration = lambda: False
    _lux_bucket(hass, rt)
    req = FakeRequest(hass)
    req.json = _json_body({"entry_id": "lux1"})
    resp = run(web.LuxCalibrateView().post(req))
    assert (
        resp.payload["started"] is False and "只能校准一个区" in resp.payload["reason"]
    )


# ---------------------------------------------------------------------------
# anl snapshot + trigger
# ---------------------------------------------------------------------------


def test_anl_snapshot_success():
    hass = FakeHass()
    _anl_bucket(hass, fake_anl_manager())
    resp = run(web.AnlSnapshotView().get(FakeRequest(hass)))
    m = resp.payload["managers"][0]
    assert m["current_mode"] == "night"
    assert [(a["time"], a["mode"]) for a in m["anchors"]] == [
        ("23:30", "night"),
        ("06:56", "day"),
    ]
    assert m["lights"][1]["manual_override"] is True


def test_anl_trigger_snapshots_managers():
    hass = FakeHass()
    triggered = []

    async def _trigger(reason):
        triggered.append(reason)

    mgr = fake_anl_manager("anl1")
    mgr.async_trigger = _trigger
    _anl_bucket(hass, mgr)
    resp = run(web.AnlTriggerView().post(FakeRequest(hass)))
    assert resp.payload == {"ok": True, "triggered": 1}
    assert triggered == ["page"]


# ---------------------------------------------------------------------------
# dual descriptors: per-kind lifecycles
# ---------------------------------------------------------------------------


def test_hub_mode_registers_both_descriptors():
    hass = FakeHass()
    _lux_bucket(hass, fake_lux_runtime())
    _anl_bucket(hass, fake_anl_manager())
    hass.data[web.REGISTRY_KEY] = {}
    run(web.async_setup_web(hass))
    reg = hass.data[web.REGISTRY_KEY]
    assert web.PAGE_LUX_ID == "luxbalance.main"
    assert web.PAGE_ANL_ID == "anl.main"
    assert reg[web.PAGE_LUX_ID]["module_url"].startswith(
        "/fumiha_lighting/page-lux.js?ver="
    )
    assert reg[web.PAGE_ANL_ID]["module_url"].startswith(
        "/fumiha_lighting/page-anl.js?ver="
    )
    assert web.STANDALONE_LUX_URL_PATH not in hass.data.get("frontend_panels", {})
    actions = [d["action"] for _, d in hass.bus.fired]
    assert actions == ["register", "register"]


def test_standalone_mode_registers_both_panels():
    hass = FakeHass()
    _lux_bucket(hass, fake_lux_runtime())
    _anl_bucket(hass, fake_anl_manager())
    run(web.async_setup_web(hass))
    panels = hass.data.get("frontend_panels", {})
    assert web.STANDALONE_LUX_URL_PATH in panels
    assert web.STANDALONE_ANL_URL_PATH in panels
    assert web.REGISTRY_KEY not in hass.data


def test_lux_descriptor_torn_down_while_anl_stays():
    """卸 lux 条目：lux 描述符拆、anl 描述符原样。"""
    hass = FakeHass()
    _lux_bucket(hass, fake_lux_runtime())
    _anl_bucket(hass, fake_anl_manager())
    hass.data[web.REGISTRY_KEY] = {}
    run(web.async_setup_web(hass))
    # lux 清空（模拟最后一个 lux 条目卸载后的 async_unload_web 调用）
    hass.data[web.DOMAIN]["lux"].clear()
    web.async_unload_web(hass)
    reg = hass.data[web.REGISTRY_KEY]
    assert web.PAGE_LUX_ID not in reg
    assert web.PAGE_ANL_ID in reg
    actions = [d["action"] for _, d in hass.bus.fired]
    assert actions == ["register", "register", "unregister"]


def test_mixed_kind_reload_does_not_tear_down_the_other():
    """混合条目下单个条目卸载（hass.data 仍有另一类型）不拆任何注册。"""
    hass = FakeHass()
    _lux_bucket(hass, fake_lux_runtime())
    _anl_bucket(hass, fake_anl_manager())
    hass.data[web.REGISTRY_KEY] = {}
    run(web.async_setup_web(hass))
    # 单条目卸载：lux 桶还剩 0，anl 桶仍 1——lux 注册该拆（lux 条目清零）
    hass.data[web.DOMAIN]["lux"].clear()
    web.async_unload_web(hass)
    assert web.PAGE_ANL_ID in hass.data[web.REGISTRY_KEY]


def test_unload_when_both_kinds_empty_detaches_recheck():
    hass = FakeHass()
    hass.data[web.DOMAIN] = {"lux": {}, "anl": {}}
    hass.data[web.REGISTRY_KEY] = {}
    run(web.async_setup_web(hass))
    flags = hass.data[web.WEB_DATA_KEY]
    assert flags.get("started_recheck") is not None
    web.async_unload_web(hass)
    assert flags.get("started_recheck") is None


# ---------------------------------------------------------------------------
# config flow: kind discriminator
# ---------------------------------------------------------------------------


def _make_flow():
    from custom_components.fumiha_lighting.config_flow import (
        FumihaLightingConfigFlow,
    )

    return FumihaLightingConfigFlow()


def test_flow_kind_step_renders():
    flow = _make_flow()
    result = run(flow.async_step_user(None))
    assert result["type"] == "form" and result["step_id"] == "user"


def test_flow_kind_selects_lux():
    flow = _make_flow()
    flow.hass = SimpleNamespace(states=SimpleNamespace(get=lambda eid: None))
    result = run(flow.async_step_user({CONF_KIND: KIND_LUX}))
    assert result["type"] == "form" and result["step_id"] == "lux_zone"


def test_flow_kind_selects_anl():
    flow = _make_flow()
    flow.hass = SimpleNamespace(states=SimpleNamespace(get=lambda eid: None))
    result = run(flow.async_step_user({CONF_KIND: KIND_ANL}))
    assert result["type"] == "form" and result["step_id"] == "anl_user"


def test_flow_reconfigure_dispatches_on_kind():
    flow = _make_flow()
    flow.hass = SimpleNamespace(states=SimpleNamespace(get=lambda eid: None))
    lux_entry = SimpleNamespace(data={CONF_KIND: KIND_LUX}, options={})
    flow._reconfigure_entry = lux_entry
    result = run(flow.async_step_reconfigure(None))
    assert result["step_id"] == "lux_reconfigure"

    anl_entry = SimpleNamespace(data={CONF_KIND: KIND_ANL}, options={})
    flow._reconfigure_entry = anl_entry
    result = run(flow.async_step_reconfigure(None))
    assert result["step_id"] == "anl_reconfigure"


def test_options_flow_dispatches_on_kind():
    flow = _make_flow()
    lux_entry = SimpleNamespace(data={CONF_KIND: KIND_LUX}, options={})
    opts = flow.async_get_options_flow(lux_entry)
    assert type(opts).__name__ == "LuxZoneOptionsFlow"
    anl_entry = SimpleNamespace(data={CONF_KIND: KIND_ANL}, options={})
    opts = flow.async_get_options_flow(anl_entry)
    assert type(opts).__name__ == "AnlGroupOptionsFlow"


# ---------------------------------------------------------------------------
# gates + manifest
# ---------------------------------------------------------------------------


def test_view_gates():
    hass = FakeHass()
    run(web.async_setup_web(hass))
    for view in hass.http.views:
        assert view.requires_auth is True
        handler = getattr(view, "get", None) or view.post
        assert getattr(handler, "_require_admin_applied", False) is True


def test_setup_web_idempotent():
    hass = FakeHass()
    run(web.async_setup_web(hass))
    run(web.async_setup_web(hass))
    assert len(hass.http.views) == 6
    assert len(hass.http.static) == 1
    assert hass.http.static[0].url_path == web.URL_BASE


def test_manifest_declares_web_dependencies():
    from pathlib import Path

    mf = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "fumiha_lighting"
        / "manifest.json"
    )
    m = json.loads(mf.read_text(encoding="utf-8"))
    assert m["domain"] == "fumiha_lighting"
    assert m["dependencies"] == ["http", "frontend"]
    assert m["after_dependencies"] == ["fumiha_hub"]
    # 版本随影子/正式版流动，只断字段在位
    assert isinstance(m["version"], str) and m["version"]


# ---------------------------------------------------------------------------
# lux stale judgment (FL-UX-01)
# ---------------------------------------------------------------------------


def test_lux_stale_reason_time_dimension():
    from custom_components.fumiha_lighting.lux.runtime import _lux_stale_reason

    # 双侧 naive：runtime 的时区归一化会把两侧拉齐（真 HA 两侧均 aware）
    now = datetime.utcnow()

    fresh = SimpleNamespace(state="126.0", last_changed=now - timedelta(minutes=5))
    assert _lux_stale_reason(fresh) is None
    old = SimpleNamespace(state="126.0", last_changed=now - timedelta(minutes=45))
    reason = _lux_stale_reason(old)
    assert reason is not None and "45" in reason
    unavail = SimpleNamespace(state="unavailable", last_changed=now)
    assert _lux_stale_reason(unavail) == "传感器不可用"
    assert _lux_stale_reason(None) == "传感器不可用"
    # naive last_changed 按 UTC 处理，不得抛 TypeError
    naive = SimpleNamespace(
        state="126.0", last_changed=datetime.utcnow() - timedelta(minutes=45)
    )
    assert _lux_stale_reason(naive) is not None
    # 混合侧（FL-IMP-04）：aware last_changed + 桩 naive now——归一支路必须
    # 执行且不得抛 TypeError（真 HA 的 state.last_changed 恒为 aware）
    aware_old = SimpleNamespace(
        state="126.0",
        last_changed=datetime.now(timezone.utc) - timedelta(minutes=45),  # noqa: UP017 — 3.10 兼容
    )
    reason = _lux_stale_reason(aware_old)
    assert reason is not None and "45" in reason
    aware_fresh = SimpleNamespace(
        state="126.0",
        last_changed=datetime.now(timezone.utc) - timedelta(minutes=5),  # noqa: UP017 — 3.10 兼容
    )
    assert _lux_stale_reason(aware_fresh) is None


# ---------------------------------------------------------------------------
# calibration cancel (V1.2.1)
# ---------------------------------------------------------------------------


def test_calibrate_cancel_endpoint_paths():
    from custom_components.fumiha_lighting.lux.runtime import ZoneRuntime

    hass = FakeHass()
    # 真 runtime（SimpleNamespace 桩没有 cancel 方法）——idle 态取消是幂等 False
    rt = ZoneRuntime.__new__(ZoneRuntime)
    rt.__dict__.update(
        {
            "hass": hass,
            "entry_id": "lux1",
            "mode": "idle",
            "zone_name": "卫生间",
            "cal_last_error": None,
            "notify": lambda: None,
            "_cal_task": None,
            "_cal_captured": False,
            "_apply_lock": asyncio.Lock(),
        }
    )
    _lux_bucket(hass, rt)

    # 未知区域 → 404
    resp = run(
        web.LuxCalibrateCancelView().post(FakeRequest(hass, body={"entry_id": "ghost"}))
    )
    assert resp.payload == {"ok": False, "error": "未知区域"}

    # 无进行中校准 → ok/cancelled False（幂等）
    resp = run(
        web.LuxCalibrateCancelView().post(FakeRequest(hass, body={"entry_id": "lux1"}))
    )
    payload = json.loads(json.dumps(resp.payload))
    assert payload == {"ok": True, "cancelled": False}


def test_calibrate_cancel_restores_and_sets_no_error():
    """取消路径：mode 复位、cal_last_error 保持 None（主动取消≠错误）。"""
    from custom_components.fumiha_lighting.lux.const import CONF_CAL_STEP_PCT
    from custom_components.fumiha_lighting.lux.runtime import ZoneRuntime

    hass = FakeHass()
    hass.async_create_task = asyncio.ensure_future  # loop 内调用
    real = ZoneRuntime.__new__(ZoneRuntime)  # 跳过 __init__（依赖真 Store/registry）
    real.__dict__.update(
        {
            "hass": hass,
            "opts": {CONF_CAL_STEP_PCT: 5},
            "mode": "idle",
            "zone_name": "卫生间",
            "light_entity": "light.test",
            "lux_entity": "sensor.test",
            "target_lux": 150.0,
            "commanded_pct": None,
            "curve": None,
            "cal_last_error": None,
            "cal_progress": "",
            "_cal_task": None,
            "_cal_was_on": False,
            "_cal_captured": False,
            "_cal_pending_off": False,
            "_stopped": False,
            "_apply_lock": asyncio.Lock(),
            "_eval_task": None,
            "_stale_warned": False,
            "lux_stale": False,
            "signal": "fumiha_lighting_lux1_update",
            "notify": lambda: None,
        }
    )
    # start_calibration 需要遍历 hass.data[DOMAIN]["lux"] 的其他区域——空桶即可；
    # 兜底复位读现场灯态（states.get→None=不可用→IDLE，同刻取消灯未被触碰）
    hass.data.setdefault(web.DOMAIN, {})["lux"] = {}
    hass.states = SimpleNamespace(get=lambda entity_id: None)

    async def scenario():
        assert real.start_calibration() is True
        assert real.mode == "calibrating"
        # 立刻取消（task 可能尚未真正起步——cancel 于调度前也必须干净收场）
        assert await real.async_cancel_calibration() is True
        assert real.mode != "calibrating"
        assert real.cal_last_error is None
        # 再取消一次 → False（幂等）
        assert await real.async_cancel_calibration() is False

    asyncio.run(scenario())


def test_calibrate_cancel_same_tick_honors_pending_off():
    """FL-IMP-33：窗口①并发 turn_off 置位 pending_off——取消收尾兑现关灯。"""
    import asyncio

    from custom_components.fumiha_lighting.lux.runtime import ZoneRuntime

    calls = []

    class _Task:
        def done(self):
            return False

        def cancel(self):
            cancelled.append(True)
            return True

        def __await__(self):
            async def _noop():
                return None

            return _noop().__await__()

    async def _async_call_light(action, data):
        calls.append(action)
        return True

    async def _schedule_evaluate(reason):
        return None

    hass = FakeHass()
    hass.states = SimpleNamespace(get=lambda entity_id: None)  # 未采集→IDLE
    cancelled = []
    rt = ZoneRuntime.__new__(ZoneRuntime)
    rt.__dict__.update(
        {
            "hass": hass,
            "entry_id": "lux1",
            "mode": "calibrating",
            "zone_name": "卫生间",
            "light_entity": "light.test",
            "lux_entity": "sensor.test",
            "commanded_pct": 40.0,
            "curve": SimpleNamespace(source="calibrated", max_lux=180.0),
            "cal_last_error": None,
            "notify": lambda: None,
            "_cal_task": _Task(),
            "_cal_captured": False,
            "_cal_was_on": False,
            "_cal_pending_off": True,  # 窗口①内并发 turn_off 已置位
            "_cal_committed": False,
            "_stopped": False,
            "_apply_lock": asyncio.Lock(),
            "_async_call_light": _async_call_light,
            "_schedule_evaluate": _schedule_evaluate,
        }
    )
    _lux_bucket(hass, rt)

    async def scenario():
        # V1.3.0 开关语义: pending = 收尾后放手到 IDLE——不再兑现关灯
        assert await rt.async_cancel_calibration() is True
        assert calls == []  # 不再下发关灯（原灯归用户/夜灯模组）
        assert rt._cal_pending_off is False
        assert rt.mode == "idle"

    asyncio.run(scenario())


def test_calibrate_cancel_committed_returns_false():
    """FL-IMP-30：曲线已采纳（_cal_committed）后取消 → False 且任务不动。"""
    import asyncio

    from custom_components.fumiha_lighting.lux.runtime import ZoneRuntime

    cancelled = []

    class _Task:
        def done(self):
            return False

        def cancel(self):
            cancelled.append(True)
            return True

    hass = FakeHass()
    rt = ZoneRuntime.__new__(ZoneRuntime)
    rt.__dict__.update(
        {
            "hass": hass,
            "entry_id": "lux1",
            "mode": "calibrating",
            "zone_name": "卫生间",
            "cal_last_error": None,
            "notify": lambda: None,
            "_cal_task": _Task(),
            "_cal_committed": True,
            "_apply_lock": asyncio.Lock(),
        }
    )
    _lux_bucket(hass, rt)
    assert run(rt.async_cancel_calibration()) is False
    assert cancelled == []  # 存盘 await 起后任务不得被打断
    assert rt.mode == "calibrating"


def test_calibrate_cancel_mid_run_restores_active():
    """FL-IMP-23：采样后中途取消——mode 回 ACTIVE、恢复被调用、无错误。

    构造：_cal_was_on=True（任务已完成采集）、curve 在位、_async_sweep 桩为
    长挂起（cancel 落在该 await）——取消后任务的 finally 走真
    _finish_calibration（捕获值路径）+ 桩记录恢复调用。
    """
    import asyncio

    from custom_components.fumiha_lighting.lux.const import CONF_CAL_STEP_PCT
    from custom_components.fumiha_lighting.lux.runtime import ZoneRuntime

    hass = FakeHass()
    hass.async_create_task = asyncio.ensure_future
    # 任务采集步会读真灯态：on + brightness 128（≈50%）→ _cal_was_on=True
    # FL-IMP-29: 可切换灯态——采集时 on（驱动捕获值），取消前切 off 使
    # 捕获值路径（ACTIVE）与现场判定路径（off→IDLE）分歧
    state = {"mode": "on"}

    def _get(entity_id):
        if entity_id != "light.test":
            return None
        if state["mode"] == "on":
            return SimpleNamespace(state="on", attributes={"brightness": 128})
        return SimpleNamespace(state="off", attributes={})

    hass.states = SimpleNamespace(get=_get)
    real = ZoneRuntime.__new__(ZoneRuntime)
    restored = []

    async def fake_restore(was_on, orig_pct):
        restored.append((was_on, orig_pct))

    async def hanging_sweep(step):
        await asyncio.sleep(3600)
        return {"up": {}, "down": {}, "baseline": 0.0, "missed_streak": 0}

    real.__dict__.update(
        {
            "hass": hass,
            "opts": {CONF_CAL_STEP_PCT: 5},
            "mode": "idle",
            "zone_name": "卫生间",
            "light_entity": "light.test",
            "lux_entity": "sensor.test",
            "target_lux": 150.0,
            "commanded_pct": None,
            "curve": SimpleNamespace(source="calibrated", max_lux=180.0),
            "cal_last_error": None,
            "cal_progress": "",
            "_cal_task": None,
            "_cal_was_on": False,
            "_cal_captured": False,
            "_cal_pending_off": False,
            "_stopped": False,
            "_apply_lock": asyncio.Lock(),
            "_eval_task": None,
            "_stale_warned": False,
            "lux_stale": False,
            "signal": "fumiha_lighting_lux1_update",
            "notify": lambda: None,
            "_async_sweep": hanging_sweep,
            "_async_restore_light": fake_restore,
        }
    )
    hass.data.setdefault(web.DOMAIN, {})["lux"] = {}

    async def scenario():
        # 前置：灯亮有曲线的区被 start_calibration 挂起→进入 CALIBRATING
        assert real.start_calibration() is True
        # 让任务推进到 sweep 挂起点（两拍调度：task 启动 + await eval_task 缺省直落；
        # 采集步读真灯态置 _cal_was_on=True / orig_pct≈50）
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert real._cal_task is not None and not real._cal_task.done()
        assert real._cal_was_on is True
        # FL-IMP-29: 取消前切现场灯态为 off——ACTIVE 断言只有捕获值路径能给
        # （现场判定会判 IDLE），删 _finish_calibration 的捕获值分支即红
        state["mode"] = "off"

        assert await real.async_cancel_calibration() is True

        # 任务 finally（真 _finish_calibration）：_cal_was_on=True + curve → 回 ACTIVE
        assert real.mode == "active"
        # 恢复调用发生：was_on=True 带 orig_pct（128/255*100≈50.2）
        assert len(restored) == 1 and restored[0][0] is True
        assert abs(restored[0][1] - 50.2) < 0.5
        assert real.cal_last_error is None
        # 幂等
        assert await real.async_cancel_calibration() is False

    asyncio.run(scenario())


def test_calibrate_cancel_same_tick_uncaptured_light_on_judges_active():
    """FL-IMP-35①：未采集分支的现场判定钉——灯亮有曲线时同刻取消回 ACTIVE。

    现场判定（else 分支）与旧判定（_cal_was_on=False→IDLE）在此分歧：
    删 else 分支（回退旧判定）即红。
    """
    import asyncio

    from custom_components.fumiha_lighting.lux.runtime import ZoneRuntime

    hass = FakeHass()
    hass.async_create_task = asyncio.ensure_future
    hass.states = SimpleNamespace(
        get=lambda entity_id: (
            SimpleNamespace(state="on", attributes={"brightness": 200})
            if entity_id == "light.test"
            else None
        )
    )
    rt = ZoneRuntime.__new__(ZoneRuntime)
    rt.__dict__.update(
        {
            "hass": hass,
            "entry_id": "lux1",
            "mode": "idle",
            "zone_name": "卫生间",
            "light_entity": "light.test",
            "lux_entity": "sensor.test",
            "target_lux": 150.0,
            "commanded_pct": None,
            "curve": SimpleNamespace(source="calibrated", max_lux=180.0),
            "cal_last_error": None,
            "cal_progress": "",
            "_cal_task": None,
            "_cal_captured": False,
            "_cal_was_on": False,
            "_cal_pending_off": False,
            "_stopped": False,
            "_apply_lock": asyncio.Lock(),
            "_eval_task": None,
            "_stale_warned": False,
            "lux_stale": False,
            "signal": "fumiha_lighting_lux1_update",
        }
    )
    _lux_bucket(hass, rt)

    async def scenario():
        assert rt.start_calibration() is True
        # 同刻取消（task 未起步）→ 兜底 _finish_calibration 未采集分支
        assert await rt.async_cancel_calibration() is True
        # 现场灯态 on + curve → ACTIVE（旧判定会判 IDLE）
        assert rt.mode == "active"
        assert rt.cal_last_error is None

    asyncio.run(scenario())


def test_calibrate_cancel_window2_started_not_captured():
    """FL-IMP-35②：窗口②（已起步未采集，取消落在 await eval_task）——
    finally 走未采集分支按现场灯态判定，恢复调用必须为空。"""
    import asyncio

    from custom_components.fumiha_lighting.lux.const import CONF_CAL_STEP_PCT
    from custom_components.fumiha_lighting.lux.runtime import ZoneRuntime

    restored = []

    async def fake_restore(was_on, orig_pct):
        restored.append((was_on, orig_pct))

    async def hanging_eval():
        await asyncio.sleep(3600)

    async def hanging_sweep(step):
        await asyncio.sleep(3600)
        return {"up": {}, "down": {}, "baseline": 0.0, "missed_streak": 0}

    hass = FakeHass()
    hass.async_create_task = asyncio.ensure_future
    hass.states = SimpleNamespace(
        get=lambda entity_id: (
            SimpleNamespace(state="on", attributes={"brightness": 200})
            if entity_id == "light.test"
            else None
        )
    )
    real = ZoneRuntime.__new__(ZoneRuntime)
    real.__dict__.update(
        {
            "hass": hass,
            "opts": {CONF_CAL_STEP_PCT: 5},
            "mode": "active",  # 校准前 ACTIVE（灯亮有曲线）
            "zone_name": "卫生间",
            "light_entity": "light.test",
            "lux_entity": "sensor.test",
            "target_lux": 150.0,
            "commanded_pct": None,
            "curve": SimpleNamespace(source="calibrated", max_lux=180.0),
            "cal_last_error": None,
            "cal_progress": "",
            "_cal_task": None,
            "_cal_captured": False,
            "_cal_was_on": False,
            "_cal_pending_off": False,
            "_stopped": False,
            "_apply_lock": asyncio.Lock(),
            "_eval_task": None,  # 窗口②挂起点（scenario 内装配真 Task）
            "_stale_warned": False,
            "lux_stale": False,
            "signal": "fumiha_lighting_lux1_update",
            "notify": lambda: None,
            "_async_sweep": hanging_sweep,
            "_async_restore_light": fake_restore,
        }
    )
    hass.data.setdefault(web.DOMAIN, {})["lux"] = {}

    async def scenario():
        real._eval_task = asyncio.ensure_future(hanging_eval())  # loop 内装配
        assert real.start_calibration() is True
        await asyncio.sleep(0)  # 任务起步，挂在 await eval_task（未到采集）
        assert real._cal_task is not None and not real._cal_task.done()
        assert real._cal_captured is False  # 窗口②特征：未采集

        assert await real.async_cancel_calibration() is True

        # finally 走未采集分支：现场灯态 on + curve → 回 ACTIVE（旧判定 IDLE）
        assert real.mode == "active"
        assert restored == []  # 灯未被 sweep 触碰，无恢复调用
        assert real.cal_last_error is None
        real._eval_task.cancel()  # 清理挂起协程防警告

    asyncio.run(scenario())


def test_calibrate_cancel_wakes_suspended_sibling():
    """FL-IMP-36①：取消收尾对称唤醒挂起区（IDLE+curve+灯亮→ACTIVE+resume）。"""
    import asyncio

    from custom_components.fumiha_lighting.lux.const import CONF_CAL_STEP_PCT
    from custom_components.fumiha_lighting.lux.runtime import ZoneRuntime

    resumes = []
    restored = []

    async def fake_restore(was_on, orig_pct):
        restored.append((was_on, orig_pct))

    async def hanging_sweep(step):
        await asyncio.sleep(3600)
        return {"up": {}, "down": {}, "baseline": 0.0, "missed_streak": 0}

    def _schedule_evaluate(reason):
        resumes.append(reason)

    hass = FakeHass()
    hass.async_create_task = asyncio.ensure_future
    hass.states = SimpleNamespace(
        get=lambda entity_id: (
            SimpleNamespace(state="on", attributes={"brightness": 200})
            if entity_id
            in ("light.main", "light.sib")  # 主区采集/恢复链 + 副区唤醒判据
            else SimpleNamespace(state="off", attributes={})
        )
    )
    sweeping = ZoneRuntime.__new__(ZoneRuntime)
    sweeping.__dict__.update(
        {
            "hass": hass,
            "opts": {CONF_CAL_STEP_PCT: 5},
            "mode": "idle",
            "entry_id": "lux1",
            "zone_name": "主区",
            "light_entity": "light.main",
            "lux_entity": "sensor.main",
            "target_lux": 150.0,
            "commanded_pct": None,
            "curve": SimpleNamespace(source="calibrated", max_lux=180.0),
            "cal_last_error": None,
            "cal_progress": "",
            "_cal_task": None,
            "_cal_captured": False,
            "_cal_was_on": False,
            "_cal_pending_off": False,
            "_stopped": False,
            "_apply_lock": asyncio.Lock(),
            "_eval_task": None,
            "_stale_warned": False,
            "lux_stale": False,
            "signal": "fumiha_lighting_lux1_update",
            "notify": lambda: None,
            "_async_sweep": hanging_sweep,
            # FL-IMP-44: 恢复链可见——异常不得被 suppress 静默吞掉
            "_async_restore_light": fake_restore,
        }
    )
    sibling = ZoneRuntime.__new__(ZoneRuntime)
    sibling.__dict__.update(
        {
            "hass": hass,
            "entry_id": "lux2",
            "mode": "active",  # ACTIVE 起步——start_calibration 将它挂起为 IDLE
            "zone_name": "副区",
            "light_entity": "light.sib",
            "lux_entity": "sensor.sib",
            "target_lux": 150.0,
            "commanded_pct": None,
            "curve": SimpleNamespace(source="calibrated", max_lux=180.0),
            "cal_last_error": None,
            "cal_progress": "",
            "_cal_task": None,
            "_cal_captured": False,
            "_cal_was_on": False,
            "_cal_pending_off": False,
            "_stopped": False,
            "_apply_lock": asyncio.Lock(),
            "_eval_task": None,
            "_stale_warned": False,
            "lux_stale": False,
            "signal": "fumiha_lighting_lux2_update",
            "notify": lambda: None,
            "_schedule_evaluate": _schedule_evaluate,
        }
    )
    hass.data.setdefault(web.DOMAIN, {})["lux"] = {
        "lux1": sweeping,
        "lux2": sibling,
    }

    async def scenario():
        assert sweeping.start_calibration() is True
        # start_calibration 把 ACTIVE 副区挂起为 IDLE
        assert sibling.mode == "idle"
        await asyncio.sleep(0)
        assert await sweeping.async_cancel_calibration() is True

        # 取消收尾对称唤醒副区：IDLE+curve+灯亮 → ACTIVE + resume 调度
        assert sibling.mode == "active"
        assert resumes == ["resume"]
        # FL-IMP-44: 主区恢复链真实执行（采集步已捕获：was_on=True +
        # orig_pct=200/255*100≈78.4）——不得被 suppress 静默吞掉
        assert len(restored) == 1 and restored[0][0] is True
        assert abs(restored[0][1] - 78.4) < 0.5

    asyncio.run(scenario())


def test_calibrate_cancel_honors_pending_off_in_finally():
    """FL-IMP-36②：采集中关灯请求置 pending_off——取消后 finally 兑现关灯
    （跳过亮度恢复、turn_off 恰一次、mode IDLE）。"""
    import asyncio

    from custom_components.fumiha_lighting.lux.const import CONF_CAL_STEP_PCT
    from custom_components.fumiha_lighting.lux.runtime import ZoneRuntime

    restored = []
    calls = []

    async def fake_restore(was_on, orig_pct):
        restored.append((was_on, orig_pct))

    async def hanging_sweep(step):
        await asyncio.sleep(3600)
        return {"up": {}, "down": {}, "baseline": 0.0, "missed_streak": 0}

    async def fake_call_light(action, data):
        calls.append(action)
        return True

    hass = FakeHass()
    hass.async_create_task = asyncio.ensure_future
    hass.states = SimpleNamespace(
        get=lambda entity_id: (
            SimpleNamespace(state="on", attributes={"brightness": 200})
            if entity_id == "light.test"
            else None
        )
    )
    real = ZoneRuntime.__new__(ZoneRuntime)
    real.__dict__.update(
        {
            "hass": hass,
            "opts": {CONF_CAL_STEP_PCT: 5},
            "mode": "idle",
            "zone_name": "卫生间",
            "light_entity": "light.test",
            "lux_entity": "sensor.test",
            "target_lux": 150.0,
            "commanded_pct": None,
            "curve": SimpleNamespace(source="calibrated", max_lux=180.0),
            "cal_last_error": None,
            "cal_progress": "",
            "_cal_task": None,
            "_cal_captured": False,
            "_cal_was_on": False,
            "_cal_pending_off": False,
            "_stopped": False,
            "_apply_lock": asyncio.Lock(),
            "_eval_task": None,
            "_stale_warned": False,
            "lux_stale": False,
            "signal": "fumiha_lighting_lux1_update",
            "notify": lambda: None,
            "_async_sweep": hanging_sweep,
            "_async_restore_light": fake_restore,
            "_async_call_light": fake_call_light,
        }
    )
    hass.data.setdefault(web.DOMAIN, {})["lux"] = {}

    async def scenario():
        assert real.start_calibration() is True
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # 采集中用户关灯（虚拟灯关请求）→ 置 pending_off
        await real.async_turn_off_requested()
        assert real._cal_pending_off is True

        assert await real.async_cancel_calibration() is True

        # finally 侧 pending_off 分支（V1.3.0）：跳过亮度恢复、放手到 IDLE、
        # 不再下发关灯
        assert restored == []
        assert calls == []
        assert real.mode == "idle"
        assert real._cal_pending_off is False

    asyncio.run(scenario())


def test_calibrate_cancel_same_tick_wakes_sibling_via_fallback():
    """FL-IMP-37：同刻取消兜底路径的孪生唤醒块——sibling 被兜底唤醒。

    sweep 桩长挂起 + cancel 早于首拍（task 未起步）→ 走取消兜底（而非
    任务 finally）；兜底唤醒块被删即红。
    """
    import asyncio

    from custom_components.fumiha_lighting.lux.const import CONF_CAL_STEP_PCT
    from custom_components.fumiha_lighting.lux.runtime import ZoneRuntime

    resumes = []

    def _schedule_evaluate(reason):
        resumes.append(reason)

    async def hanging_sweep(step):
        await asyncio.sleep(3600)
        return {"up": {}, "down": {}, "baseline": 0.0, "missed_streak": 0}

    hass = FakeHass()
    hass.async_create_task = asyncio.ensure_future
    hass.states = SimpleNamespace(
        get=lambda entity_id: (
            SimpleNamespace(state="on", attributes={"brightness": 200})
            if entity_id == "light.sib"
            else SimpleNamespace(state="off", attributes={})
        )
    )
    sweeping = ZoneRuntime.__new__(ZoneRuntime)
    sweeping.__dict__.update(
        {
            "hass": hass,
            "opts": {CONF_CAL_STEP_PCT: 5},
            "mode": "idle",
            "entry_id": "lux1",
            "zone_name": "主区",
            "light_entity": "light.main",
            "lux_entity": "sensor.main",
            "target_lux": 150.0,
            "commanded_pct": None,
            "curve": SimpleNamespace(source="calibrated", max_lux=180.0),
            "cal_last_error": None,
            "cal_progress": "",
            "_cal_task": None,
            "_cal_captured": False,
            "_cal_was_on": False,
            "_cal_pending_off": False,
            "_stopped": False,
            "_apply_lock": asyncio.Lock(),
            "_eval_task": None,
            "_stale_warned": False,
            "lux_stale": False,
            "signal": "fumiha_lighting_lux1_update",
            "notify": lambda: None,
            "_async_sweep": hanging_sweep,
        }
    )
    sibling = ZoneRuntime.__new__(ZoneRuntime)
    sibling.__dict__.update(
        {
            "hass": hass,
            "entry_id": "lux2",
            "mode": "active",  # 亮灯闭环中被 start_calibration 挂起
            "zone_name": "副区",
            "light_entity": "light.sib",
            "lux_entity": "sensor.sib",
            "target_lux": 150.0,
            "commanded_pct": None,
            "curve": SimpleNamespace(source="calibrated", max_lux=180.0),
            "cal_last_error": None,
            "cal_progress": "",
            "_cal_task": None,
            "_cal_captured": False,
            "_cal_was_on": False,
            "_cal_pending_off": False,
            "_stopped": False,
            "_apply_lock": asyncio.Lock(),
            "_eval_task": None,
            "_stale_warned": False,
            "lux_stale": False,
            "signal": "fumiha_lighting_lux2_update",
            "notify": lambda: None,
            "_schedule_evaluate": _schedule_evaluate,
        }
    )
    hass.data.setdefault(web.DOMAIN, {})["lux"] = {
        "lux1": sweeping,
        "lux2": sibling,
    }

    async def scenario():
        assert sweeping.start_calibration() is True
        # start_calibration 把 ACTIVE 的 sibling 挂起为 IDLE
        assert sibling.mode == "idle"
        # 同刻取消（cancel 先于任务首拍）→ 走兜底而非任务 finally
        assert await sweeping.async_cancel_calibration() is True

        # 兜底孪生唤醒块：sibling 恢复 ACTIVE + resume 调度
        assert sibling.mode == "active"
        assert resumes == ["resume"]
        # 主区自身：未采集 + 自身灯 off → IDLE
        assert sweeping.mode == "idle"

    asyncio.run(scenario())


def test_start_calibration_suspends_active_sibling_and_guards():
    """FL-IMP-38：start_calibration 双区协调——ACTIVE 副区被挂起；另一区
    校准中时新校准被拒（守卫分支）。"""
    import asyncio

    from custom_components.fumiha_lighting.lux.const import CONF_CAL_STEP_PCT
    from custom_components.fumiha_lighting.lux.runtime import ZoneRuntime

    async def hanging_sweep(step):
        await asyncio.sleep(3600)
        return {"up": {}, "down": {}, "baseline": 0.0, "missed_streak": 0}

    resumes = []

    def _rt(hass, entry_id, mode, light):
        rt = ZoneRuntime.__new__(ZoneRuntime)
        rt.__dict__.update(
            {
                "hass": hass,
                "opts": {CONF_CAL_STEP_PCT: 5},
                "mode": mode,
                "entry_id": entry_id,
                "zone_name": entry_id,
                "light_entity": light,
                "lux_entity": "sensor." + entry_id,
                "target_lux": 150.0,
                "commanded_pct": None,
                "curve": SimpleNamespace(source="calibrated", max_lux=180.0),
                "cal_last_error": None,
                "cal_progress": "",
                "_cal_task": None,
                "_cal_captured": False,
                "_cal_was_on": False,
                "_cal_pending_off": False,
                "_stopped": False,
                "_apply_lock": asyncio.Lock(),
                "_eval_task": None,
                "_stale_warned": False,
                "lux_stale": False,
                "signal": f"fumiha_lighting_{entry_id}_update",
                "notify": lambda: None,
                "_async_sweep": hanging_sweep,
                # FL-IMP-41: 唤醒路径会被收尾顺带触发——桩必须完整，
                # 否则回落真方法读缺失属性、任务以 AttributeError 收场
                "_schedule_evaluate": lambda reason: resumes.append((entry_id, reason)),
            }
        )
        return rt

    hass = FakeHass()
    hass.async_create_task = asyncio.ensure_future
    hass.states = SimpleNamespace(
        get=lambda entity_id: (
            SimpleNamespace(state="on", attributes={"brightness": 200})
            if entity_id in ("light.a", "light.b")
            else None
        )
    )
    a = _rt(hass, "lux1", "idle", "light.a")
    b = _rt(hass, "lux2", "active", "light.b")
    hass.data.setdefault(web.DOMAIN, {})["lux"] = {"lux1": a, "lux2": b}

    async def scenario():
        # b 处于 ACTIVE → a 启动校准把 b 挂起为 IDLE（挂起分支）
        assert a.start_calibration() is True
        assert b.mode == "idle"
        # b（同区）再次启动 → 校准互斥守卫拒绝
        assert b.start_calibration() is False
        assert b.mode == "idle"
        # FL-IMP-41: 走被测取消路径收尾（含兜底：同刻取消任务未起步，
        # 唤醒块在这里触发——绕过它兜底被删也不红，R5 教训）
        assert await a.async_cancel_calibration() is True
        # 兜底孪生唤醒：b（IDLE+curve+灯亮）→ ACTIVE + resume
        assert b.mode == "active"
        assert ("lux2", "resume") in resumes

    asyncio.run(scenario())


def test_calibrate_cancel_sibling_no_light_write_on_fallback():
    """FL-IMP-39：兜底唤醒路径（pending_off=False 分支）主区自身灯零写入，
    且不依赖异常抑制——_async_call_light 用 async 桩（真方法形态）。"""
    import asyncio

    from custom_components.fumiha_lighting.lux.const import CONF_CAL_STEP_PCT
    from custom_components.fumiha_lighting.lux.runtime import ZoneRuntime

    resumes = []
    calls = []

    def _schedule_evaluate(reason):
        resumes.append(reason)

    async def _async_call_light(action, data):
        calls.append(action)
        return True

    async def hanging_sweep(step):
        await asyncio.sleep(3600)
        return {"up": {}, "down": {}, "baseline": 0.0, "missed_streak": 0}

    hass = FakeHass()
    hass.async_create_task = asyncio.ensure_future
    hass.states = SimpleNamespace(
        get=lambda entity_id: (
            SimpleNamespace(state="on", attributes={"brightness": 200})
            if entity_id == "light.sib"
            else SimpleNamespace(state="off", attributes={})
        )
    )
    sweeping = ZoneRuntime.__new__(ZoneRuntime)
    sweeping.__dict__.update(
        {
            "hass": hass,
            "opts": {CONF_CAL_STEP_PCT: 5},
            "mode": "idle",
            "entry_id": "lux1",
            "zone_name": "主区",
            "light_entity": "light.main",
            "lux_entity": "sensor.main",
            "target_lux": 150.0,
            "commanded_pct": None,
            "curve": SimpleNamespace(source="calibrated", max_lux=180.0),
            "cal_last_error": None,
            "cal_progress": "",
            "_cal_task": None,
            "_cal_captured": False,
            "_cal_was_on": False,
            "_cal_pending_off": False,
            "_stopped": False,
            "_apply_lock": asyncio.Lock(),
            "_eval_task": None,
            "_stale_warned": False,
            "lux_stale": False,
            "signal": "fumiha_lighting_lux1_update",
            "notify": lambda: None,
            "_async_sweep": hanging_sweep,
            "_async_call_light": _async_call_light,
        }
    )
    sibling = ZoneRuntime.__new__(ZoneRuntime)
    sibling.__dict__.update(
        {
            "hass": hass,
            "entry_id": "lux2",
            "mode": "active",
            "zone_name": "副区",
            "light_entity": "light.sib",
            "lux_entity": "sensor.sib",
            "target_lux": 150.0,
            "commanded_pct": None,
            "curve": SimpleNamespace(source="calibrated", max_lux=180.0),
            "cal_last_error": None,
            "cal_progress": "",
            "_cal_task": None,
            "_cal_captured": False,
            "_cal_was_on": False,
            "_cal_pending_off": False,
            "_stopped": False,
            "_apply_lock": asyncio.Lock(),
            "_eval_task": None,
            "_stale_warned": False,
            "lux_stale": False,
            "signal": "fumiha_lighting_lux2_update",
            "notify": lambda: None,
            "_schedule_evaluate": _schedule_evaluate,
        }
    )
    hass.data.setdefault(web.DOMAIN, {})["lux"] = {
        "lux1": sweeping,
        "lux2": sibling,
    }

    async def scenario():
        assert sweeping.start_calibration() is True
        assert sibling.mode == "idle"
        assert await sweeping.async_cancel_calibration() is True
        assert sibling.mode == "active"
        assert resumes == ["resume"]
        # 主区自身灯未被触碰（兜底无 turn_off 调用——pending_off 为 False）
        assert calls == []

    asyncio.run(scenario())


def test_calibrate_natural_completion_exits_calibrating():
    """FL-IMP-40：自然完成收尾——sweep 正常返回、曲线落盘、mode 出
    CALIBRATING（finally 的 _finish_calibration 被删即 mode 永锁，红）。"""
    import asyncio

    from custom_components.fumiha_lighting.lux.const import CONF_CAL_STEP_PCT
    from custom_components.fumiha_lighting.lux.runtime import ZoneRuntime

    saved = []
    restored = []

    async def fake_restore(was_on, orig_pct):
        restored.append((was_on, orig_pct))

    sweep_result = {
        "up": {5.0: 15.0, 50.0: 100.0, 100.0: 190.0},
        "down": {50.0: 98.0, 5.0: 14.0},
        "baseline": 2.0,
        "missed_streak": 0,
    }

    class _Store:
        async def async_save_curve(self, curve):
            saved.append(curve)

        async def async_load_curve(self):
            return None

    hass = FakeHass()
    hass.async_create_task = asyncio.ensure_future
    hass.states = SimpleNamespace(
        get=lambda entity_id: (
            SimpleNamespace(state="on", attributes={"brightness": 128})
            if entity_id == "light.test"
            else None
        )
    )
    real = ZoneRuntime.__new__(ZoneRuntime)
    real.__dict__.update(
        {
            "hass": hass,
            "opts": {CONF_CAL_STEP_PCT: 5},
            "mode": "idle",
            "zone_name": "卫生间",
            "light_entity": "light.test",
            "lux_entity": "sensor.test",
            "target_lux": 150.0,
            "commanded_pct": None,
            "curve": None,
            "cal_last_error": None,
            "cal_progress": "",
            "_cal_task": None,
            "_cal_captured": False,
            "_cal_was_on": False,
            "_cal_pending_off": False,
            "_stopped": False,
            "_apply_lock": asyncio.Lock(),
            "_eval_task": None,  # 空挂起（await eval_task 直落），任务直接进 sweep
            "_stale_warned": False,
            "lux_stale": False,
            "signal": "fumiha_lighting_lux1_update",
            "notify": lambda: None,
            "storage": _Store(),
            "_async_sweep": lambda step: _async_return(sweep_result),
            "_async_restore_light": fake_restore,
        }
    )
    hass.data.setdefault(web.DOMAIN, {})["lux"] = {}

    async def scenario():
        assert real.start_calibration() is True
        # 等任务自然完成（sweep 立返 + 存盘落点）
        for _ in range(10):
            await asyncio.sleep(0)
            if real._cal_task.done():
                break
        assert real._cal_task.done()
        # finally 的 _finish_calibration 被删即 mode 卡 calibrating（红）
        assert real.mode == "active"  # 采集时灯亮 + 曲线落盘 → 回 ACTIVE
        assert real.cal_progress == ""
        assert real.cal_last_error is None
        assert saved, "曲线必须落盘"
        # FL-IMP-46: 恢复链可见（captured 分支：was_on=True + orig_pct≈50.2）
        assert len(restored) == 1 and restored[0][0] is True
        assert abs(restored[0][1] - 50.2) < 0.5

    asyncio.run(scenario())


async def _async_return(value):
    return value


def test_idle_never_revives_on_external_light_on():
    """V1.3.0 开关语义钉：IDLE 态真灯被外部打开（夜灯模组/手动）——
    不接管闭环（与夜灯模组共存，互不打架）。"""
    import asyncio

    from custom_components.fumiha_lighting.lux.runtime import ZoneRuntime

    evaluated = []

    hass = FakeHass()
    hass.async_create_task = asyncio.ensure_future
    hass.states = SimpleNamespace(
        get=lambda entity_id: (
            SimpleNamespace(state="on", attributes={"brightness": 200})
            if entity_id == "light.test"
            else None
        )
    )
    rt = ZoneRuntime.__new__(ZoneRuntime)
    rt.__dict__.update(
        {
            "hass": hass,
            "entry_id": "lux1",
            "mode": "idle",
            "zone_name": "卫生间",
            "light_entity": "light.test",
            "lux_entity": "sensor.test",
            "target_lux": 150.0,
            "commanded_pct": None,
            "curve": SimpleNamespace(source="calibrated", max_lux=180.0),
            "cal_last_error": None,
            "cal_progress": "",
            "_cal_task": None,
            "_cal_captured": False,
            "_cal_was_on": False,
            "_cal_pending_off": False,
            "_stopped": False,
            "_apply_lock": asyncio.Lock(),
            "_eval_task": None,
            "_stale_warned": False,
            "lux_stale": False,
            "signal": "fumiha_lighting_lux1_update",
            "notify": lambda: None,
        }
    )
    # 拦截 _schedule_evaluate：IDLE 态下外部开灯不得触发任何评估
    rt._schedule_evaluate = lambda trigger: evaluated.append(trigger)

    event = SimpleNamespace(
        data={"new_state": SimpleNamespace(state="on", attributes={})}
    )
    rt._on_light_event(event)
    assert rt.mode == "idle", "IDLE 态外部开灯必须保持放手（V1.3.0）"
    assert evaluated == []  # 无任何评估被调度


def test_turn_off_requested_leaves_real_light_alone():
    """V1.3.0 开关语义钉：关映射实体 = 纯放手——不下发关灯命令。"""
    import asyncio

    from custom_components.fumiha_lighting.lux.runtime import ZoneRuntime

    calls = []

    async def fake_call_light(action, data):
        calls.append(action)
        return True

    hass = FakeHass()
    rt = ZoneRuntime.__new__(ZoneRuntime)
    rt.__dict__.update(
        {
            "hass": hass,
            "entry_id": "lux1",
            "mode": "active",
            "zone_name": "卫生间",
            "light_entity": "light.test",
            "lux_entity": "sensor.test",
            "target_lux": 150.0,
            "commanded_pct": 45.0,
            "curve": SimpleNamespace(source="calibrated", max_lux=180.0),
            "cal_last_error": None,
            "cal_progress": "",
            "_cal_task": None,
            "_cal_captured": False,
            "_cal_was_on": False,
            "_cal_pending_off": False,
            "_stopped": False,
            "_apply_lock": asyncio.Lock(),
            "_eval_task": None,
            "_stale_warned": False,
            "lux_stale": False,
            "signal": "fumiha_lighting_lux1_update",
            "notify": lambda: None,
            "_async_call_light": fake_call_light,
        }
    )

    async def scenario():
        await rt.async_turn_off_requested()
        # 放手：mode 回 IDLE，但原灯保持当前状态（不下发关灯）
        assert rt.mode == "idle"
        assert calls == []
        assert rt.commanded_pct is None

    asyncio.run(scenario())
