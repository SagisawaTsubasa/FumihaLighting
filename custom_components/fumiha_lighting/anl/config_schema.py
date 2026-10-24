"""Shared anl field specs and validators — wizard forms AND the GUI config API.

单点维护字段范围/默认值：config_flow 在这里取规格生成 selector 表单，
web.py 的 /api/fumiha_lighting/anl/config 视图在这里取规格校验 GUI patch。
纯逻辑（不 import HA，时间解析用标准库），直接可测。

语义约定（写码前与 manager 消费端核对过）：
- ``transition_enabled`` 恒等于「任一过渡时长 > 0」，不再单独配置——
  manager 对它的消费只是再次门控 start/end_transition，派生值语义一致。
- ``extra_enabled`` 无人消费（旧向导遗留键），新写入不再包含。
- 逐灯覆盖数值键与全局参数键同名（OVR_BRIGHTNESS == CONF_BRIGHTNESS ==
  "brightness" 等），范围直接查 PARAMS_NUMERIC。
"""

from __future__ import annotations

import math
from datetime import datetime
from datetime import time as _dt_time

from .const import (
    ANCHOR_MODES,
    CONF_BRIGHTNESS,
    CONF_COLOR_TEMP_KELVIN,
    CONF_DAY_BRIGHTNESS,
    CONF_DAY_CI_LIGHTS,
    CONF_DAY_COLOR_TEMP_KELVIN,
    CONF_DAY_ENABLED,
    CONF_END_MODE,
    CONF_END_OFFSET,
    CONF_END_TIME,
    CONF_END_TRANSITION,
    CONF_EXTRAS,
    CONF_LIGHTS,
    CONF_ONLY_WHEN_ON,
    CONF_OVERRIDES,
    CONF_RESPECT_MANUAL,
    CONF_SETTLE_DELAY,
    CONF_START_MODE,
    CONF_START_OFFSET,
    CONF_START_TRANSITION,
    CONF_SUN_ENTITY,
    CONF_TOLERANCE_BRIGHTNESS,
    CONF_TOLERANCE_KELVIN,
    CONF_TRANSITION_ENABLED,
    CONF_TRANSITION_INTERVAL,
    CONF_TRIGGER_TIME,
    CONF_TURN_ON_LISTEN,
    CONF_VERIFY_DELAY,
    DEFAULT_BRIGHTNESS,
    DEFAULT_COLOR_TEMP_KELVIN,
    DEFAULT_DAY_BRIGHTNESS,
    DEFAULT_DAY_COLOR_TEMP_KELVIN,
    DEFAULT_END_OFFSET,
    DEFAULT_END_TIME,
    DEFAULT_EXTRA_BRIGHTNESS,
    DEFAULT_EXTRA_COLOR_TEMP_KELVIN,
    DEFAULT_SETTLE_DELAY,
    DEFAULT_START_OFFSET,
    DEFAULT_TOLERANCE_BRIGHTNESS,
    DEFAULT_TOLERANCE_KELVIN,
    DEFAULT_TRANSITION_INTERVAL,
    DEFAULT_TRIGGER_TIME,
    DEFAULT_VERIFY_DELAY,
    EXTRA_BRIGHTNESS,
    EXTRA_COLOR_TEMP_KELVIN,
    EXTRA_NAME,
    EXTRA_START,
    EXTRA_TRANSITION,
    MAX_EXTRA_PERIODS,
    MAX_TRANSITION_MINUTES,
    OVR_COLOR_TEMP_KELVIN,
    OVR_EXTRAS,
)

