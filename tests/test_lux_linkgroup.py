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
    CONF_CROSS_FADE,
    CONF_DEADBAND_LUX,
    CONF_DEADBAND_PCT,
    CONF_LIGHT_ENTITY,
    CONF_LINK_GROUP,
    CONF_LUX_ENTITY,
    CONF_MAX_STEP_PCT,
    CONF_MIN_BRIGHTNESS_PCT,
    CONF_ZONE_NAME,
)
from custom_components.fumiha_lighting.lux.logic import Decision, curve_from_manual
from custom_components.fumiha_lighting.lux.runtime import (
    _CAL_CROSS_NO_PREV,
    CalibrationError,
    ZoneRuntime,
)
from homeassistant.components.light import ATTR_BRIGHTNESS_PCT

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
            "_eval_busy": False,
            "_eval_pending": False,
            "decisions": deque(maxlen=50),
            "last_decision": None,
            "last_adjustment": None,
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
            "_cal_cross_prev_pct": _CAL_CROSS_NO_PREV,
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


# ---------------------------------------------------------------------------
# 亮度缓变（WP4）
# ---------------------------------------------------------------------------


def _active_rt_with_light(hass_states, **extra):
    hass = _make_hass(hass_states)
    a = _rt(hass, "a", "light.a", "sensor.a", link="卫", mode="active", **extra)
    a.decisions = []
    a.last_decision = None
    a.last_adjustment = None
    calls = []

    async def _call(service, data):
        calls.append((service, data))
        # 缓变后灯亮度跟随（模拟灯接受指令）——分步逼近的 current 演进
        if service == "turn_on" and ATTR_BRIGHTNESS_PCT in data:
            hass_states["light.a"] = _state(
                brightness=round(data[ATTR_BRIGHTNESS_PCT] * 2.55)
            )
        return True

    a._async_call_light = _call
    return a, calls, hass_states


def test_apply_ramps_stepwise_and_marks_reason():
    """超距 set 单步限幅 + reason 追加「缓变」；后续评估分步逼近。"""
    hass_states = {"light.a": _state(brightness=26)}  # current ≈10.2%
    a, calls, hass_states = _active_rt_with_light(hass_states, _max_step_pct=20.0)

    # 目标 100%，距 current 89.8 > 20 → 限到 30.2（reason 标缓变）
    run(a._apply("lux", 100.0, Decision("set", 100.0, 30.0, 70.0, "compensation")))
    assert calls == [("turn_on", {ATTR_BRIGHTNESS_PCT: 30})]
    assert a.commanded_pct == pytest.approx(30.2, rel=0.001)
    assert "缓变" in a.last_decision.reason

    # 第二轮评估（目标仍 100%）：current 30.2 → 限到 50.2（继续逼近）
    run(a._apply("lux", 100.0, Decision("set", 100.0, 30.0, 70.0, "compensation")))
    assert calls[-1] == ("turn_on", {ATTR_BRIGHTNESS_PCT: 50})

    # 逼近到位后（current 50.2 → 目标 65）：差 14.8 < 20 → 不变形不标记
    run(a._apply("lux", 100.0, Decision("set", 65.0, 30.0, 35.0, "compensation")))
    assert calls[-1] == ("turn_on", {ATTR_BRIGHTNESS_PCT: 65})
    assert "缓变" not in a.last_decision.reason
    assert a.last_decision.pct == pytest.approx(65.0)


def test_apply_off_decision_not_ramped():
    """off 决策不受缓变限制：直接 turn_off，不经限幅分支。"""
    hass_states = {"light.a": _state(brightness=26)}
    a, calls, _ = _active_rt_with_light(hass_states, _max_step_pct=20.0)
    run(
        a._apply(
            "lux",
            300.0,
            Decision("off", None, 200.0, -50.0, "ambient at or above target"),
        )
    )
    assert calls == [("turn_off", {})]
    assert a.mode == "idle"


# ---------------------------------------------------------------------------
# R1 首审修复钉（V1.7.0 合并批步骤 1）
# ---------------------------------------------------------------------------


