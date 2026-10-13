"""Pytest configuration for the Fumiha Lighting integration tests.

Merged stub of the homeassistant module tree: the lux super-set (web/storage/
entity-registry/frontend/http/loader/aiohttp) plus the anl additions
(controllable clock, sun canary, point-in-time recording). Packages added to
sys.path so the carried-over tests can import both by package path
(custom_components.fumiha_lighting.*) and by bare module (logic / manager /
schedule).

这套测试是「桩模式专用」：即使环境里装了真实 HA 也使用桩。
"""

import sys as _sys
import types as _types
from datetime import datetime as _datetime
from datetime import time as _time
from pathlib import Path
from typing import Any as _Any

import pytest as _pytest

Any = _Any  # 桩记录列表的注解用

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT
LUX_PKG = ROOT / "custom_components" / "fumiha_lighting" / "lux"
ANL_PKG = ROOT / "custom_components" / "fumiha_lighting" / "anl"
for _p in (REPO_ROOT, LUX_PKG, ANL_PKG):
    _sys.path.insert(0, str(_p))


def _ha_module(name: str, **attrs: _Any) -> _types.ModuleType:
    mod = _types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    _sys.modules[name] = mod
    return mod


def _identity(x):
    return x


class _ConfigEntry:
    pass


class _ConfigFlowBase:
    """Base providing the flow helpers config_flow.py calls."""

    def __init_subclass__(cls, **kwargs):
        # 吞掉 HA 的 domain= 等类关键字，不转发给 object
        super().__init_subclass__()

    def __init__(self) -> None:
        self.context: dict = {}
        self.unique_id = None
        self._reconfigure_entry = None

    def add_suggested_values_to_schema(self, schema, suggested_values):
        return schema

    async def async_set_unique_id(self, unique_id, raise_on_progress=True):
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self, error=None):
        # tests monkeypatch this to simulate conflicts
        return None

    # HA 真源码里这些是 @callback 同步方法（集成代码不带 await 直接 return）
    def async_show_form(
        self, step_id, data_schema=None, errors=None, description_placeholders=None
    ):
        return {
            "type": "form",
            "step_id": step_id,
            "errors": errors,
            "data_schema": data_schema,
        }

    def async_create_entry(self, title, data):
        return {"type": "create_entry", "title": title, "data": data}

    def async_update_reload_and_abort(
        self, entry, data=None, unique_id=None, title=None
    ):
        if isinstance(entry, dict):
            entry["data"] = data
            entry["unique_id"] = unique_id
            entry["title"] = title
        else:
            if data is not None:
                entry.data = data
            if unique_id is not None:
                entry.unique_id = unique_id
            if title is not None:
                entry.title = title
        return {"type": "abort", "reason": "reconfigure_successful"}

    def _get_reconfigure_entry(self):
        return self._reconfigure_entry


class _Store:
    def __init__(self, hass, version, key, **kwargs):
        self.hass = hass
        self.version = version
        self.key = key

    async def async_load(self):
        return None

    async def async_save(self, data):
        return None

    async def async_remove(self):
        return None


class _Selector:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return None  # 可调用：让 voluptuous 把桩实例当作普通 validator 编译


class _SelectorMode:
    BOX = "box"
    SLIDER = "slider"
    DROPDOWN = "dropdown"
    LIST = "list"


class _HomeAssistantError(Exception):
    pass