# 数值字段规格：(min, max, step, default, unit)。step 仅表单滑条消费。
TIMES_NUMERIC: dict[str, tuple] = {
    CONF_START_OFFSET: (-120, 120, 5, DEFAULT_START_OFFSET, "min"),
    CONF_END_OFFSET: (-120, 120, 5, DEFAULT_END_OFFSET, "min"),
    CONF_START_TRANSITION: (0, MAX_TRANSITION_MINUTES, 5, 0, "min"),
    CONF_END_TRANSITION: (0, MAX_TRANSITION_MINUTES, 5, 0, "min"),
    CONF_TRANSITION_INTERVAL: (1, 15, 1, DEFAULT_TRANSITION_INTERVAL, "min"),
}
TIMES_ENUM: dict[str, tuple] = {
    CONF_START_MODE: tuple(ANCHOR_MODES),
    CONF_END_MODE: tuple(ANCHOR_MODES),
}
# 值为默认时间（"HH:MM:SS"）
TIMES_TIME: dict[str, str] = {
    CONF_TRIGGER_TIME: DEFAULT_TRIGGER_TIME,
    CONF_END_TIME: DEFAULT_END_TIME,
}
TIMES_ENTITY = (CONF_SUN_ENTITY,)

PARAMS_NUMERIC: dict[str, tuple] = {
    CONF_BRIGHTNESS: (1, 100, 1, DEFAULT_BRIGHTNESS, "%"),
    CONF_COLOR_TEMP_KELVIN: (1500, 6500, 100, DEFAULT_COLOR_TEMP_KELVIN, "K"),
    CONF_DAY_BRIGHTNESS: (1, 100, 1, DEFAULT_DAY_BRIGHTNESS, "%"),
    CONF_DAY_COLOR_TEMP_KELVIN: (
        1500,
        6500,
        100,
        DEFAULT_DAY_COLOR_TEMP_KELVIN,
        "K",
    ),
    CONF_TOLERANCE_BRIGHTNESS: (0, 25, 1, DEFAULT_TOLERANCE_BRIGHTNESS, "%"),
    CONF_TOLERANCE_KELVIN: (0, 1000, 50, DEFAULT_TOLERANCE_KELVIN, "K"),
    CONF_VERIFY_DELAY: (0, 60, 1, DEFAULT_VERIFY_DELAY, "s"),
    CONF_SETTLE_DELAY: (0, 10, 0.5, DEFAULT_SETTLE_DELAY, "s"),
}
PARAMS_BOOL = (
    CONF_DAY_ENABLED,
    CONF_TURN_ON_LISTEN,
    CONF_ONLY_WHEN_ON,
    CONF_RESPECT_MANUAL,
)
FLOAT_FIELDS = frozenset({CONF_SETTLE_DELAY})

EXTRA_NUMERIC: dict[str, tuple] = {
    EXTRA_BRIGHTNESS: (1, 100, 1, DEFAULT_EXTRA_BRIGHTNESS, "%"),
    EXTRA_COLOR_TEMP_KELVIN: (
        1500,
        6500,
        100,
        DEFAULT_EXTRA_COLOR_TEMP_KELVIN,
        "K",
    ),
    EXTRA_TRANSITION: (0, MAX_TRANSITION_MINUTES, 5, 0, "min"),
}
# 逐灯覆盖的夜间/日间四个数值键与全局参数键同名，范围直接复用 PARAMS_NUMERIC；
# 四键全部可选（ANL-F040：值=全局不写覆盖），唯一硬约束是覆盖非空
OVR_KEYS = (
    CONF_BRIGHTNESS,
    OVR_COLOR_TEMP_KELVIN,
    CONF_DAY_BRIGHTNESS,
    CONF_DAY_COLOR_TEMP_KELVIN,
)

# GUI 可读写的全部键（GET 白名单；POST patch 最终落 options 的键集）
CONFIG_KEYS = (
    CONF_LIGHTS,
    CONF_START_MODE,
    CONF_END_MODE,
    CONF_TRIGGER_TIME,
    CONF_END_TIME,
    CONF_START_OFFSET,
    CONF_END_OFFSET,
    CONF_SUN_ENTITY,
    CONF_TRANSITION_ENABLED,
    CONF_START_TRANSITION,
    CONF_END_TRANSITION,
    CONF_TRANSITION_INTERVAL,
    CONF_DAY_ENABLED,
    CONF_BRIGHTNESS,
    CONF_COLOR_TEMP_KELVIN,
    CONF_DAY_BRIGHTNESS,
    CONF_DAY_COLOR_TEMP_KELVIN,
    CONF_DAY_CI_LIGHTS,
    CONF_TOLERANCE_BRIGHTNESS,
    CONF_TOLERANCE_KELVIN,
    CONF_VERIFY_DELAY,
    CONF_SETTLE_DELAY,
    CONF_TURN_ON_LISTEN,
    CONF_ONLY_WHEN_ON,
    CONF_RESPECT_MANUAL,
    CONF_EXTRAS,
    CONF_OVERRIDES,
)