def _real_rt(hass, entry_id, light, lux, link="卫", mode="idle") -> ZoneRuntime:
    """真构造 ZoneRuntime（走 __init__，供监听/夹取/strip 面测试）。"""
    entry = SimpleNamespace(
        entry_id=entry_id,
        data={
            CONF_ZONE_NAME: entry_id,
            CONF_LIGHT_ENTITY: light,
            CONF_LUX_ENTITY: lux,
        },
        options={CONF_LINK_GROUP: link} if link else {},
    )
    rt = ZoneRuntime(hass, entry)
    rt.mode = mode
    hass.data[DOMAIN]["lux"][entry_id] = rt
    return rt


def test_link_listeners_symmetric_after_late_join(monkeypatch):
    """R1 发现 1（P1）：后启动条目进桶后，setup 侧对桶内所有 runtime 各
    重建一次联动订阅——先启动的 a 也能听到 b（旧实现单向建链）。重建幂
    等：最终恰每方向一条订阅，无重复无残留。"""
    from custom_components.fumiha_lighting.lux import runtime as rt_mod

    subs: list[tuple[str, tuple]] = []

    def fake_track(hass, entities, action):
        entry = (action.__self__.entry_id, tuple(entities))
        subs.append(entry)
        return lambda: subs.remove(entry)  # 真 unsub：可断言无重复/残留

    monkeypatch.setattr(rt_mod, "async_track_state_change_event", fake_track)

    def link_subs(subscriber: str) -> list[tuple]:
        """联动订阅 = 目标不是本区自己的灯/传感器的订阅（本体三订阅剔除）。"""
        own = {
            "a": {("light.a",), ("sensor.a",)},
            "b": {("light.b",), ("sensor.b",)},
        }[subscriber]
        return [t for s, t in subs if s == subscriber and t not in own]

    hass = _make_hass({})
    a = _real_rt(hass, "a", "light.a", "sensor.a", link="卫")
    a.start()  # 桶里只有 a → 零联动订阅（旧实现就此单向断链）
    assert link_subs("a") == []
    b = _real_rt(hass, "b", "light.b", "sensor.b", link="卫")
    b.start()  # b 订到 a
    assert link_subs("b") == [("light.a",)]
    # lux_async_setup_entry 的对称化循环：对桶内所有 runtime 各调一次
    for other in hass.data[DOMAIN]["lux"].values():
        other._refresh_link_listeners()
    assert link_subs("a") == [("light.b",)]
    assert link_subs("b") == [("light.a",)]
    # 幂等：重复重建不产生重复订阅
    for other in hass.data[DOMAIN]["lux"].values():
        other._refresh_link_listeners()
    assert link_subs("a") == [("light.b",)]
    assert link_subs("b") == [("light.a",)]


def test_link_listeners_dropped_on_stop(monkeypatch):
    """unload 边界：async_stop 撤销联动订阅与本体订阅（_link_unsubs 单独
    记账、成对注销）。"""
    from custom_components.fumiha_lighting.lux import runtime as rt_mod

    subs: list[tuple[str, tuple]] = []

    def fake_track(hass, entities, action):
        entry = (action.__self__.entry_id, tuple(entities))
        subs.append(entry)
        return lambda: subs.remove(entry)

    monkeypatch.setattr(rt_mod, "async_track_state_change_event", fake_track)
    monkeypatch.setattr(
        rt_mod, "async_track_time_interval", lambda hass, action, interval: lambda: None
    )

    hass = _make_hass({})
    a = _real_rt(hass, "a", "light.a", "sensor.a", link="卫")
    b = _real_rt(hass, "b", "light.b", "sensor.b", link="卫")
    a.start()
    b.start()
    for other in hass.data[DOMAIN]["lux"].values():
        other._refresh_link_listeners()
    a_links = [t for s, t in subs if s == "a" and t == ("light.b",)]
    assert a_links == [("light.b",)]
    run(a.async_stop())
    # a 的联动订阅已撤（本体订阅同撤）；b 不受牵连
    assert [t for s, t in subs if s == "a" and t == ("light.b",)] == []
    assert [t for s, t in subs if s == "b" and t == ("light.a",)] == [("light.a",)]
