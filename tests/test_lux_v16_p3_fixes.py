"""V1.6.0 步骤 8 存量 P3 六项清偿回归钉（R11 报告 wbmcp-1022-020633-bf03）。

覆盖：发现 2（F-102 硬上限/F-104 NaN 收口零回归钉）、发现 3（NaN↔有效
交替读数 WARNING 时间窗节流）、发现 4（lux setup 失败回滚）、发现 5（基线
步 turn_off 失败响亮中止）、发现 6（calibrate 服务 target 键目标判定）。
发现 1（文案残留）无行为面，由文案 grep 验证不设钉。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import pytest
from test_setup import make_entry, make_hass

import custom_components.fumiha_lighting as pkg
from custom_components.fumiha_lighting.const import DOMAIN
from custom_components.fumiha_lighting.lux.runtime import (
    CalibrationError,
    ZoneRuntime,
)

run = asyncio.run


def _bare_runtime(**attrs) -> ZoneRuntime:
    """__new__ 裸实例（web 序列化测试同款模式），只挂控制环用到的属性。"""
    rt = ZoneRuntime.__new__(ZoneRuntime)
    rt.zone_name = "测试区"
    rt.lux_entity = "sensor.bath_lux"
    rt.light_entity = "light.bath"
    rt.commanded_pct = None
    rt._apply_lock = asyncio.Lock()
    rt.lux_stale = False
    rt._stale_warn_ts = None
    rt._lux_window = deque(maxlen=5)
    rt.notify = lambda: None
    rt._schedule_evaluate = lambda _trigger: None
    # 联动组（V1.3.0）默认无组：_linked_runtimes 空返回，协同采样零行为；
    # 两个暂存与生产 __init__ 同构（F-102 走 _async_cal_step 会触碰）。
    rt.link_group = ""
    rt._cal_cross_samples = {}
    rt._cal_cross_last_lc = {}
    for k, v in attrs.items():
        setattr(rt, k, v)
    return rt


# ---------------------------------------------------------------------------
# 发现 2 / 3：F-102 硬上限 + F-104 NaN 收口回归钉
# ---------------------------------------------------------------------------


def test_f102_hard_deadline_caps_relentless_reports(monkeypatch, caplog):
    """连续上报不停 → 恰在 started+80s 收口、返回已采样本、WARNING 可见。

    旧回归形状：hard_deadline 判断被挪动/删除后静默期被连续上报无限续期，
    _async_cal_step 永不返回、整组恒照度挂起——本钉用假时钟保证 break 恰
    发生在 FIRST_REPORT(60)+QUIET(20) 边界。
    """
    clock = {"t": datetime(2026, 10, 22, 12, 0, 0)}
    started = clock["t"]

    from homeassistant.util import dt as dt_mod

    monkeypatch.setattr(dt_mod, "utcnow", lambda: clock["t"])

    async def fake_sleep(delay):
        clock["t"] += timedelta(seconds=delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    class _States:
        def get(self, _eid):
            # 每次读都给「新」last_changed（跟着假时钟走）→ quiet_deadline
            # 每轮被续期，只有 hard_deadline 能收口
            return SimpleNamespace(state="42.0", last_changed=clock["t"])

    rt = _bare_runtime(hass=SimpleNamespace(states=_States()))

    async def _ok(service, data):
        return True

    rt._async_call_light = _ok

    with caplog.at_level(logging.WARNING):
        samples = run(rt._async_cal_step(50.0))
    elapsed = clock["t"] - started
    assert samples, "已采样本必须照常返回"
    assert timedelta(seconds=80) <= elapsed < timedelta(seconds=81)
    assert "按已采样本收口" in caplog.text


def test_f104_nan_throttle_survives_alternating_readings(caplog):
    """NaN↔有效交替读数 → WARNING 最多一条（旧布尔节流每两事件一条）、
    坏值不进窗、恢复清窗。"""
    rt = _bare_runtime()
    rt._lux_window.extend([100.0, 101.0, 102.0])

    def _ev(state):
        return SimpleNamespace(
            data={"new_state": SimpleNamespace(state=state), "old_state": None}
        )

    with caplog.at_level(logging.WARNING):
        rt._on_lux_event(_ev("nan"))  # WARNING #1
        rt._on_lux_event(_ev("42.0"))  # 恢复（清窗、lux_stale=False）
        rt._on_lux_event(_ev("nan"))  # 5 分钟窗口内 → 不再 WARNING
    warns = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "照度数据不可信" in r.getMessage()
    ]
    assert len(warns) == 1
    assert rt.lux_stale is True
    assert len(rt._lux_window) == 0  # 交替下坏值不进窗（滤波不退化）


def test_f104_recovery_resets_stale_and_clears_window():
    """恢复语义保持：坏值期后的首个有效读数清窗+复位 lux_stale。"""
    rt = _bare_runtime()
    rt._lux_window.extend([100.0, 101.0, 102.0])

    def _ev(state):
        return SimpleNamespace(
            data={"new_state": SimpleNamespace(state=state), "old_state": None}
        )

    rt._on_lux_event(_ev("nan"))
    assert rt.lux_stale is True and not rt._lux_window
    rt._on_lux_event(_ev("42.0"))
    assert rt.lux_stale is False
    assert list(rt._lux_window) == [42.0]  # 恢复后窗从新读数起算


def test_f104_stale_warn_rewarns_after_interval():
    """节流窗口（5 分钟）过后再次坏值 → 新一条 WARNING（时间窗而非永久）。"""
    rt = _bare_runtime()
    first = datetime(2026, 10, 22, 12, 0, 0)

    def _ev(state):
        return SimpleNamespace(
            data={"new_state": SimpleNamespace(state=state), "old_state": None}
        )

    from homeassistant.util import dt as dt_mod

    with mock.patch.object(dt_mod, "utcnow", lambda: first):
        rt._on_lux_event(_ev("nan"))
    assert rt._stale_warn_ts == first
    later = first + timedelta(minutes=6)
    with mock.patch.object(dt_mod, "utcnow", lambda: later):
        rt._on_lux_event(_ev("nan"))
    assert rt._stale_warn_ts == later  # 窗口到期 → 重新 WARNING（时间戳前移）


# ---------------------------------------------------------------------------
# 发现 4：lux setup 失败回滚（幻影区域缺口）
# ---------------------------------------------------------------------------


def test_setup_failure_rolls_back_runtime_bucket(monkeypatch, caplog):
    """async_load 抛异常 → runtime 出桶 + 异常沿 setup 面上抛（不吞）。"""
    hass = make_hass()
    entry = make_entry("lux", "lux1")

    async def _boom(self):
        raise RuntimeError("store 读取失败")

    monkeypatch.setattr(ZoneRuntime, "async_load", _boom)
    from custom_components.fumiha_lighting.lux import lux_async_setup_entry

    with pytest.raises(RuntimeError):
        run(lux_async_setup_entry(hass, entry))
    bucket = (hass.data.get(DOMAIN) or {}).get("lux", {})
    assert "lux1" not in bucket, "setup 失败后桶必须回滚（幻影区域缺口）"
    assert "已回滚运行时" in caplog.text


def test_setup_failure_forward_setups_rolls_back_runtime_bucket(monkeypatch):
    """forward_entry_setups 抛异常 → 同样回滚（回滚覆盖两个 await 面）。"""
    hass = make_hass()
    entry = make_entry("lux", "lux1")

    async def _boom(*_a):
        raise RuntimeError("平台转发失败")

    monkeypatch.setattr(
        hass.config_entries, "async_forward_entry_setups", _boom
    )
    from custom_components.fumiha_lighting.lux import lux_async_setup_entry

    with pytest.raises(RuntimeError):
        run(lux_async_setup_entry(hass, entry))
    bucket = (hass.data.get(DOMAIN) or {}).get("lux", {})
    assert "lux1" not in bucket


# ---------------------------------------------------------------------------
# 发现 5：基线步 turn_off 失败响亮中止
# ---------------------------------------------------------------------------


def test_baseline_turn_off_failure_aborts_calibration(caplog):
    """turn_off 失败 → CalibrationError（不采样），与 turn_on 弃点不对称
    的缺口收口；页面经 cal_last_error 显示中止原因。"""
    rt = _bare_runtime()

    calls = []

    async def _fail_off(service, data):
        calls.append(service)
        return False  # 灯不可用/服务失败形态

    rt._async_call_light = _fail_off
    with caplog.at_level(logging.WARNING), pytest.raises(
        CalibrationError, match="基线阶段无法关闭灯具"
    ):
        run(rt._async_cal_step(None))
    assert calls == ["turn_off"]


# ---------------------------------------------------------------------------
# 发现 6：calibrate 服务 target 键目标判定 + 标量 entity_id
# ---------------------------------------------------------------------------


def test_calibrate_service_target_key_counts_as_target(monkeypatch, caplog):
    """data 带 target 键（services.yaml 推荐写法的嵌套形态）→ 视为有目标，
    不走「未识别到有效目标」静默放弃；目标未命中区域时响亮抛错。"""
    hass = make_hass()
    run(pkg.async_setup_entry(hass, make_entry("lux", "lux1")))
    handler = hass.services.registered[(DOMAIN, "calibrate")]
    assert handler is not None

    async def _extract(*_a, **_k):
        return set()

    monkeypatch.setattr(pkg, "async_extract_entity_ids", _extract)
    call = SimpleNamespace(data={"target": {"entity_id": ["light.bath"]}})
    with pytest.raises(Exception) as exc:
        run(handler(call))
    # has_target=True → 解析为空 → 响亮「未解析出任何实体」而非静默 return
    assert "目标未解析出任何实体" in str(exc.value)
    assert "未识别到有效目标" not in caplog.text


def test_calibrate_service_scalar_entity_id_counts_as_target(monkeypatch):
    """标量 entity_id（cv.entity_ids 的 YAML 常见形态）→ 视为有目标。"""
    hass = make_hass()
    run(pkg.async_setup_entry(hass, make_entry("lux", "lux1")))
    handler = hass.services.registered[(DOMAIN, "calibrate")]

    async def _extract(*_a, **_k):
        return set()

    monkeypatch.setattr(pkg, "async_extract_entity_ids", _extract)
    call = SimpleNamespace(data={"entity_id": "light.bath"})
    with pytest.raises(Exception) as exc:
        run(handler(call))
    assert "目标未解析出任何实体" in str(exc.value)
