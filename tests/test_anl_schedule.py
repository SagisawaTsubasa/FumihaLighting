"""Pure schedule-logic tests (no stubs needed)."""

from datetime import datetime

import pytest
from schedule import lerp_params, step_datetimes, upcoming_band_start


def test_step_datetimes_aligned():
    start = datetime(2026, 10, 2, 18, 0)
    anchor = datetime(2026, 10, 2, 19, 0)
    steps = step_datetimes(start, anchor, 5)
    assert steps[0] == start
    assert steps[-1] == anchor  # 末步精确锚点（factor=1 归位）
    assert len(steps) == 13  # 18:00..18:55 共 12 步 + 锚点
    assert steps[1] == datetime(2026, 10, 2, 18, 5)


def test_step_datetimes_clamps_last_interval():
    """dur 不整除间隔时，末步钳到锚点。"""
    start = datetime(2026, 10, 2, 18, 0)
    anchor = datetime(2026, 10, 2, 18, 8)
    steps = step_datetimes(start, anchor, 5)
    assert steps == [start, datetime(2026, 10, 2, 18, 5), anchor]


def test_step_datetimes_rejects_zero_interval():
    with pytest.raises(ValueError):
        step_datetimes(datetime(2026, 10, 2, 18, 0), datetime(2026, 10, 2, 19, 0), 0)


def test_lerp_params_endpoints_and_mid():
    assert lerp_params((100, 4000), (25, 2200), 0.0) == (100, 4000)
    assert lerp_params((100, 4000), (25, 2200), 1.0) == (25, 2200)
    b, k = lerp_params((100, 4000), (25, 2200), 0.5)
    assert (b, k) == (62, 3100)  # round(62.5)=62 银行家舍入，钉住现行为
    # factor 越界钳位
    assert lerp_params((100, 4000), (25, 2200), 2.0) == (25, 2200)


def test_upcoming_band_start_today_future():
    now = datetime(2026, 10, 2, 12, 0)
    start = upcoming_band_start(now, 18 * 60, 60)
    assert start == datetime(2026, 10, 2, 18, 0)


def test_upcoming_band_start_active_crossing_midnight():
    """23:50 开始、跨 0:20 结束的带，在 0:10 时应定位到昨天的起点。"""
    now = datetime(2026, 10, 3, 0, 10)
    start = upcoming_band_start(now, 23 * 60 + 50, 30)
    assert start == datetime(2026, 10, 2, 23, 50)


def test_upcoming_band_start_midband_today():
    now = datetime(2026, 10, 2, 18, 30)
    start = upcoming_band_start(now, 18 * 60, 60)
    assert start == datetime(2026, 10, 2, 18, 0)


def test_upcoming_band_start_passed_moves_to_tomorrow():
    now = datetime(2026, 10, 2, 19, 30)
    start = upcoming_band_start(now, 18 * 60, 60)
    assert start == datetime(2026, 10, 3, 18, 0)
