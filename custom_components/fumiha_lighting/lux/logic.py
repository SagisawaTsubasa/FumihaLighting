"""Pure logic for lux_balance: curve model, control law, calibration synthesis.

This module must stay free of Home Assistant imports so tests can run with
plain pytest (trip count convention).
"""

from __future__ import annotations

from dataclasses import dataclass

CurvePoint = tuple[float, float]


@dataclass(frozen=True)
class Decision:
    """Outcome of one control-law evaluation."""

    action: str  # "off" | "noop" | "set"
    pct: float | None
    ambient_lux: float
    needed_lux: float
    reason: str


class Curve:
    """Piecewise-linear mapping from brightness percent to added lux.

    Points are kept sorted by percent; a (0, 0) anchor is inserted so the
    curve always starts at "light off contributes nothing".
    """

    def __init__(self, points: list[CurvePoint], source: str = "calibrated") -> None:
        pts = sorted((float(p), float(v)) for p, v in points)
        if not pts:
            raise ValueError("curve needs at least one point")
        merged: list[CurvePoint] = []
        for pct, lux in pts:
            if merged and merged[-1][0] == pct:
                prev = merged[-1]
                merged[-1] = (pct, (prev[1] + lux) / 2)
            else:
                merged.append((pct, lux))
        if merged[0][0] > 0:
            merged.insert(0, (0.0, 0.0))
        self.points: list[CurvePoint] = merged
        self.source = source

    @property
    def max_lux(self) -> float:
        return self.points[-1][1]

    @property
    def slope_20_80(self) -> float:
        """Average added lux per percent across the 20-80% operating range."""
        span = self.lux_at(80) - self.lux_at(20)
        return span / 60.0

    def lux_at(self, pct: float) -> float:
        """Added lux contributed by the light running at ``pct``."""
        p = min(100.0, max(0.0, float(pct)))
        segments = zip(self.points, self.points[1:], strict=False)
        for (p0, v0), (p1, v1) in segments:
            if p <= p1:
                if p1 <= p0:
                    return v1
                frac = (p - p0) / (p1 - p0)
                return v0 + (v1 - v0) * frac
        return self.points[-1][1]

    def pct_for(self, lux: float) -> float:
        """Inverse mapping: brightness percent that adds ``lux`` at the sensor."""
        target = float(lux)
        if target <= 0:
            return 0.0
        if target >= self.points[-1][1]:
            return 100.0
        for (p0, v0), (p1, v1) in zip(self.points, self.points[1:], strict=False):
            if target <= v1:
                span = v1 - v0
                if span <= 0:
                    return p1
                frac = (target - v0) / span
                return p0 + (p1 - p0) * frac
        return 100.0


def decide(
    curve: Curve,
    lux_now: float,
    pct_now: float,
    target: float,
    deadband_pct: float,
    min_pct: float,
) -> Decision:
    """Constant-illuminance control law.

    Ambient estimate = measured lux minus the contribution we believe the
    light is currently making; the requested contribution is inverted through
    the curve. Deadband only applies while the light is already on so a fresh
    turn-on always produces at least ``min_pct``.
    """
    ambient = max(0.0, lux_now - curve.lux_at(pct_now))
    needed = target - ambient
    if needed <= 0:
        return Decision("off", None, ambient, needed, "ambient at or above target")
    pct = min(100.0, max(min_pct, curve.pct_for(needed)))
    if pct_now > 0 and abs(pct - pct_now) < deadband_pct:
        return Decision("noop", pct, ambient, needed, "within deadband")
    return Decision("set", pct, ambient, needed, "compensation")


def calibration_steps(step_pct: int) -> list[int]:
    """Sweep percentages for a given step size, always 0→…→100.

    Steps that do not divide 100 evenly would otherwise never reach the
    100% point, leaving the top of the curve to extrapolation.
    """
    step = max(2, min(20, int(step_pct)))
    steps = list(range(0, 101, step))
    if steps[-1] != 100:
        steps.append(100)
    return steps


def combine_passes(
    up: dict[float, float], down: dict[float, float]
) -> list[CurvePoint]:
    """Average the up-sweep and down-sweep medians per percent.

    Points missing from one pass keep the other; missing from both simply do
    not appear (the curve interpolates across them).
    """
    result: list[CurvePoint] = []
    for pct in sorted(set(up) | set(down)):
        values = [v for v in (up.get(pct), down.get(pct)) if v is not None]
        result.append((pct, sum(values) / len(values)))
    return result


def subtract_baseline(points: list[CurvePoint], baseline: float) -> list[CurvePoint]:
    """Convert raw lux medians into contributions relative to the 0% reading."""
    return [(pct, max(0.0, lux - baseline)) for pct, lux in points]


def smooth3(points: list[CurvePoint]) -> list[CurvePoint]:
    """Three-point moving average along the sweep; shorter lists pass through."""
    if len(points) < 3:
        return list(points)
    result: list[CurvePoint] = []
    for idx, (pct, lux) in enumerate(points):
        prev_v = points[idx - 1][1] if idx > 0 else lux
        next_v = points[idx + 1][1] if idx < len(points) - 1 else lux
        result.append((pct, (prev_v + lux + next_v) / 3))
    return result


def enforce_monotonic(points: list[CurvePoint]) -> list[CurvePoint]:
    """Pool-adjacent-violators isotonic regression (non-decreasing fit).

    Sensor noise can invert neighbouring samples; a real dimming curve is
    monotonic, so violations get pooled into their local mean.
    """
    if len(points) < 2:
        return list(points)
    blocks: list[list[float]] = []  # [sum_of_values, count]
    for _, lux in points:
        blocks.append([lux, 1.0])
        while len(blocks) >= 2 and (
            blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]
        ):
            sum_b, cnt_b = blocks.pop()
            sum_a, cnt_a = blocks.pop()
            blocks.append([sum_a + sum_b, cnt_a + cnt_b])
    result: list[CurvePoint] = []
    idx = 0
    for total, cnt in blocks:
        mean = total / cnt
        for _ in range(int(cnt)):
            pct, _ = points[idx]
            result.append((pct, mean))
            idx += 1
    return result


def build_curve(points: list[CurvePoint], source: str = "calibrated") -> Curve:
    """Final synthesis step: sorted, deduplicated curve with (0,0) anchor."""
    return Curve(points, source=source)


def curve_from_manual(slope: float) -> Curve:
    """Linear fallback curve from a manual lux-per-percent coefficient."""
    slope = max(0.0, float(slope))
    return Curve([(0.0, 0.0), (100.0, slope * 100.0)], source="manual")
