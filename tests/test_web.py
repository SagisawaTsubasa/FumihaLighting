"""Merged web-layer tests: two page REST surfaces, dual descriptors,
REST write endpoints and the kind-discriminating config flow."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from datetime import time as _time
from types import SimpleNamespace

import conftest as stubs
import pytest
from conftest import flush_deferred_callbacks as _flush_deferred

from custom_components.fumiha_lighting import web
from custom_components.fumiha_lighting.anl.const import CONF_DAY_ENABLED
from custom_components.fumiha_lighting.config_flow import (
    FumihaLightingConfigFlow,
)
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
        self.calls = []

    async def async_call(self, domain, service, data, blocking=False):
        # F-057：web/删条目的归还路径走 services.async_call——桩记录供断言
        self.calls.append((domain, service, dict(data)))

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
        self.config_entries = SimpleNamespace(
            async_update_entry=lambda *a, **k: None,
            # F-067：跨条目引用检查读 config_entries.async_entries——桩缺省空表
            async_entries=lambda domain: [],
        )


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
        transition_enabled=True,  # 存储派生值（end_transition>0）
        transitions_active=False,  # 生效总闸：day 关 → manager 归零（真实分叉）
        day_enabled=False,
        day_ci_lights=[],  # V1.6.0 单灯化：接管灯清单（组级守卫=非空）
        respect_manual=True,
    )
    # F-061：序列化读 ci_configured()——默认按真实语义镜像生效判据
    # F-065 + V1.6.0 + V1.7.0：日间模式开 + 清单非空（虚拟灯键废除，
    # 逐灯有效性由 ci_zone_name 表达）
    mgr.ci_configured = lambda: bool(mgr.day_enabled and mgr.day_ci_lights)
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
    # 双桶都 setdefault（seed_light_zone 先建 lux 桶时 anl 桶不缺）
    buckets = hass.data.setdefault(web.DOMAIN, {})
    buckets.setdefault("lux", {})
    buckets.setdefault("anl", {})
    for rt in runtimes:
        buckets["lux"][rt.entry_id] = rt
    return buckets


def _anl_bucket(hass, *managers):
    buckets = hass.data.setdefault(web.DOMAIN, {})
    buckets.setdefault("lux", {})
    buckets.setdefault("anl", {})
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


def test_lux_overview_link_group_fields():
    """V1.3.0 联动组：组标签+同组成员名单随载荷下发；未设组两键 None。

    成员在 web 层按公开属性 link_group 自算（ANL-F-003 教训：不直调
    runtime 私有方法），页面 chip 据此渲染组标签+名单。
    """
    hass = FakeHass()
    wet = fake_lux_runtime("lux1", "湿区")
    dry = fake_lux_runtime("lux2", "干区")
    wet.link_group = "卫"
    dry.link_group = "卫"
    solo = fake_lux_runtime("lux3", "书房")
    _lux_bucket(hass, wet, dry, solo)
    resp = run(web.LuxOverviewView().get(FakeRequest(hass)))
    payload = json.loads(json.dumps(resp.payload, allow_nan=False))
    zones = {z["zone_name"]: z for z in payload["zones"]}
    assert zones["湿区"]["link_group"] == "卫"
    assert zones["湿区"]["link_peers"] == ["干区"]
    assert zones["干区"]["link_peers"] == ["湿区"]
    assert zones["书房"]["link_group"] is None
    assert zones["书房"]["link_peers"] is None


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


def test_target_write_rejects_bool_and_overflow():
    """ANL-F-156：LuxTargetView 数值口径统一走 _finite_number——bool 是
    int 子类（float(True)=1.0 旧码可混过范围门）、超大整数 float() 抛
    OverflowError 不在旧 except 内 → 500；统一后均须 400 且零写入。"""
    hass = FakeHass()
    rt = fake_lux_runtime("lux1")
    rt.set_target = lambda v: (_ for _ in ()).throw(AssertionError("must not write"))
    _lux_bucket(hass, rt)
    for bad in (True, 10**400):
        req = FakeRequest(hass)
        req.json = _json_body({"entry_id": "lux1", "value": bad})
        resp = run(web.LuxTargetView().post(req))
        assert resp.status == 400, bad


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
# lux link group editor (V1.7.1 GUI 编辑入口，anl/config 先例)
# ---------------------------------------------------------------------------


def test_link_group_get_returns_values_and_members():
    """GET 下发三键现值（merged_options 回填默认）+ link_members 完整组员
    名单（含本区、排序稳定）；未设组两键空。名单只含已加载区——与
    overview chip 的 link_peers（排除自身）键名分工不同。"""
    hass = FakeHass()
    wet = fake_lux_runtime("lux1", "湿区")
    wet.link_group = "卫"
    wet.entry = _config_entry(
        "lux1", data={"kind": "lux"}, options={"link_group": "卫"}
    )
    dry = fake_lux_runtime("lux2", "干区")
    dry.link_group = "卫"
    dry.entry = _config_entry(
        "lux2",
        data={"kind": "lux"},
        options={"link_group": "卫", "cross_fade": 0.8, "max_step_pct": 30},
    )
    # solo 不带 link_group 属性——getattr 兜底容忍（缺属性按未设组）
    solo = fake_lux_runtime("lux3", "书房")
    solo.entry = _config_entry("lux3", data={"kind": "lux"})
    _lux_bucket(hass, wet, dry, solo)
    req = FakeRequest(hass)
    req.query = {"entry_id": "lux1"}
    resp = run(web.LuxLinkGroupView().get(req))
    payload = json.loads(json.dumps(resp.payload, allow_nan=False))
    assert payload["ok"] is True
    assert payload["zone_name"] == "湿区"
    assert payload["link_group"] == "卫"
    # 缺省键由 merged_options 回填默认（单一来源）
    assert payload["cross_fade"] == 0.5
    assert payload["max_step_pct"] == 20.0
    # 完整组员名单：含本区、排序稳定（overview 的 link_peers 排除本区）
    assert payload["link_members"] == ["干区", "湿区"]
    req2 = FakeRequest(hass)
    req2.query = {"entry_id": "lux3"}
    payload2 = run(web.LuxLinkGroupView().get(req2)).payload
    assert payload2["link_group"] is None
    assert payload2["link_members"] is None


def test_link_group_get_unknown_entry():
    hass = FakeHass()
    req = FakeRequest(hass)
    req.query = {"entry_id": "nope"}
    assert run(web.LuxLinkGroupView().get(req)).status == 404


def test_link_group_post_writes_mirror_options_and_strips():
    """POST 合法：三键落 options、镜像保留其余 options 键（options 流是
    全镜像写，漏带会把扫描间隔等静默打回默认）、link_group strip 归一。"""
    hass = _recording_hass()
    entry = _config_entry(
        "lux1",
        data={"kind": "lux"},
        options={"scan_interval_s": 30, "link_group": "旧组"},
    )
    rt = fake_lux_runtime("lux1")
    rt.entry = entry
    _lux_bucket(hass, rt)
    resp = run(
        web.LuxLinkGroupView().post(
            FakeRequest(
                hass,
                {
                    "entry_id": "lux1",
                    "link_group": " 卫生间 ",
                    "cross_fade": 0.8,
                    "max_step_pct": 30,
                },
            )
        )
    )
    assert resp.payload["ok"] is True
    assert resp.payload["link_group"] == "卫生间"
    # ANL-F-150：响应带新组 link_peers——桶内无同组对等区 → None
    assert resp.payload["link_peers"] is None
    options = recorded[0][1]["options"]
    # strip 归一（lux/flow.py 写入侧同语义：尾空格静默不同组的根治）
    assert options["link_group"] == "卫生间"
    assert options["cross_fade"] == 0.8
    assert options["max_step_pct"] == 30
    assert options["scan_interval_s"] == 30  # 镜像保留（只替换三键）


def test_link_group_post_returns_new_group_peers():
    """ANL-F-150：响应 link_peers 按新 group 现查桶内其他区（排除自身）
    ——本 entry 的 runtime 属性此刻仍是旧值（reload 未发生），不得用它。"""
    hass = _recording_hass()
    entry = _config_entry("lux1", data={"kind": "lux"}, options={"link_group": "旧组"})
    rt = fake_lux_runtime("lux1", "湿区")
    rt.entry = entry
    rt.link_group = "旧组"
    dry = fake_lux_runtime("lux2", "干区")
    dry.entry = _config_entry(
        "lux2", data={"kind": "lux"}, options={"link_group": "新组"}
    )
    dry.link_group = "新组"
    _lux_bucket(hass, rt, dry)
    resp = run(
        web.LuxLinkGroupView().post(
            FakeRequest(
                hass,
                {"entry_id": "lux1", "link_group": "新组",
                 "cross_fade": 0.5, "max_step_pct": 20},
            )
        )
    )
    assert resp.payload["ok"] is True and resp.payload["link_group"] == "新组"
    # 干区已在「新组」→ 成为对等区；排除自身（湿区自己不在列）
    assert resp.payload["link_peers"] == ["干区"]


def test_link_group_post_empty_group_disables():
    """空串=停用（flow 同语义）：落盘空串、响应报 None。"""
    hass = _recording_hass()
    entry = _config_entry("lux1", data={"kind": "lux"}, options={"link_group": "卫"})
    rt = fake_lux_runtime("lux1")
    rt.entry = entry
    rt.link_group = "卫"
    _lux_bucket(hass, rt)
    resp = run(
        web.LuxLinkGroupView().post(
            FakeRequest(
                hass,
                {"entry_id": "lux1", "link_group": "",
                 "cross_fade": 0.5, "max_step_pct": 20},
            )
        )
    )
    assert resp.payload["ok"] is True and resp.payload["link_group"] is None
    assert recorded[0][1]["options"]["link_group"] == ""


def test_link_group_post_rejects_invalid_values():
    """非法值 400 且零写入：范围与 options flow selector 同源（0.1~1.0 /
    5~50）/ 非数字 / 非字符串标签 / 非对象 body（ANL-F-020 同款防线）。"""
    hass = _recording_hass()
    entry = _config_entry("lux1", data={"kind": "lux"})
    rt = fake_lux_runtime("lux1")
    rt.entry = entry
    _lux_bucket(hass, rt)
    base = {
        "entry_id": "lux1",
        "link_group": "卫",
        "cross_fade": 0.5,
        "max_step_pct": 20,
    }
    for bad in (
        {**base, "cross_fade": 0.05},
        {**base, "cross_fade": 1.5},
        {**base, "cross_fade": float("nan")},
        {**base, "cross_fade": "abc"},
        {**base, "cross_fade": None},
        # ANL-F-153：bool 是 int 子类（float(True)=1.0 可混过范围门）；
        # 超大整数 float() 抛 OverflowError——均须 400 而非 500/混过
        {**base, "cross_fade": True},
        {**base, "cross_fade": 10**400},
        {**base, "max_step_pct": 4},
        {**base, "max_step_pct": 55},
        {**base, "max_step_pct": float("inf")},
        {**base, "max_step_pct": None},
        {**base, "link_group": 42},
        # ANL-F-158：缺 link_group 键 ≠ 空串——静默停用是「键 miss 静默降级」，
        # 须 400（留空停用请显式传空串，test_link_group_post_empty_group_disables）
        {"entry_id": "lux1", "cross_fade": 0.5, "max_step_pct": 20},
    ):
        resp = run(web.LuxLinkGroupView().post(FakeRequest(hass, bad)))
        assert resp.status == 400, bad
    for bad_body in ([], None, "x"):
        resp = run(web.LuxLinkGroupView().post(FakeRequest(hass, bad_body)))
        assert resp.status == 400, bad_body
    resp = run(  # entry_id 非字符串 → 400（unhashable 防线）
        web.LuxLinkGroupView().post(
            FakeRequest(hass, {**base, "entry_id": ["x"]})
        )
    )
    assert resp.status == 400
    assert recorded == []  # 校验失败绝不写条目


def test_link_group_post_unknown_entry():
    hass = FakeHass()
    resp = run(
        web.LuxLinkGroupView().post(
            FakeRequest(
                hass,
                {"entry_id": "nope", "link_group": "卫",
                 "cross_fade": 0.5, "max_step_pct": 20},
            )
        )
    )
    assert resp.status == 404


# ---------------------------------------------------------------------------
# anl snapshot + trigger
# ---------------------------------------------------------------------------


def test_anl_snapshot_success():
    hass = FakeHass()
    _anl_bucket(hass, fake_anl_manager())
    resp = run(web.AnlSnapshotView().get(FakeRequest(hass)))
    m = resp.payload["managers"][0]
    assert m["current_mode"] == "night"
    # ANL-F-041：两字段必须分叉下发（chip 判据的唯一守钉）
    assert m["transitions_active"] is False
    assert m["transition_enabled"] is True
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
# anl config editor (GET/POST /api/fumiha_lighting/anl/config)
# ---------------------------------------------------------------------------


def _config_entry(entry_id="anl1", data=None, options=None):
    # disabled_by/state：F-093 引用判据读这两个真机 ConfigEntry 恒有的
    # 属性——桩补默认形态（未禁用、已加载），保桩忠实度
    return SimpleNamespace(
        entry_id=entry_id,
        title="自动夜灯",
        data=data if data is not None else {},
        options=options if options is not None else {},
        disabled_by=None,
        state=stubs.ConfigEntryState.LOADED,
    )


def _config_manager(entry):
    mgr = fake_anl_manager(entry.entry_id)
    mgr.entry = entry
    return mgr


recorded = []


def _recording_hass():
    recorded.clear()
    hass = FakeHass()

    def _update(entry, **kw):
        # 真实写回（options），连续 POST 才能看到前次结果（FL-F-011 同款教训：
        # 只记录不写回的桩会让顺序依赖用例失真）
        if "options" in kw:
            entry.options = kw["options"]
        recorded.append((entry, kw))
        return True

    hass.config_entries = SimpleNamespace(
        async_update_entry=_update,
        # F-067：跨条目引用检查读 config_entries.async_entries——桩缺省空表
        async_entries=lambda domain: [],
    )
    return hass


def test_anl_config_get_success():
    hass = FakeHass()
    entry = _config_entry(
        data={"kind": "anl", "lights": ["light.a"], "brightness": 30},
        options={"tolerance_kelvin": 200},
    )
    _anl_bucket(hass, _config_manager(entry))
    req = FakeRequest(hass)
    req.query = {"entry_id": "anl1"}
    resp = run(web.AnlConfigView().get(req))
    assert resp.payload["ok"] is True
    cfg = resp.payload["config"]
    assert cfg["brightness"] == 30
    assert cfg["tolerance_kelvin"] == 200  # options 覆盖 data
    assert "limits" in resp.payload and "params" in resp.payload["limits"]
    assert "kind" not in cfg  # 白名单外键不外泄


def test_anl_config_get_unknown_entry():
    hass = FakeHass()
    req = FakeRequest(hass)
    req.query = {"entry_id": "nope"}
    resp = run(web.AnlConfigView().get(req))
    assert resp.status == 404


def test_anl_config_post_times_derives_transition_enabled():
    hass = _recording_hass()
    entry = _config_entry(
        data={"kind": "anl", "lights": ["light.a"], "transition_enabled": False}
    )
    _anl_bucket(hass, _config_manager(entry))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"times": {"start_transition": 30}}})
        )
    )
    assert resp.payload["ok"] is True
    options = recorded[0][1]["options"]
    assert options["start_transition"] == 30
    assert options["transition_enabled"] is True


def test_anl_config_post_params_and_overrides_merge():
    hass = _recording_hass()
    entry = _config_entry(
        data={
            "kind": "anl",
            "lights": ["light.a", "light.b"],
            "extras": [{"name": "", "start": "18:00:00", "brightness": 60,
                        "color_temp_kelvin": 3000, "transition": 0}],
            "extra_count": 1,
            # 死键见证（ANL-F-044）：三个键都在 data 里出现，断言才有变异力
            "extra_enabled": True,
            "custom_per_light": ["light.b"],
        }
    )
    _anl_bucket(hass, _config_manager(entry))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {
                "entry_id": "anl1",
                "patch": {
                    "params": {"brightness": 15},
                    "overrides": {"light.b": {
                        "brightness": 10,
                        "color_temp_kelvin": 2700,
                        "extras": {"0": {"brightness": 40,
                                         "color_temp_kelvin": 2800}},
                    }},
                },
            })
        )
    )
    assert resp.payload["ok"] is True
    options = recorded[0][1]["options"]
    assert options["brightness"] == 15
    assert options["overrides"]["light.b"]["extras"]["0"]["brightness"] == 40
    assert "light.a" not in options.get("overrides", {})
    # 死键不向 options 固化（ANL-F-029 钉）：entry.data 带 extra_count:1，
    # 过滤被回滚时本行即红
    for dead in ("extra_count", "extra_enabled", "custom_per_light"):
        assert dead not in options


def test_anl_config_post_overrides_remove_restores_global():
    hass = _recording_hass()
    entry = _config_entry(
        data={"kind": "anl", "lights": ["light.a", "light.b"],
              "overrides": {"light.a": {"brightness": 5, "color_temp_kelvin": 2200}}},
    )
    _anl_bucket(hass, _config_manager(entry))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"overrides_remove": ["light.a"]}})
        )
    )
    assert resp.payload["ok"] is True
    assert recorded[0][1]["options"]["overrides"] == {}


def test_anl_config_post_rejects_out_of_range():
    hass = _recording_hass()
    entry = _config_entry(data={"kind": "anl", "lights": ["light.a"]})
    _anl_bucket(hass, _config_manager(entry))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {"brightness": 0}}})
        )
    )
    assert resp.status == 400 and "1" in resp.payload["error"]
    assert recorded == []  # 校验失败绝不写条目


def test_anl_config_post_rejects_unknown_light_override():
    hass = _recording_hass()
    entry = _config_entry(data={"kind": "anl", "lights": ["light.a"]})
    _anl_bucket(hass, _config_manager(entry))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"overrides": {"light.zzz": {
                                   "brightness": 10, "color_temp_kelvin": 2700}}}})
        )
    )
    assert resp.status == 400 and "light.zzz" in resp.payload["error"]


def test_anl_config_post_rejects_too_many_extras():
    hass = _recording_hass()
    entry = _config_entry(data={"kind": "anl", "lights": ["light.a"]})
    _anl_bucket(hass, _config_manager(entry))
    items = [{"name": "", "start": "18:00", "brightness": 60,
              "color_temp_kelvin": 3000, "transition": 0}] * 6
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1", "patch": {"extras": items}})
        )
    )
    assert resp.status == 400


def test_anl_config_post_rejects_unknown_section():
    hass = FakeHass()
    entry = _config_entry(data={"kind": "anl", "lights": ["light.a"]})
    _anl_bucket(hass, _config_manager(entry))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"hax": {"brightness": 1}}})
        )
    )
    assert resp.status == 400 and "hax" in resp.payload["error"]


def test_anl_config_post_unknown_entry():
    hass = FakeHass()
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {
                "entry_id": "nope",
                "patch": {"params": {"brightness": 10}},
            })
        )
    )
    assert resp.status == 404


def test_anl_config_post_rejects_nan():
    """ANL-F-019：NaN/Inf 不得 500 或落库。"""
    hass = _recording_hass()
    entry = _config_entry(data={"kind": "anl", "lights": ["light.a"]})
    _anl_bucket(hass, _config_manager(entry))
    for bad in (float("nan"), float("inf"), float("-inf")):
        resp = run(
            web.AnlConfigView().post(
                FakeRequest(hass, {"entry_id": "anl1",
                                   "patch": {"params": {"brightness": bad}}})
            )
        )
        assert resp.status == 400 and "有限" in resp.payload["error"]
    assert recorded == []


def test_anl_config_post_rejects_non_object_body():
    """ANL-F-020：null/list/标量 body → 400 而非 500。"""
    hass = FakeHass()
    entry = _config_entry(data={"kind": "anl", "lights": ["light.a"]})
    _anl_bucket(hass, _config_manager(entry))
    for bad_body in ([], None, "x"):
        resp = run(
            web.AnlConfigView().post(FakeRequest(hass, bad_body))
        )
        assert resp.status == 400, bad_body
    # entry_id 非字符串 → 400（unhashable 防线）
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": ["anl1"],
                               "patch": {"params": {"brightness": 10}}})
        )
    )
    assert resp.status == 400


def test_anl_config_get_serializes_time_objects():
    """ANL-F-015：TimeSelector 提交后进程内是 time 对象，GET 必须字符串化
    （json_response 桩走真 dumps，漏转换即 TypeError）。"""
    hass = FakeHass()
    entry = _config_entry(
        data={"kind": "anl", "lights": ["light.a"],
              "trigger_time": _time(22, 0), "end_time": _time(6, 30),
              "extras": [{"name": "", "start": _time(18, 0), "brightness": 60,
                          "color_temp_kelvin": 3000, "transition": 0}]},
    )
    _anl_bucket(hass, _config_manager(entry))
    req = FakeRequest(hass)
    req.query = {"entry_id": "anl1"}
    resp = run(web.AnlConfigView().get(req))
    cfg = resp.payload["config"]
    assert cfg["trigger_time"] == "22:00:00"
    assert cfg["end_time"] == "06:30:00"
    assert cfg["extras"][0]["start"] == "18:00:00"


def test_anl_config_post_extras_shrink_prunes_override_extras():
    """ANL-F-017/028：extras 覆盖键是位置序号，按 start 身份判漂移——
    被删时段原序号被后继者顶替时精确清除该序号覆盖（等数量的删+加同样
    命中）；身份未变（尾部删除/原位编辑）保留覆盖。"""
    hass = _recording_hass()
    entry = _config_entry(
        data={"kind": "anl", "lights": ["light.a"],
              "extras": [
                  {"name": "a", "start": "18:00:00", "brightness": 60,
                   "color_temp_kelvin": 3000, "transition": 0},
                  {"name": "b", "start": "20:00:00", "brightness": 60,
                   "color_temp_kelvin": 3000, "transition": 0},
              ],
              "overrides": {"light.a": {
                  "brightness": 10, "color_temp_kelvin": 2700,
                  "extras": {"0": {"brightness": 40, "color_temp_kelvin": 2800}}}}},
    )
    _anl_bucket(hass, _config_manager(entry))
    # 删头部（18:00 消失，20:00 顶到序号 0）→ 序号 0 身份漂移 → 清除覆盖
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {
                "entry_id": "anl1",
                "patch": {"extras": [{"name": "b", "start": "20:00", "brightness": 60,
                                      "color_temp_kelvin": 3000, "transition": 0}]},
            })
        )
    )
    assert resp.payload["pruned_override_extras"] == 1
    options = recorded[0][1]["options"]
    assert "extras" not in options["overrides"]["light.a"]
    assert options["overrides"]["light.a"]["brightness"] == 10  # 夜间覆盖保留
    # 尾部删除（18:00 身份仍在序号 0）→ 覆盖保留（ANL-F-028 精准语义）
    resp2 = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {
                "entry_id": "anl1",
                "patch": {"extras": [{"name": "b", "start": "20:00", "brightness": 60,
                                      "color_temp_kelvin": 3000, "transition": 0},
                                     {"name": "c", "start": "21:00", "brightness": 60,
                                      "color_temp_kelvin": 3000, "transition": 0}]},
            })
        )
    )
    assert resp2.payload["pruned_override_extras"] == 0
    # 等数量换 start（删一条+加一条的合成路径）→ 身份漂移 → 清除
    resp3 = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {
                "entry_id": "anl1",
                "patch": {"overrides": {"light.a": {
                    "brightness": 10, "color_temp_kelvin": 2700,
                    "extras": {"0": {"brightness": 40, "color_temp_kelvin": 2800}}}}},
            })
        )
    )
    assert resp3.payload["ok"] is True
    resp4 = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {
                "entry_id": "anl1",
                "patch": {"extras": [{"name": "x", "start": "22:00", "brightness": 60,
                                      "color_temp_kelvin": 3000, "transition": 0},
                                     {"name": "y", "start": "21:00", "brightness": 60,
                                      "color_temp_kelvin": 3000, "transition": 0}]},
            })
        )
    )
    assert resp4.payload["pruned_override_extras"] == 1
    assert "extras" not in recorded[-1][1]["options"]["overrides"]["light.a"]
    # 原位参数编辑（start 不变）→ 覆盖保留
    run(
        web.AnlConfigView().post(
            FakeRequest(hass, {
                "entry_id": "anl1",
                "patch": {"overrides": {"light.a": {
                    "brightness": 10, "color_temp_kelvin": 2700,
                    "extras": {"0": {"brightness": 40, "color_temp_kelvin": 2800}}}}},
            })
        )
    )
    resp6 = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {
                "entry_id": "anl1",
                "patch": {"extras": [{"name": "x", "start": "22:00", "brightness": 70,
                                      "color_temp_kelvin": 3000, "transition": 0},
                                     {"name": "y", "start": "21:00", "brightness": 60,
                                      "color_temp_kelvin": 3000, "transition": 0}]},
            })
        )
    )
    assert resp6.payload["pruned_override_extras"] == 0
    assert recorded[-1][1]["options"]["overrides"]["light.a"]["extras"]["0"][
        "brightness"] == 40


def test_anl_config_post_extras_drift_drops_empty_override_shell():
    """ANL-F-049：只带时段覆盖的灯 + 漂移 → 逐灯空壳必须消失
    （overrides 表本身保留 {}，用于压掉 entry.data 的旧值——页面据此
    判「跟随全局」，留 {"light.a": {}} 会误判「未跟随全局」）。"""
    hass = _recording_hass()
    entry = _config_entry(
        data={"kind": "anl", "lights": ["light.a"],
              "extras": [{"name": "a", "start": "18:00:00", "brightness": 60,
                          "color_temp_kelvin": 3000, "transition": 0}],
              "overrides": {"light.a": {
                  "extras": {"0": {"brightness": 40, "color_temp_kelvin": 2800}}}}},
    )
    _anl_bucket(hass, _config_manager(entry))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {
                "entry_id": "anl1",
                "patch": {"extras": [{"name": "x", "start": "20:00", "brightness": 60,
                                      "color_temp_kelvin": 3000, "transition": 0}]},
            })
        )
    )
    assert resp.payload["pruned_override_extras"] == 1
    # 逐灯空壳必须消失（ANL-F-049）；overrides 表本身刻意保留 {}——
    # 压掉 entry.data 的旧值（删键会让 data 旧覆盖复活）
    assert "light.a" not in recorded[0][1]["options"]["overrides"]


def test_anl_config_post_failed_section_leaves_options_untouched():
    """ANL-F-026：组合 patch 后段校验 400 时，已执行的前段 prune 不得污染
    entry.options 的嵌套对象（深拷贝防线；recorded==[] 只能看见顶层写入）。"""
    hass = _recording_hass()
    entry = _config_entry(
        data={"kind": "anl", "lights": ["light.a"],
              "extras": [
                  {"name": "a", "start": "18:00:00", "brightness": 60,
                   "color_temp_kelvin": 3000, "transition": 0},
                  {"name": "b", "start": "20:00:00", "brightness": 60,
                   "color_temp_kelvin": 3000, "transition": 0},
              ],
              "overrides": {"light.a": {
                  "brightness": 10, "color_temp_kelvin": 2700,
                  "extras": {"0": {"brightness": 40, "color_temp_kelvin": 2800}}}}},
    )
    _anl_bucket(hass, _config_manager(entry))
    # extras 换 start（会触发 prune）+ overrides 引用未知灯（400）
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {
                "entry_id": "anl1",
                "patch": {
                    "extras": [{"name": "x", "start": "22:00", "brightness": 60,
                                "color_temp_kelvin": 3000, "transition": 0},
                               {"name": "y", "start": "20:00", "brightness": 60,
                                "color_temp_kelvin": 3000, "transition": 0}],
                    "overrides": {"light.zzz": {
                        "brightness": 1, "color_temp_kelvin": 2700}},
                },
            })
        )
    )
    assert resp.status == 400
    assert recorded == []  # 顶层无写入
    # 嵌套对象未被就地改（旧条目 data 的 overrides extras 子表原样）
    assert entry.data["overrides"]["light.a"]["extras"] == {
        "0": {"brightness": 40, "color_temp_kelvin": 2800}
    }
    # 请求体里的旧列表对象也未被改（extras 是顶层替换，主要防的是 overrides）
    assert entry.data["extras"][0]["start"] == "18:00:00"


def test_flow_step_ids_all_have_handlers():
    """ANL-F-030 元门禁：每个 async_show_form 的 step_id 必须有同名
    async_step_<id> 承接（cur_step 跟随表单 step_id 派发——F-014 类 P0
    靠逐链手写驱动测不出来，此钉锁死整类）。"""
    from custom_components.fumiha_lighting.anl.flow import AnlGroupOptionsFlow
    from custom_components.fumiha_lighting.lux.flow import LuxZoneOptionsFlow

    checks = []

    def scan(flow, steps):
        for step_id, user_input in steps:
            result = run(getattr(flow, f"async_step_{step_id}")(user_input))
            if result["type"] == "form":
                sid = result["step_id"]
                assert hasattr(type(flow), f"async_step_{sid}"), (
                    f"{type(flow).__name__} 表单 step_id={sid} 无承接方法"
                )
                checks.append((type(flow).__name__, sid))

    times_input = {
        "start_mode": "fixed", "end_mode": "fixed",
        "trigger_time": "22:00", "end_time": "06:00",
        "start_offset": 0, "end_offset": 0, "sun_entity": "sun.sun",
        "start_transition": 0, "end_transition": 0, "transition_interval": 5,
    }
    anl_cfg = FumihaLightingConfigFlow()
    anl_cfg.hass = SimpleNamespace(states=SimpleNamespace(get=lambda eid: None))
    anl_cfg._init_state()
    scan(anl_cfg, [("anl_user", None), ("anl_user", {"lights": ["light.a"]}),
                   ("time_details", times_input), ("settings", None)])

    anl_opts = AnlGroupOptionsFlow()
    anl_opts.config_entry = SimpleNamespace(
        data={"kind": "anl", "lights": ["light.a"]}, options={})
    scan(anl_opts, [("init", None), ("anl_init", times_input),
                    ("settings", None)])

    lux_cfg = FumihaLightingConfigFlow()
    lux_cfg.hass = SimpleNamespace(states=SimpleNamespace(get=lambda eid: None))
    lux_cfg._init_state()
    scan(lux_cfg, [("user", None), ("user", {CONF_KIND: KIND_LUX})])

    lux_opts = LuxZoneOptionsFlow()
    lux_opts.config_entry = SimpleNamespace(options={})
    scan(lux_opts, [("init", None), ("lux_init", {
        "scan_interval_s": 45, "deadband_pct": 5, "deadband_lux": 10,
        "min_brightness_pct": 5, "auto_turn_off": False})])

    assert len(checks) >= 6  # anl 4 + lux 2 的 form 面都被驱动过


def test_anl_reconfigure_uid_conflict_aborts():
    """ANL-F-030：灯集变化且与他人冲突 → abort reason=anl_unique_id_mismatch
    （桩改为记录 + 可注入冲突，error= 关键字写错即红）。"""
    flow = _make_flow()
    flow.hass = SimpleNamespace(states=SimpleNamespace(get=lambda eid: None))
    flow.source = "reconfigure"
    entry = SimpleNamespace(
        data={CONF_KIND: "anl", "lights": ["light.old"], "brightness": 25},
        options={}, unique_id="anl:light.old",
    )
    flow._reconfigure_entry = entry
    flow._conflict_uid = True  # 注入冲突（桩语义：unique_id 已被占用）
    run(flow.async_step_reconfigure(None))
    run(flow.async_step_anl_reconfigure({"lights": ["light.new"]}))
    from conftest import AbortFlowStub

    with pytest.raises(AbortFlowStub) as exc_info:
        run(flow.async_step_time_details({
            "start_mode": "fixed", "end_mode": "fixed",
            "trigger_time": "22:00", "end_time": "06:00",
            "start_offset": 0, "end_offset": 0, "sun_entity": "sun.sun",
            "start_transition": 0, "end_transition": 0, "transition_interval": 5,
        }))
        run(flow.async_step_settings({
            "day_enabled": False, "brightness": 25, "color_temp_kelvin": 2200,
            "day_brightness": 100, "day_color_temp_kelvin": 4000,
            "tolerance_brightness": 4, "tolerance_kelvin": 150,
            "verify_delay": 5, "settle_delay": 1,
            "turn_on_listen": True, "only_when_on": False, "respect_manual": True,
        }))
    assert exc_info.value.reason == "anl_unique_id_mismatch"
    assert flow._abort_calls == ["anl_unique_id_mismatch"]


def test_anl_options_flow_two_step_chain():
    """options 流两步：init（时间锚点）→ settings → create_entry；
    灯具不可改（unique_id 绑定灯集），GUI 管理键全镜像携带（ANL-F-014/016）。"""
    from custom_components.fumiha_lighting.anl.flow import AnlGroupOptionsFlow

    opts = AnlGroupOptionsFlow()
    opts.config_entry = SimpleNamespace(
        entry_id="anl1",
        data={"kind": "anl", "lights": ["light.a"], "brightness": 25},
        options={"extras": [{"name": "晚", "start": "18:00:00", "brightness": 60,
                             "color_temp_kelvin": 3000, "transition": 0}],
                 "extra_count": 1, "overrides": {}},
    )
    first = run(opts.async_step_init(None))
    assert first["type"] == "form" and first["step_id"] == "anl_init"
    second = run(opts.async_step_anl_init({
        "start_mode": "fixed", "end_mode": "fixed",
        "trigger_time": "21:30", "end_time": "06:00",
        "start_offset": 0, "end_offset": 0, "sun_entity": "sun.sun",
        "start_transition": 0, "end_transition": 0, "transition_interval": 5,
    }))
    assert second["step_id"] == "settings"
    final = run(opts.async_step_settings({
        "day_enabled": False, "brightness": 20, "color_temp_kelvin": 2200,
        "day_brightness": 100, "day_color_temp_kelvin": 4000,
        "tolerance_brightness": 4, "tolerance_kelvin": 150,
        "verify_delay": 5, "settle_delay": 1,
        "turn_on_listen": True, "only_when_on": False, "respect_manual": True,
    }))
    assert final["type"] == "create_entry"
    data = final["data"]
    assert data["trigger_time"] == "21:30:00"  # 边界归一化，存储形状稳定
    assert data["brightness"] == 20
    assert data["extras"][0]["name"] == "晚"  # GUI 管理键镜像
    # options 流没有灯具步：不写 lights 键（防空列表镜像覆盖 data 的灯表）
    assert "lights" not in data
    # 灯具步已从 options 流移除：no_lights 错误分支结构上不可达
    assert not hasattr(type(opts), "async_step_lights")


def test_anl_reconfigure_light_change_updates_unique_id():
    """ANL-F-016：reconfigure 改灯集必须同步 unique_id（lux 同款三步）。"""
    flow = _make_flow()
    flow.hass = SimpleNamespace(states=SimpleNamespace(get=lambda eid: None))
    flow.source = "reconfigure"
    entry = SimpleNamespace(
        entry_id="anl1",
        data={CONF_KIND: "anl", "lights": ["light.old"], "brightness": 25},
        options={},
        unique_id="anl:light.old",
    )
    flow._reconfigure_entry = entry
    run(flow.async_step_reconfigure(None))
    run(flow.async_step_anl_reconfigure({"lights": ["light.new"]}))
    run(flow.async_step_time_details({
        "start_mode": "fixed", "end_mode": "fixed",
        "trigger_time": "22:00", "end_time": "06:00",
        "start_offset": 0, "end_offset": 0, "sun_entity": "sun.sun",
        "start_transition": 0, "end_transition": 0, "transition_interval": 5,
    }))
    result = run(flow.async_step_settings({
        "day_enabled": False, "brightness": 25, "color_temp_kelvin": 2200,
        "day_brightness": 100, "day_color_temp_kelvin": 4000,
        "tolerance_brightness": 4, "tolerance_kelvin": 150,
        "verify_delay": 5, "settle_delay": 1,
        "turn_on_listen": True, "only_when_on": False, "respect_manual": True,
    }))
    assert result["type"] == "abort"
    assert entry.unique_id == "anl:light.new"
    assert entry.data["lights"] == ["light.new"]


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
    _flush_deferred()  # 延迟拆除到点（ANL-F-039）
    reg = hass.data[web.REGISTRY_KEY]
    assert web.PAGE_LUX_ID not in reg
    assert web.PAGE_ANL_ID in reg
    actions = [d["action"] for _, d in hass.bus.fired]
    assert actions == ["register", "register", "unregister"]


def test_mixed_kind_reload_does_not_tear_down_the_other():
    """混合条目下直接调 async_unload_web 的幂等面：lux 桶空时 lux 注册该拆、
