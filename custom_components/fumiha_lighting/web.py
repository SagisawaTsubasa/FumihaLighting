"""Web layer: two page modules over one static tree + REST views.

Pages talk to the runtimes through THESE views (own-REST architecture):
entities are the HA-native adapter layer, never the GUI backend. Hub
registration is per kind — each kind's descriptor lives and dies with its
own entries (lux descriptor gone when no lux entries remain, anl likewise).
"""

from __future__ import annotations

import logging
import math
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
from homeassistant.loader import async_get_loaded_integration
from homeassistant.util import dt as dt_util

from .anl.const import DOMAIN as _  # noqa: F401 — 子包自证
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
from .lux.const import LUX_STALE_AFTER_S, TARGET_MAX_LUX, TARGET_MIN_LUX

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
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return round(f, ndigits) if math.isfinite(f) else None


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


def _serialize_lux_zone(runtime) -> dict:
    """One zone snapshot; sync reads only, safe in a handler."""
    last = runtime.last_decision
    last_adjustment = (
        dt_util.as_local(runtime.last_adjustment).isoformat()
        if runtime.last_adjustment
        else None
    )
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
        "lux_stale": runtime.lux_stale,
        "cal_last_error": runtime.cal_last_error,
        "stale_limit_min": LUX_STALE_AFTER_S // 60,
        "curve": _serialize_curve(runtime.curve),
        "decisions": list(runtime.decisions),
        "ambient_lux_est": (
            _finite_or_none(last.ambient_lux, 1) if last is not None else None
        ),
        "needed_lux": (
            _finite_or_none(last.needed_lux, 1) if last is not None else None
        ),
        "last_adjustment": last_adjustment,
        "light_entity": runtime.light_entity,
        "lux_entity": runtime.lux_entity,
    }


class LuxOverviewView(HomeAssistantView):
    """GET /api/fumiha_lighting/lux/overview — all zones in one payload."""

    url = f"{API_BASE}/lux/overview"
    name = "api:fumiha_lighting:lux_overview"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        zones = [_serialize_lux_zone(rt) for rt in _lux_runtimes(hass).values()]
        zones.sort(key=lambda z: z["zone_name"])
        return web.json_response({"zones": zones})


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


class LuxTargetView(HomeAssistantView):
    """POST /api/fumiha_lighting/lux/target — write the target straight to the
    runtime (GUI never goes through the number entity)."""

    url = f"{API_BASE}/lux/target"
    name = "api:fumiha_lighting:lux_target"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — malformed JSON must answer loudly
            return _error(400, "请求体不是合法 JSON")
        entry_id = body.get("entry_id")
        value = body.get("value")
        runtime = _lux_runtimes(hass).get(entry_id)
        if runtime is None:
            return _error(404, "未知区域")
        try:
            v = float(value)
        except (TypeError, ValueError):
            return _error(400, "目标照度不是数字")
        if not math.isfinite(v) or v < TARGET_MIN_LUX or v > TARGET_MAX_LUX:
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
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _error(400, "请求体不是合法 JSON")
        runtime = _lux_runtimes(hass).get(body.get("entry_id"))
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
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _error(400, "请求体不是合法 JSON")
        runtime = _lux_runtimes(hass).get(body.get("entry_id"))
        if runtime is None:
            return _error(404, "未知区域")
        cancelled = await runtime.async_cancel_calibration()
        return web.json_response({"ok": True, "cancelled": cancelled})


# --------------------------------------------------------------------------
# anl views
# --------------------------------------------------------------------------


def _fmt_time(t) -> str | None:
    return t.strftime("%H:%M") if t is not None else None


def _serialize_anl_manager(mgr) -> dict:
    """One manager snapshot; sync reads only, safe in a handler."""
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
        "transition_enabled": mgr.transition_enabled,
        "day_enabled": mgr.day_enabled,
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
            payload.append(_serialize_anl_manager(mgr))
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


async def async_setup_web(hass: HomeAssistant) -> None:
    """Register static assets and the API views (once per HA run), then
    register whichever kinds currently have entries."""
    flags: dict = hass.data.setdefault(WEB_DATA_KEY, {})

    if not flags.get("views"):
        hass.http.register_view(LuxOverviewView())
        hass.http.register_view(LuxTargetView())
        hass.http.register_view(LuxCalibrateView())
        hass.http.register_view(LuxCalibrateCancelView())
        hass.http.register_view(AnlSnapshotView())
        hass.http.register_view(AnlTriggerView())
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
    """Called after every entry unload; tears down each kind's page only
    when that kind has no entries left."""
    flags: dict = hass.data.get(WEB_DATA_KEY) or {}
    if not flags:
        return

    if not _lux_runtimes(hass) and flags.get("page_registered_lux"):
        _teardown_page(hass, flags, KIND_LUX)
    if not _anl_managers(hass) and flags.get("page_registered_anl"):
        _teardown_page(hass, flags, KIND_ANL)

    if (
        not flags.get("page_registered_lux")
        and not flags.get("page_registered_anl")
        and flags.get("started_recheck")
    ):
        unsub = flags.pop("started_recheck")
        unsub()
