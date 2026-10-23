"""恒照度联动组测试（V1.3.0→v1.7.0 批，计划书 §WP2/WP3/§测试）。

覆盖：_external_contribution 数值面（交叉曲线优先/近似系数/灯灭按 0）、
2×2 场景扣除后收敛（ambient 数值用例）、协同采样 per-pct 入样与防重、
交叉曲线产出（同组合管线+存储+内存+notify）、质量门跳过、主曲线失败
废弃边界（错误必须响，不产出半截曲线）。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from test_setup import make_hass

from custom_components.fumiha_lighting.const import DOMAIN
from custom_components.fumiha_lighting.lux.const import (
    CAL_MIN_USEFUL_LUX,
    CONF_AUTO_TURN_OFF,
    CONF_CAL_STEP_PCT,
    CONF_DEADBAND_LUX,
    CONF_DEADBAND_PCT,
    CONF_MIN_BRIGHTNESS_PCT,
)
from custom_components.fumiha_lighting.lux.logic import curve_from_manual
from custom_components.fumiha_lighting.lux.runtime import (
    CalibrationError,
    ZoneRuntime,
)

run = asyncio.run
LOGGER = "custom_components.fumiha_lighting.lux.runtime"


# ---------------------------------------------------------------------------
# 装配：__new__ 裸实例（test_web/test_lux_v16_p3_fixes 同款模式）
# ---------------------------------------------------------------------------


def _make_hass(states: dict):
    hass = make_hass()
    hass.states = SimpleNamespace(get=states.get)
    hass.data.setdefault(DOMAIN, {})["lux"] = {}
    hass.async_create_task = asyncio.ensure_future  # start_calibration 建任务
    return hass


def _rt(hass, entry_id, light, lux, link="", **extra) -> ZoneRuntime:
    rt = ZoneRuntime.__new__(ZoneRuntime)
    rt.__dict__.update(
        {
            "hass": hass,
            "entry_id": entry_id,
            "zone_name": entry_id,
            "opts": {
                CONF_DEADBAND_PCT: 5.0,
                CONF_DEADBAND_LUX: 10.0,
                CONF_MIN_BRIGHTNESS_PCT: 5.0,
                CONF_AUTO_TURN_OFF: False,
            },
            "mode": "idle",
            "light_entity": light,
            "lux_entity": lux,
            "target_lux": 150.0,
            "commanded_pct": None,
            "curve": None,
            "cal_last_error": None,
            "cal_progress": "",
            "lux_stale": False,
            "_stale_warn_ts": None,
            "_lux_window": deque(maxlen=5),
            "_off_streak": 0,
            "_apply_lock": asyncio.Lock(),
            "_eval_task": None,
            "_cal_task": None,
            "_cal_captured": False,
            "_cal_was_on": False,
            "_cal_pending_off": False,
            "_stopped": False,
            "signal": f"fumiha_lighting_{entry_id}_update",
            "notify": lambda: None,
            # 联动组面（与生产 __init__ 同构）
            "link_group": link,
            "_cross_fade": 0.5,
            "_max_step_pct": 20.0,
            "_cross_curves": {},
            "_cal_cross_samples": {},
            "_cal_cross_last_lc": {},
        }
    )
    for k, v in extra.items():
        rt.__dict__[k] = v
    hass.data[DOMAIN]["lux"][entry_id] = rt
    return rt


def _state(state="on", brightness=None):
    attrs = {} if brightness is None else {"brightness": brightness}
    return SimpleNamespace(state=state, attributes=attrs, last_changed=None)


class _Store:
    """记录曲线落盘调用的假存储（主曲线+交叉曲线两通道）。"""

    def __init__(self):
        self.saved: list[object] = []
        self.cross_saved: list[tuple[str, object]] = []

    async def async_save_curve(self, curve):
        self.saved.append(curve)

    async def async_save_cross_curve(self, light_entity, curve):
        self.cross_saved.append((light_entity, curve))


async def _async_true():
    return True


async def _async_none(*a, **k):
    return None


async def _async_return(value):
    return value


# ---------------------------------------------------------------------------
# _external_contribution 数值面（WP3.3）
# ---------------------------------------------------------------------------


def _pair_with_states(states):
    hass = _make_hass(states)
    a = _rt(hass, "a", "light.a", "sensor.a", link="卫")
    b = _rt(hass, "b", "light.b", "sensor.b", link="卫")
    return hass, a, b


def test_external_contribution_cross_curve_takes_priority():
    """交叉曲线在场时直接用，近似系数不参与。"""
    states = {"light.b": _state(brightness=128)}  # ≈50.2%
    _, a, b = _pair_with_states(states)
    b.curve = curve_from_manual(2.0)  # 主曲线 lux_at(50.2)≈100.4
    cross = curve_from_manual(0.8)  # 交叉曲线 lux_at(50.2)≈40.16
    a._cross_curves["light.b"] = cross
    assert a._external_contribution() == pytest.approx(40.16, rel=0.001)


def test_external_contribution_approx_fallback():
    """无交叉曲线时 = 对方主曲线 × cross_fade（近似系数生效）。"""
    states = {"light.b": _state(brightness=128)}
    _, a, b = _pair_with_states(states)
    b.curve = curve_from_manual(2.0)
    assert a._external_contribution() == pytest.approx(100.4 * 0.5, rel=0.001)


def test_external_contribution_off_or_missing_is_zero():
    """灯灭/读不到状态按 0；brightness 缺失回退 commanded_pct；无组恒 0。"""
    states = {"light.b": _state(state="off", brightness=200)}
    _, a, b = _pair_with_states(states)
    b.curve = curve_from_manual(2.0)
    assert a._external_contribution() == 0.0

    states2 = {"light.b": _state(brightness=None)}
    hass2 = _make_hass(states2)
    a2 = _rt(hass2, "a", "light.a", "sensor.a", link="卫")
    b2 = _rt(hass2, "b", "light.b", "sensor.b", link="卫", commanded_pct=30.0)
    b2.curve = curve_from_manual(1.0)
    assert a2._external_contribution() == pytest.approx(30.0 * 0.5, rel=0.001)

    states3 = {"light.b": _state(brightness=128)}
    hass3 = _make_hass(states3)
    a3 = _rt(hass3, "a", "light.a", "sensor.a", link="")
    b3 = _rt(hass3, "b", "light.b", "sensor.b", link="")
    b3.curve = curve_from_manual(2.0)
    assert a3._external_contribution() == 0.0


# ---------------------------------------------------------------------------
# 2×2 场景扣除后收敛（ambient 数值用例）
# ---------------------------------------------------------------------------


def test_compute_decision_converges_across_pair_dimming():
    """对方灯调亮、本区传感器读数同步抬升时 ambient 不变 → 决策不变。

    震荡根因数值演示：不扣交叉贡献时 B 的补偿会被 A 当环境扰动（needed
    随 B 变化连锁修正）；扣除后 A 的决策序列无同向连锁修正。
    """
    hass_states = {
        # A 传感器读数 = 真实环境光 30 + A 自贡献 50.2（curve 1 lx/%×50.2%）
        #                + B 对 A 贡献 50.2（curve 2 lx/%×50.2%×cross_fade 0.5）
        "sensor.a": SimpleNamespace(
            state="130.4", attributes={}, last_changed=None
        ),
        "light.a": _state(brightness=128),  # A 当前 50.2%
    }
    hass, a, b = _pair_with_states(hass_states)
    a.curve = curve_from_manual(1.0)  # A 灯 → A 传感器：1 lx/%
    b.curve = curve_from_manual(2.0)

    # 基线：B 灯 50%（brightness=128）
    hass_states["light.b"] = _state(brightness=128)
    decision, lux_now = run(a._async_compute_decision())
    assert lux_now == pytest.approx(80.2, rel=0.001)  # 130.4 − 50.2（B 已扣）
    # ambient = 再扣自己贡献 → 真实环境光 30（计划书验收 2 的数值版）
    assert decision.ambient_lux == pytest.approx(30.0, rel=0.001)
    action, pct = decision.action, decision.pct

    # B 调到 100%（贡献 200×0.5=100），物理上 A 传感器同步 +49.8 → 读数 180.2
    hass_states["sensor.a"] = SimpleNamespace(
        state="180.2", attributes={}, last_changed=None
    )
    hass_states["light.b"] = _state(brightness=255)
    a._lux_window.clear()
    decision2, lux_now2 = run(a._async_compute_decision())
    assert lux_now2 == pytest.approx(80.2, rel=0.001)  # 180.2 − 100（B 已扣）
    assert decision2.ambient_lux == pytest.approx(30.0, rel=0.001)
    assert decision2.action == action
    assert decision2.pct == pct  # 决策不变（无连锁修正）


# ---------------------------------------------------------------------------
# 协同采样（WP3.1）
# ---------------------------------------------------------------------------


def test_cal_step_collects_cross_samples_and_dedupes(monkeypatch):
    """_async_cal_step 顺带记录同组 Y 传感器读数；同一 last_changed 不重复。"""
    clock = {"t": datetime(2026, 10, 24, 12, 0, 0)}
    started = clock["t"]

    from homeassistant.util import dt as dt_mod

    monkeypatch.setattr(dt_mod, "utcnow", lambda: clock["t"])

    async def fake_sleep(delay):
        clock["t"] += timedelta(seconds=delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    x_lc = {"t": started + timedelta(seconds=1)}  # X 传感器：一次新读数后不变
    y_lc = {"t": started + timedelta(seconds=2)}

    def fake_get(eid):
        if eid == "sensor.x":
            return SimpleNamespace(
                state="10.0", attributes={}, last_changed=x_lc["t"]
            )
        if eid == "sensor.y":
            return SimpleNamespace(
                state="30.0", attributes={}, last_changed=y_lc["t"]
            )
        return None

    hass = _make_hass({})
    hass.states = SimpleNamespace(get=fake_get)
    x = _rt(hass, "x", "light.x", "sensor.x", link="卫")
    _rt(hass, "y", "light.y", "sensor.y", link="卫")  # 只为入桶（协同采样目标）

    async def _ok_call(action, data):
        return True

    x._async_call_light = _ok_call

    samples = run(x._async_cal_step(50.0))
    assert samples == pytest.approx([10.0])
    assert x._cal_cross_samples["light.y"] == {50.0: [30.0]}
    assert x._cal_cross_last_lc["light.y"] == y_lc["t"]

    # 第二档：Y 传感器 last_changed 变新 → 新档入样（防重使同一条读数
    # 在轮询中只记一次，单条即正确）
    y_lc["t"] = clock["t"] + timedelta(seconds=5)
    run(x._async_cal_step(75.0))
    assert x._cal_cross_samples["light.y"][50.0] == [30.0]  # 旧档不被污染
    assert x._cal_cross_samples["light.y"][75.0] == [30.0]


# ---------------------------------------------------------------------------
# 交叉曲线产出（WP3.2）
# ---------------------------------------------------------------------------


def test_publish_cross_curves_full_pipeline():
    """采样数据走同组合管线（source="cross"）→ 存储+内存+notify 三落点。"""
    states = {"light.b": _state(state="off")}
    hass, a, b = _pair_with_states(states)
    store = _Store()
    b.storage = store
    notified = []
    b.notify = lambda: notified.append(True)
    a._cal_cross_samples = {
        "light.b": {
            None: [30.0, 30.0],  # 基线档（X 灯灭）
            25.0: [55.0],
            50.0: [80.0, 82.0],  # 上下两遍同档累积
            75.0: [105.0],
            100.0: [130.0],
        }
    }

    run(a._async_publish_cross_curves())

    assert len(store.cross_saved) == 1
    light_entity, curve = store.cross_saved[0]
    assert light_entity == "light.a"
    assert curve.source == "cross"
    # 线性 +1 lx/%（基线 30 扣除）经 smooth3 后 50% 档 ≈ 50.33
    assert curve.lux_at(50.0) == pytest.approx(50.33, rel=0.02)
    assert curve.max_lux == pytest.approx(91.67, rel=0.02)
    assert b._cross_curves["light.a"] is curve
    assert notified == [True]


def test_publish_cross_curves_low_quality_skipped(caplog):
    """贡献低于 CAL_MIN_USEFUL_LUX → WARNING+不产出（近似兜底顶上）。"""
    assert CAL_MIN_USEFUL_LUX > 0.8  # 用例前提：增量低于质量门
    states = {"light.b": _state(state="off")}
    hass, a, b = _pair_with_states(states)
    store = _Store()
    b.storage = store
    a._cal_cross_samples = {
        "light.b": {None: [30.0], 50.0: [30.5], 100.0: [30.8]}  # 增量 <5 lx
    }
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        run(a._async_publish_cross_curves())
    assert store.cross_saved == []
    assert b._cross_curves == {}
    assert any("近似系数兜底" in r.getMessage() for r in caplog.records)


def test_publish_cross_curves_missing_baseline_skipped(caplog):
    """缺基线档 → WARNING+跳过，不产出半截曲线。"""
    states = {"light.b": _state(state="off")}
    hass, a, b = _pair_with_states(states)
    store = _Store()
    b.storage = store
    a._cal_cross_samples = {"light.b": {50.0: [80.0], 100.0: [130.0]}}
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        run(a._async_publish_cross_curves())
    assert store.cross_saved == []
    assert any("基线" in r.getMessage() for r in caplog.records)


def test_publish_per_zone_failure_does_not_poison_others(caplog):
    """单 Y 存储失败 → WARNING+跳过该 Y，其余 Y 照常产出。"""
    states = {"light.b": _state(state="off"), "light.c": _state(state="off")}
    hass = _make_hass(states)
    a = _rt(hass, "a", "light.a", "sensor.a", link="卫")
    b = _rt(hass, "b", "light.b", "sensor.b", link="卫")
    c = _rt(hass, "c", "light.c", "sensor.c", link="卫")

    class _BoomStore(_Store):
        async def async_save_cross_curve(self, light_entity, curve):
            raise RuntimeError("disk full")

    b.storage = _BoomStore()
    c.storage = _Store()
    a._cal_cross_samples = {
        "light.b": {None: [30.0], 50.0: [80.0], 100.0: [130.0]},
        "light.c": {None: [10.0], 50.0: [40.0], 100.0: [70.0]},
    }
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        run(a._async_publish_cross_curves())
    assert c.storage.cross_saved and c.storage.cross_saved[0][0] == "light.a"
    assert b._cross_curves == {}
    assert any("disk full" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 失败边界（WP3.4）：主曲线失败 → 交叉采样一并废弃
# ---------------------------------------------------------------------------


def test_calibration_failure_discards_cross_samples():
    """sweep 抛 CalibrationError → finally 废弃暂存，交叉曲线零产出。"""
    states = {"light.b": _state(state="off")}
    hass, a, b = _pair_with_states(states)
    store = _Store()
    b.storage = store

    async def _boom(step):
        raise CalibrationError("有效采样点不足，无法拟合曲线")

    a.opts = {**a.opts, CONF_CAL_STEP_PCT: 5}
    hass.states = SimpleNamespace(
        get=lambda eid: _state(state="off") if eid == "light.a" else None
    )
    a._async_sweep = _boom
    a._async_restore_light = _async_none
    a._cal_cross_samples = {"light.b": {None: [30.0], 50.0: [80.0]}}

    async def scenario():
        assert a.start_calibration() is True
        for _ in range(50):
            await asyncio.sleep(0)
            if a._cal_task.done():
                break
        assert a._cal_task.done()

    run(scenario())
    assert a.cal_last_error is not None
    assert a._cal_cross_samples == {}  # 废弃
    assert a._cal_cross_last_lc == {}
    assert store.cross_saved == []  # 零产出（无半截曲线）
    assert b._cross_curves == {}


def test_natural_completion_publishes_from_samples():
    """端到端：自然完成 → 主曲线采纳 → 交叉曲线产出（publish 被调）。"""
    states = {"light.b": _state(state="off")}
    hass, a, b = _pair_with_states(states)
    store = _Store()
    a.storage = store
    b.storage = store

    sweep_result = {
        "up": {25.0: 55.0, 50.0: 80.0, 75.0: 105.0, 100.0: 130.0},
        "down": {50.0: 82.0, 75.0: 104.0},
        "baseline": 30.0,
        "missed_streak": 0,
    }
    a._cal_cross_samples = {
        "light.b": {
            None: [30.0],
            25.0: [40.0],
            50.0: [55.0],
            75.0: [70.0],
            100.0: [85.0],
        }
    }
    a._async_sweep = lambda step: _async_return(sweep_result)
    a._async_restore_light = _async_none
    a.opts = {**a.opts, CONF_CAL_STEP_PCT: 5}
    hass.states = SimpleNamespace(get=lambda eid: None)  # 灯不可用→收尾 IDLE

    async def scenario():
        assert a.start_calibration() is True
        for _ in range(50):
            await asyncio.sleep(0)
            if a._cal_task.done():
                break
        assert a._cal_task.done()

    run(scenario())
    assert a.cal_last_error is None
    assert len(store.saved) == 1  # 主曲线落盘
    assert len(store.cross_saved) == 1  # 交叉曲线落盘
    _, cross = store.cross_saved[0]
    assert cross.source == "cross"
    assert b._cross_curves["light.a"] is cross
