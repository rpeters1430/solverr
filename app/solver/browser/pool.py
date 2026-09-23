import asyncio
import logging
import time
from typing import Optional

from app.config import settings
from app.solver.browser.models import _PooledCamoufox

try:
    from camoufox.async_api import AsyncCamoufox
    CAMOUFOX_AVAILABLE = True
except ImportError:
    CAMOUFOX_AVAILABLE = False

logger = logging.getLogger("solverr.browser")

# A hung launch (e.g. under PUID/PGID) would otherwise hold _lock and serialize every acquire().
CAMOUFOX_LAUNCH_TIMEOUT_SECONDS = 30


class CamoufoxPool:
    """Bounded pool of warm, no-proxy Camoufox processes, handing out a fresh context per solve.

    Instances are recycled after N uses or N seconds so one fingerprint isn't reused forever.
    """

    def __init__(self, size: int):
        self.size = max(1, size)
        self._idle: "asyncio.Queue[_PooledCamoufox]" = asyncio.Queue()
        self._all_instances: "set[_PooledCamoufox]" = set()
        self._created = 0
        self._lock = asyncio.Lock()
        self.recycles_total = 0

    async def acquire(self) -> _PooledCamoufox:
        try:
            inst = self._idle.get_nowait()
            inst.uses += 1
            return inst
        except asyncio.QueueEmpty:
            pass

        async with self._lock:
            if self._created < self.size:
                inst = await self._launch_instance()
                self._created += 1
                self._all_instances.add(inst)
                inst.uses += 1
                return inst

        # At capacity - wait for a peer to finish and check its instance back in.
        inst = await self._idle.get()
        inst.uses += 1
        return inst

    async def release(self, inst: _PooledCamoufox, force_recycle: bool = False):
        """`force_recycle` evicts an instance that failed at the process level instead of re-queuing it."""
        if force_recycle or self._should_recycle(inst):
            self.recycles_total += 1
            self._all_instances.discard(inst)
            await self._close_instance(inst)
            async with self._lock:
                self._created -= 1
                fresh = await self._relaunch_with_retry()
                if fresh is not None:
                    self._created += 1
                    self._all_instances.add(fresh)
            if fresh is not None:
                self._idle.put_nowait(fresh)
            return
        self._idle.put_nowait(inst)

    async def _relaunch_with_retry(self, attempts: int = 3, backoff_seconds: float = 1.0) -> Optional[_PooledCamoufox]:
        """Retry a recycled instance's relaunch a few times before giving up."""
        for attempt in range(1, attempts + 1):
            try:
                return await self._launch_instance()
            except Exception as e:
                if attempt == attempts:
                    logger.error(
                        f"[CamoufoxPool] Failed to relaunch recycled instance after {attempts} attempt(s) "
                        f"({e}). Pool capacity reduced by 1 until a future acquire() replenishes it."
                    )
                    return None
                logger.warning(f"[CamoufoxPool] Relaunch attempt {attempt}/{attempts} failed ({e}); retrying in {backoff_seconds}s...")
                await asyncio.sleep(backoff_seconds)
        return None

    def _should_recycle(self, inst: _PooledCamoufox) -> bool:
        return (
            inst.uses >= settings.CAMOUFOX_POOL_RECYCLE_USES
            or (time.monotonic() - inst.created_at) >= settings.CAMOUFOX_POOL_RECYCLE_SECONDS
        )

    async def _launch_instance(self) -> _PooledCamoufox:
        cm = AsyncCamoufox(
            headless=settings.HEADLESS,
            humanize=True,
            disable_coop=True,
            os="linux",
            config={'forceScopeAccess': True},
            i_know_what_im_doing=True
        )
        try:
            browser = await asyncio.wait_for(cm.__aenter__(), timeout=CAMOUFOX_LAUNCH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as e:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass
            raise TimeoutError(
                f"Camoufox launch did not complete within {CAMOUFOX_LAUNCH_TIMEOUT_SECONDS}s"
            ) from e
        logger.info(f"[CamoufoxPool] Warmed stealth browser instance ({self._created + 1}/{self.size})")
        return _PooledCamoufox(cm=cm, browser=browser, created_at=time.monotonic())

    async def _close_instance(self, inst: _PooledCamoufox):
        try:
            await inst.cm.__aexit__(None, None, None)
        except Exception as e:
            logger.debug(f"[CamoufoxPool] Instance close notice: {e}")

    async def close(self):
        """Close all pooled instances (idle and in-use) cleanly."""
        async with self._lock:
            while True:
                try:
                    self._idle.get_nowait()
                except asyncio.QueueEmpty:
                    break
            for inst in list(self._all_instances):
                await self._close_instance(inst)
            self._all_instances.clear()
            self._created = 0
            logger.info("[CamoufoxPool] Pool stopped.")
