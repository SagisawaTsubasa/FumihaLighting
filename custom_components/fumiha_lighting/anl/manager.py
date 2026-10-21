"""Per-light state machine manager for HA Auto Night Light."""

from __future__ import annotations

import asyncio
import copy
import enum
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import homeassistant.util.dt as dt_util
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_TRANSITION,
)
from homeassistant.components.light import (
    DOMAIN as LIGHT_DOMAIN,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    SUN_EVENT_SUNRISE,
    SUN_EVENT_SUNSET,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_time,
    async_track_state_change_event,
)
from homeassistant.helpers.sun import get_astral_event_date

from .const import (
    ANCHOR_FIXED,
    ANCHOR_SUNRISE,
    ANCHOR_SUNSET,
    CONF_BRIGHTNESS,
    CONF_COLOR_TEMP_KELVIN,
    CONF_DAY_BRIGHTNESS,
    CONF_DAY_CI_ENABLED,
    CONF_DAY_CI_VIRTUAL_LIGHT,
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
    DEFAULT_RESPECT_MANUAL,
    DEFAULT_SETTLE_DELAY,
    DEFAULT_START_OFFSET,
    DEFAULT_SUN_ENTITY,
    DEFAULT_TOLERANCE_BRIGHTNESS,
    DEFAULT_TOLERANCE_KELVIN,
    DEFAULT_TRANSITION_INTERVAL,
    DEFAULT_TRIGGER_TIME,
    DEFAULT_TURN_ON_LISTEN,
    DEFAULT_VERIFY_DELAY,
    EXTRA_BRIGHTNESS,
    EXTRA_COLOR_TEMP_KELVIN,
    EXTRA_START,
    EXTRA_TRANSITION,
    MODE_DAY,
    MODE_EXTRA_PREFIX,
    MODE_NIGHT,
    OVR_BRIGHTNESS,
    OVR_COLOR_TEMP_KELVIN,
    OVR_DAY_BRIGHTNESS,
    OVR_DAY_COLOR_TEMP_KELVIN,
    OVR_EXTRA_BRIGHTNESS,
    OVR_EXTRA_COLOR_TEMP_KELVIN,
    OVR_EXTRAS,
)
from .schedule import lerp_params, step_datetimes, upcoming_band_start

_LOGGER = logging.getLogger(__name__)

_NOT_IN_BAND = object()  # _band_params 哨兵：当前不在任何过渡带内

# F-076：归还侧 lux mode 白名单。虚拟灯 attributes["mode"]（lux/light.py
# extra_state_attributes 实测键名）取值 active/passthrough/calibrating/idle
# （lux/const.py:49-52；runtime._on_light_event 用同款二元组判「闭环在跑」）。
# 只有闭环真在跑才归还：idle 无闭环可放；calibrating 时 lux 对 turn_off 只置
# _cal_pending_off、扫频结束跳过亮度恢复（runtime.py:565-571、738）——此时
# 打扰会让灯停在校准末档；None（非 lux 灯/属性缺失/实体缺失）一律不动。
_CI_HANDBACK_LUX_MODES = ("active", "passthrough")


class LightState(enum.Enum):
    """State machine states for a single light."""

    IDLE = "idle"  # 等待下一次定时触发
    TURN_ON_PENDING = "turn_on_pending"  # 监听到开灯，等待属性稳定
    PENDING = "pending"  # 触发时间已到，待检查当前状态
    MATCHED = "matched"  # 当前状态已符合预期，跳过控制
    SETTING = "setting"  # 已下发控制指令，等待验证
    VERIFIED = "verified"  # 控制后验证通过
    MISMATCH = "mismatch"  # 控制后仍不符合预期
    OFFLINE = "offline"  # 实体不可用，本轮跳过
    SKIPPED_OFF = "skipped_off"  # 灯当前关闭且配置为不主动开灯


@dataclass
class LightMachine:
    """State machine context for one light."""

    entity_id: str
    state: LightState = LightState.IDLE
    last_error: str | None = field(default=None)
    target: tuple[int, int] | None = field(default=None)  # 最近下发的 (亮度, 色温)
    # 过渡带内状态（v2.1.0 定点调度引擎）
    band_key: tuple | None = field(default=None)  # 当前所在带身份
    expected: tuple[int, int] | None = field(default=None)  # 本带最近预期值
    applied: tuple[int, int] | None = field(default=None)  # 我方最后成功下发的参数
    mismatch_steps: int = field(default=0)  # 连续偏离预期的步数
    manual_override: bool = field(default=False)  # 本带内已被手动接管


@dataclass(frozen=True)
class PlanStep:
    """One scheduled transition step for one light.

    entrance=True 表示该步是带入口（factor=0）：带入口重置预期值为本步
    目标；非入口首观测步（带中途入表/重建）重置为 None——跳过一次接管
    判定，避免把灯具原生渐变的自然滞后误判成手动调节。

    band_key = (target_mode, 带锚点日期 ISO, from_mode) 三元组（F-075）：
    [0] 带目标模式、[2] 带起点时刻解析出的模式——extra 屏障守卫靠 [2]
    区分「带起点落在既有 extra 内」（from==该 extra → 照跑）与「带中途
    新 extra 介入」（from!=该 extra → 让位）。
    """

    entity_id: str
    brightness: int
    kelvin: int
    band_key: tuple
    entrance: bool = False


