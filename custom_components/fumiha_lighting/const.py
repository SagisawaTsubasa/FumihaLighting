"""Shared constants for the Fumiha Lighting integration.

Sub-packages keep their own const modules (lux/const.py, anl/const.py) —
both define DOMAIN = "fumiha_lighting" so their modules stay import-identical
to their pre-merge sources. This file holds only the cross-kind contract.
"""

DOMAIN = "fumiha_lighting"

# Entry kind discriminator, stored in entry.data by the config flow.
CONF_KIND = "kind"
KIND_LUX = "lux"
KIND_ANL = "anl"

# Web layer (single static tree, two page modules).
URL_BASE = "/fumiha_lighting"
API_BASE = "/api/fumiha_lighting"

# Page descriptors for the Fumiha Hub registry.
PAGE_LUX_ID = "luxbalance.main"
PAGE_LUX_TITLE = "亮度均衡"
PAGE_LUX_ICON = "mdi:tune-vertical"
PAGE_LUX_ELEMENT = "lux-balance-page"
PAGE_LUX_MODULE = "page-lux.js"
PAGE_LUX_ORDER = 10

PAGE_ANL_ID = "anl.main"
PAGE_ANL_TITLE = "夜灯助手"
PAGE_ANL_ICON = "mdi:weather-night"
PAGE_ANL_ELEMENT = "anl-page"
PAGE_ANL_MODULE = "page-anl.js"
PAGE_ANL_ORDER = 20

STANDALONE_LUX_URL_PATH = "lux-balance"
STANDALONE_ANL_URL_PATH = "auto-night-light"

REGISTRY_KEY = "fumiha_hub_registry"
BUS_EVENT_PAGE_CHANGED = "fumiha_hub_page_changed"
