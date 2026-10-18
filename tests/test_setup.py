"""Entry-lifecycle tests for the merged two-kind integration (FL-F-007).

The merge's core risk face: setup/unload symmetry across both kinds,
service registration, and — critically — that a freshly created anl entry
is never touched by any migration chain (FL-F-001 regression pin).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from conftest import (  # noqa: E402 — 依赖 conftest 桩先行（sys.path 由 conftest 建立）
    flush_deferred_callbacks as _flush_deferred,
)

import custom_components.fumiha_lighting as pkg
import custom_components.fumiha_lighting.web as web
from custom_components.fumiha_lighting.const import (
    CONF_KIND,
    DOMAIN,
    KIND_ANL,
    KIND_LUX,
)


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


class FakeHass:
    def __init__(self):
        self.data = {}
        self.bus = FakeBus()
        self.http = FakeHttp()
        registered = {}
        self.services = SimpleNamespace(
            registered=registered,
            has_service=lambda d, s: (d, s) in registered,
            async_register=lambda d, s, h, schema=None: registered.__setitem__(
                (d, s), h
            ),
            async_remove=lambda d, s: registered.pop((d, s), None),
        )
        update_calls: list[dict] = []

        def _update_entry(entry, data=None, options=None, version=None):
            # 真实语义：写回条目（FL-F-011——迁移链的持久化通道必须可见）
            if data is not None:
                entry.data = data
            if options is not None:
                entry.options = options
            if version is not None:
                entry.version = version
            update_calls.append({"data": data, "options": options, "version": version})
            return _ok(None)

        self.config_entries = SimpleNamespace(
            async_update_entry=_update_entry,
            update_calls=update_calls,
            async_unload_platforms=lambda *a, **k: _ok(True),
            async_forward_entry_setups=lambda *a, **k: _ok(True),
        )


async def _ok(v):
    return v


def make_entry(kind, entry_id):
    data = {CONF_KIND: kind}
    if kind == KIND_LUX:
        data.update(
            {
                "zone_name": "卫生间",
                "light_entity": "light.bath",
                "lux_entity": "sensor.bath_lux",
            }
        )
    else:
        # fixed 锚点：避开 astral canary（sunset/sunrise 会触达 get_astral_event_date）
        data.update(
            {
                "lights": ["light.bedroom"],
                "start_mode": "fixed",
                "trigger_time": "22:00:00",
                "end_mode": "fixed",
                "end_time": "06:00:00",
            }
        )
    entry = SimpleNamespace(
        entry_id=entry_id, data=data, options={}, version=1, title=kind
    )
    # async_on_unload 桩：记录卸载回调（含 update listener）
    entry._unsubs = []

    def _on_unload(cb):
        entry._unsubs.append(cb)
        return None

    entry.async_on_unload = _on_unload
    entry.add_update_listener = lambda listener: _on_unload(listener)
    return entry


def make_hass():
    hass = FakeHass()
    hass.states = SimpleNamespace(
        get=lambda eid: SimpleNamespace(
            state="off", attributes={"supported_color_modes": ["brightness"]}
        )
    )
    return hass



def test_setup_unload_lux_and_anl_symmetry():
    """两类型各 setup/unload 一次：双桶收支平衡、web flags 正确。"""
    hass = make_hass()
    lux_entry = make_entry(KIND_LUX, "lux1")
    anl_entry = make_entry(KIND_ANL, "anl1")

    assert run(pkg.async_setup_entry(hass, lux_entry))
    assert run(pkg.async_setup_entry(hass, anl_entry))

    buckets = hass.data[DOMAIN]
    assert set(buckets["lux"]) == {"lux1"}
    assert set(buckets["anl"]) == {"anl1"}

    flags = hass.data[web.WEB_DATA_KEY]
    assert flags["page_registered_lux"] == "standalone"  # 无壳 → 独立面板
    assert flags["page_registered_anl"] == "standalone"

    # 卸载两类型
    assert run(pkg.async_unload_entry(hass, lux_entry))
    assert run(pkg.async_unload_entry(hass, anl_entry))
    assert buckets["lux"] == {} and buckets["anl"] == {}
    # 拆除延迟复查（ANL-F-039）：unload 后先挂 pending，到点才拆
    _flush_deferred()
    # standalone 拆除真断言（FL-F-012）：panel 键按 frontend_url_path 查、
    # flags 弹出——hub registry 键在 standalone 下不存在，不能用 page_id 查。
    panels = hass.data.get("frontend_panels", {})
    assert "lux-balance" not in panels and "auto-night-light" not in panels
    flags_after = hass.data[web.WEB_DATA_KEY]
    assert flags_after.get("page_registered_lux") is None
    assert flags_after.get("page_registered_anl") is None


def test_unload_one_kind_tears_down_its_page_only():
    """ANL-F-036/039 生产路径回归钉：混合安装下真删除 lux（卸载后无 setup
    跟进）——延迟复查到点后 lux 页面必须拆、anl 页面保留。变异自检：恢复
    外层双空门控 → deferred 不再挂起 → 到点后 lux panel 不拆 → 必红。"""
    hass = make_hass()
    lux_entry = make_entry(KIND_LUX, "lux1")
    anl_entry = make_entry(KIND_ANL, "anl1")
    run(pkg.async_setup_entry(hass, lux_entry))
    run(pkg.async_setup_entry(hass, anl_entry))

    assert run(pkg.async_unload_entry(hass, lux_entry))

    # 延迟窗口内：pending 挂起，页面原样（不再同步拆——reload 不闪）
    panels = hass.data.get("frontend_panels", {})
    assert "lux-balance" in panels and "auto-night-light" in panels
    flags = hass.data[web.WEB_DATA_KEY]
    assert flags.get("pending_teardown_lux") is True

    # 真删除：无 setup 跟进 → 延迟复查到点 → lux 拆、anl 留
    _flush_deferred()
    panels = hass.data.get("frontend_panels", {})
    assert "lux-balance" not in panels
    assert "auto-night-light" in panels
    flags = hass.data[web.WEB_DATA_KEY]
    assert flags.get("page_registered_lux") is None
    assert flags.get("page_registered_anl") == "standalone"


def test_reload_window_does_not_churn_registration():
    """ANL-F-039 回归钉：保存触发的 reload（unload→setup 连续）不得产生
    拆页+重注册——setup 回填撤销 pending，延迟复查到点后注册原样。"""
    hass = make_hass()
    lux_entry = make_entry(KIND_LUX, "lux1")
    run(pkg.async_setup_entry(hass, lux_entry))
    panels_before = dict(hass.data.get("frontend_panels", {}))
    bus_before = list(hass.bus.fired)

    assert run(pkg.async_unload_entry(hass, lux_entry))
    assert run(pkg.async_setup_entry(hass, lux_entry))  # reload 的回填
    _flush_deferred()

    panels_after = hass.data.get("frontend_panels", {})
    assert panels_after == panels_before  # 面板键原样（未拆重挂）
    unregister = [
        e for e in hass.bus.fired[len(bus_before):]
        if e[1] and e[1].get("action") == "unregister"
    ]
    assert unregister == []  # 总线无 unregister 事件


def test_unload_failure_keeps_bucket_and_page():
    """ANL-F-037a：lux 平台卸载失败时条目仍 loaded，runtime 留桶 →
    延迟复查到点后页面也不得拆。"""
    hass = make_hass()
    lux2 = make_entry(KIND_LUX, "lux2")
    run(pkg.async_setup_entry(hass, lux2))
    hass.config_entries.async_unload_platforms = lambda entry, platforms: _ok(
        False
    )
    assert not run(pkg.async_unload_entry(hass, lux2))
    assert set(hass.data[DOMAIN]["lux"]) == {"lux2"}  # runtime 未出桶
    _flush_deferred()
    assert "lux-balance" in hass.data.get("frontend_panels", {})  # 页面未拆


def test_services_registered_once_and_survive_unload():
    """setup 注册 calibrate/trigger_now；重复 setup 不重复；卸载后保留。"""
    hass = make_hass()
    lux_entry = make_entry(KIND_LUX, "lux1")
    anl_entry = make_entry(KIND_ANL, "anl1")

    run(pkg.async_setup_entry(hass, lux_entry))
    run(pkg.async_setup_entry(hass, anl_entry))
    assert (DOMAIN, "calibrate") in hass.services.registered
    assert (DOMAIN, "trigger_now") in hass.services.registered

    run(pkg.async_setup_entry(hass, lux_entry))  # reload：不重复注册
    count = sum(1 for k in hass.services.registered if k == (DOMAIN, "calibrate"))
    assert count == 1

    run(pkg.async_unload_entry(hass, lux_entry))
    run(pkg.async_unload_entry(hass, anl_entry))
    # 卸载策略：服务保留（空桶调用只告警，重 setup 复用）
    assert (DOMAIN, "calibrate") in hass.services.registered
    assert (DOMAIN, "trigger_now") in hass.services.registered


def test_fresh_anl_entry_is_never_migrated():
    """FL-F-001 回归钉：新建 anl 条目 setup 前后 data/options/version 不被改写。

    fake 的 async_update_entry 做真实写回并记录调用（FL-F-011）：迁移链的
    历史写入通道恰是 config_entries.async_update_entry，no-op 桩会让钉失效。
    """
    hass = make_hass()
    anl_entry = make_entry(KIND_ANL, "anl1")
    data_before = dict(anl_entry.data)
    options_before = dict(anl_entry.options)
    version_before = anl_entry.version

    assert run(pkg.async_setup_entry(hass, anl_entry))

    assert anl_entry.data == data_before
    assert anl_entry.options == options_before
    assert anl_entry.version == version_before
    # 迁移链若复活，必经此调用改写条目——零调用是第二道锁
    assert hass.config_entries.update_calls == []


def test_fresh_lux_entry_data_untouched_by_setup():
    hass = make_hass()
    lux_entry = make_entry(KIND_LUX, "lux1")
    data_before = dict(lux_entry.data)

    assert run(pkg.async_setup_entry(hass, lux_entry))

    assert lux_entry.data == data_before


def run(coro):
    return asyncio.run(coro)
