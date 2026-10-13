"""Unit tests for the pure logic of the lux_balance integration."""

import pytest
from logic import (
    Curve,
    build_curve,
    calibration_steps,
    combine_passes,
    curve_from_manual,
    decide,
    enforce_monotonic,
    smooth3,
    subtract_baseline,
)


def linear_curve(slope: float) -> Curve:
    return Curve([(0.0, 0.0), (100.0, slope * 100.0)])


class TestCurve:
    def test_interpolation_midpoints(self):
        curve = Curve([(0.0, 0.0), (50.0, 100.0), (100.0, 150.0)])
        assert curve.lux_at(25) == pytest.approx(50)
        assert curve.lux_at(75) == pytest.approx(125)
        assert curve.lux_at(0) == 0
        assert curve.lux_at(100) == pytest.approx(150)

    def test_lux_at_clamps_out_of_range(self):
        curve = linear_curve(1.0)
        assert curve.lux_at(-5) == 0
        assert curve.lux_at(120) == pytest.approx(100)

    def test_pct_for_inverse(self):
        curve = linear_curve(1.5)
        assert curve.pct_for(0) == 0
        assert curve.pct_for(-1) == 0
        assert curve.pct_for(75) == pytest.approx(50)
        assert curve.pct_for(150) == pytest.approx(100)
        assert curve.pct_for(999) == 100

    def test_roundtrip(self):
        curve = Curve([(0.0, 0.0), (20.0, 40.0), (60.0, 90.0), (100.0, 160.0)])
        for pct in range(0, 101, 5):
            assert curve.pct_for(curve.lux_at(pct)) == pytest.approx(pct, abs=0.01)

    def test_inserts_zero_anchor(self):
        curve = Curve([(5.0, 10.0), (100.0, 200.0)])
        assert curve.points[0] == (0.0, 0.0)

    def test_merges_duplicate_percents(self):
        curve = Curve([(50.0, 10.0), (50.0, 20.0), (100.0, 100.0)])
        pcts = [p for p, _ in curve.points]
        assert len(pcts) == len(set(pcts))
        assert curve.points[1] == (50.0, 15.0)

    def test_slopes(self):
        curve = linear_curve(2.0)
        assert curve.max_lux == pytest.approx(200)
        assert curve.slope_20_80 == pytest.approx(2.0)

    def test_empty_points_rejected(self):
        with pytest.raises(ValueError):
            Curve([])


class TestDecide:
    def test_off_when_ambient_above_target(self):
        decision = decide(linear_curve(1.0), 200, 0, 150, 5, 5)
        assert decision.action == "off"
        assert decision.ambient_lux == pytest.approx(200)
        assert decision.needed_lux == pytest.approx(-50)

    def test_ambient_subtracts_current_contribution(self):
        # light at 50% on a 1 lux/% curve contributes 50; measured 150 -> 100 ambient
        decision = decide(linear_curve(1.0), 150, 50, 150, 5, 5)
        assert decision.ambient_lux == pytest.approx(100)
        assert decision.needed_lux == pytest.approx(50)
        assert decision.action == "noop"  # wants 50%, already at 50%

    def test_set_when_off_below_target(self):
        decision = decide(linear_curve(1.0), 20, 0, 150, 5, 5)
        assert decision.action == "set"
        assert decision.pct == pytest.approx(100)  # needs 130 lux, curve caps at 100

    def test_set_from_off_ignores_deadband(self):
        # computed 2% (< deadband 5) but light is off: must still turn on
        decision = decide(linear_curve(1.0), 148, 0, 150, 5, 1)
        assert decision.action == "set"
        assert decision.pct == pytest.approx(2)

    def test_min_pct_clamp(self):
        decision = decide(linear_curve(1.0), 0, 0, 2, 5, 5)
        assert decision.action == "set"
        assert decision.pct == pytest.approx(5)

    def test_max_pct_clamp(self):
        decision = decide(linear_curve(1.0), 0, 30, 500, 5, 5)
        assert decision.action == "set"
        assert decision.pct == pytest.approx(100)

    def test_deadband_noop_when_on(self):
        decision = decide(linear_curve(1.0), 152, 50, 150, 5, 5)
        assert decision.action == "noop"


class TestCalibrationSynthesis:
    def test_calibration_steps(self):
        assert calibration_steps(5) == list(range(0, 101, 5))
        assert calibration_steps(10) == list(range(0, 101, 10))
        assert calibration_steps(0) == calibration_steps(2)
        assert calibration_steps(99) == calibration_steps(20)

    def test_calibration_steps_appends_100(self):
        # Steps that do not divide 100 must still reach the 100% point.
        for step in (3, 6, 7, 9, 13, 17, 19):
            steps = calibration_steps(step)
            assert steps[-1] == 100
            assert steps[-2] < 100

    def test_combine_passes_averages_and_keeps_singles(self):
        result = dict(combine_passes({20: 120.0, 40: 220.0}, {20: 110.0}))
        assert result[20] == pytest.approx(115)
        assert result[40] == pytest.approx(220)

    def test_subtract_baseline_floors_at_zero(self):
        result = subtract_baseline([(5, 8.0), (10, 24.0)], 10.0)
        assert result == [(5, 0.0), (10, 14.0)]

    def test_smooth3_averages_neighbours(self):
        points = [(5, 10.0), (10, 30.0), (15, 20.0), (20, 21.0)]
        smoothed = dict(smooth3(points))
        assert smoothed[5] == pytest.approx(50 / 3)
        assert smoothed[10] == pytest.approx(20)
        assert smoothed[15] == pytest.approx(71 / 3)
        assert smoothed[20] == pytest.approx(62 / 3)

    def test_smooth3_passthrough_short(self):
        points = [(5, 1.0), (10, 2.0)]
        assert smooth3(points) == points

    def test_enforce_monotonic_pools_inversions(self):
        points = [(5, 10.0), (10, 5.0), (15, 20.0)]
        fitted = enforce_monotonic(points)
        values = [v for _, v in fitted]
        assert values == sorted(values)
        assert values[0] == pytest.approx(7.5)
        assert values[2] == pytest.approx(20)

    def test_enforce_monotonic_keeps_sorted_input(self):
        points = [(5, 1.0), (10, 2.0), (15, 3.0)]
        assert enforce_monotonic(points) == points

    def test_build_curve_full_pipeline(self):
        raw = combine_passes({5: 18.0, 10: 34.0}, {5: 22.0, 10: 36.0})
        pts = enforce_monotonic(smooth3(subtract_baseline(raw, 5.0)))
        curve = build_curve(pts)
        assert curve.points[0] == (0.0, 0.0)
        assert curve.source == "calibrated"
        assert curve.lux_at(5) == pytest.approx(15)

    def test_curve_from_manual(self):
        curve = curve_from_manual(1.5)
        assert curve.source == "manual"
        assert curve.max_lux == pytest.approx(150)
        assert curve.pct_for(75) == pytest.approx(50)

    def test_curve_from_manual_zero_is_linear_zero(self):
        curve = curve_from_manual(0)
        assert curve.pct_for(1) == 100  # anything needed still saturates the table