class NightLightManager:
    """Manage scheduled trigger and per-light state machines."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize from config entry."""
        self.hass = hass
        self.entry = entry
        data = {**entry.data, **entry.options}
        self.lights: list[str] = data[CONF_LIGHTS]
        self.sun_entity: str = data.get(CONF_SUN_ENTITY, DEFAULT_SUN_ENTITY)
        self.start_mode: str = data.get(CONF_START_MODE, ANCHOR_SUNSET)
        self.start_offset: int = data.get(CONF_START_OFFSET, DEFAULT_START_OFFSET)
        self.end_mode: str = data.get(CONF_END_MODE, ANCHOR_SUNRISE)
        self.end_offset: int = data.get(CONF_END_OFFSET, DEFAULT_END_OFFSET)
        self.trigger_time: str = data.get(CONF_TRIGGER_TIME, DEFAULT_TRIGGER_TIME)
        self.end_time: str = data.get(CONF_END_TIME, DEFAULT_END_TIME)
        self.extras: list[dict] = [dict(e) for e in data.get(CONF_EXTRAS, [])]
        self.transition_enabled: bool = data.get(CONF_TRANSITION_ENABLED, False)
        self.start_transition: int = (
            int(data.get(CONF_START_TRANSITION, 0)) if self.transition_enabled else 0
        )
        self.end_transition: int = (
            int(data.get(CONF_END_TRANSITION, 0))
            if self.transition_enabled and data.get(CONF_DAY_ENABLED, False)
            else 0
        )
        self.transition_interval: int = int(
            data.get(CONF_TRANSITION_INTERVAL, DEFAULT_TRANSITION_INTERVAL)
        )
        self.respect_manual: bool = data.get(
            CONF_RESPECT_MANUAL, DEFAULT_RESPECT_MANUAL
        )
        self.brightness: int = data.get(CONF_BRIGHTNESS, DEFAULT_BRIGHTNESS)
        self.kelvin: int = data.get(CONF_COLOR_TEMP_KELVIN, DEFAULT_COLOR_TEMP_KELVIN)
        self.tol_brightness: int = data.get(
            CONF_TOLERANCE_BRIGHTNESS, DEFAULT_TOLERANCE_BRIGHTNESS
        )
        self.tol_kelvin: int = data.get(CONF_TOLERANCE_KELVIN, DEFAULT_TOLERANCE_KELVIN)
        self.verify_delay: int = data.get(CONF_VERIFY_DELAY, DEFAULT_VERIFY_DELAY)
        self.only_when_on: bool = data.get(CONF_ONLY_WHEN_ON, False)
        self.turn_on_listen: bool = data.get(
            CONF_TURN_ON_LISTEN, DEFAULT_TURN_ON_LISTEN
        )
        self.settle_delay: float = data.get(CONF_SETTLE_DELAY, DEFAULT_SETTLE_DELAY)
        self.day_enabled: bool = data.get(CONF_DAY_ENABLED, False)
        self.day_brightness: int = data.get(CONF_DAY_BRIGHTNESS, DEFAULT_DAY_BRIGHTNESS)
        self.day_kelvin: int = data.get(
            CONF_DAY_COLOR_TEMP_KELVIN, DEFAULT_DAY_COLOR_TEMP_KELVIN
        )
        # 日间恒照度联动（V1.5.0）：CI 开时日间段不直控真实灯，改为驱动
        # 恒照度虚拟灯（lux 闭环接管）；日间参数与逐灯日间覆盖保留存储、
        # 不再消费。虚拟灯只存 entity_id，运行时 hass.states.get 判存在性。
        self.day_ci_enabled: bool = bool(data.get(CONF_DAY_CI_ENABLED, False))
        self.day_ci_virtual_light: str = str(
            data.get(CONF_DAY_CI_VIRTUAL_LIGHT, "") or ""
        )
        # 逐灯覆盖做深拷贝，避免就地修改 entry.data；同时清理已缩减时段的残留索引
        self.overrides: dict[str, dict] = copy.deepcopy(data.get(CONF_OVERRIDES, {}))
        for ovr in self.overrides.values():
            extras_ovr = ovr.get(OVR_EXTRAS)
            if isinstance(extras_ovr, dict):
                stale = [
                    key
                    for key in extras_ovr
                    if not str(key).isdigit() or int(key) >= len(self.extras)
                ]
                for key in stale:
                    del extras_ovr[key]
        self.machines: dict[str, LightMachine] = {
            light: LightMachine(entity_id=light) for light in self.lights
        }
        self._unsub_time = None
        self._unsub_state = None
        self._unsub_sun = None
        # 定点调度计划：步进时刻 → 取消句柄（v2.1.0 替代间隔轮询）
        self._plan: dict[datetime, list[PlanStep]] = {}
        self._plan_unsub: dict[datetime, callable] = {}
        # 额外时段起点定时器（F-059）：时段序号 → 取消句柄——transition=0
        # 的时段没有计划步，参数按时生效只靠这里的定点触发
        self._extra_unsubs: dict[int, callable] = {}
        # async_call_later 句柄集中管理，stop() 时全部取消 (M1)
        self._pending_delays: set = set()
        self._stopped = False
        self._warned_sun_missing: set[str] = set()
        self._warned_sun_fallback = False
        self._warned_bad_anchor = False
        self._warned_ci = False

    @property
    def _has_transitions(self) -> bool:
        """Return True if any anchor has a transition band configured."""
        if self.start_transition > 0 or self.end_transition > 0:
            return True
        return any(int(e.get(EXTRA_TRANSITION, 0) or 0) > 0 for e in self.extras)

    @property
    def transitions_active(self) -> bool:
        """页面「过渡带开启」chip 判据（ANL-F-041 总闸语义 + F-062 CI 修正）。

        必须读生效总闸而非 transition_enabled 存储派生值（后者在日间关闭时
        不反映 end_transition 被 manager 归零的事实）；且 CI 配置开时日间侧
        带被滤掉——仅剩日间侧带时页面不得谎报「过渡中」，总闸按生效带算。
        CI 关时生效带==原始带，语义不变。_effective_transition_bands 是纯
        查表+锚点解析，快照视图 5s 轮询成本与 anchor_snapshot 同量级；
        web 层禁调私有（ANL-F-003 家规）。
        """
        if not self._has_transitions:
            return False
        return bool(self._effective_transition_bands())

    # ------------------------------------------------------- 日间恒照度联动

    def _ci_configured(self) -> bool:
        """CI 配置面判据：日间模式开 + CI 开关开 + 虚拟灯已配置。

        F-065：CI 是日间子功能——day_enabled 关闭时日间段根本不存在
        （_anchor_modes 不产生 MODE_DAY），运行时零虚拟灯调用（夜间/extra
        归还分支与 _async_extra_start 同判据，不会误关用户手开的虚拟灯）；
        本判据须与之一致，web 快照 day_ci_active 同源（不再谎报接管）。
        """
        return bool(
            self.day_enabled
            and self.day_ci_enabled
            and self.day_ci_virtual_light
        )

    # 公开别名（F-061）：web 序列化要报 day_ci_active，web 层禁调私有
    # （ANL-F-003 家规；紧挨 transitions_active 的家规位置——受类体定义
    # 序约束落在 _ci_configured 之后）
    ci_configured = _ci_configured

    def _ci_active_for(self, mode: str | None) -> bool:
        """CI 生效判据：配置开（含日间模式，F-065）且 当前解析时段 == 日间。"""
        return self._ci_configured() and mode == MODE_DAY

    def _virtual_lux_mode(self) -> str | None:
        """Read the virtual light's lux runtime mode attribute (F-076).

        虚拟灯 state 是真灯镜像（lux/light.py is_on→真灯 state）——判
        「lux 闭环是否在跑」必须读它暴露的 mode 属性而非 state（否则夜间
        ANL 自己把真灯打开也会被误判成「用户手开了虚拟灯」）。实体缺失或
        属性缺失返回 None，调用方按不动处理。
        """
        state = self.hass.states.get(self.day_ci_virtual_light)
        if state is None:
            return None
        return state.attributes.get("mode")

    async def _async_ensure_virtual(self, on: bool) -> bool:
        """Switch the constant-illuminance virtual light on/off; return success.

        turn_on 绝不带亮度/亮度百分比：带亮度会落 lux passthrough 直控真实灯
        （lux/runtime.py:499-511 实证），不带亮度才进 ACTIVE 闭环
        （runtime.py:512）；turn_off 是 lux 纯放手语义（mode→IDLE、对真实
        灯零下发，runtime.py:562-576），夜间锚点据此安全归还。
        F-068 状态预检：实体缺失或 unavailable/unknown 时先于服务调用失败
        ——light.turn_on 对不存在 entity_id 不抛异常（服务层静默忽略未匹配
        目标），只看「服务未抛异常」会把假成功当接管。服务失败：首次
        WARNING（_warned_ci 每 manager 实例节流一次，同 _warned_sun_fallback
        模式；预检与异常共用同一节流旗标），后续同实例内 DEBUG；返回 False
        由调用方回落日间参数直控——错误必须响但保可用。
        服务后二次校验 STATE_ON 不做（R2 审查建议，有意偏离）：blocking
        返回时实体状态可能尚未刷新，会造成假失败；lux 闭环自会处理灯没开。
        """
        state = self.hass.states.get(self.day_ci_virtual_light)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            # F-079：文案按方向区分——只有接管（on=True）失败才写「回落日间
            # 参数直控」；归还（on=False）失败时调用方按当前时段（夜间/extra）
            # 参数继续，不存在日间回落，写「回落日间」会误导排查方向。
            tail = (
                "改回落日间参数直控"
                if on
                else "归还跳过（虚拟灯状态未知），按当前时段参数继续"
            )
            verb = "开启" if on else "关闭"
            if not self._warned_ci:
                self._warned_ci = True
                _LOGGER.warning(
                    "恒照度虚拟灯 %s 不可用，%s %s",
                    self.day_ci_virtual_light,
                    verb,
                    tail,
                )
            else:
                _LOGGER.debug(
                    "恒照度虚拟灯 %s 仍不可用，%s %s",
                    self.day_ci_virtual_light,
                    verb,
                    tail,
                )
            return False
        service = SERVICE_TURN_ON if on else SERVICE_TURN_OFF
        try:
            await self.hass.services.async_call(
                LIGHT_DOMAIN,
                service,
                {ATTR_ENTITY_ID: self.day_ci_virtual_light},
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001
            verb = "开启" if on else "关闭"
            tail = (
                "本组回落日间参数直控"
                if on
                else "归还跳过（虚拟灯状态未知），按当前时段参数继续"
            )
            if not self._warned_ci:
                self._warned_ci = True
                _LOGGER.warning(
                    "恒照度虚拟灯 %s %s 失败：%s（%s）",
                    self.day_ci_virtual_light,
                    verb,
                    err,
                    tail,
                )
            else:
                _LOGGER.debug(
                    "恒照度虚拟灯 %s %s 仍失败：%s（%s）",
                    self.day_ci_virtual_light,
                    verb,
                    err,
                    tail,
                )
            return False
        return True

    def _effective_transition_bands(self) -> list[tuple]:
        """Transition bands with day-side bands dropped while CI is configured.

        CI 下日间参数被忽略，趋向日间参数的插值同样忽略——target/from 含
        MODE_DAY 的带不生成步进（night↔extra 过渡不受影响），带中途时段
        解析回纯锚点参数。配置重叠告警（_warn_overlapping_bands）仍用原始
        _transition_bands：它面向配置面，不做 CI 过滤。
        """
        bands = self._transition_bands()
        if not self._ci_configured():
            return bands
        filtered: list[tuple] = []
        for anchor_min, target_mode, dur in bands:
            from_mode = self._mode_at((anchor_min - dur) % 1440)
            if target_mode == MODE_DAY or from_mode == MODE_DAY:
                continue
            filtered.append((anchor_min, target_mode, dur))
        return filtered

    def params_for(self, entity_id: str, mode: str) -> tuple[int, int]:
        """Resolve effective (brightness, kelvin) for a light, applying overrides."""
        ovr = self.overrides.get(entity_id, {})
        if mode.startswith(MODE_EXTRA_PREFIX):
            idx = int(mode[len(MODE_EXTRA_PREFIX) :])
            extra = self.extras[idx]
            ovr_extra = ovr.get(OVR_EXTRAS, {}).get(str(idx), {})
            return (
                ovr_extra.get(
                    OVR_EXTRA_BRIGHTNESS,
                    extra.get(EXTRA_BRIGHTNESS, DEFAULT_EXTRA_BRIGHTNESS),
                ),
                ovr_extra.get(
                    OVR_EXTRA_COLOR_TEMP_KELVIN,
                    extra.get(EXTRA_COLOR_TEMP_KELVIN, DEFAULT_EXTRA_COLOR_TEMP_KELVIN),
                ),
            )
        if mode == MODE_DAY:
            return (
                ovr.get(OVR_DAY_BRIGHTNESS, self.day_brightness),
                ovr.get(OVR_DAY_COLOR_TEMP_KELVIN, self.day_kelvin),
            )
        return (
            ovr.get(OVR_BRIGHTNESS, self.brightness),
            ovr.get(OVR_COLOR_TEMP_KELVIN, self.kelvin),
        )

    def start(self) -> None:
        """Schedule the daily trigger at the night-start anchor."""
        self._stopped = False
        self._schedule_trigger()
        if self._uses_custom_sun_entity():
            self._unsub_sun = async_track_state_change_event(
                self.hass, [self.sun_entity], self._async_sun_entity_changed
            )
        if self._has_transitions:
            self._rebuild_day_plan()
            self._warn_overlapping_bands()
            _LOGGER.info(
                "Transition bands scheduled at fixed step points (%d min interval)",
                self.transition_interval,
            )
        # 额外时段起点定时器无条件排（F-059）：extras 可以没有过渡带，
        # 「0=立即」时段的参数按时生效只靠这里的定时器
        self._schedule_extra_starts()
        if self.turn_on_listen:
            self._unsub_state = async_track_state_change_event(
                self.hass, self.lights, self._async_light_state_changed
            )
        _LOGGER.info(
            "Auto night light started for %s (start=%s/%s, end=%s/%s)",
            self.lights,
            self.start_mode,
            self.trigger_time,
            self.end_mode,
            self.end_time,
        )

    def stop(self) -> None:
        """Cancel the schedule, the listeners and every pending delay."""
        self._stopped = True
        for unsub in (
            self._unsub_time,
            self._unsub_state,
            self._unsub_sun,
        ):
            if unsub is not None:
                unsub()
        self._unsub_time = None
        self._unsub_state = None
        self._unsub_sun = None
        for unsub in self._plan_unsub.values():
            unsub()
        self._plan_unsub.clear()
        self._plan.clear()
        for unsub in self._extra_unsubs.values():
            unsub()
        self._extra_unsubs.clear()
        for cancel in self._pending_delays:
            cancel()
        self._pending_delays.clear()

    def _delay(self, delay: float, action) -> None:
        """Schedule ``action`` after ``delay`` seconds; tracked for stop() (M1)."""
        if self._stopped:
            return
        handle = None

        @callback
        def _wrap(now):
            self._pending_delays.discard(handle)
            action(now)

        handle = async_call_later(self.hass, delay, _wrap)
        self._pending_delays.add(handle)

    def _uses_custom_sun_entity(self) -> bool:
        """Return True if any anchor relies on a non-default sun entity."""
        if self.sun_entity == DEFAULT_SUN_ENTITY:
            return False
        return self.start_mode != ANCHOR_FIXED or self.end_mode != ANCHOR_FIXED

    def _sun_dt(self, event_attr: str, sun_event: str, date) -> datetime | None:
        """Resolve a sun event datetime.

        非默认实体读其 next_setting/next_rising 属性；
        默认 sun.sun（或属性缺失回退）用 HA 天文计算。
        """
        if self.sun_entity != DEFAULT_SUN_ENTITY:
            state = self.hass.states.get(self.sun_entity)
            if state is not None:
                value = state.attributes.get(event_attr)
                if isinstance(value, datetime):
                    # sun.sun 的 next_* 属性是原生 datetime 对象
                    return value
                parsed = (
                    dt_util.parse_datetime(value) if isinstance(value, str) else None
                )
                if parsed is not None:
                    return parsed
            if event_attr not in self._warned_sun_missing:
                self._warned_sun_missing.add(event_attr)
                _LOGGER.warning(
                    "%s missing %s, falling back to astral calculation",
                    self.sun_entity,
                    event_attr,
                )
            else:
                _LOGGER.debug(
                    "%s still missing %s, using astral calculation",
                    self.sun_entity,
                    event_attr,
                )
        try:
            return get_astral_event_date(self.hass, sun_event, date)
        except Exception as err:  # noqa: BLE001
            # Snapshot view reaches here up to 4×/poll (ANL-F-017): throttle
            # to first sighting, same as the sun-fallback warning.
            if not self._warned_sun_fallback:
                self._warned_sun_fallback = True
                _LOGGER.warning("Sun event %s unavailable: %s", sun_event, err)
            return None

    def _anchor_time(self, mode: str, fixed: str, offset: int):
        """Resolve one anchor's time-of-day for mode resolution."""
        if mode == ANCHOR_FIXED:
            return dt_util.parse_time(fixed)
        event_attr = "next_setting" if mode == ANCHOR_SUNSET else "next_rising"
        sun_event = SUN_EVENT_SUNSET if mode == ANCHOR_SUNSET else SUN_EVENT_SUNRISE
        value = self._sun_dt(event_attr, sun_event, dt_util.now().date())
        if value is None:
            return dt_util.parse_time(fixed)
        return dt_util.as_local(value + timedelta(minutes=offset)).time()

    def _next_start_dt(self) -> datetime | None:
        """Compute the next occurrence datetime of the night-start anchor."""
        now = dt_util.now()

        def _fixed_next():
            t = dt_util.parse_time(self.trigger_time)
            if t is None:
                return None
            candidate = now.replace(
                hour=t.hour, minute=t.minute, second=0, microsecond=0
            )
            if candidate <= now:
                candidate += timedelta(days=1)
            return candidate

        if self.start_mode == ANCHOR_FIXED:
            return _fixed_next()
        event_attr = (
            "next_setting" if self.start_mode == ANCHOR_SUNSET else "next_rising"
        )
        sun_event = (
            SUN_EVENT_SUNSET if self.start_mode == ANCHOR_SUNSET else SUN_EVENT_SUNRISE
        )
        for days in (0, 1):
            date = (now + timedelta(days=days)).date()
            value = self._sun_dt(event_attr, sun_event, date)
            if value is None:
                break
            candidate = dt_util.as_local(value) + timedelta(minutes=self.start_offset)
            if candidate > now:
                return candidate
            if self.sun_entity != DEFAULT_SUN_ENTITY:
                # 自定义 sun 实体只暴露“下一次”事件这一个数据点，拿不到
                # 之后的下一次（M4）。单日语义：等实体在事件过后刷新属性时
                # 会触发重排，这里先退回固定时间兜底。
                _LOGGER.debug(
                    "%s only exposes its next event; falling back to fixed time",
                    self.sun_entity,
                )
                break

        # Snapshot view polls this every 5s: throttle to first sighting
        # (same pattern as _sun_dt, ANL-F-004).
        if not self._warned_sun_fallback:
            self._warned_sun_fallback = True
            _LOGGER.warning("Sun-based start anchor unavailable, using fixed time")
        return _fixed_next()

    def _schedule_trigger(self) -> None:
        """(Re)schedule the daily trigger at the night-start anchor."""
        if self._stopped:
            # F-078：卸载后已排队回调（sun 变化/锚点触发）不得重排每日句柄
            return
        if self._unsub_time is not None:
            self._unsub_time()
            self._unsub_time = None
        when = self._next_start_dt()
        if when is None:
            _LOGGER.error("Cannot schedule trigger: invalid trigger time")
            return
        self._unsub_time = async_track_point_in_time(
            self.hass, self._async_anchor_trigger, when
        )
        _LOGGER.info("Night light trigger scheduled at %s", when)

    def _schedule_extra_starts(self) -> None:
        """(Re)schedule one point-in-time timer per extra period (F-059).

        每个时段一个定点句柄，触发后自循环重排次日（extra 起点是固定时刻，
        不依赖天文事件——锚点/sun 重排路径不触及本表）。解析失败 DEBUG
        跳过：上游 schema 已保证格式，这里是防御。
        F-069/F-070：每次调用先取消并清空全部旧句柄再全量重注册（已触发
        句柄的 unsub 是无害 no-op）——回调不再自行 pop，杜绝「pop 到别人
        刚注册的次日句柄」的泄漏路径；同刻 start 只注册首个 index（与
        _mode_at 列表序平局语义一致；schema 重复校验之外的运行时兜底）。
        """
        for unsub in self._extra_unsubs.values():
            unsub()
        self._extra_unsubs.clear()
        if self._stopped:
            return
        now = dt_util.now()
        seen_starts: set[int] = set()
        for i, extra in enumerate(self.extras):
            t = dt_util.parse_time(extra.get(EXTRA_START, ""))
            if t is None:
                _LOGGER.debug("Extra period %d has unparseable start, no timer", i)
                continue
            start_min = t.hour * 60 + t.minute
            if start_min in seen_starts:
                _LOGGER.debug(
                    "Extra period %d duplicates an earlier start minute, no timer", i
                )
                continue
            seen_starts.add(start_min)
            candidate = now.replace(
                hour=t.hour, minute=t.minute, second=0, microsecond=0
            )
            if candidate <= now:
                candidate += timedelta(days=1)
            self._extra_unsubs[i] = async_track_point_in_time(
                self.hass, self._make_extra_start_callback(i), candidate
            )

    def _make_extra_start_callback(self, index: int):
        """One extra period's start callback: apply params, re-arm for tomorrow.

        F-069：不在 _run 里 pop 句柄（_schedule_extra_starts 每次全量重
        注册，pop 会误伤重排后的次日句柄）；try/finally 保证 _async_extra_
        start 抛任何异常时次日定时器仍重排——与 _async_anchor_trigger 的
        M5 语义（重排无条件发生）对齐。
        """

        async def _run(_now) -> None:
            if self._stopped:
                return
            try:
                await self._async_extra_start(index)
            finally:
                if not self._stopped:
                    self._schedule_extra_starts()

        return _run

    async def _async_extra_start(self, index: int) -> None:
        """Fire one extra period's start: CI hand-back, then per-light apply.

        额外时段归属 anl：CI 配置开时先无条件归还虚拟灯（lux off=纯放手，
        对真实灯零下发，与 trigger 非 day 路径同语义、幂等）。随后逐灯并发
        清接管并按该时段参数下发（F-072：与 async_trigger 的 gather 模式
        对齐，灯多不串行拖长；异常逐结果记 ERROR、预期保持清空）：
        transition>0 时与该带锚点步同刻 benign 双发（同参幂等、_matches
        短路，不会双写）；transition=0 的时段没有任何计划步，本定时器是
        参数生效的唯一入口——页面「0=立即」文案由此成立。
        """
        if self._ci_configured():
            # F-076：只归还真在跑的 lux 闭环（active/passthrough）——校准中
            # turn_off 只置 _cal_pending_off 会跳过恢复；idle/未知模式不动
            if self._virtual_lux_mode() in _CI_HANDBACK_LUX_MODES:
                await self._async_ensure_virtual(False)
            else:
                _LOGGER.debug(
                    "extra start: virtual light %s lux mode not active, "
                    "skip hand-back",
                    self.day_ci_virtual_light,
                )

        async def _apply(entity_id: str):
            machine = self.machines[entity_id]
            machine.manual_override = False
            machine.mismatch_steps = 0
            params = self.params_for(entity_id, f"{MODE_EXTRA_PREFIX}{index}")
            ok = await self._async_process_light(entity_id, *params)
            return entity_id, params, ok

        results = await asyncio.gather(
            *(_apply(entity_id) for entity_id in self.machines),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                _LOGGER.error("extra period %d start failed: %s", index, result)
                continue
            entity_id, params, ok = result
            machine = self.machines[entity_id]
            if ok:
                machine.expected = params
                machine.applied = params
            else:
                machine.expected = None

    async def _async_anchor_trigger(self, _now) -> None:
        """Fire at the night-start anchor, then schedule the next round.

        无论本轮触发是否抛异常，都必须重排下一次触发，否则集成会静默停摆 (M5)。
        """
        try:
            await self.async_trigger(reason="anchor")
        finally:
            if not self._stopped:
                self._schedule_trigger()
                self._rebuild_day_plan()

    async def _async_sun_entity_changed(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Reschedule only when the custom sun entity's next-event times change (L1)."""
        if self._stopped:
            # F-078：卸载后事件分派竞态——不重排、不触发虚拟灯调用
            return
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is None:
            return
        for attr in ("next_rising", "next_setting"):
            new_value = new_state.attributes.get(attr)
            old_value = (
                old_state.attributes.get(attr) if old_state is not None else None
            )
            if new_value != old_value:
                _LOGGER.debug("%s %s changed, rescheduling", self.sun_entity, attr)
                self._schedule_trigger()
                self._rebuild_day_plan()
                return

    def _warn_overlapping_bands(self) -> None:
        """Warn when transition bands overlap.

        胜出规则 = 列表序（额外时段优先，其次夜间、日间），与
        _band_params 首匹配和计划 claim 一致 (P3-2)。
        F-066：transition=0 的 extra 对 _transition_bands 不可见（不构成
        带），但起点落在任一带窗口内时带内计划步会让位于该时段（F-066
        屏障守卫）——配置面必须给出 WARNING。
        """
        bands = self._transition_bands()
        for i, (anchor_min, target, dur) in enumerate(bands):
            start_a = (anchor_min - dur) % 1440
            for other_min, other_target, other_dur in bands[i + 1 :]:
                start_b = (other_min - other_dur) % 1440
                # 环形区间相交：任一带起点落在另一带内
                if (start_a - start_b) % 1440 < other_dur or (
                    start_b - start_a
                ) % 1440 < dur:
                    _LOGGER.warning(
                        "过渡带重叠：'%s' 与 '%s'，重叠区按列表序靠前的 "
                        "'%s' 插值（额外时段优先于夜间/日间锚点）",
                        target,
                        other_target,
                        target,
                    )
        for extra in self.extras:
            if int(extra.get(EXTRA_TRANSITION, 0) or 0) > 0:
                continue  # 构成带的 extra 已被上面的带间相交检查覆盖
            t = dt_util.parse_time(extra.get(EXTRA_START, ""))
            if t is None:
                continue
            start_min = t.hour * 60 + t.minute
            for anchor_min, target_mode, dur in bands:
                band_start = (anchor_min - dur) % 1440
                if (start_min - band_start) % 1440 < dur:
                    _LOGGER.warning(
                        "额外时段 %s 落在过渡带内（%s），带内计划步将让位于该时段",
                        extra.get(EXTRA_START),
                        target_mode,
                    )

    def _night_anchor_times(self) -> tuple:
        """Resolve (night start, night end) anchor times-of-day."""
        return (
            self._anchor_time(self.start_mode, self.trigger_time, self.start_offset),
            self._anchor_time(self.end_mode, self.end_time, self.end_offset),
        )

    def _anchor_modes(self) -> list[tuple]:
        """Return [(anchor_time, mode)] for extras and base anchors.

        额外时段锚点排在基础锚点之前，同一时刻冲突时额外时段优先。
        """
        anchors: list[tuple] = []
        for i, extra in enumerate(self.extras):
            anchors.append(
                (dt_util.parse_time(extra.get(EXTRA_START, "")), f"extra_{i}")
            )
        t_night_start, t_night_end = self._night_anchor_times()
        anchors.append((t_night_start, MODE_NIGHT))
        anchors.append((t_night_end, MODE_DAY if self.day_enabled else None))
        return anchors

    def _mode_at(self, minutes: int) -> str | None:
        """Resolve the active mode at a given minute-of-day (anchor model)."""
        best_mode: str | None = None
        best_delta: int | None = None
        for t, mode in self._anchor_modes():
            if t is None:
                continue
            anchor_min = t.hour * 60 + t.minute
            delta = (minutes - anchor_min) % 1440
            if best_delta is None or delta < best_delta:
                best_mode, best_delta = mode, delta
        return best_mode

    def anchor_snapshot(self) -> dict:
        """Public view-model for the page: anchors, current mode, next anchor.

        Computed in one pass so the snapshot view (which polls every 5s) has
        a single contract surface instead of reaching into _anchor_modes /
        _mode_at / _next_start_dt internals (ANL-F-003 — those are private
        and have been renamed before; README records current_mode() dying as
        dead code once already).

        next_anchor is the anchor (of ANY kind in _anchor_modes) whose next
        occurrence is soonest — the night-start-only _next_start_dt is the
        scheduling trigger and is NOT what the page displays (ANL-F-001).
        """
        now = dt_util.now()
        minutes = now.hour * 60 + now.minute
        anchors = []
        next_anchor: datetime | None = None
        for t, mode in self._anchor_modes():
            if t is None:
                # ANL-F-013: an unparseable anchor silently vanishing from
                # the timetable must be audible at least once — one flag per
                # manager (the web layer cannot see these anymore).
                if not self._warned_bad_anchor:
                    self._warned_bad_anchor = True
                    _LOGGER.warning(
                        "夜灯助手存在无法解析的锚点（%s），已从时刻表跳过", mode
                    )
                continue
            anchors.append((t, mode))
            candidate = now.replace(
                hour=t.hour, minute=t.minute, second=0, microsecond=0
            )
            if candidate <= now:
                candidate += timedelta(days=1)
            if next_anchor is None or candidate < next_anchor:
                next_anchor = candidate
        return {
            "anchors": anchors,
            "current_mode": self._mode_at(minutes),
            "next_anchor": next_anchor,
        }

    def _transition_bands(self) -> list[tuple]:
        """Return [(anchor_minutes, target_mode, duration_min)] for active bands."""
        bands: list[tuple] = []
        for i, extra in enumerate(self.extras):
            dur = int(extra.get(EXTRA_TRANSITION, 0) or 0)
            t = dt_util.parse_time(extra.get(EXTRA_START, ""))
            if dur > 0 and t is not None:
                bands.append((t.hour * 60 + t.minute, f"extra_{i}", dur))
        t_night_start, t_night_end = self._night_anchor_times()
        if self.start_transition > 0 and t_night_start is not None:
            bands.append(
                (
                    t_night_start.hour * 60 + t_night_start.minute,
                    MODE_NIGHT,
                    self.start_transition,
                )
            )
        if self.end_transition > 0 and t_night_end is not None and self.day_enabled:
            bands.append(
                (
                    t_night_end.hour * 60 + t_night_end.minute,
                    MODE_DAY,
                    self.end_transition,
                )
            )
        return bands

    def _band_params(self, entity_id: str, now_min: int):
        """Interpolate params inside a transition band.

        返回 _NOT_IN_BAND 表示当前不在任何过渡带内；
        返回 None 表示带内但起点无生效模式（本轮不干预）；
        否则返回插值后的 (brightness, kelvin)。
        起点参数取带起点时刻按纯锚点解析的模式快照。
        """
        for anchor_min, target_mode, dur in self._effective_transition_bands():
            band_start = (anchor_min - dur) % 1440
            elapsed = (now_min - band_start) % 1440
            if elapsed >= dur:
                continue
            from_mode = self._mode_at(band_start)
            if from_mode is None:
                return None
            factor = elapsed / dur
            return lerp_params(
                self.params_for(entity_id, from_mode),
                self.params_for(entity_id, target_mode),
                factor,
            )
        return _NOT_IN_BAND

    def current_params(self, entity_id: str) -> tuple[int, int] | None:
        """Resolve the effective (brightness, kelvin) for right now.

        过渡带内返回插值，带外按锚点模式取参；无生效模式返回 None。
        """
        now = dt_util.now().time()
        now_min = now.hour * 60 + now.minute
        if self._has_transitions:
            band = self._band_params(entity_id, now_min)
            if band is not _NOT_IN_BAND:
                return band
        mode = self._mode_at(now_min)
        if mode is None:
            return None
        return self.params_for(entity_id, mode)

    def _rebuild_day_plan(self) -> None:
        """Rebuild the fixed-step transition plan for upcoming bands.

        astral/锚点解析只在重建时发生一次，步进点本身是纯查表下发；
        句柄集中管理，stop()/重建时全部取消。
        """
        for unsub in self._plan_unsub.values():
            unsub()
        self._plan_unsub.clear()
        self._plan.clear()
        if self._stopped or not self._has_transitions:
            return
        now = dt_util.now()
        # 全部带窗口（半开 [start, start+dur)），按生效带列表序（CI 开时
        # 已滤掉日间侧带）。步进是否生成用"窗口包含"判定而非精确时刻
        # 去重——两条带网格错位时精确去重会漏，导致重叠区交替下发并互相
        # 重置接管状态 (P2)。
        effective_bands = self._effective_transition_bands()
        all_windows = [
            ((anchor_min - dur) % 1440, dur) for anchor_min, _, dur in effective_bands
        ]
        scheduled_windows: list[tuple[int, int]] = []
        for band_i, (anchor_min, target_mode, dur) in enumerate(effective_bands):
            band_start_min = (anchor_min - dur) % 1440
            start = upcoming_band_start(now, band_start_min, dur)
            if start is None:
                continue
            anchor_dt = start + timedelta(minutes=dur)
            from_mode = self._mode_at(band_start_min)
            if from_mode is None:
                # 起点无生效模式：该带不生成步进，但窗口仍占位——与
                # _band_params 的首匹配短路语义保持一致 (P3-1)
                _LOGGER.debug(
                    "Band ending at %s has no active from-mode, not scheduled",
                    anchor_dt,
                )
                scheduled_windows.append((band_start_min, dur))
                continue
            # F-075：band_key 三元组 (target, date, from_mode)——extra 屏障
            # 守卫消费 [2] 区分「带起点落在既有 extra 内」（照跑）与「带中
            # 途新 extra 介入」（让位）；from_mode 即上方建带时的解析结果
            band_key = (target_mode, start.date().isoformat(), from_mode)
            # 半开窗口不含自己的锚点分钟：锚点步（factor=1）只有在没有
            # 更靠后的带覆盖该分钟时才生成，否则与后继带的步进同刻冲突
            later_covers_anchor = any(
                (anchor_min - win_start) % 1440 < win_dur
                for win_start, win_dur in all_windows[band_i + 1 :]
            )
            for step_dt in step_datetimes(start, anchor_dt, self.transition_interval):
                if step_dt <= now:
                    continue  # 已过去的步进不补发
                if step_dt == anchor_dt and later_covers_anchor:
                    continue
                step_min = step_dt.hour * 60 + step_dt.minute
                if any(
                    (step_min - win_start) % 1440 < win_dur
                    for win_start, win_dur in scheduled_windows
                ):
                    continue  # 靠前的带在该时刻胜出（首匹配语义）
                factor = (step_dt - start).total_seconds() / 60 / dur
                entrance = step_dt == start
                for entity_id in self.lights:
                    b, k = lerp_params(
                        self.params_for(entity_id, from_mode),
                        self.params_for(entity_id, target_mode),
                        factor,
                    )
                    self._plan.setdefault(step_dt, []).append(
                        PlanStep(entity_id, b, k, band_key, entrance)
                    )
            scheduled_windows.append((band_start_min, dur))
        for when, steps in self._plan.items():
            # 兜底去重：窗口是半开的、不含各自的锚点分钟，两条带共享锚点
            # 分钟时双方都生成同刻步进。保留列表序靠前带的一条——与
            # _mode_at 平局规则（额外时段优先）一致 (P1-1)。
            seen_entities: set[str] = set()
            unique_steps = [
                step
                for step in steps
                if not (
                    step.entity_id in seen_entities or seen_entities.add(step.entity_id)
                )
            ]
            self._plan[when] = unique_steps
            self._plan_unsub[when] = async_track_point_in_time(
                self.hass, self._make_plan_step_callback(when, unique_steps), when
            )
        _LOGGER.info("Transition plan rebuilt: %d step point(s)", len(self._plan))

    def _make_plan_step_callback(self, when: datetime, steps: list[PlanStep]):
        @callback
        def _run(_now) -> None:
            self._plan_unsub.pop(when, None)
            if self._stopped:
                return
            # F-071：守卫与下发按实际触发时刻——计划时刻 when 仅用于句柄键，
            # 迟到步按真实时钟判定（防绕过 F-060 的 CI 日间守卫）
            self.hass.async_create_task(self._async_run_steps(steps, dt_util.now()))

        return _run

    async def _async_run_steps(self, steps: list[PlanStep], when: datetime) -> None:
        """Execute one step point: band-entry reset, takeover check, steer."""
        if self._stopped:
            return
        results = await asyncio.gather(
            *(self._async_run_step(step, when) for step in steps),
            return_exceptions=True,
        )
        for step, result in zip(steps, results, strict=False):
            if isinstance(result, Exception):
                _LOGGER.error("%s step failed: %s", step.entity_id, result)

    async def _async_run_step(self, step: PlanStep, when: datetime) -> None:
        """Process one light's scheduled step (P3-7: 逐灯并发，互不阻塞)."""
        if self._stopped:
            return
        mode_now = self._mode_at(when.hour * 60 + when.minute)
        if self._ci_configured() and mode_now == MODE_DAY:
            # F-060：CI 下非日侧带的窗口可能跨过日间锚点，落在日间的步进
            # 不得直控真实灯。被动计划步也不得经 lux 把关着的灯点亮——
            # 日间接管只归 trigger/listener 路径（此处不做 ensure_virtual）。
            _LOGGER.debug(
                "%s step at %s lands in CI daytime, skipped", step.entity_id, when
            )
            return
        if (
            isinstance(mode_now, str)
            and mode_now.startswith(MODE_EXTRA_PREFIX)
            and step.band_key[0] != mode_now
            and step.band_key[2] != mode_now
        ):
            # F-066：extra 时段屏障——extra 起点定时器已下发自身参数并接管，
            # 其它带在带内的后续插值步让位（否则 extra 参数最多维持一个步进
            # 间隔就被拉回插值曲线）。两种例外照跑：extra 自己带的步
            #（band_key[0] 目标==当前 extra 模式）；带起点本就落在该 extra
            # 内的带（band_key[2] from==当前 extra，F-075——否则「晚饭
            # extra→夜锚点」的整段插值被吞、锚点步一步跳变）。
            _LOGGER.debug(
                "%s step at %s falls inside extra period %s, yielded",
                step.entity_id,
                when,
                mode_now,
            )
            return
        machine = self.machines.get(step.entity_id)
        if machine is None:
            return
        if machine.state == LightState.TURN_ON_PENDING:
            # 开灯监听已在处理（settle delay 中），等其重新播种预期，
            # 否则刚开灯的默认态会被误判成手动调节 (P3 竞态)
            return
        if machine.band_key != step.band_key:
            # 带入口：清接管；预期值仅入口步播种（=起点参数）。
            # 入带时不在曲线上的灯（已处夜间参数/被手动调过）在这里
            # 被判接管，整带保持现状——带内手动优先，锚点归位。
            # 中途入表（重启/重建后首观测步非入口）预期置 None：跳过
            # 一次判定，按旧行为拉回曲线一次后再正常判定 (P1)。
            machine.band_key = step.band_key
            machine.manual_override = False
            machine.mismatch_steps = 0
            machine.expected = (step.brightness, step.kelvin) if step.entrance else None
        if machine.manual_override:
            _LOGGER.debug("%s manual override active, step skipped", step.entity_id)
            return
        state = self.hass.states.get(step.entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            # 不可观测步不计数：「连续 2 步」只统计可核验的步 (P3-5)
            machine.mismatch_steps = 0
            return
        # 接管判定只在可核验（亮且上报亮度）时进行；关灯/无亮度上报
        # 不判接管 (P2a)
        verifiable = state.state == STATE_ON and (
            state.attributes.get(ATTR_BRIGHTNESS) is not None
        )
        if verifiable and machine.expected is not None:
            # 在曲线 = 匹配预期值，或匹配我方最后成功下发的参数——
            # from-mode 参数可能从未下发给常亮灯，不能只认预期值 (P2-3)
            on_curve = self._matches(state, *machine.expected) or (
                machine.applied is not None and self._matches(state, *machine.applied)
            )
            if not on_curve:
                machine.mismatch_steps += 1
                # 入口步保持单次判定（既定带入口语义）；带内连续 2 步不符
                # 才判接管——单步偏差可能只是回读滞后或灯具能力边界 (P2-1)
                if self.respect_manual and (
                    step.entrance or machine.mismatch_steps >= 2
                ):
                    machine.manual_override = True
                    _LOGGER.info(
                        "%s manual change detected (current %s/%sK, expected "
                        "%s%%/%sK) — light left alone until next anchor",
                        step.entity_id,
                        state.attributes.get(ATTR_BRIGHTNESS),
                        state.attributes.get(ATTR_COLOR_TEMP_KELVIN),
                        machine.expected[0],
                        machine.expected[1],
                    )
                    return
                # 带内首次偏离：视为手动调节的观测，本步不下发——手动值
                # 一次都不被拉回；下一步仍偏离即接管，回到曲线则计数清零
                if self.respect_manual:
                    _LOGGER.debug(
                        "%s first mismatch observed, not steering this step",
                        step.entity_id,
                    )
                    return
            else:
                machine.mismatch_steps = 0
        ok = await self._async_process_light(
            step.entity_id,
            step.brightness,
            step.kelvin,
            allow_turn_on=False,
            transition_s=self.transition_interval * 60,
        )
        # 仅在确实生效（已匹配或下发成功）时推进预期；服务失败保持
        # 上次预期，下一步可重试且不会误判接管 (P2b)。不可核验的灯
        # 不推进预期，后续步持续跳过接管判定。
        if ok and verifiable:
            machine.expected = (step.brightness, step.kelvin)
            machine.applied = (step.brightness, step.kelvin)

    async def _async_light_state_changed(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Handle a configured light turning on inside the night window."""
        old_state = event.data["old_state"]
        new_state = event.data["new_state"]
        if new_state is None or new_state.state != STATE_ON:
            return
        if old_state is not None and old_state.state not in (
            STATE_OFF,
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            return  # 仅响应 关→开，忽略运行中的属性变化，避免自触发循环
        entity_id = event.data["entity_id"]
        params = self.current_params(entity_id)
        if params is None:
            _LOGGER.debug("%s turned on outside all active periods, ignored", entity_id)
            return
        brightness, kelvin = params
        machine = self.machines.get(entity_id)
        if machine is None:
            _LOGGER.debug(
                "%s not in machine table (stale listener?), skipped", entity_id
            )
            return
        # 日间恒照度联动（V1.5.0）：dt_util.now() 只取一次算分钟，防两次
        # 取值不一致
        now = dt_util.now()
        mode = self._mode_at(now.hour * 60 + now.minute)
        if self._ci_active_for(mode):
            machine.manual_override = False
            machine.mismatch_steps = 0
            machine.expected = None
            machine.applied = None
            machine.state = LightState.IDLE
            if await self._async_ensure_virtual(True):
                _LOGGER.info(
                    "%s turned on in daytime: constant-illuminance closed loop "
                    "takes over via virtual light %s (real light untouched)",
                    entity_id,
                    self.day_ci_virtual_light,
                )
                return  # 无 settle 延迟、真实灯零下发
            # ensure 失败（含 F-068 预检）：params 此刻=日间参数，落到既有
            # settle 路径（fallback 日间参数直控，WARNING 已由 ensure 节流发出）
        elif self._ci_configured():
            # 防御性归还：夜间 lux 闭环仍在跑（F-076：判 attributes["mode"]
            # 而非 state——state 是真灯镜像，本集成夜间开灯也会命中）时关掉
            # 它避免两闭环抢灯（lux off=纯放手，对真实灯零下发）；idle/
            # calibrating/None 不动（calibrating 时 lux 自己收尾）
            if self._virtual_lux_mode() in _CI_HANDBACK_LUX_MODES:
                await self._async_ensure_virtual(False)
        # 关→开重新加入曲线：清接管并重设预期基准与计数
        machine.manual_override = False
        machine.mismatch_steps = 0
        machine.expected = params
        machine.state = LightState.TURN_ON_PENDING
        _LOGGER.info(
            "%s turned on (target %s%%/%sK), checking after %.1fs settle delay",
            entity_id,
            brightness,
            kelvin,
            self.settle_delay,
        )
        self._delay(
            self.settle_delay,
            lambda _now: self.hass.async_create_task(
                self._async_settle_apply(entity_id, brightness, kelvin)
            ),
        )

    async def _async_settle_apply(
        self, entity_id: str, brightness: int, kelvin: int
    ) -> None:
        """Turn-on settle delay 后的下发；失败不保留播种值 (P2-2)。

        预期清空后退回"中途入表"语义：下一计划步跳过一次接管判定、
        拉回一次并重新播种，而不是把下发失败误判成手动调节。
        """
        machine = self.machines.get(entity_id)
        ok = await self._async_process_light(entity_id, brightness, kelvin)
        if machine is None:
            return
        if ok:
            machine.expected = (brightness, kelvin)
            machine.applied = (brightness, kelvin)
            machine.mismatch_steps = 0
        else:
            machine.expected = None

    async def async_trigger(self, reason: str = "manual") -> None:
        """Run one check-and-set round for all lights (concurrent per light).

        使用当前时段参数而非硬编码夜间参数 (M2)，白天手动触发不会再把灯
        调到夜间亮度；逐灯并发避免灯具多时串行等待拖长整轮时间 (L5)。
        F-072：CI 判定与虚拟灯调用提升到触发轮级——N 盏灯整轮只调一次
        turn_on/turn_off（lux 侧每次 turn_on 都会重跑一次闭环决策，逐灯
        调用是 N 次冗余）；接管成功直接清全部 machine 并返回，失败落到
        下方逐灯 fallback（params 此刻=日间参数）。
        """
        if self._stopped:
            # F-078：卸载竞态早退（含页面「立即触发」在卸载后到达）——
            # unloaded manager 不该再对虚拟灯发 turn_off 或重复夜间下发
            return
        _LOGGER.info("Auto night light triggered (%s)", reason)
        # dt_util.now() 只取一次算分钟，防两次取值不一致
        now = dt_util.now()
        mode = self._mode_at(now.hour * 60 + now.minute)
        if self._ci_active_for(mode):
            if await self._async_ensure_virtual(True):
                # 恒照度闭环接管：真实灯零下发、无验证延迟，全部 machine
                # 归位（虚拟灯整轮只调一次）
                for machine in self.machines.values():
                    machine.state = LightState.IDLE
                    machine.manual_override = False
                    machine.mismatch_steps = 0
                    machine.expected = None
                    machine.applied = None
                return
            # ensure 失败（含 F-068 预检）：落到下方逐灯 fallback 直控
            # （WARNING 已由 ensure 节流发出）
        elif self._ci_configured():
            # 夜间/额外时段：每轮一次幂等归还虚拟灯（lux off=纯放手，对真实
            # 灯零下发），失败不阻断夜间下发（告警已响）；在 gather 之前
            # 执行，「先关虚拟灯、后直控真实灯」的顺序由此钉住。
            # F-076：只归还真在跑的 lux 闭环（active/passthrough）——idle
            # 无闭环可放、calibrating 时打扰会让校准收尾跳过亮度恢复
            if self._virtual_lux_mode() in _CI_HANDBACK_LUX_MODES:
                await self._async_ensure_virtual(False)
            else:
                _LOGGER.debug(
                    "trigger: virtual light %s lux mode not active, "
                    "skip hand-back",
                    self.day_ci_virtual_light,
                )

        async def _one(entity_id: str) -> None:
            params = self.current_params(entity_id)
            if params is None:
                _LOGGER.debug("%s outside all active periods, skipped", entity_id)
                return
            machine = self.machines.get(entity_id)
            if machine is not None:
                # 锚点归位：清除带内接管，预期基准重置为当前时段参数
                machine.manual_override = False
                machine.mismatch_steps = 0
            ok = await self._async_process_light(entity_id, *params)
            if machine is not None:
                if ok:
                    machine.expected = params
                    machine.applied = params
                else:
                    # 失败不保留播种值 (P2-2)，退回中途入表语义
                    machine.expected = None

        results = await asyncio.gather(
            *(_one(entity_id) for entity_id in self.lights),
            return_exceptions=True,
        )
        for entity_id, result in zip(self.lights, results, strict=False):
            if isinstance(result, Exception):
                _LOGGER.error("%s trigger round failed: %s", entity_id, result)

    @staticmethod
    def _to_pct(brightness_byte: int) -> int:
        """Convert HA brightness (0-255) to percent."""
        return round(brightness_byte * 100 / 255)

    @staticmethod
    def _to_byte(brightness_pct: float) -> int:
        """Convert percent (1-100) to HA brightness (1-255)."""
        return min(255, max(1, round(brightness_pct * 255 / 100)))

    def _supports_color_temp(self, state: State) -> bool:
        """Return True if the light supports color temperature."""
        modes = state.attributes.get("supported_color_modes")
        if not modes:  # 未上报时乐观假定支持，避免漏发色温
            return True
        return "color_temp" in modes

    def _matches(self, state: State, brightness: int, kelvin: int) -> bool:
        """Return True if the light already matches target within tolerance.

        brightness 参数为百分比（1-100）。
        """
        if state.state != STATE_ON:
            return False
        cur_brightness = state.attributes.get(ATTR_BRIGHTNESS)
        if cur_brightness is None:
            return False
        if abs(self._to_pct(cur_brightness) - brightness) > self.tol_brightness:
            return False
        # 灯具不支持/未上报色温时仅校验亮度
        if not self._supports_color_temp(state):
            return True
        # 支持色温但当前处于彩光（hs/rgb）模式：状态里没有 color_temp_kelvin，
        # 不能视为已匹配，需要纠回色温 (M3)
        color_mode = state.attributes.get("color_mode")
        if color_mode is not None and color_mode != "color_temp":
            return False
        cur_kelvin = state.attributes.get(ATTR_COLOR_TEMP_KELVIN)
        return cur_kelvin is None or abs(cur_kelvin - kelvin) <= self.tol_kelvin

    async def _async_process_light(
        self,
        entity_id: str,
        brightness: int,
        kelvin: int,
        allow_turn_on: bool = True,
        transition_s: int | None = None,
    ) -> bool:
        """Advance the state machine for one light.

        Returns True when the light matched or the command was accepted.
        """
        if self._stopped:
            return False
        machine = self.machines[entity_id]
        machine.state = LightState.PENDING
        state = self.hass.states.get(entity_id)

        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            machine.state = LightState.OFFLINE
            _LOGGER.warning("%s unavailable, skipped", entity_id)
            return False

        if state.state != STATE_ON and (self.only_when_on or not allow_turn_on):
            machine.state = LightState.SKIPPED_OFF
            _LOGGER.debug("%s is off, skipped", entity_id)
            return False

        if self._matches(state, brightness, kelvin):
            machine.state = LightState.MATCHED
            _LOGGER.debug("%s already matches target, no service call", entity_id)
            return True

        # 状态不符，下发控制
        machine.state = LightState.SETTING
        machine.target = (brightness, kelvin)
        service_data = {
            ATTR_ENTITY_ID: entity_id,
            ATTR_BRIGHTNESS: self._to_byte(brightness),
        }
        if transition_s is not None:
            # 灯具原生渐变：步进之间由灯平滑滑变（不支持的灯自动忽略）
            service_data[ATTR_TRANSITION] = transition_s
        if self._supports_color_temp(state):
            # NumberSelector 可能给出 float，个别灯平台对类型严格校验 (L9)
            service_data[ATTR_COLOR_TEMP_KELVIN] = int(kelvin)
        _LOGGER.info(
            "%s mismatch (brightness=%s kelvin=%s), setting to %s%%/%sK",
            entity_id,
            state.attributes.get(ATTR_BRIGHTNESS),
            state.attributes.get(ATTR_COLOR_TEMP_KELVIN),
            brightness,
            kelvin,
        )
        try:
            await self.hass.services.async_call(
                LIGHT_DOMAIN, SERVICE_TURN_ON, service_data, blocking=True
            )
        except Exception as err:  # noqa: BLE001
            machine.state = LightState.MISMATCH
            machine.last_error = str(err)
            _LOGGER.error("%s service call failed: %s", entity_id, err)
            return False

        # 延迟验证，避免灯具状态尚未刷新
        self._delay(
            self.verify_delay,
            lambda _now: self.hass.async_create_task(self._async_verify(entity_id)),
        )
        return True

    async def _async_verify(self, entity_id: str) -> None:
        """Verify the light reached the target after control."""
        if self._stopped:
            return
        machine = self.machines[entity_id]
        state = self.hass.states.get(entity_id)
        target = machine.target or self.params_for(entity_id, MODE_NIGHT)
        if state is not None and self._matches(state, *target):
            machine.state = LightState.VERIFIED
            machine.last_error = None
            machine.applied = target
            _LOGGER.info("%s verified", entity_id)
        else:
            machine.state = LightState.MISMATCH
            _LOGGER.warning(
                "%s still mismatched after control: %s",
                entity_id,
                state.state if state else "missing",
            )