def test_mutual_link_events_no_ping_pong():
    """R1 发现 1 配套钉：对称监听后 A↔B 彼此写灯互触发重评估——交叉贡
    献扣除使双方决策不变，链条一步终止（死区收敛为理论依据，无乒乓）。"""
    states = {
        # 双方传感器读数都按「环境 30 + 双方贡献」配平：扣除后 ambient=30
        "sensor.a": SimpleNamespace(state="130.39", attributes={}, last_changed=None),
        "sensor.b": SimpleNamespace(state="155.49", attributes={}, last_changed=None),
        "light.a": _state(brightness=128),
        "light.b": _state(brightness=128),
    }
    hass, a, b = _pair_with_states(states)
    a.curve = curve_from_manual(1.0)
    b.curve = curve_from_manual(2.0)
    a.mode = b.mode = "active"
    a.target_lux = b.target_lux = 30.0
    writes: list[tuple[str, str]] = []

    for rt in (a, b):

        def _call(service, data, _rt=rt):
            writes.append((_rt.entry_id, service))
            return _async_true()

        rt._async_call_light = _call

    async def scenario():
        # 对方灯亮度变化事件（对称监听下的互触发路径）
        b._on_linked_event(SimpleNamespace(data={"new_state": _state("on", 130)}))
        for _ in range(10):
            await asyncio.sleep(0)
        a._on_linked_event(SimpleNamespace(data={"new_state": _state("on", 130)}))
        for _ in range(10):
            await asyncio.sleep(0)

    run(scenario())
    # 双方都重评估过（决策环有记录）但零写灯——死区内 noop，链一步终止
    assert len(a.decisions) >= 1 and len(b.decisions) >= 1
    assert writes == []


def test_turn_on_first_frame_deducts_external_contribution():
    """R1 发现 2（P2）：开灯首帧共用 _prepare_lux——扣除组员贡献 + 中值
    窗 + 照度死区同判据（旧实现原始瞬时值 → needed 偏小亮度显著偏低）。"""
    states = {
        "sensor.a": SimpleNamespace(state="130.4", attributes={}, last_changed=None),
        "light.a": _state(state="off"),
        "light.b": _state(brightness=255),  # B 全亮：贡献 2.0×100×0.5=100.4
    }
    hass, a, b = _pair_with_states(states)
    a.curve = curve_from_manual(1.0)
    b.curve = curve_from_manual(2.0)
    calls: list[tuple[str, dict]] = []

    async def _call(service, data):
        calls.append((service, data))
        return True

    a._async_call_light = _call
    run(a.async_turn_on_requested(None))
    # net = 130.4 − 100.39 = 30 → ambient 30（灯灭自身零贡献）→ needed 120
    # → set 100%（旧无扣除实现：lux 130.4 > target → off → 兜底 min 5%）
    assert calls == [("turn_on", {"brightness_pct": 100})]


def test_light_pct_unknown_is_none_and_consumers_agree():
    """R1 发现 3（P2）：亮度未知三态 None——不贡献、不作限幅基准（_apply
    退化为不限幅直发），0/100 分叉静默默认废除。"""
    states = {"light.b": _state(brightness=None)}  # ON 但不上报亮度
    hass, a, b = _pair_with_states(states)
    b.curve = curve_from_manual(2.0)
    assert a._external_contribution() == 0.0  # 未知不贡献
    assert b._current_pct() is None  # 旧码此处判 100

    b.mode = "active"
    b.decisions.clear()
    b.last_decision = None
    sent: list[dict] = []

    async def _call(service, data):
        sent.append(data)
        return True

    b._async_call_light = _call
    run(b._apply("lux", 100.0, Decision("set", 65.0, 30.0, 35.0, "compensation")))
    # 直发决策亮度：旧码 current=100 → 缓变限到 80%（反向过冲）
    assert sent == [{"brightness_pct": 65}]
    assert "缓变" not in b.last_decision.reason


def test_current_pct_known_brightness_still_ramps():
    """三态不伤既有面：亮度可读时 _apply 照常单步缓变。"""
    hass_states = {"light.a": _state(brightness=26)}  # current ≈10.2%
    a, calls, _ = _active_rt_with_light(hass_states, _max_step_pct=20.0)
    run(a._apply("lux", 100.0, Decision("set", 100.0, 30.0, 70.0, "compensation")))
    assert calls == [("turn_on", {"brightness_pct": 30})]
    assert "缓变" in a.last_decision.reason


