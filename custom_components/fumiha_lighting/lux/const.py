"""Constants for the lux_balance integration."""

from collections.abc import Mapping
from typing import Any

DOMAIN = "fumiha_lighting"
PLATFORMS = ["light", "number", "button", "sensor"]

# config entry data keys
CONF_ZONE_NAME = "zone_name"
CONF_LIGHT_ENTITY = "light_entity"
CONF_LUX_ENTITY = "lux_entity"

# options keys
CONF_TARGET_LUX = "target_lux"
CONF_SCAN_INTERVAL_S = "scan_interval_s"
CONF_DEADBAND_PCT = "deadband_pct"
CONF_MIN_BRIGHTNESS_PCT = "min_brightness_pct"
CONF_CAL_STEP_PCT = "calibrate_step_pct"
CONF_AUTO_TURN_OFF = "auto_turn_off"
CONF_MANUAL_SLOPE = "manual_slope"

DEFAULT_TARGET_LUX = 150.0
# 单一来源：number 实体（number.py）、web 页面（web.py 序列化 target_*）都以
# 这三个常量为界（页面输入校验消费 payload，不再各自硬编码）。
TARGET_MIN_LUX = 10.0
TARGET_MAX_LUX = 500.0
TARGET_STEP_LUX = 5.0
DEFAULT_SCAN_INTERVAL_S = 45
DEFAULT_DEADBAND_PCT = 5.0
DEFAULT_MIN_BRIGHTNESS_PCT = 5.0
DEFAULT_CAL_STEP_PCT = 5
DEFAULT_AUTO_TURN_OFF = False
DEFAULT_MANUAL_SLOPE = 0.0

# runtime modes (also used as sensor state values, keep lowercase)
MODE_IDLE = "idle"
MODE_ACTIVE = "active"
MODE_CALIBRATING = "calibrating"
MODE_PASSTHROUGH = "passthrough"

# calibration timing (per DESIGN.md §5.1, grounded in 2026-09-25 measurements)
CAL_FIRST_REPORT_TIMEOUT_S = 60
CAL_QUIET_PERIOD_S = 20
CAL_CMD_SETTLE_S = 2
CAL_POLL_INTERVAL_S = 1
CAL_TIMEOUT_UPGRADE_STREAK = 2
CAL_MIN_STEP_PCT = 2
CAL_MIN_USEFUL_LUX = 5.0  # calibrated curve below this is treated as no response

DECISION_RING_SIZE = 50

SERVICE_CALIBRATE = "calibrate"


def merged_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Options merged over defaults; single source of truth for runtime."""
    return {
        CONF_TARGET_LUX: options.get(CONF_TARGET_LUX, DEFAULT_TARGET_LUX),
        CONF_SCAN_INTERVAL_S: options.get(
            CONF_SCAN_INTERVAL_S, DEFAULT_SCAN_INTERVAL_S
        ),
        CONF_DEADBAND_PCT: options.get(CONF_DEADBAND_PCT, DEFAULT_DEADBAND_PCT),
        CONF_MIN_BRIGHTNESS_PCT: options.get(
            CONF_MIN_BRIGHTNESS_PCT, DEFAULT_MIN_BRIGHTNESS_PCT
        ),
        CONF_CAL_STEP_PCT: options.get(CONF_CAL_STEP_PCT, DEFAULT_CAL_STEP_PCT),
        CONF_AUTO_TURN_OFF: options.get(CONF_AUTO_TURN_OFF, DEFAULT_AUTO_TURN_OFF),
        CONF_MANUAL_SLOPE: options.get(CONF_MANUAL_SLOPE, DEFAULT_MANUAL_SLOPE),
    }  # 注：CONF_TARGET_LUX 只作启动初值；运行时目标以 number 实体为准
