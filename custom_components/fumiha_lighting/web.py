"""Web layer: two page modules over one static tree + REST views.

Pages talk to the runtimes through THESE views (own-REST architecture):
entities are the HA-native adapter layer, never the GUI backend. Hub
registration is per kind — each kind's descriptor lives and dies with its
own entries (lux descriptor gone when no lux entries remain, anl likewise).
"""

from __future__ import annotations

import copy
import logging
import math
from datetime import time as _dt_time
from pathlib import Path

from aiohttp import web

from homeassistant.components import frontend
from homeassistant.components.http import (
    HomeAssistantView,
    StaticPathConfig,
    require_admin,
)
from homeassistant.core import (
    EVENT_HOMEASSISTANT_STARTED,
    Event,
    HomeAssistant,
    callback,
)
from homeassistant.helpers.event import async_call_later
from homeassistant.loader import async_get_loaded_integration
from homeassistant.util import dt as dt_util

from .anl.config_schema import (
    CONFIG_KEYS,
    _finite_number,
    limits_payload,
    validate_extras,
    validate_overrides,
    validate_params,
    validate_times,
)
from .anl.const import (
    CONF_CUSTOM_PER_LIGHT,
    CONF_DAY_CI_ENABLED,
    CONF_DAY_CI_LIGHTS,
    CONF_DAY_CI_VIRTUAL_LIGHT,
    CONF_DAY_ENABLED,
    CONF_END_TIME,
    CONF_END_TRANSITION,
    CONF_EXTRA_COUNT,
    CONF_EXTRA_ENABLED,
    CONF_EXTRAS,
    CONF_LIGHTS,
    CONF_OVERRIDES,
    CONF_START_TRANSITION,
    CONF_TRANSITION_ENABLED,
    CONF_TRIGGER_TIME,
    EXTRA_START,
    OVR_EXTRAS,
)
from .anl.const import DOMAIN as _  # noqa: F401 — 子包自证
from .anl.manager import (
    async_release_virtual_light,
    entry_effective_day_ci_lights,
)
from .const import (
    API_BASE,
    BUS_EVENT_PAGE_CHANGED,
    DOMAIN,
    KIND_ANL,
    KIND_LUX,
    PAGE_ANL_ELEMENT,
    PAGE_ANL_ICON,
    PAGE_ANL_ID,
    PAGE_ANL_MODULE,
    PAGE_ANL_ORDER,
    PAGE_ANL_TITLE,
    PAGE_LUX_ELEMENT,
    PAGE_LUX_ICON,
    PAGE_LUX_ID,
    PAGE_LUX_MODULE,
    PAGE_LUX_ORDER,
    PAGE_LUX_TITLE,
    REGISTRY_KEY,
    STANDALONE_ANL_URL_PATH,
    STANDALONE_LUX_URL_PATH,
    URL_BASE,
)
from .light_registry import build_light_zone_index
from .lux.const import (
    CONF_CROSS_FADE,
    CONF_LINK_GROUP,
    CONF_MAX_STEP_PCT,
    LUX_STALE_AFTER_S,
    TARGET_MAX_LUX,
    TARGET_MIN_LUX,
    TARGET_STEP_LUX,
    merged_options,
)

_LOGGER = logging.getLogger(__name__)

WEB_DATA_KEY = f"{DOMAIN}_web_registered"


def _manifest_version(hass: HomeAssistant) -> str:
    try:
        return async_get_loaded_integration(hass, DOMAIN).version or "dev"
    except Exception:  # noqa: BLE001 — version is only a cache buster
        _LOGGER.debug("Could not resolve integration version for page URL")
        return "dev"


def _descriptor(page_id: str, version: str) -> dict:
    if page_id == PAGE_LUX_ID:
        return {
            "id": PAGE_LUX_ID,
            "title": PAGE_LUX_TITLE,
            "icon": PAGE_LUX_ICON,
            "order": PAGE_LUX_ORDER,
            "version": version,
            "module_url": f"{URL_BASE}/{PAGE_LUX_MODULE}?ver={version}",
            "element": PAGE_LUX_ELEMENT,
        }
    return {
        "id": PAGE_ANL_ID,
        "title": PAGE_ANL_TITLE,
        "icon": PAGE_ANL_ICON,
        "order": PAGE_ANL_ORDER,
        "version": version,
        "module_url": f"{URL_BASE}/{PAGE_ANL_MODULE}?ver={version}",
        "element": PAGE_ANL_ELEMENT,
    }


# --------------------------------------------------------------------------
# lux views
# --------------------------------------------------------------------------


def _lux_runtimes(hass: HomeAssistant) -> dict:
    return (hass.data.get(DOMAIN) or {}).get("lux", {})


def _anl_managers(hass: HomeAssistant) -> dict:
    return (hass.data.get(DOMAIN) or {}).get("anl", {})


def _finite_or_none(value, ndigits: int) -> float | None:
    # ANL-F-156：口径统一走 _finite_number——旧码只捕 (TypeError, ValueError)，
    # 被手改 store 里的超大整数 float() 抛 OverflowError → overview 500
    f = _finite_number(value)
    return round(f, ndigits) if f is not None else None


def _serialize_curve(curve) -> dict | None:
    if curve is None:
        return None
    points: list[list[float]] = []
    for pct, lux in curve.points:
        p = _finite_or_none(pct, 1)
        v = _finite_or_none(lux, 2)
        if p is None or v is None:
            continue
        points.append([p, v])
    if len(points) < 2:
        return None
    max_lux = _finite_or_none(curve.max_lux, 1)
    if max_lux is None:
        max_lux = max(v for _, v in points)
    slope = _finite_or_none(curve.slope_20_80, 2)
    if slope is None:
        by_pct = dict(points)
        p20, p80 = by_pct.get(20.0), by_pct.get(80.0)
        if p20 is not None and p80 is not None:
            slope = round((p80 - p20) / 60.0, 2)
    return {
        "source": curve.source,
        "max_lux": max_lux,
        "slope_20_80": slope,
        "points": points,
    }


