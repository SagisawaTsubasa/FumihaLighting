"""Persistent storage for the calibration curve (Home Assistant Store wrapper)."""

from __future__ import annotations

import asyncio
import logging
import math

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .logic import Curve, CurvePoint

STORAGE_VERSION = 1
_LOGGER = logging.getLogger(__name__)

# R1 发现 8：读-改-写按 entry_id 加锁——两个 save 之间有 await，锁保
# 「后写者覆盖」不再丢掉对方的 cross_curves 键。此前靠「校准全局串行」
# 这条跨模块不变量兜底（推理结论而非代码保证），任何 sweep 外新 save
# 路径都会静默丢键。按运行中 loop 记账：生产 HA 单 loop 恒一把锁；测试
# 每用例新 loop（asyncio.run）时换新锁，避免跨 loop 复用抛 RuntimeError。
_SAVE_LOCKS: dict[str, tuple[object, asyncio.Lock]] = {}


def _save_lock(entry_id: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    cached = _SAVE_LOCKS.get(entry_id)
    if cached is None or cached[0] is not loop:
        cached = (loop, asyncio.Lock())
        _SAVE_LOCKS[entry_id] = cached
    return cached[1]


class ZoneStorage:
    """One storage file per config entry; survives restarts, not in git."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._entry_id = entry_id
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
        # ANL-F-139：非有限点（NaN/inf）能穿过解析与 Curve（其唯一异常源是
        # 空点集）——落进决策链后 `max(0, gross-NaN)` 归零 ambient → 决策恒
        # 过驱，全程无日志。整条忽略（不是逐点剔除：采样断续的曲线与缺档
        # 不可区分，宁缺毋错），按 F-104 坏数据 WARNING 口径。
        if not all(math.isfinite(p) and math.isfinite(v) for p, v in points):
            _LOGGER.warning("lux_balance: 校准曲线含非有限值，已忽略并等待重新校准")
            return None
        if len(points) < 2:
            return None
        # Curve 的 ValueError 分支已删（ANL-F-139）：非空 points 不会触发其
        # 唯一异常源（空点集），保留 try/except 会给后来者「已校验」的错觉
        source = str(data.get("source", "calibrated"))
        return Curve(points, source=source)

    async def async_save_curve(self, curve: Curve) -> None:
        # 读-改-写：保留同文件内的 cross_curves（联动组交叉曲线，V1.3.0）
        async with _save_lock(self._entry_id):
            data = await self._store.async_load()
            data = data if isinstance(data, dict) else {}
            await self._store.async_save(
                {
                    **data,
                    "points": [[pct, lux] for pct, lux in curve.points],
                    "source": curve.source,
                    "calibrated_at": dt_util.utcnow().isoformat(),
                }
            )

    async def async_load_cross_curves(self) -> dict[str, Curve]:
        """Load cross curves (group light -> THIS zone's sensor), keyed by the
        other zone's light entity id (V1.3.0 联动组)."""
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return {}
        raw = data.get("cross_curves")
        if not isinstance(raw, dict):
            return {}
        result: dict[str, Curve] = {}
        for light_entity, payload in raw.items():
            if not isinstance(payload, dict):
                continue
            pts = payload.get("points")
            if not isinstance(pts, list):
                continue
            try:
                points = [(float(p), float(v)) for p, v in pts]
            except (TypeError, ValueError):
                # R1 发现 4：有数据但坏了必须响（与主曲线同款 WARNING）——
                # 静默 continue 的唯一表现是联动「好像没生效」（近似系数
                # 与真曲线可差 2 倍以上），用户无从排查
                _LOGGER.warning(
                    "lux_balance: 交叉曲线数据损坏（%s），已忽略该条并按近似系数兜底",
                    light_entity,
                )
                continue
            if len(points) < 2:
                continue
            # ANL-F-139：非有限点整条忽略（同主曲线口径）；Curve 的
            # ValueError 死分支删除——非空点集不触发其唯一异常源
            if not all(math.isfinite(p) and math.isfinite(v) for p, v in points):
                _LOGGER.warning(
                    "lux_balance: 交叉曲线含非有限值（%s），已忽略该条并按近似系数兜底",
                    light_entity,
                )
                continue
            result[str(light_entity)] = Curve(points, source="cross")
        return result

    async def async_save_cross_curve(self, light_entity: str, curve: Curve) -> None:
        """Persist one cross curve, preserving the main curve on disk."""
        async with _save_lock(self._entry_id):
            data = await self._store.async_load()
            data = data if isinstance(data, dict) else {}
            raw = data.get("cross_curves")
            raw = raw if isinstance(raw, dict) else {}
            raw[str(light_entity)] = {
                "points": [[pct, lux] for pct, lux in curve.points],
                "source": curve.source,
                "calibrated_at": dt_util.utcnow().isoformat(),
            }
            await self._store.async_save({**data, "cross_curves": raw})

    async def async_remove_cross_curve(self, light_entity: str) -> None:
        """Drop one cross curve by the other zone's light entity id（R1 发现
        14：改绑/删除后他区 store 里以本灯为键的孤儿曲线回收）。

        键不存在时零写入（幂等 no-op，不产生无谓落盘）。"""
        async with _save_lock(self._entry_id):
            data = await self._store.async_load()
            raw = data.get("cross_curves") if isinstance(data, dict) else None
            if not isinstance(raw, dict) or light_entity not in raw:
                return
            raw.pop(light_entity, None)
            await self._store.async_save({**data, "cross_curves": raw})

    async def async_remove(self) -> None:
        """Drop the storage file when the config entry is deleted."""
        # ANL-F-141：删除也是一条写路径，必须与三个 save 同锁——改绑流程
        # async_remove → async_update_reload_and_abort 之间条目仍 loaded，
        # 校准任务可在 remove 的 await 窗口里 async_save_curve 把旧灯曲线
        # 写回已删文件（新灯下次启动静默加载按旧灯标定的曲线）。
        async with _save_lock(self._entry_id):
            await self._store.async_remove()
        # 锁记账清理：条目删除后不再有任何写路径取锁，残留条目只会让
        # _SAVE_LOCKS 随删条目无限增长。已持旧锁对象的并发等待者不受
        # dict 摘除影响（互斥按对象成立）。
        _SAVE_LOCKS.pop(self._entry_id, None)
