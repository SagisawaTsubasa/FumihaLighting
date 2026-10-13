"""Merged web-layer tests: two page REST surfaces, dual descriptors,
REST write endpoints and the kind-discriminating config flow."""

from __future__ import annotations

import asyncio
import json
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
    def __init__(self, hass):
        self.app = {"hass": hass}


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
    assert web.PAGE_LUX_ID == "luxshadow.main"  # 影子预览版
    assert web.PAGE_ANL_ID == "anlshadow.main"
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
    assert len(hass.http.views) == 5
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
    assert m["version"] == "1.0.1"