def _serialize_lux_zone(runtime, runtimes=None) -> dict:
    """One zone snapshot; sync reads only, safe in a handler."""
    last = runtime.last_decision
    last_adjustment = (
        dt_util.as_local(runtime.last_adjustment).isoformat()
        if runtime.last_adjustment
        else None
    )
    # V1.3.0 联动组：组标签+同组成员名单（zone_name）。成员在 web 层按
    # 公开属性 link_group 自算（不直调 runtime 私有方法，ANL-F-003 教训）；
    # 未设组两键为 None，页面不渲染联动组 chip。
    link_group = getattr(runtime, "link_group", "") or None
    link_peers = None
    if link_group and runtimes:
        peers = [
            other.zone_name
            for other in runtimes.values()
            if other is not runtime
            and getattr(other, "link_group", "") == link_group
        ]
        link_peers = peers or None
    return {
        "entry_id": runtime.entry_id,
        "zone_name": runtime.zone_name,
        "mode": runtime.mode,
        "cal_progress": runtime.cal_progress,
        "target_lux": _finite_or_none(runtime.target_lux, 1),
        "commanded_pct": (
            _finite_or_none(runtime.commanded_pct, 1)
            if runtime.commanded_pct is not None
            else None
        ),
        "target_min": TARGET_MIN_LUX,
        "target_max": TARGET_MAX_LUX,
        # R1 发现 10：target_step 随载荷下发（const 单一来源注释的同一约定
        # ——TARGET_STEP_LUX 改动时页面不再静默不同步；页面 ?? 5 仅为兜底）
        "target_step": TARGET_STEP_LUX,
        "lux_stale": runtime.lux_stale,
        "cal_last_error": runtime.cal_last_error,
        "stale_limit_min": LUX_STALE_AFTER_S // 60,
        "curve": _serialize_curve(runtime.curve),
        "decisions": list(reversed(runtime.decisions)),  # 最新在上（V1.2.2）
        "ambient_lux_est": (
            _finite_or_none(last.ambient_lux, 1) if last is not None else None
        ),
        "needed_lux": (
            _finite_or_none(last.needed_lux, 1) if last is not None else None
        ),
        "last_adjustment": last_adjustment,
        "light_entity": runtime.light_entity,
        "lux_entity": runtime.lux_entity,
        "link_group": link_group,
        "link_peers": link_peers,
    }


class LuxOverviewView(HomeAssistantView):
    """GET /api/fumiha_lighting/lux/overview — all zones in one payload."""

    url = f"{API_BASE}/lux/overview"
    name = "api:fumiha_lighting:lux_overview"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        runtimes = _lux_runtimes(hass)
        zones = [_serialize_lux_zone(rt, runtimes) for rt in runtimes.values()]
        zones.sort(key=lambda z: z["zone_name"])
        return web.json_response({"zones": zones})


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