def normalize_time(value) -> str | None:
    """'HH:MM' / 'HH:MM:SS' / datetime.time → 'HH:MM:SS'；非法返回 None。

    datetime.time 分支（ANL-F-015）：TimeSelector 提交后 HA 侧是 time 对象，
    向导边界与 GET 序列化都要归一化为字符串存储形状。
    """
    if isinstance(value, _dt_time):
        return value.strftime("%H:%M:%S")
    if not isinstance(value, str):
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(value.strip(), fmt).strftime("%H:%M:%S")
        except ValueError:
            continue
    return None


def _clean_num(key: str, value, spec: tuple):
    """单个数值字段校验。返回 (value, error)；int（settle_delay 为 float）。"""
    lo, hi, _step, _default, _unit = spec
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f"{key} 需为数字"
    if not math.isfinite(value):  # ANL-F-019：NaN/Inf 穿过比较会 500 或落库
        return None, f"{key} 需为有限数字"
    if value < lo or value > hi:
        return None, f"{key} 需在 {lo:g}–{hi:g} 之间"
    if key in FLOAT_FIELDS:
        return float(value), None
    return int(round(value)), None


def _num_check(spec_by_key: dict):
    def _check(payload, key):
        if key not in payload:
            return None, None  # patch 语义：缺省键跳过
        value, err = _clean_num(key, payload[key], spec_by_key[key])
        return (value, None) if err is None else (None, err)

    return _check


def _enum_check(spec_by_key: dict):
    def _check(payload, key):
        if key not in payload:
            return None, None
        value = payload[key]
        if value not in spec_by_key[key]:
            return None, f"{key} 需为 {'/'.join(spec_by_key[key])} 之一"
        return value, None

    return _check


def _time_check(spec_by_key: dict):
    def _check(payload, key):
        if key not in payload:
            return None, None
        normalized = normalize_time(payload[key])
        if normalized is None:
            return None, f"{key} 需为 HH:MM 或 HH:MM:SS 格式的时间"
        return normalized, None

    return _check


def _entity_check(payload, key):
    if key not in payload:
        return None, None
    value = payload[key]
    if not isinstance(value, str) or not value.strip():
        return None, f"{key} 需为非空实体 ID"
    return value.strip(), None


def _bool_check(payload, key):
    if key not in payload:
        return None, None
    if not isinstance(payload[key], bool):
        return None, f"{key} 需为布尔值"
    return payload[key], None


def _day_ci_lights_check(lights: set):
    """day_ci_lights 校验器工厂（V1.6.0 单灯化）：形状 + 灯集子集。

    列表 ⊆ 灯集（组内接管灯清单不得引用未选灯具——reconfigure 改灯后由
    flow 携带侧裁剪，GUI 写入侧硬拒绝）；空列表合法（=CI 关）。重复实体
    拒绝（F-070 同刻双时段同型：集合键里重复项是配置面错误，不静默去重）。
    fumiha_lighting 虚拟灯的注册表身份拒绝不在此处——本模块纯逻辑不
    import HA，注册表校验在 web.py POST 链做（F-058 虚拟灯三验同款语义）。
    """

    def _check(payload, key):
        if key not in payload:
            return None, None
        value = payload[key]
        if not isinstance(value, list) or any(
            not isinstance(e, str) for e in value
        ):
            return None, f"{key} 需为实体 ID 字符串列表"
        cleaned: list[str] = []
        for raw in value:
            entity = raw.strip()
            if not entity:
                return None, f"{key} 含空实体 ID"
            if entity not in lights:
                return None, f"未知灯具: {entity}"
            if entity in cleaned:
                return None, f"{key} 含重复实体: {entity}"
            cleaned.append(entity)
        return cleaned, None

    return _check