_ha_module("homeassistant")
_sys.modules["homeassistant"].__path__ = []
_ha_module("homeassistant.components")
_sys.modules["homeassistant.components"].__path__ = []
_ha_module(
    "homeassistant.components.light",
    ATTR_BRIGHTNESS="brightness",
    ATTR_BRIGHTNESS_PCT="brightness_pct",
    ATTR_COLOR_TEMP_KELVIN="color_temp_kelvin",
    ATTR_TRANSITION="transition",
    DOMAIN="light",
    ColorMode=_types.SimpleNamespace(BRIGHTNESS="brightness"),
    LightEntity=type("LightEntity", (), {}),
)
_ha_module(
    "homeassistant.config_entries",
    ConfigEntry=_ConfigEntry,
    ConfigFlow=type("ConfigFlow", (_ConfigFlowBase,), {}),
    ConfigFlowResult=dict,
    OptionsFlow=type(
        "OptionsFlow",
        (_ConfigFlowBase,),
        {"__init__": lambda self, config_entry=None: _ConfigFlowBase.__init__(self)},
    ),
    SOURCE_RECONFIGURE="reconfigure",
)
_ha_module(
    "homeassistant.const",
    ATTR_ENTITY_ID="entity_id",
    SERVICE_TURN_ON="turn_on",
    STATE_ON="on",
    STATE_OFF="off",
    STATE_UNAVAILABLE="unavailable",
    STATE_UNKNOWN="unknown",
    SUN_EVENT_SUNRISE="sunrise",
    SUN_EVENT_SUNSET="sunset",
)
_ha_module(
    "homeassistant.core",
    Event=type("Event", (), {}),
    EventStateChangedData=type("EventStateChangedData", (), {}),
    HomeAssistant=type("HomeAssistant", (), {}),
    ServiceCall=type("ServiceCall", (), {}),
    State=type("State", (), {}),
    callback=_identity,
    EVENT_HOMEASSISTANT_STARTED="homeassistant_started",
)
_ha_module("homeassistant.exceptions", HomeAssistantError=_HomeAssistantError)
_ha_module("homeassistant.helpers")
_ha_module(
    "homeassistant.helpers.dispatcher",
    async_dispatcher_connect=lambda *a, **k: lambda: None,
    async_dispatcher_send=lambda *a, **k: None,
    dispatcher_send=lambda *a, **k: None,
)

# 定点调度句柄记录：tests 直接检查 (when, callback) 列表
POINT_IN_TIME_CALLS: list[tuple[_Any, _Any]] = []


def _fake_track_point_in_time(hass, action, when):
    POINT_IN_TIME_CALLS.append((when, action))
    return lambda: POINT_IN_TIME_CALLS.remove((when, action))


CALL_LATER_CALLS: list[tuple[float, Any]] = []


def _fake_async_call_later(hass, delay, action):
    CALL_LATER_CALLS.append((delay, action))
    return lambda: CALL_LATER_CALLS.remove((delay, action))


def _fake_track_state_change_event(hass, entities, action):
    return lambda: None


def _fake_track_time_interval(hass, action, interval):
    return lambda: None


_ha_module(
    "homeassistant.helpers.event",
    async_call_later=_fake_async_call_later,
    async_track_point_in_time=_fake_track_point_in_time,
    async_track_state_change_event=_fake_track_state_change_event,
    async_track_time_interval=_fake_track_time_interval,
)
_ha_module(
    "homeassistant.helpers.service",
    async_extract_entity_ids=lambda *a, **k: [],
)
_ha_module(
    "homeassistant.helpers.config_validation",
    config_entry_only_config_schema=lambda domain: None,
    entity_ids=lambda v: v,
)
_ha_module("homeassistant.helpers.storage", Store=_Store)


class _EntityRegistryStub:
    """er.async_get(hass) 的返回物：模块级单例，测试直接塞 _entries_for。"""

    def __init__(self):
        self._entries_for: dict = {}

    def async_get(self, entity_id):
        return None


_ENTITY_REGISTRY = _EntityRegistryStub()


def _async_entries_for_config_entry(registry, entry_id):
    return registry._entries_for.get(entry_id, [])


_ha_module(
    "homeassistant.helpers.entity_registry",
    async_get=lambda hass: _ENTITY_REGISTRY,
    async_entries_for_config_entry=_async_entries_for_config_entry,
)
_ha_module("homeassistant.helpers.device_registry", DeviceInfo=dict)
_ha_module(
    "homeassistant.helpers.selector",
    TextSelector=_Selector,
    TextSelectorConfig=_Selector,
    TextSelectorType=_types.SimpleNamespace(TEXT="text"),
    EntitySelector=_Selector,
    EntitySelectorConfig=_Selector,
    NumberSelector=_Selector,
    NumberSelectorConfig=_Selector,
    NumberSelectorMode=_SelectorMode,
    BooleanSelector=_Selector,
    SelectSelector=_Selector,
    SelectSelectorConfig=_Selector,
    SelectSelectorMode=_SelectorMode,
    TimeSelector=_Selector,
)

