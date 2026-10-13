"""Pure transition-schedule logic (no HA imports — directly unit-testable).

定点调度引擎的纯逻辑：步进时刻表、参数插值、带起点定位。
manager.py 只做粘合，决策全部在这里，方便 pytest 钉住行为。
"""

from __future__ import annotations

from datetime import datetime, timedelta


def step_datetimes(
    band_start: datetime, anchor: datetime, interval_min: int
) -> list[datetime]:
    """Step times covering [band_start, anchor]: start, every interval, and the anchor.

    末步精确落在锚点（factor=1.0 归位）；首步即带入口（factor=0.0，
    是接管判定的第一观测点）。interval_min 必须为正。
    """
    if interval_min <= 0:
        raise ValueError("interval must be positive")
    steps: list[datetime] = []
    t = band_start
    while t < anchor:
        steps.append(t)
        t += timedelta(minutes=interval_min)
    steps.append(anchor)
    return steps


def lerp_params(
    from_pair: tuple[int, int], to_pair: tuple[int, int], factor: float
) -> tuple[int, int]:
    """Linear interpolation of (brightness_pct, kelvin); factor clamped to [0, 1]."""
    factor = min(1.0, max(0.0, factor))
    b_from, k_from = from_pair
    b_to, k_to = to_pair
    return (
        round(b_from + (b_to - b_from) * factor),
        round(k_from + (k_to - k_from) * factor),
    )


def upcoming_band_start(
    now: datetime, band_start_min: int, dur_min: int
) -> datetime | None:
    """Locate the band_start datetime of the currently-active or next band.

    在「昨天/今天/明天」三个候选里找锚点（band_start + dur）晚于 now 且
    最早的一个——覆盖跨午夜进行中的带（如 23:50 开始、跨 0:20 结束）。
    无候选返回 None（当日带已全部结束，等下次重建）。
    """
    base = now.replace(
        hour=band_start_min // 60, minute=band_start_min % 60, second=0, microsecond=0
    )
    best: tuple[datetime, datetime] | None = None
    for offset in (-1, 0, 1):
        start = base + timedelta(days=offset)
        anchor = start + timedelta(minutes=dur_min)
        if anchor > now and (best is None or anchor < best[1]):
            best = (start, anchor)
    return best[0] if best is not None else None
