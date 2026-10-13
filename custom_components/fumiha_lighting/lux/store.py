"""Persistent storage for the calibration curve (Home Assistant Store wrapper)."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .logic import Curve, CurvePoint

STORAGE_VERSION = 1
_LOGGER = logging.getLogger(__name__)


class ZoneStorage:
    """One storage file per config entry; survives restarts, not in git."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")

    async def async_load_curve(self) -> Curve | None:
        """Load the calibrated curve; return None when absent or unreadable."""
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return None
        raw = data.get("points")
        if not isinstance(raw, list):
            return None
        try:
            points: list[CurvePoint] = [(float(p), float(v)) for p, v in raw]
        except (TypeError, ValueError):
            _LOGGER.warning("lux_balance: 校准曲线数据损坏，已忽略并等待重新校准")
            return None
        if len(points) < 2:
            return None
        source = str(data.get("source", "calibrated"))
        try:
            return Curve(points, source=source)
        except ValueError:
            return None

    async def async_save_curve(self, curve: Curve) -> None:
        await self._store.async_save(
            {
                "points": [[pct, lux] for pct, lux in curve.points],
                "source": curve.source,
                "calibrated_at": dt_util.utcnow().isoformat(),
            }
        )

    async def async_remove(self) -> None:
        """Drop the storage file when the config entry is deleted."""
        await self._store.async_remove()