def _section(payload, allowed: dict, err_prefix: str):
    """分节校验骨架。allowed: key → 校验器(payload, key) -> (value, error)。"""
    if not isinstance(payload, dict) or not payload:
        return None, f"{err_prefix} 需为非空字典"
    unknown = sorted(set(payload) - set(allowed))
    if unknown:
        return None, f"{err_prefix} 含未知字段: {', '.join(unknown)}"
    clean: dict = {}
    for key, check in allowed.items():
        value, err = check(payload, key)
        if err is not None:
            return None, err
        if key in payload:
            clean[key] = value
    return clean, None


def validate_times(payload) -> tuple[dict | None, str | None]:
    """时间锚点分节：来源枚举 + 固定时间 + 偏移 + 过渡时长 + 太阳实体。"""
    allowed: dict = {k: _enum_check(TIMES_ENUM) for k in TIMES_ENUM}
    allowed.update({k: _time_check(TIMES_TIME) for k in TIMES_TIME})
    allowed.update({k: _num_check(TIMES_NUMERIC) for k in TIMES_NUMERIC})
    allowed.update({k: _entity_check for k in TIMES_ENTITY})
    return _section(payload, allowed, "times")


def validate_params(payload, lights: set) -> tuple[dict | None, str | None]:
    """参数分节：亮度/色温（含日间）+ 容差 + 延迟 + 行为开关 + 恒照度清单。

    lights 必填：day_ci_lights 的灯集子集校验依赖组内灯清单（V1.6.0 单灯
    化；web 层从条目合并配置取）。V1.7.0 反查推导：day_ci_virtual_light
    键废除（校验器删除，patch 携带该键按未知字段 400）；day_ci_lights 的
    区域归属校验（∈light_zone_index）在 web 层执行——本模块纯逻辑不
    import HA，schema 层保留形状/子集格式校验。
    """
    allowed: dict = {k: _num_check(PARAMS_NUMERIC) for k in PARAMS_NUMERIC}
    allowed.update({k: _bool_check for k in PARAMS_BOOL})
    allowed[CONF_DAY_CI_LIGHTS] = _day_ci_lights_check(lights)
    return _section(payload, allowed, "params")


def validate_extras(payload) -> tuple[list | None, str | None]:
    """额外时段：整表替换（GUI 管理增删改），0–5 项。

    未知键拒绝 + 数值键必填（ANL-F-023）：静默补默认会把拼写错误
    （如 color_temp → 3000K 默认值）吞成一次看似成功的写入。
    """
    if not isinstance(payload, list):
        return None, "extras 需为列表"
    if len(payload) > MAX_EXTRA_PERIODS:
        return None, f"额外时段最多 {MAX_EXTRA_PERIODS} 个"
    clean: list[dict] = []
    for i, item in enumerate(payload):
        if not isinstance(item, dict):
            return None, f"额外时段 {i + 1} 需为字典"
        unknown = set(item) - {EXTRA_NAME, EXTRA_START, *EXTRA_NUMERIC}
        if unknown:
            return None, (
                f"额外时段 {i + 1} 含未知字段: {', '.join(sorted(unknown))}"
            )
        name = item.get(EXTRA_NAME, "")
        if not isinstance(name, str):
            return None, f"额外时段 {i + 1} 的 name 需为字符串"
        start = normalize_time(item.get(EXTRA_START))
        if start is None:
            return None, f"额外时段 {i + 1} 的 start 需为 HH:MM 或 HH:MM:SS"
        missing = [k for k in EXTRA_NUMERIC if k not in item]
        if missing:
            return None, f"额外时段 {i + 1} 缺少 {', '.join(missing)}"
        extra: dict = {EXTRA_NAME: name.strip()[:60], EXTRA_START: start}
        for key, spec in EXTRA_NUMERIC.items():
            value, err = _clean_num(key, item[key], spec)
            if err is not None:
                return None, f"额外时段 {i + 1} 的 {err}"
            extra[key] = value
        clean.append(extra)
    # F-070：同刻双时段拒绝——双定时器同触发 + 自循环重排会互踩句柄、
    # 次日重复触发；_mode_at 对同刻锚点按列表序平局，参数归属也不确定
    starts = [e[EXTRA_START] for e in clean]
    if len(starts) != len(set(starts)):
        return None, "额外时段开始时间不能重复"
    return clean, None