def test_record_line_shows_gross_and_deduction():
    """R1 发现 13：决策环 lux= 带对账分量「净值（原始−扣）」——可与传感
    器实体读数核对；无外部贡献（无组）时保持旧格式零噪音。"""
    states = {
        "sensor.a": SimpleNamespace(state="130.4", attributes={}, last_changed=None),
        "light.b": _state(brightness=255),
        "light.a": _state(state="off"),
    }
    hass, a, b = _pair_with_states(states)
    b.curve = curve_from_manual(2.0)  # B 主曲线：近似系数贡献 2.0×100×0.5
    decision, lux_now = run(a._async_compute_decision())
    line = a._record("lux", lux_now, decision)
    assert line is not None
    assert "原130" in line and "扣100" in line and "lux=30" in line

    # 无组：零扣除零后缀
    states2 = {
        "sensor.a": SimpleNamespace(state="42.0", attributes={}, last_changed=None)
    }
    hass2 = _make_hass(states2)
    solo = _rt(hass2, "solo", "light.a", "sensor.a", link="")
    decision2, lux2 = run(solo._async_compute_decision())
    line2 = solo._record("lux", lux2, decision2)
    assert "原" not in line2 and "lux=42" in line2


def test_cal_step_attributes_gap_samples_to_previous_step(monkeypatch):
    """R1 发现 6（P3）：慢组员在「上档窗口关闭后、本档 started 前」到达的
    已稳定读数补录进上一档（旧实现 o_lc < started 永久跳过）。"""
    clock = {"t": datetime(2026, 10, 24, 12, 0, 0)}
    from homeassistant.util import dt as dt_mod

    monkeypatch.setattr(dt_mod, "utcnow", lambda: clock["t"])

    async def fake_sleep(delay):
        clock["t"] += timedelta(seconds=delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    y_lc = {"t": None}  # 慢组员：基线窗内一报未发

    def fake_get(eid):
        if eid == "sensor.x":
            return SimpleNamespace(
                state="10.0", attributes={}, last_changed=clock["t"]
            )
        if eid == "sensor.y":
            if y_lc["t"] is None:
                return None
            return SimpleNamespace(
                state="30.0", attributes={}, last_changed=y_lc["t"]
            )
        return None

    hass = _make_hass({})
    hass.states = SimpleNamespace(get=fake_get)
    x = _rt(hass, "x", "light.x", "sensor.x", link="卫")
    _rt(hass, "y", "light.y", "sensor.y", link="卫")  # 慢组员入桶（协同采样目标）

    async def _ok(action, data):
        return True

    x._async_call_light = _ok

    # _async_run_calibration 入口的防重基准预置（测试直接调 _async_cal_
    # step，手动等价布景）：floor=现在，上一档=基线（None）
    floor = clock["t"]
    x._cal_cross_last_lc["light.y"] = floor
    x._cal_cross_prev_pct = None

    # 间隙读数：基线窗关闭后、25% 档 started 前到达（floor < lc < started）
    clock["t"] += timedelta(seconds=30)
    y_lc["t"] = clock["t"] - timedelta(seconds=2)
    run(x._async_cal_step(25.0))
    # 补录进基线档（而非 25% 档），且防重（多次轮询不重复入样）
    assert x._cal_cross_samples["light.y"] == {None: [30.0]}
    assert x._cal_cross_prev_pct == 25.0

    # 首档前（无上一档哨兵）：间隙读数不得冒充基线
    hass2 = _make_hass({})
    hass2.states = SimpleNamespace(get=fake_get)
    x2 = _rt(hass2, "x2", "light.x", "sensor.x", link="卫")
    _rt(hass2, "y2", "light.y", "sensor.y", link="卫")
    x2._async_call_light = _ok
    x2._cal_cross_last_lc["light.y"] = clock["t"]
    x2._cal_cross_prev_pct = _CAL_CROSS_NO_PREV
    run(x2._async_cal_step(25.0))
    assert x2._cal_cross_samples.get("light.y", {}) == {}


def test_max_step_pct_clamped_at_construction():
    """R1 发现 7（P3）：消费点从 const 夹取——.storage 手改 0/负值/超界
    不再退化成「永不动/每轮下漂」。"""
    for raw, expected in ((0, 1.0), (-20, 1.0), (500, 100.0), (20, 20.0)):
        hass = _make_hass({})
        entry = SimpleNamespace(
            entry_id="e",
            data={
                CONF_ZONE_NAME: "z",
                CONF_LIGHT_ENTITY: "light.a",
                CONF_LUX_ENTITY: "sensor.a",
            },
            options={CONF_MAX_STEP_PCT: raw},
        )
        rt = ZoneRuntime(hass, entry)
        assert rt._max_step_pct == expected


def test_cross_fade_clamped_at_construction():
    """ANL-F-140（P3）：cross_fade 消费点夹取 0..1（与 max_step_pct 同族）
    ——.storage 手改后三种静默错扣形态收敛：0=近似兜底失效（组内扣 0 回
    互串震荡）、负值=扣成「加」（ambient 虚高过驱）、>1=过扣归零 ambient
    （同样过驱），三种都无日志且与正常限幅不可区分。"""
    for raw, expected in ((0, 0.0), (-0.5, 0.0), (7, 1.0), (0.5, 0.5)):
        hass = _make_hass({})
        entry = SimpleNamespace(
            entry_id="e",
            data={
                CONF_ZONE_NAME: "z",
                CONF_LIGHT_ENTITY: "light.a",
                CONF_LUX_ENTITY: "sensor.a",
            },
            options={CONF_CROSS_FADE: raw},
        )
        rt = ZoneRuntime(hass, entry)
        assert rt._cross_fade == expected


def test_link_group_stripped_at_runtime_read():
    """R1 发现 9（P3）：读取侧 strip 兜底存量——尾空格组名不再静默成组。"""
    hass = _make_hass({})
    entry = SimpleNamespace(
        entry_id="e",
        data={
            CONF_ZONE_NAME: "z",
            CONF_LIGHT_ENTITY: "light.a",
            CONF_LUX_ENTITY: "sensor.a",
        },
        options={CONF_LINK_GROUP: " 卫生间 "},
    )
    rt = ZoneRuntime(hass, entry)
    assert rt.link_group == "卫生间"


def _fake_store_module(monkeypatch, initial):
    """ZoneStorage.Store 替身：内存 dict 存取。返回 (模块, 替身类)。"""
    from custom_components.fumiha_lighting.lux import store as store_mod

    class _FakeStore:
        saves = 0

        def __init__(self, hass, version, key):
            self.data = initial

        async def async_load(self):
            return self.data

        async def async_save(self, data):
            _FakeStore.saves += 1
            self.data = data

    monkeypatch.setattr(store_mod, "Store", _FakeStore)
    return store_mod, _FakeStore


def test_load_cross_curves_warns_on_corrupt_payload(monkeypatch, caplog):
    """R1 发现 4（P2）：交叉曲线「有数据但坏了」必须响（主曲线同款
    WARNING 带 light_entity）；形状缺失类保持静默（与主曲线 None 同语义）。"""
    store_mod, _fake_store_cls = _fake_store_module(
        monkeypatch,
        {
            "cross_curves": {
                "light.good": {"points": [[0, 0], [100, 50]]},
                "light.badparse": {"points": [["x", "y"]]},
                "light.notlist": {"points": "oops"},
                "light.short": {"points": [[0, 0]]},
            }
        },
    )
    storage = store_mod.ZoneStorage(None, "e1")
    with caplog.at_level(logging.WARNING, logger=store_mod._LOGGER.name):
        curves = run(storage.async_load_cross_curves())
    assert set(curves) == {"light.good"}
    messages = [r.getMessage() for r in caplog.records]
    assert any("light.badparse" in m for m in messages)  # 坏数据 → WARNING
    assert not any("light.notlist" in m for m in messages)  # 形状缺失 → 静默
    assert not any("light.short" in m for m in messages)


def test_save_lock_preserves_both_keys(monkeypatch):
    """R1 发现 8（P3）：读-改-写按 entry_id 加锁——并发 save 不再以
    「后写者覆盖」丢掉对方的 cross_curves 键。"""
    store_mod, _fake_store_cls = _fake_store_module(monkeypatch, None)
    storage = store_mod.ZoneStorage(None, "e1")

    async def scenario():
        await asyncio.gather(
            storage.async_save_curve(curve_from_manual(1.0)),
            storage.async_save_cross_curve("light.b", curve_from_manual(2.0)),
        )

    run(scenario())
    assert "points" in storage._store.data
    assert "light.b" in storage._store.data["cross_curves"]


def test_drop_cross_curve_removes_memory_and_disk(monkeypatch):
    """R1 发现 14（P3）：改绑/删除后他区孤儿交叉曲线回收——内存+落盘
    两处，幂等（键不存在零落盘零 notify）。"""
    store_mod, _fake_store_cls = _fake_store_module(
        monkeypatch,
        {
            "points": [[0, 0], [100, 50]],
            "cross_curves": {"light.old": {"points": [[0, 0], [100, 9]]}},
        },
    )
    hass = _make_hass({})
    a = _rt(hass, "a", "light.a", "sensor.a", link="卫")
    a.storage = store_mod.ZoneStorage(None, "e1")
    a._cross_curves["light.old"] = curve_from_manual(1.0)
    notified = []
    a.notify = lambda: notified.append(True)

    run(a.async_drop_cross_curve("light.old"))
    assert "light.old" not in a._cross_curves
    assert "light.old" not in a.storage._store.data["cross_curves"]
    assert notified == [True]

    # 幂等：再删同键零写入零 notify
    saves_before = _fake_store_cls.saves
    run(a.async_drop_cross_curve("light.old"))
    assert _fake_store_cls.saves == saves_before
    assert notified == [True]


def test_load_curve_rejects_non_finite_points(monkeypatch, caplog):
    """ANL-F-139（P3）：主曲线含 NaN/inf 整条忽略并 WARNING——非有限点能
    穿过解析与 Curve（其唯一异常源是空点集）直落决策链，`max(0, gross-
    NaN)` 归零 ambient → 决策恒过驱且全程无日志。"""
    store_mod, _cls = _fake_store_module(
        monkeypatch,
        {"points": [[0, 0], [50, float("nan")], [100, 50]]},
    )
    storage = store_mod.ZoneStorage(None, "e1")
    with caplog.at_level(logging.WARNING, logger=store_mod._LOGGER.name):
        curve = run(storage.async_load_curve())
    assert curve is None
    assert any("非有限值" in r.getMessage() for r in caplog.records)


def test_load_cross_curves_rejects_non_finite_points(monkeypatch, caplog):
    """ANL-F-139（P3）：交叉曲线含非有限点整条忽略（近似系数兜底）并
    WARNING 带 light_entity；好曲线不受牵连。"""
    store_mod, _cls = _fake_store_module(
        monkeypatch,
        {
            "cross_curves": {
                "light.good": {"points": [[0, 0], [100, 50]]},
                "light.nan": {"points": [[0, 0], [100, float("inf")]]},
            }
        },
    )
    storage = store_mod.ZoneStorage(None, "e1")
    with caplog.at_level(logging.WARNING, logger=store_mod._LOGGER.name):
        curves = run(storage.async_load_cross_curves())
    assert set(curves) == {"light.good"}
    assert any(
        "light.nan" in r.getMessage() and "非有限值" in r.getMessage()
        for r in caplog.records
    )


def test_async_remove_serialized_with_saves_and_cleans_lock(monkeypatch):
    """ANL-F-141（P3）：async_remove 与三个 save 同锁——改绑窗口
    （async_remove → reload 卸载之间条目仍 loaded）内校准落盘不再把旧灯
    曲线写回已删文件；删除后锁记账清理（_SAVE_LOCKS 不随删条目增长）。"""
    from custom_components.fumiha_lighting.lux import store as store_mod

    removed = []

    class _Store:
        def __init__(self, hass, version, key):
            self.data = {"points": [[0, 0], [100, 50]]}

        async def async_load(self):
            return self.data

        async def async_save(self, data):
            self.data = data

        async def async_remove(self):
            removed.append(True)
            self.data = None

    monkeypatch.setattr(store_mod, "Store", _Store)
    storage = store_mod.ZoneStorage(None, "e1")

    async def scenario():
        lock = store_mod._save_lock("e1")
        async with lock:
            task = asyncio.create_task(storage.async_remove())
            for _ in range(5):
                await asyncio.sleep(0)
            # 持锁期间 remove 被挡（旧码直接删文件，校准 save 可插队写回）
            assert not task.done()
        await task
        assert removed == [True]
        # 锁记账清理：条目删除后 _SAVE_LOCKS 不残留
        assert store_mod._SAVE_LOCKS.get("e1") is None

    run(scenario())
