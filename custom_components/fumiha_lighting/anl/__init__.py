"""Night-light group kind: setup/unload wrappers for the merged domain.

The manager itself lives in ``anl/manager.py`` unchanged; this module only
adapts the former top-level integration glue to the two-kind layout
(``hass.data[DOMAIN]["anl"]``). Services are registered at the top level.

No migration hook on purpose (FL-F-001): the config flow creates version-1
entries whose data is already current-shape, and the merged README's migration
path is "delete old entry → recreate" — there are no legacy-shaped entries
for a migration chain to serve, and running one would poison fresh options.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .manager import NightLightManager

_LOGGER = logging.getLogger(__name__)


async def anl_async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one night-light group into the merged data layout."""
    manager = NightLightManager(hass, entry)
    manager.start()
    hass.data[DOMAIN].setdefault("anl", {})[entry.entry_id] = manager
    entry.async_on_unload(manager.stop)
    return True


async def anl_async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload one night-light group."""
    manager: NightLightManager | None = (
        hass.data.get(DOMAIN, {}).get("anl", {}).pop(entry.entry_id, None)
    )
    if manager is not None:
        manager.stop()
    return True