def validate_overrides(
    payload, lights: set, extras_count: int
) -> tuple[dict | None, str | None]:
    """逐灯覆盖：{entity_id: 参数对}，entity 必须已选，整灯语义。

    四类键全部可选（ANL-F-040：夜间对不再必填——消费端 params_for 对缺失
    键回落全局值，GUI 按「值=全局不写覆盖」提交，只为真正改动的键建覆盖）；
    唯一硬约束是覆盖不得为空字典（空=该灯无任何覆盖，恢复全局走
    overrides_remove 分节，不在这里表达）。
    """
    if not isinstance(payload, dict) or not payload:
        return None, "overrides 需为非空字典"
    clean: dict = {}
    for entity, ovr in payload.items():
        if entity not in lights:
            return None, f"未知灯具: {entity}"
        if not isinstance(ovr, dict):
            return None, f"{entity} 的覆盖需为字典"
        unknown = set(ovr) - set(OVR_KEYS) - {OVR_EXTRAS}
        if unknown:
            return None, f"{entity} 的覆盖含未知字段: {', '.join(sorted(unknown))}"
        entry: dict = {}
        for key in OVR_KEYS:
            if key not in ovr:
                continue
            value, err = _clean_num(key, ovr[key], PARAMS_NUMERIC[key])
            if err is not None:
                return None, f"{entity} 的 {err}"
            entry[key] = value
        ovr_extras = ovr.get(OVR_EXTRAS, {})
        if not isinstance(ovr_extras, dict):
            return None, f"{entity} 的 extras 覆盖需为字典"
        clean_extras: dict = {}
        for idx, pair in ovr_extras.items():
            if not isinstance(idx, str) or not idx.isdigit():
                return None, f"{entity} 的 extras 键需为时段序号字符串"
            if int(idx) >= extras_count:
                return None, f"{entity} 的 extras 序号 {idx} 超出时段数量"
            if not isinstance(pair, dict):
                return None, f"{entity} 的时段 {idx} 覆盖需为字典"
            unknown_pair = set(pair) - {CONF_BRIGHTNESS, OVR_COLOR_TEMP_KELVIN}
            if unknown_pair:  # ANL-F-023：拼写错误不得静默丢弃
                return None, (
                    f"{entity} 的时段 {idx} 覆盖含未知字段: "
                    f"{', '.join(sorted(unknown_pair))}"
                )
            if CONF_BRIGHTNESS not in pair or OVR_COLOR_TEMP_KELVIN not in pair:
                return None, f"{entity} 的时段 {idx} 覆盖缺少亮度/色温"
            sub: dict = {}
            for key in (CONF_BRIGHTNESS, OVR_COLOR_TEMP_KELVIN):
                spec = EXTRA_NUMERIC.get(key)
                value, err = _clean_num(key, pair[key], spec)
                if err is not None:
                    return None, f"{entity} 的时段 {idx} {err}"
                sub[key] = value
            clean_extras[idx] = sub
        if clean_extras:
            entry[OVR_EXTRAS] = clean_extras
        if not entry:  # ANL-F-040：空覆盖 = 无任何改动，拒绝而非存空壳
            return None, f"{entity} 的覆盖为空（恢复全局请走 overrides_remove）"
        clean[entity] = entry
    return clean, None


def limits_payload() -> dict:
    """GET 响应的 limits 面：GUI 输入控件的 min/max/step 单一来源。"""

    def _num(specs: dict) -> dict:
        return {
            key: {"min": lo, "max": hi, "step": step, "default": default, "unit": unit}
            for key, (lo, hi, step, default, unit) in specs.items()
        }

    return {
        "times": _num(TIMES_NUMERIC),
        "params": _num(PARAMS_NUMERIC),
        "extras": _num(EXTRA_NUMERIC),
        "enums": {key: list(values) for key, values in TIMES_ENUM.items()},
        "max_extras": MAX_EXTRA_PERIODS,
    }
