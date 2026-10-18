"""Direct unit tests for anl.config_schema (ANL-F-021: the GUI config API's
validation layer was previously only covered indirectly via endpoints)."""

from __future__ import annotations

from datetime import time as dt_time

from custom_components.fumiha_lighting.anl.config_schema import (
    EXTRA_NUMERIC,
    PARAMS_NUMERIC,
    TIMES_NUMERIC,
    limits_payload,
    normalize_time,
    validate_extras,
    validate_overrides,
    validate_params,
    validate_times,
)

LIGHTS = {"light.a", "light.b"}


def _extra(**kw):
    item = {"name": "n", "start": "18:00", "brightness": 60,
            "color_temp_kelvin": 3000, "transition": 0}
    item.update(kw)
    return item


# ---- normalize_time -------------------------------------------------------


def test_normalize_time_accepts_both_formats():
    assert normalize_time("22:00") == "22:00:00"
    assert normalize_time("06:30:15") == "06:30:15"
    # strptime 对非补零小时宽容（"7:05" 合法），归一化补齐
    assert normalize_time(" 7:05 ") == "07:05:00"


def test_normalize_time_accepts_time_object():
    assert normalize_time(dt_time(22, 0)) == "22:00:00"
    assert normalize_time(dt_time(6, 30, 15)) == "06:30:15"


def test_normalize_time_rejects_garbage():
    assert normalize_time("25:00") is None
    assert normalize_time(None) is None
    assert normalize_time(123) is None
    assert normalize_time("abc") is None


# ---- validate_params ------------------------------------------------------


def test_validate_params_ok_and_rounding():
    clean, err = validate_params({"brightness": 25.4, "settle_delay": 1.5,
                                  "day_enabled": True})
    assert err is None
    assert clean["brightness"] == 25  # int round
    assert clean["settle_delay"] == 1.5  # float 保留
    assert clean["day_enabled"] is True


def test_validate_params_rejects_bool_as_number():
    clean, err = validate_params({"brightness": True})
    assert clean is None and "数字" in err


def test_validate_params_rejects_non_finite():
    for bad in (float("nan"), float("inf"), float("-inf")):
        clean, err = validate_params({"brightness": bad})
        assert clean is None and "有限" in err
        clean2, err2 = validate_params({"settle_delay": bad})
        assert clean2 is None and "有限" in err2  # float 字段同样拒绝（ANL-F-019）


def test_validate_params_rejects_out_of_range_and_unknown():
    assert validate_params({"brightness": 0})[1] is not None
    assert validate_params({"brightness": 101})[1] is not None
    assert "hax" in validate_params({"hax": 1})[1]


# ---- validate_times -------------------------------------------------------


def test_validate_times_ok_with_enum_and_time():
    clean, err = validate_times({
        "start_mode": "sunset", "end_mode": "fixed",
        "end_time": "06:00", "start_offset": -30,
    })
    assert err is None
    assert clean["end_time"] == "06:00:00"
    assert clean["start_offset"] == -30


def test_validate_times_rejects_bad_enum_and_time():
    assert "start_mode" in validate_times({"start_mode": "noon"})[1]
    assert "trigger_time" in validate_times({"trigger_time": "24:00"})[1]


# ---- validate_extras ------------------------------------------------------


def test_validate_extras_ok_and_name_truncated():
    clean, err = validate_extras([_extra(name="x" * 100)])
    assert err is None
    assert len(clean[0]["name"]) == 60
    assert clean[0]["start"] == "18:00:00"


def test_validate_extras_rejects_unknown_and_missing_keys():
    # 拼写错（ANL-F-023）
    assert "未知字段" in validate_extras([_extra(color_temp=1)])[1]
    missing = _extra()
    del missing["color_temp_kelvin"]
    assert "缺少" in validate_extras([missing])[1]


def test_validate_extras_rejects_over_five():
    assert "最多" in validate_extras([_extra()] * 6)[1]


# ---- validate_overrides ---------------------------------------------------


def test_validate_overrides_ok_with_extras_pair():
    clean, err = validate_overrides(
        {"light.a": {"brightness": 10, "color_temp_kelvin": 2700,
                     "extras": {"0": {"brightness": 40, "color_temp_kelvin": 2800}}}},
        LIGHTS, 1,
    )
    assert err is None
    assert clean["light.a"]["extras"]["0"]["brightness"] == 40


def test_validate_overrides_rejects_unknown_entity_and_empty():
    assert "未知灯具" in validate_overrides(
        {"light.z": {"brightness": 1, "color_temp_kelvin": 2}}, LIGHTS, 0)[1]
    # ANL-F-040：夜间对改可选，唯一硬约束是覆盖非空
    assert "覆盖为空" in validate_overrides(
        {"light.a": {}}, LIGHTS, 0)[1]
    clean, err = validate_overrides(
        {"light.a": {"day_brightness": 80}}, LIGHTS, 0)
    assert err is None and clean["light.a"] == {"day_brightness": 80}


def test_validate_overrides_rejects_unknown_pair_key_and_bad_index():
    bad_pair = {"light.a": {"brightness": 1, "color_temp_kelvin": 2700,
                            "extras": {"0": {"brightness": 1, "color_temp": 2}}}}
    assert "未知字段" in validate_overrides(bad_pair, LIGHTS, 1)[1]
    bad_idx = {"light.a": {"brightness": 1, "color_temp_kelvin": 2700,
                           "extras": {"3": {"brightness": 1, "color_temp_kelvin": 2}}}}
    assert "超出" in validate_overrides(bad_idx, LIGHTS, 1)[1]


# ---- limits_payload -------------------------------------------------------


def test_limits_payload_covers_all_specs():
    limits = limits_payload()
    assert set(limits["times"]) == set(TIMES_NUMERIC)
    assert set(limits["params"]) == set(PARAMS_NUMERIC)
    assert set(limits["extras"]) == set(EXTRA_NUMERIC)
    sample = limits["params"]["brightness"]
    assert sample == {"min": 1, "max": 100, "step": 1, "default": 25, "unit": "%"}
    assert limits["max_extras"] == 5
    assert limits["enums"]["start_mode"] == ["fixed", "sunset", "sunrise"]