anl 注册保留（ANL-F-036；生产路径回归钉在 test_setup）。"""
    hass = FakeHass()
    _lux_bucket(hass, fake_lux_runtime())
    _anl_bucket(hass, fake_anl_manager())
    hass.data[web.REGISTRY_KEY] = {}
    run(web.async_setup_web(hass))
    # 单条目卸载：lux 桶还剩 0，anl 桶仍 1——lux 注册该拆（lux 条目清零）
    hass.data[web.DOMAIN]["lux"].clear()
    web.async_unload_web(hass)
    _flush_deferred()  # 延迟拆除到点（ANL-F-039）
    assert web.PAGE_ANL_ID in hass.data[web.REGISTRY_KEY]


def test_deferred_teardown_detaches_started_recheck():
    """ANL-F-050：_check 内的 recheck 收尾零覆盖面——lux 有页面、anl 从未
    注册时，尾部同步收尾被 page_registered_lux 挡住，只有延迟复查能卸。"""
    hass = FakeHass()
    _lux_bucket(hass, fake_lux_runtime())
    _anl_bucket(hass)
    run(web.async_setup_web(hass))
    flags = hass.data[web.WEB_DATA_KEY]
    hass.data[web.DOMAIN]["lux"].clear()
    web.async_unload_web(hass)
    assert flags.get("started_recheck") is not None  # 尾部同步路径被挡
    _flush_deferred()
    assert flags.get("started_recheck") is None  # 只有 _check→recheck 能卸


def test_unload_when_both_kinds_empty_detaches_recheck():
    hass = FakeHass()
    hass.data[web.DOMAIN] = {"lux": {}, "anl": {}}
    hass.data[web.REGISTRY_KEY] = {}
    run(web.async_setup_web(hass))
    flags = hass.data[web.WEB_DATA_KEY]
    assert flags.get("started_recheck") is not None
    web.async_unload_web(hass)
    _flush_deferred()  # 双 kind 全空 → 延迟复查里拆页并卸 recheck
    assert flags.get("started_recheck") is None


# ---------------------------------------------------------------------------
# config flow: kind discriminator
# ---------------------------------------------------------------------------


def _make_flow():
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
    lux_entry = SimpleNamespace(
        entry_id="lux1", data={CONF_KIND: KIND_LUX}, options={}
    )
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


def test_options_flow_has_init_step_both_kinds():
    """回归钉：options 流入口固定为 async_step_init（缺它真机打开即
    UnknownStep——合并改名的回归，V1.4.0 修复后钉住）。"""
    from custom_components.fumiha_lighting.anl.flow import AnlGroupOptionsFlow
    from custom_components.fumiha_lighting.lux.flow import LuxZoneOptionsFlow

    assert hasattr(LuxZoneOptionsFlow, "async_step_init")
    assert hasattr(AnlGroupOptionsFlow, "async_step_init")

    lux = LuxZoneOptionsFlow()
    lux.config_entry = SimpleNamespace(options={})
    result = run(lux.async_step_init(None))
    assert result["type"] == "form" and result["step_id"] == "lux_init"

    anl = AnlGroupOptionsFlow()
    anl.config_entry = SimpleNamespace(data={"kind": "anl", "lights": ["light.a"]},
                                       options={})
    result = run(anl.async_step_init(None))
    assert result["type"] == "form" and result["step_id"] == "anl_init"


def test_anl_wizard_three_step_chain():
    """新建向导 3 步：灯具 → 时间 → 参数；产物不含 extras/overrides。"""
    flow = _make_flow()
    flow.hass = SimpleNamespace(states=SimpleNamespace(get=lambda eid: None))
    first = run(flow.async_step_user({CONF_KIND: KIND_ANL}))
    assert first["step_id"] == "anl_user"  # 灯具选择
    second = run(flow.async_step_anl_user({"lights": ["light.a", "light.b"]}))
    assert second["step_id"] == "time_details"
    third = run(flow.async_step_time_details({
        "start_mode": "fixed", "end_mode": "fixed",
        "trigger_time": "22:00", "end_time": "06:00",
        "start_offset": 0, "end_offset": 0,
        "sun_entity": "sun.sun",
        "start_transition": 0, "end_transition": 0, "transition_interval": 5,
    }))
    assert third["step_id"] == "settings"
    # 空灯列表报 no_lights
    flow2 = _make_flow()
    flow2.hass = SimpleNamespace(states=SimpleNamespace(get=lambda eid: None))
    empty = run(flow2.async_step_anl_user({"lights": []}))
    assert empty["errors"] == {"base": "no_lights"}


def test_anl_wizard_create_entry_has_no_gui_managed_keys():
    flow = _make_flow()
    flow.hass = SimpleNamespace(states=SimpleNamespace(get=lambda eid: None))
    run(flow.async_step_anl_user({"lights": ["light.x"]}))
    run(flow.async_step_time_details({
        "start_mode": "fixed", "end_mode": "fixed",
        "trigger_time": "22:00", "end_time": "06:00",
        "start_offset": 0, "end_offset": 0, "sun_entity": "sun.sun",
        "start_transition": 0, "end_transition": 0, "transition_interval": 5,
    }))
    result = run(flow.async_step_settings({
        "day_enabled": False, "brightness": 25, "color_temp_kelvin": 2200,
        "day_brightness": 100, "day_color_temp_kelvin": 4000,
        "tolerance_brightness": 4, "tolerance_kelvin": 150,
        "verify_delay": 5, "settle_delay": 1,
        "turn_on_listen": True, "only_when_on": False, "respect_manual": True,
    }))
    assert result["type"] == "create_entry"
    data = result["data"]
    assert data["kind"] == "anl"
    assert data["lights"] == ["light.x"]
    assert data["transition_enabled"] is False
    for key in ("extras", "extra_count", "custom_per_light", "overrides",
                "extra_enabled"):
        assert key not in data


def test_anl_reconfigure_carries_gui_managed_keys():
    """reconfigure 收敛 data 清 options 时，GUI 管理键必须并入 data。"""
    flow = _make_flow()
    flow.hass = SimpleNamespace(states=SimpleNamespace(get=lambda eid: None))
    flow.source = "reconfigure"
    entry = SimpleNamespace(
        entry_id="anl1",
        data={CONF_KIND: "anl", "lights": ["light.a"], "brightness": 25},
        options={"extras": [{"name": "晚", "start": "18:00:00", "brightness": 60,
                             "color_temp_kelvin": 3000, "transition": 0}],
                 "extra_count": 1, "custom_per_light": [], "overrides": {}},
        unique_id="anl:light.a",
    )
    flow._reconfigure_entry = entry
    run(flow.async_step_reconfigure(None))
    run(flow.async_step_anl_reconfigure({"lights": ["light.a"]}))
    run(flow.async_step_time_details({
        "start_mode": "fixed", "end_mode": "fixed",
        "trigger_time": "23:00", "end_time": "06:00",
        "start_offset": 0, "end_offset": 0, "sun_entity": "sun.sun",
        "start_transition": 0, "end_transition": 0, "transition_interval": 5,
    }))
    result = run(flow.async_step_settings({
        "day_enabled": False, "brightness": 25, "color_temp_kelvin": 2200,
        "day_brightness": 100, "day_color_temp_kelvin": 4000,
        "tolerance_brightness": 4, "tolerance_kelvin": 150,
        "verify_delay": 5, "settle_delay": 1,
        "turn_on_listen": True, "only_when_on": False, "respect_manual": True,
    }))
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["extras"][0]["name"] == "晚"  # GUI 改动未被冲掉
    assert entry.data["trigger_time"] == "23:00:00"  # 向导字段已更新（归一化）
    assert entry.options == {}  # 全量收敛进 data


# ---------------------------------------------------------------------------
# gates + manifest
# ---------------------------------------------------------------------------


def test_view_gates():
    hass = FakeHass()
    run(web.async_setup_web(hass))
    for view in hass.http.views:
        assert view.requires_auth is True
        # 双方法视图（AnlConfigView get+post）每个 handler 都要过门禁
        # （ANL-F-021：原实现只查 get or post，post 的 require_admin 漏断言）
        for method in ("get", "post", "put", "delete"):
            handler = getattr(view, method, None)
            if handler is not None:
                assert getattr(handler, "_require_admin_applied", False) is True, (
                    type(view).__name__ + "." + method
                )


def test_setup_web_idempotent():
    hass = FakeHass()
    run(web.async_setup_web(hass))
    run(web.async_setup_web(hass))
    assert len(hass.http.views) == 8
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
        "link_group": "",  # 无组：_linked_runtimes 空返回（桩形状对齐生产 __init__）
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
        "link_group": "",  # 无组：_linked_runtimes 空返回（桩形状对齐生产 __init__）
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
            "_stale_warn_ts": None,
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
        "link_group": "",  # 无组：_linked_runtimes 空返回（桩形状对齐生产 __init__）
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
        "link_group": "",  # 无组：_linked_runtimes 空返回（桩形状对齐生产 __init__）
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
        "link_group": "",  # 无组：_linked_runtimes 空返回（桩形状对齐生产 __init__）
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
            "_stale_warn_ts": None,
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
        "link_group": "",  # 无组：_linked_runtimes 空返回（桩形状对齐生产 __init__）
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
            "_stale_warn_ts": None,
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
        "link_group": "",  # 无组：_linked_runtimes 空返回（桩形状对齐生产 __init__）
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
            "_stale_warn_ts": None,
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
        "link_group": "",  # 无组：_linked_runtimes 空返回（桩形状对齐生产 __init__）
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
            "_stale_warn_ts": None,
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
        "link_group": "",  # 无组：_linked_runtimes 空返回（桩形状对齐生产 __init__）
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
            "_stale_warn_ts": None,
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
        "link_group": "",  # 无组：_linked_runtimes 空返回（桩形状对齐生产 __init__）
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
            "_stale_warn_ts": None,
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
        "link_group": "",  # 无组：_linked_runtimes 空返回（桩形状对齐生产 __init__）
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
            "_stale_warn_ts": None,
            "lux_stale": False,
            "signal": "fumiha_lighting_lux1_update",
            "notify": lambda: None,
            "_async_sweep": hanging_sweep,
        }
    )
    sibling = ZoneRuntime.__new__(ZoneRuntime)
    sibling.__dict__.update(
        {
        "link_group": "",  # 无组：_linked_runtimes 空返回（桩形状对齐生产 __init__）
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
            "_stale_warn_ts": None,
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
            "link_group": "",  # 无组：_linked_runtimes 空返回（桩对齐生产）
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
                "_stale_warn_ts": None,
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
        "link_group": "",  # 无组：_linked_runtimes 空返回（桩形状对齐生产 __init__）
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
            "_stale_warn_ts": None,
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
        "link_group": "",  # 无组：_linked_runtimes 空返回（桩形状对齐生产 __init__）
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
            "_stale_warn_ts": None,
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
            "_stale_warn_ts": None,
            "lux_stale": False,
            "signal": "fumiha_lighting_lux1_update",
            "notify": lambda: None,
            "link_group": "",  # 无组：_linked_runtimes 空返回（协同采样零行为）
            "_cal_cross_samples": {},
            "_cal_cross_last_lc": {},
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
        "link_group": "",  # 无组：_linked_runtimes 空返回（桩形状对齐生产 __init__）
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
            "_stale_warn_ts": None,
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
        "link_group": "",  # 无组：_linked_runtimes 空返回（桩形状对齐生产 __init__）
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
            "_stale_warn_ts": None,
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


# ---------------------------------------------------------------------------
# anl day CI (V1.5.0): wizard fields, entry data, serializer, POST guard
# ---------------------------------------------------------------------------


def test_anl_settings_schema_has_no_ci_fields():
    """V1.6.0 单灯化：CI 两键移出向导第 3 步（GUI 编辑 + 携带）。"""
    from custom_components.fumiha_lighting.anl.const import (
        CONF_DAY_CI_ENABLED,
        CONF_DAY_CI_LIGHTS,
        CONF_DAY_CI_VIRTUAL_LIGHT,
    )
    from custom_components.fumiha_lighting.anl.flow import _settings_schema

    schema = _settings_schema({})
    keys = {m.schema for m in schema.schema}
    assert CONF_DAY_CI_ENABLED not in keys
    assert CONF_DAY_CI_LIGHTS not in keys
    assert CONF_DAY_CI_VIRTUAL_LIGHT not in keys


def test_anl_entry_data_wizard_produces_no_ci_keys():
    """V1.6.0：向导产物不含 CI 键（新条目 CI 关，GUI 后补；死键不固化）。"""
    times_input = {
        "start_mode": "fixed", "end_mode": "fixed",
        "trigger_time": "22:00", "end_time": "06:00",
        "start_offset": 0, "end_offset": 0, "sun_entity": "sun.sun",
        "start_transition": 0, "end_transition": 0, "transition_interval": 5,
    }

    def _run_wizard(settings):
        flow = _make_flow()
        flow.hass = SimpleNamespace(states=SimpleNamespace(get=lambda eid: None))
        run(flow.async_step_user({CONF_KIND: KIND_ANL}))
        run(flow.async_step_anl_user({"lights": ["light.a"]}))
        run(flow.async_step_time_details(times_input))
        return run(flow.async_step_settings(settings))

    result = _run_wizard({
        "day_enabled": True, "brightness": 25, "color_temp_kelvin": 2200,
        "day_brightness": 100, "day_color_temp_kelvin": 4000,
        "tolerance_brightness": 4, "tolerance_kelvin": 150,
        "verify_delay": 5, "settle_delay": 1,
        "turn_on_listen": True, "only_when_on": False, "respect_manual": True,
    })
    assert result["type"] == "create_entry"
    data = result["data"]
    assert "day_ci_enabled" not in data
    assert "day_ci_lights" not in data
    assert "day_ci_virtual_light" not in data


def test_anl_snapshot_serializes_day_ci_keys():
    """V1.7.0 快照契约：day_ci_virtual_light 键废除；逐灯 ci_zone_name 与
    清单解耦（ANL-F-132：∈反查表即给出——「∈区域未勾选」的灯凭它解除禁
    用，逐灯开关是接管唯一新开入口，混判会让新开接管在 UI 上不存在）；
    ∉任何区域的灯恒 None（B2 是物理归属，非勾选态）。"""
    hass = FakeHass()
    stubs.seed_light_zone(hass, "lux-x", "light.a", "卫生间", "light.a_virt")
    stubs.seed_light_zone(hass, "lux-y", "light.b", "干区", "light.b_virt")
    mgr = fake_anl_manager(day_ci_lights=["light.a"])
    # 第三盏灯：index 非空但本灯 ∉index（区域未配/已删形态）→ 恒 None
    from custom_components.fumiha_lighting.anl.manager import LightMachine

    mgr.machines["light.c"] = LightMachine(entity_id="light.c")
    _anl_bucket(hass, mgr)
    resp = run(web.AnlSnapshotView().get(FakeRequest(hass)))
    m = resp.payload["managers"][0]
    assert m["day_ci_lights"] == ["light.a"]
    assert "day_ci_virtual_light" not in m  # 死键不再下发
    lights = {lt["entity_id"]: lt for lt in m["lights"]}
    assert lights["light.a"]["ci_zone_name"] == "卫生间"
    # ANL-F-132：清单外但 ∈index → 归属照给（勾选态由页面按清单合成）
    assert lights["light.b"]["ci_zone_name"] == "干区"
    assert lights["light.c"]["ci_zone_name"] is None  # ∈表才有值，缺表不谎报
    # 无反查表（lux 桶空）：清单内灯也不得谎报归属
    hass2 = FakeHass()
    _anl_bucket(hass2, fake_anl_manager(day_ci_lights=["light.a"]))
    resp2 = run(web.AnlSnapshotView().get(FakeRequest(hass2)))
    lights2 = {lt["entity_id"]: lt for lt in resp2.payload["managers"][0]["lights"]}
    assert lights2["light.a"]["ci_zone_name"] is None


def test_anl_config_post_rejects_retired_virtual_light_key():
    """V1.7.0：day_ci_virtual_light 死键化——patch 携带该键按未知字段 400。"""
    hass = _recording_hass()
    entry = _config_entry(data={"kind": "anl", "lights": ["light.a"]})
    _anl_bucket(hass, _config_manager(entry))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {
                                   "day_ci_virtual_light": "light.ghost"}}})
        )
    )
    assert resp.status == 400
    assert "未知字段" in resp.payload["error"]
    assert "day_ci_virtual_light" in resp.payload["error"]
    assert recorded == []  # 校验失败绝不写条目


def test_anl_config_get_whitelist_drops_virtual_light_key():
    """V1.7.0：GET 白名单（CONFIG_KEYS）不再含 day_ci_virtual_light——
    编辑面读不到死键（存量条目 storage 残留值不下发）。"""
    hass = FakeHass()
    entry = _config_entry(
        data={"kind": "anl", "lights": ["light.a"],
              "day_ci_virtual_light": "light.virt"}
    )
    _anl_bucket(hass, _config_manager(entry))
    req = FakeRequest(hass)
    req.query = {"entry_id": "anl1"}
    resp = run(web.AnlConfigView().get(req))
    cfg = resp.payload["config"]
    assert "day_ci_virtual_light" not in cfg
    assert cfg["day_ci_lights"] == []  # ANL-F-102 镜像恒在：无生效清单 → []
    assert resp.payload["ok"] is True


def test_anl_config_post_dead_virtual_light_key_popped_on_save():
    """V1.7.0 死键惯例：存量条目的 day_ci_virtual_light 残留在任意一次保存
    后从 options 消失（day_ci_enabled 同款落盘侧清理）。"""
    hass = _recording_hass()
    entry = _config_entry(
        data={"kind": "anl", "lights": ["light.a"],
              "day_ci_virtual_light": "light.virt"}
    )
    _anl_bucket(hass, _config_manager(entry))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {
                                   "day_ci_lights": []}}})
        )
    )
    assert resp.payload["ok"] is True
    options = recorded[0][1]["options"]
    assert options["day_ci_lights"] == []
    assert "day_ci_virtual_light" not in options  # 死键 pop
    assert "day_ci_enabled" not in options  # 既有死键照常


def test_anl_config_post_valid_zone_ci_lights_ok():
    """V1.7.0 zone 归属校验：清单灯 ∈ 反查表 → 200 落盘（零归还——
    同 zone 保留）。"""
    hass = _recording_hass()
    stubs.seed_light_zone(hass, "lux-x", "light.a", "卫生间", "light.a_virt")
    entry = _config_entry(data={"kind": "anl", "lights": ["light.a"]})
    _anl_bucket(hass, _config_manager(entry))
    resp = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {
                                   "day_enabled": True,
                                   "day_ci_lights": ["light.a"]}}})
        )
    )
    assert resp.payload["ok"] is True
    options = recorded[0][1]["options"]
    assert options["day_ci_lights"] == ["light.a"]
    assert "day_ci_virtual_light" not in options


def test_anl_config_post_stale_zone_entries_removal_unlocked():
    """ANL-F-134：存量失效清单项放行（校验只打本次新增）——≥2 条失效项
    时逐个取消各落盘一次，清单最终清空。旧整表校验在此场景互锁：取消任
    一条都因另一条 400，永远摘不掉（页面逐灯开关每次提交全表）。"""
    hass = _recording_hass()
    entry = _config_entry(
        data={"kind": "anl", "lights": ["light.a", "light.bad1", "light.bad2"],
              CONF_DAY_ENABLED: True,
              "day_ci_lights": ["light.bad1", "light.bad2"]}
    )
    _anl_bucket(hass, _config_manager(entry))
    # 第一刀：取消 bad1 → 提交 [bad2]（存量放行）→ 200
    resp1 = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {
                                   "day_ci_lights": ["light.bad2"]}}})
        )
    )
    assert resp1.payload["ok"] is True
    assert recorded[0][1]["options"]["day_ci_lights"] == ["light.bad2"]
    # 第二刀：取消 bad2 → 提交 [] → 200 清空（旧码同样能过的 N=1 尾态）
    resp2 = run(
        web.AnlConfigView().post(
            FakeRequest(hass, {"entry_id": "anl1",
                               "patch": {"params": {"day_ci_lights": []}}})
        )
    )
    assert resp2.payload["ok"] is True
    assert entry.options["day_ci_lights"] == []  # _recording_hass 真实写回