# frontend（面板/注册表双路径）
_ha_module(
    "homeassistant.components.frontend",
    DATA_PANELS="frontend_panels",
    async_register_built_in_panel=lambda hass, **kw: hass.data.setdefault(
        "frontend_panels", {}
    ).__setitem__(kw["frontend_url_path"], kw),
    async_remove_panel=lambda hass, path, warn_if_unknown=True: hass.data.get(
        "frontend_panels", {}
    ).pop(path, None),
)


class _StaticPathConfig:
    def __init__(self, url_path, path, cache_headers=True):
        self.url_path = url_path
        self.path = path
        self.cache_headers = cache_headers


class _HomeAssistantView:
    requires_auth = False


def _require_admin(func):
    func._require_admin_applied = True
    return func


_ha_module(
    "homeassistant.components.http",
    HomeAssistantView=_HomeAssistantView,
    StaticPathConfig=_StaticPathConfig,
    require_admin=_require_admin,
)

# loader（manifest 版本缓存戳）
_VERSIONS = {"fumiha_lighting": "1.0.0"}


def _get_loaded_integration(hass, domain):
    class _Loaded:
        version = _VERSIONS.get(domain)

    return _Loaded()


_ha_module("homeassistant.loader", async_get_loaded_integration=_get_loaded_integration)


# sun canary：固定锚点测试不应触达 astral（继承 BaseException 绕过兜底）
class _AstralCanary(BaseException):
    """继承 BaseException：绕过 manager._sun_dt 的 except Exception 兜底。"""


def _astral_canary(*_args, **_kwargs):
    raise _AstralCanary("astral 被调用——固定锚点测试不应触达")


_ha_module("homeassistant.helpers.sun", get_astral_event_date=_astral_canary)


# aiohttp：无条件打桩——真实 Response 的负载对象会让 snapshot 断言面
# 随环境漂移（lux LX-F-018 同款教训）；桩保证 resp.payload 恒为 dict。
_ha_module("aiohttp")
_aio_web = _ha_module("aiohttp.web")
_aio_web.Request = type("Request", (), {})


class _StubResponse:
    def __init__(self, payload=None, status=200):
        self.payload = payload
        self.status = status


_aio_web.Response = _StubResponse
_aio_web.json_response = lambda data, status=200: _StubResponse(
    payload=data, status=status
)


# homeassistant.util.dt：可控时钟（anl）+ 全套 lux 需要的方法
class _FakeClock:
    now_dt = _datetime(2026, 10, 12, 12, 0, 0)


@_pytest.fixture(autouse=True)
def _restore_fake_clock():
    """防止单个测试改写 _FakeClock.now_dt 后泄漏到后续测试（ANL 源仓同款）。"""
    saved = _FakeClock.now_dt
    yield
    _FakeClock.now_dt = saved


_dt_mod = _types.ModuleType("homeassistant.util.dt")
_dt_mod.now = lambda *a, **k: _FakeClock.now_dt


def _parse_time(value):
    try:
        return _time.fromisoformat(value)
    except (TypeError, ValueError):
        return None  # 真实 HA 的 parse_time 失败返回 None


def _parse_datetime(value):
    if not isinstance(value, str):
        raise TypeError("parse_datetime expects a string (as real HA does)")
    try:
        return _datetime.fromisoformat(value)
    except ValueError:
        return None


_dt_mod.parse_time = _parse_time
_dt_mod.parse_datetime = _parse_datetime
_dt_mod.as_local = lambda x: x
_dt_mod.as_utc = lambda x: x
_dt_mod.utcnow = lambda: _datetime.utcnow()
_ha_module("homeassistant.util", dt=_dt_mod)
_util_mod = _sys.modules["homeassistant.util"]
_util_mod.__path__ = []
_sys.modules["homeassistant.util.dt"] = _dt_mod

# 子模块挂到父模块属性上，供 from homeassistant.helpers import X 使用
for name in [m for m in list(_sys.modules) if m.startswith("homeassistant.")]:
    parent_name, _, child = name.rpartition(".")
    parent = _sys.modules.get(parent_name)
    if parent is not None:
        setattr(parent, child, _sys.modules[name])