async def _json_object(request: web.Request) -> dict | None:
    """POST body → dict；非对象/非法 JSON 返回 None（ANL-F-020：四个 POST
    视图共用，防 null/list/标量 body 触发 AttributeError → 500）。"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed JSON must answer loudly
        return None
    return body if isinstance(body, dict) else None


def _entry_id_of(body: dict) -> str | None:
    """entry_id 必须是字符串（非 str 会让 dict.get 抛 unhashable → 500）。"""
    entry_id = body.get("entry_id")
    return entry_id if isinstance(entry_id, str) else None


def _hms(value):
    """time 对象 → "HH:MM:SS" 字符串（其余原样）；GET 序列化防线（ANL-F-015）。"""
    if isinstance(value, _dt_time):
        return value.strftime("%H:%M:%S")
    return value


class LuxTargetView(HomeAssistantView):
    """POST /api/fumiha_lighting/lux/target — write the target straight to the
    runtime (GUI never goes through the number entity)."""

    url = f"{API_BASE}/lux/target"
    name = "api:fumiha_lighting:lux_target"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        body = await _json_object(request)
        if body is None:
            return _error(400, "请求体不是合法 JSON 对象")
        entry_id = _entry_id_of(body)
        value = body.get("value")
        runtime = _lux_runtimes(hass).get(entry_id)
        if runtime is None:
            return _error(404, "未知区域")
        # ANL-F-156：数值口径单点走 _finite_number——旧码 float(True)=1.0 可
        # 混过范围门、超大整数 float() 抛 OverflowError 不在既有 except 内
        # → 500；统一后 bool/溢出/非有限均归 400
        v = _finite_number(value)
        if v is None:
            return _error(400, "目标照度不是数字")
        if v < TARGET_MIN_LUX or v > TARGET_MAX_LUX:
            return _error(
                400,
                f"目标照度需在 {TARGET_MIN_LUX:g}–{TARGET_MAX_LUX:g} lx 之间",
            )
        runtime.set_target(v)
        return web.json_response(
            {"ok": True, "target_lux": round(runtime.target_lux, 1)}
        )


class LuxCalibrateView(HomeAssistantView):
    """POST /api/fumiha_lighting/lux/calibrate — start one sweep; the GUI
    reads the boolean result instead of relying on service exceptions."""

    url = f"{API_BASE}/lux/calibrate"
    name = "api:fumiha_lighting:lux_calibrate"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        body = await _json_object(request)
        if body is None:
            return _error(400, "请求体不是合法 JSON 对象")
        runtime = _lux_runtimes(hass).get(_entry_id_of(body))
        if runtime is None:
            return _error(404, "未知区域")
        started = runtime.start_calibration()
        if started:
            return web.json_response({"ok": True, "started": True})
        return web.json_response(
            {
                "ok": True,
                "started": False,
                "reason": "本区或另一区域已有校准进行中，同一卫生间一次只能校准一个区",
            }
        )


class LuxCalibrateCancelView(HomeAssistantView):
    """POST /api/fumiha_lighting/lux/calibrate/cancel — abort a running
    sweep and restore the light; idempotent when nothing is running."""

    url = f"{API_BASE}/lux/calibrate/cancel"
    name = "api:fumiha_lighting:lux_calibrate_cancel"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        body = await _json_object(request)
        if body is None:
            return _error(400, "请求体不是合法 JSON 对象")
        runtime = _lux_runtimes(hass).get(_entry_id_of(body))
        if runtime is None:
            return _error(404, "未知区域")
        cancelled = await runtime.async_cancel_calibration()
        return web.json_response({"ok": True, "cancelled": cancelled})


class LuxLinkGroupView(HomeAssistantView):
    """GET/POST /api/fumiha_lighting/lux/link_group — one zone's link group.

    GUI 编辑入口（V1.7.1，套 anl/config 先例）：联动组三键原藏在原生
    options 流第 2 页（「校准与降级」，须先过第 1 页提交），面板内编辑
    统一入口；组员名单可见性超越原生对话框（原生表单只有本条目的输入框）。
    GET ?entry_id= 下发三键现值（merged_options 回填默认）+ ``link_members``
    完整组员名单（复用 overview link_peers 的公开属性算法，含本区——
    编辑弹窗展示完整组员面，前端标「本区」；overview 的 link_peers 排除
    自身是卡片 chip 语义，两键分工不同）。POST {entry_id, link_group,
    cross_fade, max_step_pct} 键全必填（缺键 400——ANL-F-158，停用请
    显式传空串，缺键静默停用是「键 miss 静默降级」形状）：link_group
    strip 归一（lux/flow.py 写入侧
    同语义，空串=停用）、数值范围与 options flow selector 同源（不设第二
    套数字）；过校验后镜像写 entry.options（只替换三键，其余 options 键
    原样保留——options 流保存是全镜像，漏带会把扫描间隔/死区等静默打回
    默认），顶层 update listener 自动 reload 重建 runtime。
    """

    url = f"{API_BASE}/lux/link_group"
    name = "api:fumiha_lighting:lux_link_group"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        runtime = _lux_runtimes(hass).get(request.query.get("entry_id"))
        if runtime is None:
            return _error(404, "未知区域")
        opts = merged_options(runtime.entry.options)
        group = str(opts[CONF_LINK_GROUP] or "").strip()
        members = None
        if group:
            # 与 _serialize_lux_zone 的 link_peers 同一公开属性算法；含本区
            # 且排序，展示面稳定
            members = sorted(
                other.zone_name
                for other in _lux_runtimes(hass).values()
                if getattr(other, "link_group", "") == group
            )
        return web.json_response(
            {
                "ok": True,
                "zone_name": runtime.zone_name,
                "link_group": group or None,
                "cross_fade": opts[CONF_CROSS_FADE],
                "max_step_pct": opts[CONF_MAX_STEP_PCT],
                "link_members": members,
            }
        )

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        body = await _json_object(request)
        if body is None:
            return _error(400, "请求体不是合法 JSON 对象")
        entry_id = _entry_id_of(body)
        if entry_id is None:
            return _error(400, "entry_id 需为字符串")
        runtime = _lux_runtimes(hass).get(entry_id)
        if runtime is None:
            return _error(404, "未知区域")
        # entry 引用自 runtime 快照点取得（await 之后不再重读 hass.data 桶，
        # AnlConfigView 同款并发模式）
        entry = runtime.entry
        # ANL-F-158：缺键 ≠ 空串——cross_fade/max_step_pct 缺键已 400，
        # 标签键缺键被 str(group or "") 吞成空串=静默解散联动组（ambient
        # 交叉扣除无声停用）；留空停用请显式传空串
        if "link_group" not in body:
            return _error(400, "缺少 link_group（留空请显式传空串）")
        group = body.get("link_group")
        if group is not None and not isinstance(group, str):
            return _error(400, "联动组标签需为字符串")
        # 写入侧 strip 归一（lux/flow.py 同语义）：尾空格静默不同组的根治；
        # 空串=停用
        group = str(group or "").strip()
        # 数值解析（ANL-F-153）：bool 是 int 子类（float(True)=1.0 可混过
        # 范围门），超大整数 float() 抛 OverflowError 不在既有 except 内
        # →500——显式拒 bool/非 int-float，OverflowError 一并归入 400
        fade_raw = body.get("cross_fade")
        step_raw = body.get("max_step_pct")
        if (
            isinstance(fade_raw, bool)
            or not isinstance(fade_raw, (int, float))
            or isinstance(step_raw, bool)
            or not isinstance(step_raw, (int, float))
        ):
            return _error(400, "交叉系数与缓变步幅需为数字")
        try:
            cross_fade = float(fade_raw)
            max_step_pct = float(step_raw)
        except (TypeError, ValueError, OverflowError):
            return _error(400, "交叉系数与缓变步幅需为数字")
        # 范围与 options flow selector 同源（lux/flow.py async_step_lux_
        # advanced：cross_fade 0.1~1.0 / max_step_pct 5~50），不设第二套数字
        if not math.isfinite(cross_fade) or not 0.1 <= cross_fade <= 1.0:
            return _error(400, "交叉系数需在 0.1–1.0 之间")
        if not math.isfinite(max_step_pct) or not 5 <= max_step_pct <= 50:
            return _error(400, "缓变步幅需在 5–50 之间")
        options = {
            **entry.options,
            CONF_LINK_GROUP: group,
            CONF_CROSS_FADE: cross_fade,
            CONF_MAX_STEP_PCT: max_step_pct,
        }
        hass.config_entries.async_update_entry(entry, options=options)
        # ANL-F-150：响应带新组的 link_peers（排除自身，overview 同算法）
        # ——前端乐观更新 chip 不致把有组员的组闪成「暂无同组成员」。本
        # entry 的 runtime 属性此刻仍是旧值（reload 尚未发生），故按新
        # group 现查桶内其他区，排除自身后无可复用 stale 值
        peers = None
        if group:
            peers = [
                other.zone_name
                for eid, other in _lux_runtimes(hass).items()
                if eid != entry_id
                and getattr(other, "link_group", "") == group
            ]
            peers = peers or None
        return web.json_response(
            {
                "ok": True,
                "link_group": group or None,
                "cross_fade": cross_fade,
                "max_step_pct": max_step_pct,
                "link_peers": peers,
            }
        )


# --------------------------------------------------------------------------
# anl views
# --------------------------------------------------------------------------


def _fmt_time(t) -> str | None:
    return t.strftime("%H:%M") if t is not None else None


def _serialize_anl_manager(mgr, hass: HomeAssistant | None = None) -> dict:
    """One manager snapshot; sync reads only, safe in a handler.

    V1.7.0 反查推导：day_ci_virtual_light 键废除（移交对象由反查表推导，
    页面不再展示组级虚拟灯）；逐灯携带 ci_zone_name（B2 归属标注，逐灯弹
    窗三态提示的数据源）。ANL-F-132：归属与清单解耦——∈ 反查表即给出，
    不再看清单勾选态；「∈区域未勾选」的灯凭它解除禁用并提示「勾选后接管」
    （归属是物理事实，勾选是编辑态，混判会让新开接管在 UI 上不存在）。
    反查失败静默为 None（失效告警由 manager 动作路径负责）；接管是否生效
    由页面按 清单 ∧ ∈区域 ∧ day_ci_active 自行合成。
    """
    index = build_light_zone_index(hass) if hass is not None else {}
    lights = []
    for entity_id, machine in mgr.machines.items():
        lights.append(
            {
                "entity_id": entity_id,
                "state": machine.state.value,
                "manual_override": machine.manual_override,
                "mismatch_steps": machine.mismatch_steps,
                "target": list(machine.target) if machine.target else None,
                "applied": list(machine.applied) if machine.applied else None,
                "last_error": machine.last_error,
                # B2：恒照度区域归属（∈index 即有值，与清单勾选解耦）
                "ci_zone_name": (
                    index[entity_id]["zone_name"]
                    if entity_id in index
                    else None
                ),
            }
        )
    snap = mgr.anchor_snapshot()
    # Unparseable anchors are already filtered (and warned once) inside
    # anchor_snapshot — the web layer never sees them.
    anchors = [
        {"time": _fmt_time(t), "mode": mode}
        for t, mode in snap["anchors"]
        if _fmt_time(t) is not None
    ]
    next_anchor = snap["next_anchor"]
    return {
        "entry_id": mgr.entry.entry_id,
        "title": mgr.entry.title,
        "lights": lights,
        "anchors": anchors,
        "current_mode": snap["current_mode"],
        "next_anchor": next_anchor.isoformat() if next_anchor else None,
        "start_mode": mgr.start_mode,
        "end_mode": mgr.end_mode,
        # 生效判据（ANL-F-041 + F-062）：transition_enabled 是存储派生值，
        # 日间关闭时 end_transition 会被 manager 归零；CI 开时日间侧带还被
        # 过滤——页面 chip 必须读按生效带算的总闸
        "transitions_active": mgr.transitions_active,
        # 仅供诊断（ANL-F-052 附带）：页面禁读，零消费方
        "transition_enabled": mgr.transition_enabled,
        "day_enabled": mgr.day_enabled,
        # V1.6.0 单灯化：day_ci_enabled 布尔键废除，快照携带接管灯清单
        # （诊断面，页面只读 day_ci_active 生效值）
        "day_ci_lights": list(mgr.day_ci_lights),
        # 生效判据（F-061 + F-065 + V1.7.0 反查）：日间模式开 + CI 清单非空
        # 才报 True（逐灯有效性由 ci_zone_name 表达，页面不得拿它谎报接管）
        "day_ci_active": mgr.ci_configured(),
        "respect_manual": mgr.respect_manual,
    }


class AnlSnapshotView(HomeAssistantView):
    """GET /api/fumiha_lighting/anl/snapshot — all managers in one payload."""

    url = f"{API_BASE}/anl/snapshot"
    name = "api:fumiha_lighting:anl_snapshot"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        payload = []
        for mgr in _anl_managers(hass).values():
            payload.append(_serialize_anl_manager(mgr, hass))
        payload.sort(key=lambda m: m["title"])
        return web.json_response({"managers": payload})


class AnlTriggerView(HomeAssistantView):
    """POST /api/fumiha_lighting/anl/trigger — trigger every group."""

    url = f"{API_BASE}/anl/trigger"
    name = "api:fumiha_lighting:anl_trigger"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        # Snapshot before awaiting (async_trigger awaits light services).
        managers = list(_anl_managers(hass).values())
        for mgr in managers:
            await mgr.async_trigger(reason="page")
        return web.json_response({"ok": True, "triggered": len(managers)})


class AnlConfigView(HomeAssistantView):
    """GET/POST /api/fumiha_lighting/anl/config — one group's editable config.

    GET ?entry_id= 返回合并视图的白名单键 + limits（GUI 输入控件范围单一
    来源）。POST body ``{entry_id, patch}``，patch 分节：times / params /
    extras（整表替换）/ overrides（逐灯整灯 upsert）/ overrides_remove
    （恢复全局）。校验经 anl.config_schema（与向导同一份字段规格），通过后
    整份合并配置写入 entry.options（与 options 流相同的全镜像约定），
    update listener 自动 reload 重建 manager。CI 停用/改指时在写条目前
    best-effort 归还旧虚拟灯（F-057；被其他 anl 条目引用时跳过——F-067）。
    """

    url = f"{API_BASE}/anl/config"
    name = "api:fumiha_lighting:anl_config"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        mgr = _anl_managers(hass).get(request.query.get("entry_id"))
        if mgr is None:
            return _error(404, "未知条目")
        merged = {**mgr.entry.data, **mgr.entry.options}
        config = {key: merged[key] for key in CONFIG_KEYS if key in merged}
        # GET 镜像读入迁移（ANL-F-102）：存量条目只有 day_ci_enabled=true
        # （两 CI 新键都缺，CONFIG_KEYS 也不含旧键）时白名单过滤后两键皆无
        # ——面板以 config 为唯一来源，全量提交空清单 → 保存一次静默清零
        # CI 并归还虚拟灯。按生效判据（entry_effective_day_ci_lights 单一
        # 来源）展开灯集全集，与 manager 读入迁移同语义。
        config[CONF_DAY_CI_LIGHTS] = entry_effective_day_ci_lights(merged)
        # time 对象防御性字符串化：TimeSelector 提交后进程内是 datetime.time
        # （HA 重启回读存储才变字符串），直接 json_response 会 500（ANL-F-015）
        for key in (CONF_TRIGGER_TIME, CONF_END_TIME):
            if key in config:
                config[key] = _hms(config[key])
        if isinstance(config.get(CONF_EXTRAS), list):
            config[CONF_EXTRAS] = [
                {k: _hms(v) for k, v in item.items()}
                if isinstance(item, dict)
                else item
                for item in config[CONF_EXTRAS]
            ]
        return web.json_response(
            {"ok": True, "config": config, "limits": limits_payload()}
        )

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        body = await _json_object(request)
        if body is None:
            return _error(400, "请求体不是合法 JSON 对象")
        patch = body.get("patch")
        if not isinstance(patch, dict) or not patch:
            return _error(400, "patch 缺失或为空")
        unknown_sections = sorted(
            set(patch) - {"times", "params", "extras", "overrides", "overrides_remove"}
        )
        if unknown_sections:
            return _error(400, f"未知分节: {', '.join(unknown_sections)}")
        entry_id = _entry_id_of(body)
        if entry_id is None:
            return _error(400, "entry_id 需为字符串")
        # await（request.json）之后不再重读 hass.data 桶——entry 引用在快照点
        # 固定，防并发 reload/卸载把 dict 抽走（trigger_now 同款并发模式）。
        mgr = _anl_managers(hass).get(entry_id)
        if mgr is None:
            return _error(404, "未知条目")
        entry = mgr.entry
        merged = {**entry.data, **entry.options}
        lights = set(merged.get(CONF_LIGHTS, []))
        # 旧 CI 配置快照（F-057/F-058）：归还判据与「存量值放行」都以改动
        # 前的合并值为准；400 路径必须发生在归还之前（校验失败绝不动灯）。
        # V1.6.0 单灯化：生效清单走读入迁移判据（entry_effective_day_ci_
        # lights 单一来源）——存量 day_ci_enabled=true 的条目同样构成引用。
        # V1.7.0 反查推导：虚拟灯配置键废除，归还判据按反查表推导的 zone
        # 集合差集（见归还块）
        old_ci_lights = entry_effective_day_ci_lights(merged)
        # ANL-F-106：跨字段校验门控的存量输入（day_enabled 改前值）。
        old_day_enabled = bool(merged.get(CONF_DAY_ENABLED))

        if "times" in patch:
            clean, err = validate_times(patch["times"])
            if err is not None:
                return _error(400, err)
            merged.update(clean)
            # transition_enabled 恒等派生（与向导 _entry_data 同一语义）
            merged[CONF_TRANSITION_ENABLED] = bool(
                merged.get(CONF_START_TRANSITION, 0)
                or merged.get(CONF_END_TRANSITION, 0)
            )
        if "params" in patch:
            clean, err = validate_params(patch["params"], lights=lights)
            if err is not None:
                return _error(400, err)
            merged.update(clean)
            # day_ci_lights 区域归属校验（V1.7.0 反查推导，A1/A2 同源）：
            # 新增进清单的灯必须 ∈ light_zone_index——移交对象由系统推导，
            # 「灯不属于任何恒照度区域」的接管是无效控制，入口逐灯 400
            # 报名（计划书生效语义：清单保留存储、400 拦无效写入）。
            # ANL-F-134：只校验本次新增（ANL-F-106「校验只打本次改动面」
            # 同哲学）——页面是逐灯开关、每次提交全表，整表校验遇 ≥2 条
            # 失效存量会互锁（取消任一条都因另一条 400，永远摘不掉，且
            # 同单捆绑的 overrides 修改一并被锁）；存量失效项放行，运行时
            # 走失效告警+跳过语义（计划书生效语义）。本集成虚拟恒照度灯
            # 不在反查表（表只收 lux 区真实灯）——F-064 的「清单混入自家
            # 虚拟灯」新增拒绝由此自然覆盖。shape/灯集子集已在
            # validate_params 拒绝（schema 层保留格式校验）。
            ci_lights = clean.get(CONF_DAY_CI_LIGHTS)
            if ci_lights:
                added = [e for e in ci_lights if e not in set(old_ci_lights)]
                if added:
                    index = build_light_zone_index(hass)
                    outside = [e for e in added if e not in index]
                    if outside:
                        return _error(
                            400,
                            "；".join(
                                f"灯 {e} 不属于任何恒照度区域" for e in outside
                            )
                            + "（请先在恒照度侧为该灯配置区域，或取消其接管勾选）",
                        )
        # overrides 一律在深拷贝副本上改动（ANL-F-026）：merged 是浅拷贝，
        # 嵌套 dict 与 entry.options 同对象——就地 prune/upsert 会在后段
        # 校验 400 时构成"已改内存、未触发 reload"的静默部分写入。
        overrides = copy.deepcopy(merged.get(CONF_OVERRIDES) or {})
        pruned = 0
        if "extras" in patch:
            clean, err = validate_extras(patch["extras"])
            if err is not None:
                return _error(400, err)
            # 逐灯 extras 覆盖键是位置序号（manager 消费端契约）。身份判据
            # （ANL-F-028）：序号 i 的 start 与旧表不一致（含增删导致的后移
            # 与等数量的删+加）即视为漂移，精确清除该序号的覆盖；身份未变
            # 的原位参数编辑保留覆盖。
            old_extras = merged.get(CONF_EXTRAS, []) or []
            drifted = {
                i
                for i, item in enumerate(clean)
                if i >= len(old_extras)
                or _hms(old_extras[i].get(EXTRA_START))
                != item[EXTRA_START]
            }
            drifted.update(
                range(len(clean), len(old_extras))
            )  # 缩短时尾部序号全部失位
            if drifted:
                for ovr in overrides.values():
                    if not isinstance(ovr, dict):
                        continue
                    ovr_extras = ovr.get(OVR_EXTRAS)
                    if isinstance(ovr_extras, dict):
                        hit = [
                            idx
                            for idx in ovr_extras
                            if idx.isdigit() and int(idx) in drifted
                        ]
                        for idx in hit:
                            del ovr_extras[idx]
                        pruned += len(hit)
                        if not ovr_extras:
                            ovr.pop(OVR_EXTRAS, None)
            merged[CONF_EXTRAS] = clean
        if "overrides" in patch:
            clean, err = validate_overrides(
                patch["overrides"], lights, len(merged.get(CONF_EXTRAS, []) or [])
            )
            if err is not None:
                return _error(400, err)
            overrides.update(clean)
        if "overrides_remove" in patch:
            remove = patch["overrides_remove"]
            if not isinstance(remove, list) or not all(
                isinstance(x, str) for x in remove
            ):
                return _error(400, "overrides_remove 需为实体 ID 字符串列表")
            unknown = [x for x in remove if x not in lights]
            if unknown:
                return _error(400, "未知灯具: " + ", ".join(unknown))
            for entity_id in remove:
                overrides.pop(entity_id, None)
        if "extras" in patch or "overrides" in patch or "overrides_remove" in patch:
            # prune 清空子表后可能留下 {} 空壳（ANL-F-049）——与「空覆盖拒绝」
            # 校验自相矛盾，且页面把 {} 误判成「未跟随全局」；落盘前清掉
            empty = [k for k, v in overrides.items() if isinstance(v, dict) and not v]
            for k in empty:
                del overrides[k]
            # 深拷副本统一单点落盘（空表也写——清除语义需要覆盖 data 旧值）
            merged[CONF_OVERRIDES] = overrides
        # 死键不再向 options 固化（ANL-F-022/029 + V1.6.0 单灯化 + V1.7.0
        # 反查推导）：无任何消费方——day_ci_enabled 由读入迁移消费、
        # day_ci_virtual_light 由反查表取代，落盘侧按死键惯例 pop
        for dead in (
            CONF_EXTRA_ENABLED,
            CONF_CUSTOM_PER_LIGHT,
            CONF_EXTRA_COUNT,
            CONF_DAY_CI_ENABLED,
            CONF_DAY_CI_VIRTUAL_LIGHT,
        ):
            merged.pop(dead, None)
        # CI 跨字段校验（F-065/F-061，V1.7.0 反查后仅剩一查）：CI 是日间
        # 子功能，接管清单非空而 day_enabled 关是「页面谎报已接管」的配置
        # ——入口就 400，绝不静默落盘（虚拟灯空查随键废除而消失）。
        # ANL-F-106：校验只打本次改动面——仅当 patch 触碰判据输入（两键
        # 唯一入口是 params 分节，_section 按提交键透传）、或解析后组合≠
        # 存量组合时执行（存量值放行）：flow 携带侧不看 day_enabled
        # （_carry_gui_keys），day_ci_lights≠[] + day_enabled=False 的失效
        # 组合（F-077 失效配置语义）可合法存量，未触碰判据输入的保存
        # （逐灯弹窗只发 overrides）不得被无关 400 阻塞；组合变化（主动关
        # 日间）或显式提交判据键仍 400。
        ci_inputs_touched = "params" in patch and any(
            k in patch["params"]
            for k in (
                CONF_DAY_ENABLED,
                CONF_DAY_CI_LIGHTS,
            )
        )
        ci_combo_changed = (
            bool(merged.get(CONF_DAY_CI_LIGHTS)),
            bool(merged.get(CONF_DAY_ENABLED)),
        ) != (bool(old_ci_lights), old_day_enabled)
        if (
            (ci_inputs_touched or ci_combo_changed)
            and merged.get(CONF_DAY_CI_LIGHTS)
            and not merged.get(CONF_DAY_ENABLED)
        ):
            return _error(400, "day_ci_lights 需要先启用日间模式（day_enabled）")
        # 存量迁移回填（ANL-F-102）：patch 未携带 day_ci_lights（旧前端或
        # 非 UI 调用只改时间/参数）且旧生效清单非空、日间仍开 → 按旧清单
        # 回填，防死键 pop 把存量 CI 静默清零。显式提交空列表（新 UI 主动
        # 关闭）不在回填之列，用户语义优先。回填值是存量已生效配置，按
        # 「存量值放行」惯例不再加卡（V1.7.0：虚拟灯前提随键废除而消失，
        # 区域缺失不阻断回填——运行时按失效告警+跳过语义处理）。
        if (
            CONF_DAY_CI_LIGHTS not in merged
            and old_ci_lights
            and merged.get(CONF_DAY_ENABLED)
        ):
            merged[CONF_DAY_CI_LIGHTS] = old_ci_lights
        # CI 停用或改指 → 归还旧 zone 虚拟灯（F-057；V1.7.0 反查推导）：
        # best-effort turn_off（lux off=纯放手），失败 WARNING 不阻断保存；
        # 全部校验通过后才动灯，且 reload 场景（CI 保持开启、灯未变）零
        # 调用。F-067：其他 anl 条目仍引用同一虚拟灯时跳过归还（关掉会
        # 静默终止别组的日间闭环），INFO 可观测。引用判据=生效清单经反查
        # 表推导的 zone 集合差集（day_enabled 关的失效配置不构成引用，
        # F-077 同源——flow 侧 _async_release_stale_virtual 同语义）。
        if old_ci_lights:
            index = build_light_zone_index(hass)
            old_zones = {
                index[entity_id]["virtual_entity_id"]
                for entity_id in old_ci_lights
                if entity_id in index
            }
            new_lights = (
                entry_effective_day_ci_lights(merged)
                if merged.get(CONF_DAY_ENABLED)
                else []
            )
            new_zones = {
                index[entity_id]["virtual_entity_id"]
                for entity_id in new_lights
                if entity_id in index
            }
            # F-083：归还统一走 async_release_virtual_light（lux mode 守卫
            # + 跨条目检查 + best-effort turn_off，anl/manager.py 单一来源）；
            # F-090：配置侧挂点启用 calibrating 有界延迟归还
            for virtual in sorted(old_zones - new_zones):
                await async_release_virtual_light(
                    hass,
                    virtual,
                    exclude_entry_id=entry.entry_id,
                    retry_on_calibrating=True,
                )
        hass.config_entries.async_update_entry(entry, options=merged)
        return web.json_response({"ok": True, "pruned_override_extras": pruned})


# --------------------------------------------------------------------------
# Registration (per-kind descriptors, independent lifecycles)
# --------------------------------------------------------------------------


def _register_page(hass: HomeAssistant, flags: dict, kind: str) -> None:
    """(Re-)register one kind's page in whichever mode fits right now.

    Probe = registry key existence, kept truthful by the hub (created in its
    setup, removed with the entry). Each kind tears down independently.
    """
    page_id = PAGE_LUX_ID if kind == KIND_LUX else PAGE_ANL_ID
    url_path = STANDALONE_LUX_URL_PATH if kind == KIND_LUX else STANDALONE_ANL_URL_PATH
    mode = "hub" if REGISTRY_KEY in hass.data else "standalone"
    flag = f"page_registered_{kind}"
    # 同 kind setup 回填 → 撤销 pending 拆除（reload 窗口零 churn，ANL-F-039）
    if flags.pop(f"pending_teardown_{kind}", None) is not None:
        handle = flags.pop(f"deferred_teardown_{kind}", None)
        if handle is not None:
            handle()
        _LOGGER.debug("%s setup 回填，撤销延迟拆除", page_id)
    current = flags.get(flag)
    if current == mode:
        return
    if current is not None:
        _teardown_page(hass, flags, kind)

    if mode == "hub":
        registry = hass.data.setdefault(REGISTRY_KEY, {})
        if page_id not in registry:
            registry[page_id] = _descriptor(page_id, _manifest_version(hass))
            hass.bus.async_fire(
                BUS_EVENT_PAGE_CHANGED, {"id": page_id, "action": "register"}
            )
            _LOGGER.info("%s 页面已注册到文羽面板壳", page_id)
        flags[flag] = "hub"
        return

    panels = hass.data.get(frontend.DATA_PANELS) or {}
    if url_path not in panels:
        version = _manifest_version(hass)
        title = PAGE_LUX_TITLE if kind == KIND_LUX else PAGE_ANL_TITLE
        icon = PAGE_LUX_ICON if kind == KIND_LUX else PAGE_ANL_ICON
        module = PAGE_LUX_MODULE if kind == KIND_LUX else PAGE_ANL_MODULE
        element = PAGE_LUX_ELEMENT if kind == KIND_LUX else PAGE_ANL_ELEMENT
        frontend.async_register_built_in_panel(
            hass,
            component_name="custom",
            frontend_url_path=url_path,
            sidebar_title=title,
            sidebar_icon=icon,
            config={
                "_panel_custom": {
                    "name": element,
                    "embed_iframe": False,
                    "trust_external": False,
                    "module_url": f"{URL_BASE}/{module}?ver={version}",
                }
            },
            require_admin=True,
        )
        _LOGGER.info("未检测到文羽面板壳，%s 页面注册为独立侧边栏面板", title)
    flags[flag] = "standalone"


def _teardown_page(hass: HomeAssistant, flags: dict, kind: str) -> None:
    page_id = PAGE_LUX_ID if kind == KIND_LUX else PAGE_ANL_ID
    url_path = STANDALONE_LUX_URL_PATH if kind == KIND_LUX else STANDALONE_ANL_URL_PATH
    mode = flags.pop(f"page_registered_{kind}", None)
    if mode == "hub":
        registry = hass.data.get(REGISTRY_KEY)
        if registry is not None and registry.pop(page_id, None) is not None:
            hass.bus.async_fire(
                BUS_EVENT_PAGE_CHANGED, {"id": page_id, "action": "unregister"}
            )
    elif mode == "standalone":
        frontend.async_remove_panel(hass, url_path, warn_if_unknown=False)


def _schedule_deferred_teardown(hass: HomeAssistant, flags: dict, kind: str) -> None:
    """桶空后延迟复查再拆页（ANL-F-039）：设置保存触发的 reload 是
    unload→setup 连续动作，同步拆页会产生 unregister→register 事件对
    （混合安装下保存一次闪一次）。标记 + 延迟复查：同 kind 的 setup 在
    _register_page 开头撤销 pending（桶已回填，注册原样复用）；真删除
    （无 setup 跟进）到点后才真正拆。"""
    flags[f"pending_teardown_{kind}"] = True
    old = flags.pop(f"deferred_teardown_{kind}", None)
    if old is not None:
        old()

    @callback
    def _check(_now) -> None:
        # @callback 必须（ANL-F-045）：async_call_later 经 HassJob 分派，
        # 裸同步函数会进 executor 线程——跨线程 async_fire/拆面板属不支持用法
        flags.pop(f"deferred_teardown_{kind}", None)
        if flags.pop(f"pending_teardown_{kind}", None) is None:
            _LOGGER.debug("%s 延迟拆除被 setup 回填撤销", kind)
            return
        bucket = _lux_runtimes(hass) if kind == KIND_LUX else _anl_managers(hass)
        if not bucket:
            _LOGGER.info("%s 延迟复查：桶仍空，拆除页面", kind)
            _teardown_page(hass, flags, kind)
        else:
            # setup 抢在计时器前回填的竞态窗口（本机制存在的另一半理由）
            _LOGGER.debug("%s 延迟复查：桶已回填，保留页面", kind)
        _teardown_recheck_if_done(hass, flags)

    flags[f"deferred_teardown_{kind}"] = async_call_later(hass, 2, _check)


@callback
def _teardown_recheck_if_done(hass: HomeAssistant, flags: dict) -> None:
    """双 kind 页面注册全空时拆除 started recheck 监听（真删除的收尾）。"""
    if (
        not flags.get("page_registered_lux")
        and not flags.get("page_registered_anl")
        and flags.get("started_recheck")
    ):
        _LOGGER.debug("双 kind 页面已全空，卸下 HA started 复查监听")
        unsub = flags.pop("started_recheck")
        unsub()


async def async_setup_web(hass: HomeAssistant) -> None:
    """Register static assets and the API views (once per HA run), then
    register whichever kinds currently have entries."""
    flags: dict = hass.data.setdefault(WEB_DATA_KEY, {})

    if not flags.get("views"):
        hass.http.register_view(LuxOverviewView())
        hass.http.register_view(LuxTargetView())
        hass.http.register_view(LuxCalibrateView())
        hass.http.register_view(LuxCalibrateCancelView())
        hass.http.register_view(LuxLinkGroupView())
        hass.http.register_view(AnlSnapshotView())
        hass.http.register_view(AnlTriggerView())
        hass.http.register_view(AnlConfigView())
        flags["views"] = True

    if not flags.get("static"):
        static_dir = Path(__file__).parent / "static"
        await hass.http.async_register_static_paths(
            [StaticPathConfig(URL_BASE, str(static_dir), cache_headers=False)]
        )
        flags["static"] = True

    if _lux_runtimes(hass):
        _register_page(hass, flags, KIND_LUX)
    if _anl_managers(hass):
        _register_page(hass, flags, KIND_ANL)

    if not flags.get("started_recheck"):
        flags["started_recheck"] = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, _make_started_recheck(hass, flags)
        )


def _make_started_recheck(hass: HomeAssistant, flags: dict):
    @callback
    def _recheck(event: Event) -> None:
        _LOGGER.debug("HA started: re-checking page registration modes")
        if _lux_runtimes(hass):
            _register_page(hass, flags, KIND_LUX)
        if _anl_managers(hass):
            _register_page(hass, flags, KIND_ANL)

    return _recheck


@callback
def async_unload_web(hass: HomeAssistant) -> None:
    """Called after every entry unload; schedules a deferred teardown for
    each kind whose bucket just went empty (immediate teardown would churn
    unregister/register through every GUI-save reload — ANL-F-039)."""
    flags: dict = hass.data.get(WEB_DATA_KEY) or {}
    if not flags:
        return

    if not _lux_runtimes(hass) and flags.get("page_registered_lux"):
        _schedule_deferred_teardown(hass, flags, KIND_LUX)
    if not _anl_managers(hass) and flags.get("page_registered_anl"):
        _schedule_deferred_teardown(hass, flags, KIND_ANL)
    # 页面从未注册（桶空且无 flag）时无 deferred 可跑，recheck 直接收尾
    _teardown_recheck_if_done(hass, flags)
