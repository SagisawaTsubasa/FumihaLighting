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
import math
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
    CONF_CROSS_FADE,
    CONF_DEADBAND_LUX,
    CONF_DEADBAND_PCT,
    CONF_LIGHT_ENTITY,
    CONF_LINK_GROUP,
    CONF_LUX_ENTITY,
    CONF_MANUAL_SLOPE,
    CONF_MAX_STEP_PCT,
    CONF_MIN_BRIGHTNESS_PCT,
    CONF_SCAN_INTERVAL_S,
    CONF_TARGET_LUX,
    CONF_ZONE_NAME,
    DECISION_RING_SIZE,
    DOMAIN,
    LUX_STALE_AFTER_S,
    LUX_STALE_WARN_INTERVAL_S,
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


def _parse_lux_state(state) -> float | None:
    """float(state.state) 且要求有限（F-104）：NaN/inf 能通过 float()，但
    NaN 混进中值窗口会污染整窗（median 传 NaN）、下游 max(0.0, nan)=0 会
    算出错误亮度下发；非数字字符串同样不可用。web 层 _finite_or_none
    （web.py）同判据——「非有限即不可用」。None = 调用方按 stale 处理。
    """
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


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
        # 本轮校准是否完成过灯态采集：采集前取消时 _cal_was_on 未置位不可信，
        # mode 恢复走现场灯态判定（FL-IMP-27/32）。
        self._cal_captured: bool = False
        # 新曲线一旦落进 self.curve（存盘 await 之前）即不可回退——此后取消
        # 返回 cancelled=False（视同自然完成，FL-IMP-24）。
        self._cal_committed: bool = False
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
        # 照度不可信 WARNING 的时间窗节流（V1.6.0 P3，R11 报告发现 3）：
        # 旧「一次 WARNING」布尔旗标被每个有效样本复位，NaN↔有效交替读数
        # 的抖动传感器每两事件一条 WARNING 刷屏——改 LUX_STALE_WARN_INTERVAL_S
        # 时间窗，与「恢复」解耦（恢复只清窗+复位 lux_stale，不动本时间戳）。
        self._stale_warn_ts: datetime | None = None
        self._eval_busy = False
        self._eval_pending = False
        self._cal_was_on = False
        self._cal_pending_off = False
        self._stopped = False
        # V1.3.0 联动组：同 link_group 标签的条目互认为同一照明空间；
        # ambient 计算扣除组内其他灯对本传感器的贡献（交叉曲线优先，
        # 近似 = 对方主曲线 × cross_fade 兜底）。
        self.link_group: str = str(self.opts.get(CONF_LINK_GROUP) or "")
        self._cross_fade: float = float(self.opts[CONF_CROSS_FADE])
        self._max_step_pct: float = float(self.opts[CONF_MAX_STEP_PCT])
        # 组内其他灯 → 本传感器的贡献曲线（校准协同采样产出），键=对方灯实体
        self._cross_curves: dict[str, Curve] = {}
        # 联动组协同采样暂存（WP3.1，仅校准期存活）：本校准区为 X 时，顺带
        # 记录同组 Y 区传感器读数。键=Y 区灯实体（=交叉曲线存储键，与
        # _cross_curves/async_save_cross_curve 对齐），值={档位→样本列表}
        # （None=基线档）；_cal_cross_last_lc 记各 Y 传感器上次已记读数的
        # last_changed，防同一条读数在轮询中重复入样。填充=_async_cal_step、
        # 消费=_async_publish_cross_curves、废弃=_async_run_calibration
        # finally（失败/取消一并废弃，不产出半截曲线）。
        self._cal_cross_samples: dict[str, dict[float | None, list[float]]] = {}
        self._cal_cross_last_lc: dict[str, datetime | None] = {}
        # 传感器读数滑动窗口（V1.2.2）：评估用中值而非瞬时值——变化触发
        # 上报的量化噪声（±10 lx 级抖动）直通闭环会造成亮度狩猎。
        self._lux_window: deque[float] = deque(maxlen=5)

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
        # V1.3.0 联动组：组内其他灯对本传感器的交叉贡献曲线
        self._cross_curves = await self.storage.async_load_cross_curves()

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
        # V1.3.0 开关语义: no restart self-heal — a light left on by another
        # module must not silently become a compensation target. The loop
        # resumes when the virtual light is switched on again (automation or
        # manual).
        # V1.3.0 联动组: 组内其他灯的开关/调光影响本区 ambient——状态变化
        # 触发本区重评估（仅评估，不接管对方的灯）。
        if self.link_group:
            for other in self.hass.data.get(DOMAIN, {}).get("lux", {}).values():
                if other is self:
                    continue
                if other.link_group == self.link_group:
                    self._unsubs.append(
                        async_track_state_change_event(
                            self.hass, [other.light_entity], self._on_linked_event
                        )
                    )

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
        # V1.3.0 开关语义: IDLE = hands off. The loop is entered ONLY via the
        # virtual light (async_turn_on_requested) — an external switch-on
        # (night-light module, manual) never revives compensation here, or
        # the two modules would fight over the same fixture.
        self.notify()

    @callback
    def _on_linked_event(self, event: Event[EventStateChangedData]) -> None:
        """联动组内其他灯的状态变化——本区 ACTIVE 时重评估 ambient。"""
        if self.mode == MODE_ACTIVE:
            self._schedule_evaluate("linked")

    @callback
    def _on_lux_event(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        lux_now = _parse_lux_state(new_state)
        if lux_now is None:
            # F-104：非数字/非有限读数按数据不可信处理（WARNING 时间窗节流、
            # 清滤波窗口，V1.6.0 P3）；不排评估——闭环在 _async_compute_
            # decision 同判据处保持当前亮度
            self._mark_lux_stale("照度读数非数字或非有限值")
            return
        if self.lux_stale:
            # 恢复：清窗防断档前后读数混算（F-104）；只复位数据可信态，
            # 不复位 WARNING 节流（交替抖动下每事件重来=刷屏，R11 发现 3）
            self._lux_window.clear()
        self.lux_stale = False
        self._lux_window.append(lux_now)
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

    def _mark_lux_stale(self, reason: str) -> None:
        """照度数据不可信的统一收口（F-104）：置 lux_stale 供页面显示、清空
        滤波窗口——坏值与断档前的旧读数不得再进中值（V1.2.2 滤波只服务
        可信数据）。WARNING 按时间窗节流（LUX_STALE_WARN_INTERVAL_S，V1.6.0
        P3）：坏值期内至多一条，NaN↔有效交替读数同样至多 5 分钟一条（旧
        「一次」布尔节流被每个有效样本复位，交替抖动每两事件刷屏——R11
        报告发现 3）；节流窗口内的重复坏值落 DEBUG 可追。恢复（下一有效
        读数）只复位数据可信态，不动节流时间戳。"""
        self._lux_window.clear()
        now = dt_util.utcnow()
        due = (
            self._stale_warn_ts is None
            or (now - self._stale_warn_ts).total_seconds()
            >= LUX_STALE_WARN_INTERVAL_S
        )
        was = self.lux_stale
        self.lux_stale = True
        if due:
            self._stale_warn_ts = now
        if due or not was:
            # due=节流窗口到期（响 WARNING）；not was=坏值期重入（状态从
            # 可信→不可信，页面须刷新）——两者之外零 notify（交替抖动不打
            # 满信号面）
            _LOGGER.log(
                logging.WARNING if due else logging.DEBUG,
                "%s: 照度数据不可信（%s），保持当前亮度", self.zone_name, reason,
            )
            self.notify()

    def _linked_runtimes(self) -> list[ZoneRuntime]:
        """同联动组的其他 runtime（V1.3.0）；未设标签返回空。"""
        if not self.link_group:
            return []
        return [
            other
            for other in self.hass.data.get(DOMAIN, {}).get("lux", {}).values()
            if other is not self and other.link_group == self.link_group
        ]

    def _external_contribution(self) -> float:
        """组内其他灯对本传感器的当前照度贡献（FL-IMP 联动组）。

        交叉曲线优先（校准协同采样产出，键=对方灯实体）；无交叉曲线时
        近似 = 对方主曲线 × cross_fade。灯灭/读不到按 0。
        """
        total = 0.0
        for other in self._linked_runtimes():
            state = self.hass.states.get(other.light_entity)
            if state is None or state.state != STATE_ON:
                continue
            brightness = state.attributes.get(ATTR_BRIGHTNESS)
            if isinstance(brightness, (int, float)) and brightness > 0:
                other_pct = min(100.0, brightness / 255.0 * 100)
            elif other.commanded_pct is not None:
                other_pct = other.commanded_pct
            else:
                continue
            if other_pct <= 0:
                continue
            cross = self._cross_curves.get(other.light_entity)
            if cross is not None:
                total += cross.lux_at(other_pct)
            elif other.curve is not None:
                total += other.curve.lux_at(other_pct) * self._cross_fade
        return total

    async def _async_compute_decision(
        self,
    ) -> tuple[Decision | None, float]:
        lux_state = self.hass.states.get(self.lux_entity)
        stale_reason = _lux_stale_reason(lux_state)
        if stale_reason is not None:
            self._mark_lux_stale(stale_reason)
            return None, 0.0
        lux_now = _parse_lux_state(lux_state)
        if lux_now is None:
            # F-104：非数字/非有限值与 stale 同路——闭环不得拿坏数据调灯
            self._mark_lux_stale("照度读数非数字或非有限值")
            return None, 0.0
        if self.lux_stale:
            # 恢复：清窗防断档前后读数混算（F-104）；只复位数据可信态，
            # 不复位 WARNING 节流（与 _on_lux_event 恢复分支同语义）
            self._lux_window.clear()
        self.lux_stale = False
        # V1.2.2 读数滤波：瞬时值含量化噪声，中值窗口压平抖动
        self._lux_window.append(lux_now)
        lux_now = statistics.median(self._lux_window)
        # V1.3.0 联动组: 扣除组内其他灯的贡献——否则对方的补偿会被本区
        # 当成环境扰动，多灯同房间时互相拉锯（震荡根因）。先中值滤波压平
        # 本传感器噪声，再扣外部贡献（对方灯状态属性无量化抖动问题）。
        lux_now = max(0.0, lux_now - self._external_contribution())

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
            deadband_lux=float(self.opts[CONF_DEADBAND_LUX]),
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
            lux_now = _parse_lux_state(lux_state)
            if lux_now is None:
                # F-104：非数字/非有限值与 stale 同路——不拿坏数据算亮度，
                # 无级 turn_on 由灯保持自身默认
                self._mark_lux_stale("照度读数非数字或非有限值")
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
            # V1.3.0 开关语义: off = hands off. The real light is left as-is
            # (user/night-light module owns it while compensation is off).
            self.notify()

    # --------------------------------------------------------------- calibration

    async def async_cancel_calibration(self) -> bool:
        """Cancel a running sweep; False when none is running (idempotent).

        The task's own ``finally`` restores the light and resets the mode;
        ``cal_last_error`` stays untouched — a user-initiated cancel is not
        an error.
        """
        if self.mode != MODE_CALIBRATING or self._cal_committed:
            # 已落盘新曲线后取消：视同自然完成（FL-IMP-24）。
            return False
        task = self._cal_task
        if task is None or task.done():
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if self.mode == MODE_CALIBRATING:
            # 同刻取消（task 未起步）：finally 不会执行，收尾由这里完成
            # （_finish_calibration 对未采集走现场灯态判定，FL-IMP-27）；
            # 灯未被 sweep 触碰无需 restore。
            self._finish_calibration()
            # V1.3.0 开关语义: pending = 收尾后放手到 IDLE（用户明确要求停，
            # 强制放手态）；无 pending 时保留 _finish_calibration 的判定
            # （未采集→现场灯态 / 已采集→捕获值）。
            if self._cal_pending_off:
                self.mode = MODE_IDLE
                self.commanded_pct = None
            self._cal_pending_off = False
            # 对称唤醒挂起区（动作与 finally 主线一致：resume 评估 + 日志，
            # FL-IMP-28）
            for other in self.hass.data.get(DOMAIN, {}).get("lux", {}).values():
                if other is self or other.mode != MODE_IDLE:
                    continue
                o_light = self.hass.states.get(other.light_entity)
                if (
                    o_light is not None
                    and o_light.state == STATE_ON
                    and other.curve is not None
                ):
                    _LOGGER.info("%s: 恢复 %s 的闭环", self.zone_name, other.zone_name)
                    other.mode = MODE_ACTIVE
                    other.notify()
                    other._schedule_evaluate("resume")
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
        self._cal_committed = False
        self.cal_progress = "准备中"
        self.notify()
        self._cal_task = self.hass.async_create_task(self._async_run_calibration())
        return True

    async def _async_run_calibration(self) -> None:
        was_on = False
        orig_pct: float | None = None
        captured = False
        self._cal_captured = False
        try:
            # Let an in-flight evaluate finish (its write lands) before the
            # baseline window opens; cancellation must still propagate.
            eval_task = self._eval_task
            if eval_task is not None and not eval_task.done():
                try:
                    await eval_task
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001 — 校准不能因前一次评估失败而中止
                    # F-109：静默 pass 会让基线在非预期灯态上采集；WARNING 可追
                    _LOGGER.warning(
                        "%s: 校准前在飞评估失败（继续采集）：%s", self.zone_name, err
                    )
            async with self._apply_lock:
                # Let any in-flight light write land before we capture state.
                light_state = self.hass.states.get(self.light_entity)
                was_on = light_state is not None and light_state.state == STATE_ON
                orig_pct = self._attr_pct(light_state) if was_on else None
                self._cal_was_on = was_on
                captured = True
                self._cal_captured = True
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
            self._cal_committed = True  # 曲线已采纳，此后取消视同完成
            await self.storage.async_save_curve(self.curve)
            # V1.3.0 联动组（WP3.2）：主曲线采纳后产出交叉曲线——失败边界
            # （WP3.4）由调用位置保证：主曲线失败走不到这里，暂存在 finally
            # 一并废弃，不产出半截曲线。
            await self._async_publish_cross_curves()
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
            # V1.3.0 联动组（WP3.4）：协同采样暂存随本次校准废弃——产出已在
            # 主曲线采纳后完成；失败/取消路径丢弃半截数据（错误必须响，由
            # 各失败分支的 WARNING 承担）。
            self._cal_cross_samples = {}
            self._cal_cross_last_lc = {}
            self._finish_calibration()
            if captured and not self._cal_pending_off:
                # Best-effort: cancellation landing on this await abandons it.
                with contextlib.suppress(Exception):
                    await self._async_restore_light(was_on, orig_pct)
            if self._cal_pending_off:
                # V1.3.0 开关语义: pending = 收尾后放手到 IDLE（不再兑现关灯
                # ——关映射实体不牵连原灯）。
                self._cal_pending_off = False
                self.mode = MODE_IDLE
                self.commanded_pct = None
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
        if self._cal_captured:
            # 采集后收尾：_cal_was_on 是校准开始时的真灯态（sweep 期间灯被
            # 校准改写，收尾时的当前态不可用作判定）。
            self.mode = (
                MODE_ACTIVE
                if self._cal_was_on and self.curve is not None
                else MODE_IDLE
            )
        else:
            # 未采集即收尾（同刻取消 / 采集前取消，FL-IMP-27/32）：灯从未被
            # 校准触碰——现场灯态就是校准前状态；_cal_was_on 尚未置位，不可信。
            light = self.hass.states.get(self.light_entity)
            was_on = (
                light is not None
                and light.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
                and light.state == STATE_ON
            )
            self.mode = MODE_ACTIVE if was_on and self.curve is not None else MODE_IDLE
        self._cal_was_on = False
        self._cal_captured = False
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
            if cur is not None:
                cur_value = _parse_lux_state(cur)  # F-104：非有限/非数字不进采样
                if cur_value is not None:
                    baseline_samples = [cur_value]
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
                if not await self._async_call_light("turn_off", {}):
                    # V1.6.0 P3（R11 报告发现 5）：基线关灯失败不得照常采样
                    # ——灯物理仍亮（BLE 瞬断/服务失败）时基线=亮灯读数，
                    # subtract_baseline 对所有档位统一减去偏高基线，产出偏低
                    # 曲线让闭环长期过驱。与 turn_on 失败「本档作弃点」不同：
                    # 基线没有弃点语义，唯一响亮出路是中止校准（页面显示
                    # 「上次校准未能完成」，_async_run_calibration 的 except
                    # CalibrationError 收口）。
                    raise CalibrationError(
                        "基线阶段无法关闭灯具（灯不可用或服务失败），已中止校准"
                    )
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
        # F-102 硬上限：连续上报的变化值会不断把 quiet_deadline 推后，两个
        # 软 break 可永不成立 → _async_cal_step 永不返回、_async_sweep 不
        # 前进、整组恒照度挂起（垃圾传感器每 <20s 报一个新值即可触发）。
        # started 起算 = CAL_FIRST_REPORT_TIMEOUT_S + CAL_QUIET_PERIOD_S，
        # 也是 anl 侧归还预算（manager._CI_RELEASE_RETRY_MAX）的每步最坏口径。
        hard_deadline = started + timedelta(
            seconds=CAL_FIRST_REPORT_TIMEOUT_S + CAL_QUIET_PERIOD_S
        )
        quiet_deadline: datetime | None = None
        while True:
            now = dt_util.utcnow()
            state = self.hass.states.get(self.lux_entity)
            if state is not None:
                lc = state.last_changed
                if lc >= started and lc != last_lc:
                    value = _parse_lux_state(state)
                    if value is not None:
                        samples.append(value)
                        last_lc = lc
                        quiet_deadline = now + timedelta(seconds=CAL_QUIET_PERIOD_S)
            # V1.3.0 联动组协同采样（WP3.1）：同组 Y 区传感器在同一时间窗内
            # 的新读数顺带入样（同一 started 判据与防重 last_changed）。Y 已
            # 被 start_calibration 挂起为 IDLE（不调灯不评估），本窗内其读数
            # 只反映 X 的调光——互斥保证无污染。
            for other in self._linked_runtimes():
                o_state = self.hass.states.get(other.lux_entity)
                if o_state is None:
                    continue
                o_lc = o_state.last_changed
                if o_lc < started or (
                    o_lc == self._cal_cross_last_lc.get(other.light_entity)
                ):
                    continue
                o_value = _parse_lux_state(o_state)
                if o_value is not None:
                    self._cal_cross_last_lc[other.light_entity] = o_lc
                    self._cal_cross_samples.setdefault(
                        other.light_entity, {}
                    ).setdefault(pct, []).append(o_value)
            if now >= hard_deadline:
                _LOGGER.warning(
                    "%s: 校准 %s 档静默期超时（连续上报不停），按已采样本收口",
                    self.zone_name,
                    "基线" if pct is None else f"{pct}%",
                )
                break
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

    async def _async_publish_cross_curves(self) -> None:
        """把协同采样数据产出为各 Y 区的交叉曲线（计划书 §WP3.2）。

        管线与主曲线同型：combine（per-pct 单遍中值）/subtract/smooth3/
        monotonic/build_curve(source="cross")。单 Y 失败只 WARNING+跳过
        （运行时按近似系数兜底），不影响其他 Y、不污染主曲线成功态；质量
        门与主曲线同款（CAL_MIN_USEFUL_LUX），不达门不产出半截曲线。
        """
        for other in self._linked_runtimes():
            try:
                per_pct = self._cal_cross_samples.get(other.light_entity)
                baseline_raw = per_pct.get(None) if per_pct else None
                if not baseline_raw:
                    _LOGGER.warning(
                        "%s: 联动组交叉采样缺 %s 的基线档读数，跳过其交叉曲线"
                        "（运行时按近似系数兜底）",
                        self.zone_name,
                        other.zone_name,
                    )
                    continue
                medians = {
                    pct: statistics.median(samples)
                    for pct, samples in per_pct.items()
                    if pct is not None and samples
                }
                if not medians:
                    _LOGGER.warning(
                        "%s: 联动组交叉采样无 %s 的有效档位读数，跳过其交叉曲线"
                        "（运行时按近似系数兜底）",
                        self.zone_name,
                        other.zone_name,
                    )
                    continue
                points = enforce_monotonic(
                    smooth3(
                        subtract_baseline(
                            combine_passes(medians, {}),
                            statistics.median(baseline_raw),
                        )
                    )
                )
                max_gain = max(v for _, v in points) if points else 0.0
                if len(points) < 2 or max_gain < CAL_MIN_USEFUL_LUX:
                    _LOGGER.warning(
                        "%s: %s 的交叉曲线采样点不足或贡献过低"
                        "（最大增量 %.1f lx），不产出（运行时按近似系数兜底）",
                        self.zone_name,
                        other.zone_name,
                        max_gain,
                    )
                    continue
                cross_curve = build_curve(points, source="cross")
                await other.storage.async_save_cross_curve(
                    self.light_entity, cross_curve
                )
                other._cross_curves[self.light_entity] = cross_curve
                other.notify()
                _LOGGER.info(
                    "%s: 联动组交叉曲线已产出（%s 侧，最大增量 %.0f lx）",
                    self.zone_name,
                    other.zone_name,
                    cross_curve.max_lux,
                )
            except Exception as err:  # noqa: BLE001 — 单 Y 失败不得污染主曲线成功态
                _LOGGER.exception(
                    "%s: 联动组交叉曲线产出失败（%s 侧，跳过）：%s",
                    self.zone_name,
                    other.zone_name,
                    err,
                )

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
