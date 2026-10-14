"""Per-zone runtime: listeners, closed loop, calibration sweep.

One ZoneRuntime per config entry. The virtual light entity, number entity,
button and status sensor all hang off it. Every write path to the real light
runs under ``_apply_lock`` and re-checks the mode inside the lock, so a late
control decision can never clobber a user's turn-off or an active calibration.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import statistics
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta

from homeassistant import config_entries
from homeassistant.components.light import ATTR_BRIGHTNESS, ATTR_BRIGHTNESS_PCT
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    CAL_CMD_SETTLE_S,
    CAL_FIRST_REPORT_TIMEOUT_S,
    CAL_MIN_STEP_PCT,
    CAL_MIN_USEFUL_LUX,
    CAL_POLL_INTERVAL_S,
    CAL_QUIET_PERIOD_S,
    CAL_TIMEOUT_UPGRADE_STREAK,
    CONF_AUTO_TURN_OFF,
    CONF_CAL_STEP_PCT,
    CONF_DEADBAND_PCT,
    CONF_LIGHT_ENTITY,
    CONF_LUX_ENTITY,
    CONF_MANUAL_SLOPE,
    CONF_MIN_BRIGHTNESS_PCT,
    CONF_SCAN_INTERVAL_S,
    CONF_TARGET_LUX,
    CONF_ZONE_NAME,
    DECISION_RING_SIZE,
    DOMAIN,
    LUX_STALE_AFTER_S,
    MODE_ACTIVE,
    MODE_CALIBRATING,
    MODE_IDLE,
    MODE_PASSTHROUGH,
    TARGET_MAX_LUX,
    TARGET_MIN_LUX,
    merged_options,
)
from .logic import (
    Curve,
    Decision,
    build_curve,
    calibration_steps,
    combine_passes,
    curve_from_manual,
    decide,
    enforce_monotonic,
    smooth3,
    subtract_baseline,
)
from .store import ZoneStorage

_LOGGER = logging.getLogger(__name__)

# Steps below this percent sit in the LED low-end where missed reports are
# expected; they never count toward the "upgrade to 10% sweep" trigger.
_CAL_MISSED_COUNT_MIN_PCT = 20.0


class CalibrationError(Exception):
    """Calibration could not produce a usable curve."""


def _lux_stale_reason(state) -> str | None:
    """None = 数据可信；否则给出一句话原因（不可用 / 陈旧 > LUX_STALE_AFTER_S）。

    判定基于 ``state.last_changed``（值变化才算新数据；属性刷新推 last_updated
    但照度未变，不能当新鲜数据，FL-UX-01）。
    """
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return "传感器不可用"
    lc = getattr(state, "last_changed", None)
    if lc is not None:
        now = dt_util.utcnow()
        # 真 HA 两侧均 aware；测试桩可能任一侧 naive——归一到同侧再相减
        if (lc.tzinfo is None) != (now.tzinfo is None):
            lc = lc.replace(tzinfo=now.tzinfo)
        age = (now - lc).total_seconds()
        if age > LUX_STALE_AFTER_S:
            limit_min = LUX_STALE_AFTER_S // 60
            return (
                f"数据已 {int(age // 60)} 分钟未更新（超过 {limit_min} 分钟可信窗口）"
            )
    return None


class ZoneRuntime:
    """State machine + control loop for one bathroom zone."""

    def __init__(self, hass: HomeAssistant, entry: config_entries.ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.entry_id = entry.entry_id
        self.zone_name: str = entry.data[CONF_ZONE_NAME]
        self.light_entity: str = entry.data[CONF_LIGHT_ENTITY]
        self.lux_entity: str = entry.data[CONF_LUX_ENTITY]
        self.opts = merged_options(entry.options)
        self.curve: Curve | None = None
        self.mode: str = MODE_IDLE
        self.commanded_pct: float | None = None
        self.target_lux: float = float(self.opts[CONF_TARGET_LUX])
        self.lux_stale: bool = False
        # 上一次校准的失败原因（成功/再次启动时清除）；页面据此显示中止反馈。
        self.cal_last_error: str | None = None
        self.cal_progress: str = ""
        self.last_decision: Decision | None = None
        self.last_adjustment: datetime | None = None
        self.decisions: deque[str] = deque(maxlen=DECISION_RING_SIZE)
        self.signal = f"{DOMAIN}_{entry.entry_id}_update"
        self.storage = ZoneStorage(hass, entry.entry_id)
        self._unsubs: list[Callable[[], None]] = []
        self._cal_task: asyncio.Task | None = None
        self._eval_task: asyncio.Task | None = None
        self._apply_lock = asyncio.Lock()
        self._off_streak = 0
        self._stale_warned = False
        self._eval_busy = False
        self._eval_pending = False
        self._cal_was_on = False
        self._cal_pending_off = False
        self._stopped = False

    # ---------------------------------------------------------------- lifecycle

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.entry_id)},
            name=self.zone_name,
            manufacturer="Lux Balance",
            model="constant-illuminance zone",
        )

    async def async_load(self) -> None:
        """Load the stored curve, falling back to the manual coefficient."""
        self.curve = await self.storage.async_load_curve()
        if self.curve is None:
            slope = float(self.opts[CONF_MANUAL_SLOPE])
            if slope > 0:
                self.curve = curve_from_manual(slope)

    @callback
    def start(self) -> None:
        """Attach listeners; call after platforms are set up."""
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, [self.light_entity], self._on_light_event
            )
        )
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, [self.lux_entity], self._on_lux_event
            )
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass,
                self._on_tick,
                timedelta(seconds=float(self.opts[CONF_SCAN_INTERVAL_S])),
            )
        )
        # Self-heal after restart: a light that is already on gets a running
        # closed loop again instead of staying a passive mirror.
        state = self.hass.states.get(self.light_entity)
        if state is not None and state.state == STATE_ON and self.curve is not None:
            self.mode = MODE_ACTIVE

    async def async_stop(self) -> None:
        """Detach listeners and cancel/await in-flight tasks.

        Best-effort: the calibration's restore inside its ``finally`` usually
        completes because we await the cancelled task, but a cancellation
        landing exactly on the restore's own await can abandon it.
        """
        self._stopped = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        tasks: list[asyncio.Task] = []
        for attr in ("_cal_task", "_eval_task"):
            task = getattr(self, attr)
            setattr(self, attr, None)
            if task is not None and not task.done():
                task.cancel()
                tasks.append(task)
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @callback
    def notify(self) -> None:
        async_dispatcher_send(self.hass, self.signal)

    # ------------------------------------------------------------------ events

    @callback
    def _on_light_event(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        if self.mode == MODE_CALIBRATING:
            self.notify()
            return
        if new_state.state == STATE_OFF and self.mode in (
            MODE_ACTIVE,
            MODE_PASSTHROUGH,
        ):
            _LOGGER.info("%s: 真灯被外部关闭，停止调节", self.zone_name)
            self.mode = MODE_IDLE
            self.commanded_pct = None
        elif (
            new_state.state == STATE_ON
            and self.mode == MODE_IDLE
            and self.curve is not None
            and not self._any_zone_calibrating()
        ):
            # External switch-on: take over compensation instead of staying a
            # passive mirror (mirrors the start() restart self-heal). Gated
            # while any zone calibrates — a revived loop would pollute the
            # running sweep's samples.
            _LOGGER.info("%s: 真灯被外部打开，闭环接管", self.zone_name)
            self.mode = MODE_ACTIVE
            self._off_streak = 0
            self._schedule_evaluate("external-on")
        self.notify()

    @callback
    def _on_lux_event(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        self.lux_stale = False
        self._stale_warned = False
        self._schedule_evaluate("lux")

    @callback
    def _on_tick(self, now: datetime) -> None:
        self._schedule_evaluate("tick")

    @callback
    def _schedule_evaluate(self, trigger: str) -> None:
        if self.mode != MODE_ACTIVE or self._stopped:
            return
        if self._eval_busy:
            self._eval_pending = True
        else:
            self._eval_task = self.hass.async_create_task(self.async_evaluate(trigger))

    def _any_zone_calibrating(self) -> bool:
        """True while any *other* zone runs a calibration sweep."""
        lux_entries = self.hass.data.get(DOMAIN, {}).get("lux", {})
        return any(
            other.mode == MODE_CALIBRATING
            for other in lux_entries.values()
            if other is not self
        )

    # ------------------------------------------------------------- closed loop

    def set_target(self, value: float) -> None:
        # Bounds come from const (single source with number.py / web.py /
        # page validation — LX-F-022).
        self.target_lux = max(TARGET_MIN_LUX, min(TARGET_MAX_LUX, float(value)))
        self.notify()

    def request_evaluate(self) -> None:
        self._schedule_evaluate("target")

    async def async_evaluate(self, trigger: str) -> None:
        """Recompute the required brightness; closed-loop entry point.

        Bursts of sensor reports collapse into one run; if anything arrived
        while we were busy, a single trailing pass picks up the latest value.
        """
        if self.mode != MODE_ACTIVE or self._eval_busy:
            return
        self._eval_busy = True
        try:
            decision, lux_now = await self._async_compute_decision()
            if decision is not None:
                await self._apply(trigger, lux_now, decision)
        finally:
            self._eval_busy = False
            if self._eval_pending:
                self._eval_pending = False
                if self.mode == MODE_ACTIVE and not self._stopped:
                    self._eval_task = self.hass.async_create_task(
                        self.async_evaluate("lux")
                    )

    async def _async_compute_decision(
        self,
    ) -> tuple[Decision | None, float]:
        lux_state = self.hass.states.get(self.lux_entity)
        stale_reason = _lux_stale_reason(lux_state)
        if stale_reason is not None:
            if not self._stale_warned:
                self._stale_warned = True
                self.lux_stale = True
                _LOGGER.warning(
                    "%s: 照度数据不可信（%s），保持当前亮度",
                    self.zone_name,
                    stale_reason,
                )
                self.notify()
            return None, 0.0
        try:
            lux_now = float(lux_state.state)
        except (TypeError, ValueError):
            return None, 0.0
        self.lux_stale = False
        self._stale_warned = False

        curve = self.curve
        if curve is None:
            return (
                Decision("noop", None, lux_now, self.target_lux - lux_now, "no curve"),
                lux_now,
            )

        decision = decide(
            curve,
            lux_now,
            self._current_pct(),
            self.target_lux,
            float(self.opts[CONF_DEADBAND_PCT]),
            float(self.opts[CONF_MIN_BRIGHTNESS_PCT]),
        )
        if decision.action != "off":
            self._off_streak = 0
        elif not self.opts[CONF_AUTO_TURN_OFF]:
            decision = Decision(
                "noop",
                None,
                decision.ambient_lux,
                decision.needed_lux,
                "ambient above target (auto-off disabled)",
            )
        else:
            self._off_streak += 1
            if self._off_streak < 2:
                decision = Decision(
                    "noop",
                    None,
                    decision.ambient_lux,
                    decision.needed_lux,
                    "ambient above target (waiting confirmation)",
                )
        return decision, lux_now

    def _current_pct(self) -> float:
        """Best estimate of the real light's current percent."""
        state = self.hass.states.get(self.light_entity)
        if state is None or state.state != STATE_ON:
            return 0.0
        brightness = state.attributes.get(ATTR_BRIGHTNESS)
        if isinstance(brightness, (int, float)) and brightness > 0:
            return min(100.0, round(brightness / 255.0 * 100, 1))
        if self.commanded_pct is not None:
            return self.commanded_pct
        return 100.0

    async def _apply(self, trigger: str, lux_now: float, decision: Decision) -> None:
        async with self._apply_lock:
            if self.mode != MODE_ACTIVE or self._stopped:
                return  # state moved on / runtime stopping while in flight
            if decision.action == "set":
                ok = await self._async_call_light(
                    "turn_on", {ATTR_BRIGHTNESS_PCT: round(decision.pct)}
                )
                if ok:
                    self.commanded_pct = decision.pct
                    self.last_adjustment = dt_util.utcnow()
                else:
                    decision = replace(
                        decision, reason=f"{decision.reason} (service failed)"
                    )
                line = self._record(trigger, lux_now, decision)
                _LOGGER.debug("%s: %s", self.zone_name, line)
            elif decision.action == "off":
                self.mode = MODE_IDLE
                self.commanded_pct = None
                ok = await self._async_call_light("turn_off", {})
                if ok:
                    _LOGGER.info("%s: 环境光已达目标，自动关灯", self.zone_name)
                else:
                    # Roll back so the loop keeps running; the light stayed on.
                    # Re-check: a calibration may have started during the await.
                    if self.mode == MODE_IDLE:
                        self.mode = MODE_ACTIVE
                    decision = replace(
                        decision, reason=f"{decision.reason} (service failed)"
                    )
                self._record(trigger, lux_now, decision)
            else:
                line = self._record(trigger, lux_now, decision)
                if line is not None:
                    _LOGGER.debug("%s: %s", self.zone_name, line)
            self.notify()

    def _record(self, trigger: str, lux_now: float, decision: Decision) -> str | None:
        """Append one decision line; repeated identical no-ops are suppressed."""
        if (
            decision.action == "noop"
            and self.last_decision is not None
            and self.last_decision.action == "noop"
            and self.last_decision.reason == decision.reason
        ):
            return None
        stamp = dt_util.as_local(dt_util.utcnow()).strftime("%H:%M:%S")
        line = (
            f"{stamp} [{trigger}] lux={lux_now:.0f} "
            f"环境={decision.ambient_lux:.0f} 需求={decision.needed_lux:.0f} "
            f"→ {decision.action}"
            + (f" {decision.pct:.0f}%" if decision.pct is not None else "")
        )
        self.decisions.append(line)
        self.last_decision = decision
        return line

    async def _async_call_light(self, service: str, data: dict) -> bool:
        """Call the light service; False (with a log) when it cannot apply."""
        state = self.hass.states.get(self.light_entity)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.warning("%s: 灯实体不可用，跳过 %s", self.zone_name, service)
            return False
        registry_entry = er.async_get(self.hass).async_get(self.light_entity)
        if registry_entry is not None and registry_entry.platform == DOMAIN:
            _LOGGER.error(
                "%s: 真灯被配置为本集成的虚拟灯，拒绝调用（请重新配置为真实灯）",
                self.zone_name,
            )
            return False
        payload = {"entity_id": self.light_entity, **data}
        try:
            await self.hass.services.async_call(
                "light", service, payload, blocking=True
            )
        except HomeAssistantError as err:
            _LOGGER.warning("%s: 灯服务调用失败(%s): %s", self.zone_name, service, err)
            return False
        return True

    # --------------------------------------------------- virtual light requests

    async def async_turn_on_requested(self, brightness_pct: float | None) -> None:
        """Virtual light turned on; brightness_pct set means manual passthrough."""
        async with self._apply_lock:
            if self.mode == MODE_CALIBRATING:
                _LOGGER.info("%s: 校准进行中，忽略开灯命令", self.zone_name)
                return
            if brightness_pct is not None:
                # 手动直通是用户显式指令（含亮度写入，无照度计算），
                # 有意不做 stale 门禁——与自动分支的 _lux_stale_reason 判据不同是设计。
                self.mode = MODE_PASSTHROUGH
                self._off_streak = 0
                ok = await self._async_call_light(
                    "turn_on", {ATTR_BRIGHTNESS_PCT: round(brightness_pct)}
                )
                if ok:
                    self.commanded_pct = brightness_pct
                self.notify()
                return

            self.mode = MODE_ACTIVE
            self._off_streak = 0
            curve = self.curve
            lux_state = self.hass.states.get(self.lux_entity)
            lux_ok = curve is not None and _lux_stale_reason(lux_state) is None
            if not lux_ok:
                await self._async_call_light("turn_on", {})
                self.commanded_pct = None
                self.notify()
                return
            try:
                lux_now = float(lux_state.state)
            except (TypeError, ValueError):
                await self._async_call_light("turn_on", {})
                self.commanded_pct = None
                self.notify()
                return
            decision = decide(
                curve,
                lux_now,
                self._current_pct(),
                self.target_lux,
                float(self.opts[CONF_DEADBAND_PCT]),
                float(self.opts[CONF_MIN_BRIGHTNESS_PCT]),
            )
            # An explicit turn-on never ends in "off": floor at min brightness.
            if decision.action == "off":
                pct = float(self.opts[CONF_MIN_BRIGHTNESS_PCT])
                recorded = Decision(
                    "set",
                    pct,
                    decision.ambient_lux,
                    decision.needed_lux,
                    "turn-on floored at min brightness",
                )
            else:
                pct = decision.pct
                recorded = decision
            ok = await self._async_call_light(
                "turn_on", {ATTR_BRIGHTNESS_PCT: round(pct)}
            )
            if ok:
                self.commanded_pct = pct
            else:
                recorded = replace(
                    recorded, reason=f"{recorded.reason} (service failed)"
                )
            self._record("turn_on", lux_now, recorded)
            self.notify()

    async def async_turn_off_requested(self) -> None:
        """Virtual light turned off."""
        async with self._apply_lock:
            if self.mode == MODE_CALIBRATING:
                # Remember the intent; honoured after the sweep unwinds.
                self._cal_pending_off = True
                _LOGGER.info(
                    "%s: 校准进行中，关灯请求将在校准结束后执行", self.zone_name
                )
                return
            self.mode = MODE_IDLE
            self.commanded_pct = None
            await self._async_call_light("turn_off", {})
            self.notify()

    # --------------------------------------------------------------- calibration

    async def async_cancel_calibration(self) -> bool:
        """Cancel a running sweep; False when none is running (idempotent).

        The task's own ``finally`` restores the light and resets the mode;
        ``cal_last_error`` stays untouched — a user-initiated cancel is not
        an error.
        """
        if self.mode != MODE_CALIBRATING:
            return False
        task = self._cal_task
        if task is None or task.done():
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if self.mode == MODE_CALIBRATING:
            # Cancelled before the task ever ran (same-tick cancel): its
            # finally never executed, so the mode reset is on us. The light
            # was never touched by the sweep, so no restore is needed.
            self._finish_calibration()
        return True

    def start_calibration(self) -> bool:
        """Kick off a sweep; False when one is already running anywhere.

        Two zones share one bathroom and both presence sensors see both
        lights (DESIGN §2), so concurrent or overlapping sweeps would pollute
        each other's samples — calibration is strictly one-zone-at-a-time,
        and other zones' closed loops are suspended while it runs.
        """
        if self.mode == MODE_CALIBRATING:
            return False
        for other in self.hass.data.get(DOMAIN, {}).get("lux", {}).values():
            if other is self:
                continue
            if other.mode == MODE_CALIBRATING:
                _LOGGER.warning(
                    "%s: 另一区域（%s）校准进行中，同一卫生间一次只能校准一个区",
                    self.zone_name,
                    other.zone_name,
                )
                return False
            if other.mode == MODE_ACTIVE:
                _LOGGER.info(
                    "%s: 暂停 %s 的闭环，避免其调光污染校准采样",
                    self.zone_name,
                    other.zone_name,
                )
                other.mode = MODE_IDLE
                other.notify()
        # Set synchronously so an evaluate already past its mode check gets
        # dropped by the in-lock recheck instead of polluting the baseline.
        self.mode = MODE_CALIBRATING
        self._cal_was_on = False
        self._cal_pending_off = False
        self.cal_last_error = None
        self.cal_progress = "准备中"
        self.notify()
        self._cal_task = self.hass.async_create_task(self._async_run_calibration())
        return True

    async def _async_run_calibration(self) -> None:
        was_on = False
        orig_pct: float | None = None
        captured = False
        try:
            # Let an in-flight evaluate finish (its write lands) before the
            # baseline window opens; cancellation must still propagate.
            eval_task = self._eval_task
            if eval_task is not None and not eval_task.done():
                try:
                    await eval_task
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
            async with self._apply_lock:
                # Let any in-flight light write land before we capture state.
                light_state = self.hass.states.get(self.light_entity)
                was_on = light_state is not None and light_state.state == STATE_ON
                orig_pct = self._attr_pct(light_state) if was_on else None
                self._cal_was_on = was_on
                captured = True
            step = int(self.opts[CONF_CAL_STEP_PCT])
            while True:
                sweep = await self._async_sweep(step)
                if sweep["missed_streak"] >= CAL_TIMEOUT_UPGRADE_STREAK and step < 10:
                    _LOGGER.warning(
                        "%s: 中段连续 %d 档无上报，自动改用 10%% 粗扫",
                        self.zone_name,
                        sweep["missed_streak"],
                    )
                    step = max(CAL_MIN_STEP_PCT, 10)
                    continue
                break
            points = enforce_monotonic(
                smooth3(
                    subtract_baseline(
                        combine_passes(sweep["up"], sweep["down"]), sweep["baseline"]
                    )
                )
            )
            if len(points) < 2:
                raise CalibrationError("有效采样点不足，无法拟合曲线")
            new_curve = build_curve(points)
            if new_curve.max_lux < CAL_MIN_USEFUL_LUX:
                raise CalibrationError(
                    f"灯对传感器几乎没有贡献（最大增量 {new_curve.max_lux:.1f} lx），"
                    "保留原曲线"
                )
            self.curve = new_curve
            await self.storage.async_save_curve(self.curve)
            _LOGGER.info(
                "%s: 校准完成，最大增量 %.0f lx，20-80%% 平均斜率 %.2f lx/%%",
                self.zone_name,
                self.curve.max_lux,
                self.curve.slope_20_80,
            )
        except asyncio.CancelledError:
            raise
        except CalibrationError as err:
            self.cal_last_error = str(err)
            _LOGGER.warning("%s: 校准中止：%s", self.zone_name, err)
        except Exception as err:
            self.cal_last_error = f"内部错误：{err}"
            _LOGGER.exception("%s: 校准异常中止", self.zone_name)
        finally:
            # Sync first (cannot be interrupted), then best-effort restore.
            # A pending turn-off skips the restore: no point flashing the
            # original level just to switch it off again.
            self._finish_calibration()
            if captured and not self._cal_pending_off:
                # Best-effort: cancellation landing on this await abandons it.
                with contextlib.suppress(Exception):
                    await self._async_restore_light(was_on, orig_pct)
            if self._cal_pending_off:
                # The user asked to turn the light off during the sweep.
                self._cal_pending_off = False
                self.mode = MODE_IDLE
                self.commanded_pct = None
                with contextlib.suppress(Exception):
                    await self._async_call_light("turn_off", {})
                self.notify()
            # Wake up any zone that can compensate: IDLE + curve + light on.
            # Covers zones suspended for the sweep as well as ones switched on
            # (by hand or automation) while it ran.
            for other in self.hass.data.get(DOMAIN, {}).get("lux", {}).values():
                if other is self:
                    continue
                light = self.hass.states.get(other.light_entity)
                if (
                    other.mode == MODE_IDLE
                    and other.curve is not None
                    and light is not None
                    and light.state == STATE_ON
                ):
                    _LOGGER.info("%s: 恢复 %s 的闭环", self.zone_name, other.zone_name)
                    other.mode = MODE_ACTIVE
                    other.notify()
                    other._schedule_evaluate("resume")

    def _finish_calibration(self) -> None:
        self.mode = (
            MODE_ACTIVE if self._cal_was_on and self.curve is not None else MODE_IDLE
        )
        self._cal_was_on = False
        self.cal_progress = ""
        self.notify()

    async def _async_sweep(self, step: int) -> dict:
        steps = calibration_steps(step)
        self.cal_progress = "基线（灯全灭）"
        self.notify()
        light_state = self.hass.states.get(self.light_entity)
        # "Already off" must be *certain*: an unavailable light may actually be
        # lit (BLE dropout), and treating its reading as a dark baseline would
        # silently skew the whole curve.
        already_off = (
            light_state is not None
            and light_state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            and light_state.state != STATE_ON
        )
        baseline_samples = await self._async_cal_step(None)
        if not baseline_samples and already_off:
            # Change-driven sensor: a redundant turn-off produces no report.
            # The light was already off, so the current reading IS the baseline.
            cur = self.hass.states.get(self.lux_entity)
            if cur is not None and cur.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                with contextlib.suppress(TypeError, ValueError):
                    baseline_samples = [float(cur.state)]
        if not baseline_samples:
            raise CalibrationError("基线阶段未收到照度数据（传感器无读数）")
        baseline = statistics.median(baseline_samples)
        up: dict[float, float] = {}
        down: dict[float, float] = {}
        missed_streak = 0
        max_missed_streak = 0

        def note_miss(pct: float, gap: float, direction: str) -> None:
            nonlocal missed_streak, max_missed_streak
            # Tiny gaps come from the appended 100% point; a 1-2% delta is
            # near-certain to be sub-threshold and must not trigger a re-sweep.
            if pct >= _CAL_MISSED_COUNT_MIN_PCT and gap >= step:
                missed_streak += 1
                max_missed_streak = max(max_missed_streak, missed_streak)
            _LOGGER.warning(
                "%s: %s %d%% 档未收到上报，跳过该点", self.zone_name, direction, pct
            )

        prev_pct = steps[0]
        for pct in steps[1:]:
            self.cal_progress = f"上行 {pct}%"
            self.notify()
            samples = await self._async_cal_step(float(pct))
            gap = pct - prev_pct
            prev_pct = pct
            if samples:
                up[float(pct)] = statistics.median(samples)
                missed_streak = 0
            else:
                note_miss(pct, gap, "上行")
        missed_streak = 0  # streaks must not chain across passes
        prev_pct = steps[-1]
        for pct in reversed(steps[1:-1]):
            # Down pass starts below 100%: that level was just measured going up.
            self.cal_progress = f"下行 {pct}%"
            self.notify()
            samples = await self._async_cal_step(float(pct))
            gap = prev_pct - pct
            prev_pct = pct
            if samples:
                down[float(pct)] = statistics.median(samples)
                missed_streak = 0
            else:
                note_miss(pct, gap, "下行")
        return {
            "up": up,
            "down": down,
            "baseline": baseline,
            "missed_streak": max_missed_streak,
        }

    async def _async_cal_step(self, pct: float | None) -> list[float]:
        """Set the light (None = off), then wait for a settled lux reading."""
        started = dt_util.utcnow()
        async with self._apply_lock:
            if pct is None:
                await self._async_call_light("turn_off", {})
                self.commanded_pct = None
            else:
                ok = await self._async_call_light("turn_on", {ATTR_BRIGHTNESS_PCT: pct})
                if not ok:
                    # Light stayed at the previous level: sampling now would
                    # record the wrong level as this step's data.
                    _LOGGER.warning(
                        "%s: 校准 %s%% 档调光失败，本档作弃点", self.zone_name, pct
                    )
                    return []
                self.commanded_pct = pct
        await asyncio.sleep(CAL_CMD_SETTLE_S)
        samples: list[float] = []
        last_lc = None
        first_deadline = started + timedelta(seconds=CAL_FIRST_REPORT_TIMEOUT_S)
        quiet_deadline: datetime | None = None
        while True:
            now = dt_util.utcnow()
            state = self.hass.states.get(self.lux_entity)
            if state is not None:
                lc = state.last_changed
                if lc >= started and lc != last_lc:
                    try:
                        value = float(state.state)
                    except (TypeError, ValueError):
                        value = None
                    if value is not None:
                        samples.append(value)
                        last_lc = lc
                        quiet_deadline = now + timedelta(seconds=CAL_QUIET_PERIOD_S)
            if quiet_deadline is not None and now >= quiet_deadline:
                break
            if quiet_deadline is None and now >= first_deadline:
                break
            await asyncio.sleep(CAL_POLL_INTERVAL_S)
        return samples

    async def _async_restore_light(self, was_on: bool, orig_pct: float | None) -> None:
        async with self._apply_lock:
            if was_on:
                # Unknown original level (BLE often hides brightness): turn on
                # without a level so the fixture keeps its own default.
                data = (
                    {} if orig_pct is None else {ATTR_BRIGHTNESS_PCT: round(orig_pct)}
                )
                ok = await self._async_call_light("turn_on", data)
                if ok:
                    self.commanded_pct = orig_pct
            else:
                await self._async_call_light("turn_off", {})
                self.commanded_pct = None

    def _attr_pct(self, state) -> float | None:
        brightness = state.attributes.get(ATTR_BRIGHTNESS) if state else None
        if isinstance(brightness, (int, float)) and brightness > 0:
            return min(100.0, brightness / 255.0 * 100)
        return self.commanded_pct

    # -------------------------------------------------------------- diagnostics

    def diagnostics(self) -> dict:
        curve = self.curve
        data: dict = {
            "mode": self.mode,
            "target_lux": self.target_lux,
            "commanded_pct": self.commanded_pct,
            "lux_stale": self.lux_stale,
            "cal_progress": self.cal_progress,
            "curve_source": curve.source if curve else "none",
            "curve_max_lux": round(curve.max_lux, 1) if curve else None,
            "curve_slope_20_80": round(curve.slope_20_80, 2) if curve else None,
            "last_reason": self.last_decision.reason if self.last_decision else None,
            "last_adjustment": (
                dt_util.as_local(self.last_adjustment).isoformat()
                if self.last_adjustment
                else None
            ),
            "recent_decisions": list(self.decisions),
        }
        if self.last_decision is not None:
            data["ambient_lux_est"] = round(self.last_decision.ambient_lux, 1)
            data["needed_lux"] = round(self.last_decision.needed_lux, 1)
        return data
