import asyncio
import logging
import time
from typing import Any, Callable, List, Optional, Tuple

from app.config import cgroup_memory_usage, settings
from app.solver.browser.models import _PooledCamoufox

try:
    from camoufox.async_api import AsyncCamoufox
    CAMOUFOX_AVAILABLE = True
except ImportError:
    CAMOUFOX_AVAILABLE = False

logger = logging.getLogger("solverr.browser")

# A hung launch (e.g. under PUID/PGID) would otherwise hold _lock and serialize every acquire().
CAMOUFOX_LAUNCH_TIMEOUT_SECONDS = 30
# A wedged Firefox can hang its shutdown handshake forever; don't let that pin a release().
CAMOUFOX_CLOSE_TIMEOUT_SECONDS = 10


# Never let a request meant for a proxy leave directly when the proxy fails, and resolve SOCKS
# hostnames on the proxy, not locally. Applied over USER_PREFS so they can't be switched off.
PROXY_SAFETY_PREFS = {
    "network.proxy.failover_direct": False,
    "network.proxy.socks_remote_dns": True,
}


def firefox_user_prefs() -> dict:
    """A fresh dict per launch: Camoufox adds its own defaults to the one it is given."""
    return {**settings.USER_PREFS, **PROXY_SAFETY_PREFS}


def browser_is_connected(browser: Any) -> bool:
    """Playwright exposes is_connected(); keep compatibility with test doubles."""
    try:
        probe = getattr(browser, "is_connected", None)
        return bool(probe()) if callable(probe) else True
    except Exception:
        return False


class CamoufoxPool:
    """Bounded pool of warm, no-proxy Camoufox processes, handing out a fresh context per solve.

    Instances are recycled after N uses or N seconds so one fingerprint isn't reused forever.
    """

    def __init__(self, size: int, memory_usage: Optional[Callable[[], Optional[Tuple[int, int]]]] = None):
        self.size = max(1, size)
        # Holds idle instances, plus a None per slot freed without a replacement: that None wakes an
        # acquire() blocked at capacity so it launches into the slot instead of waiting out its timeout.
        self._idle: "asyncio.Queue[Optional[_PooledCamoufox]]" = asyncio.Queue()
        self._freed_slot_tokens = 0
        self._all_instances: "set[_PooledCamoufox]" = set()
        self._created = 0
        self._lock = asyncio.Lock()
        self._memory_usage = memory_usage or cgroup_memory_usage
        self.recycles_total = 0
        self.dead_reclaimed_total = 0
        self.idle_retired_total = 0
        self.memory_recycles_total = 0

    async def acquire(self, wait_timeout: Optional[float] = None) -> _PooledCamoufox:
        """`wait_timeout` bounds only the at-capacity wait for a peer's instance, never a launch.

        A browser that died while idle is never handed out: it is dropped and the slot relaunched."""
        deadline = None if wait_timeout is None else time.monotonic() + wait_timeout
        while True:
            try:
                inst = self._idle.get_nowait()
            except asyncio.QueueEmpty:
                inst = None
            else:
                if inst is None:
                    self._freed_slot_tokens -= 1
                    continue
                if browser_is_connected(inst.browser):
                    inst.uses += 1
                    return inst
                await self._drop_dead(inst)
                continue

            async with self._lock:
                if self._created < self.size:
                    inst = await self._launch_instance()
                    self._created += 1
                    self._all_instances.add(inst)
                    inst.uses += 1
                    return inst

            # At capacity - wait for a peer to finish and check its instance back in.
            if deadline is None:
                inst = await self._idle.get()
            else:
                inst = await asyncio.wait_for(self._idle.get(), timeout=max(0.0, deadline - time.monotonic()))
            if inst is None:
                # A slot was freed without a replacement: go launch into it.
                self._freed_slot_tokens -= 1
                continue
            if browser_is_connected(inst.browser):
                inst.uses += 1
                return inst
            await self._drop_dead(inst)

    async def release(self, inst: _PooledCamoufox, force_recycle: bool = False):
        """`force_recycle` evicts an instance that failed at the process level instead of re-queuing it."""
        memory_pressure = self._over_memory_threshold()
        if force_recycle or memory_pressure or self._should_recycle(inst):
            self.recycles_total += 1
            if memory_pressure:
                self.memory_recycles_total += 1
                logger.info("[CamoufoxPool] Container memory pressure: retiring a browser instance on release.")
            self._all_instances.discard(inst)
            await self._close_instance(inst)
            async with self._lock:
                self._created -= 1
                # Below the headroom a warm replacement could tip the container into the OOM killer;
                # the next acquire() launches one on demand instead.
                fresh = await self._relaunch_with_retry() if self._has_replacement_headroom() else None
                if fresh is not None:
                    self._created += 1
                    self._all_instances.add(fresh)
            if fresh is not None:
                fresh.last_used_at = time.monotonic()
                self._idle.put_nowait(fresh)
            else:
                self._freed_slot_tokens += 1
                self._idle.put_nowait(None)
            return
        inst.last_used_at = time.monotonic()
        self._idle.put_nowait(inst)

    async def maintain(self) -> None:
        """Drop idle instances whose browser died, and retire those idle past
        CAMOUFOX_POOL_IDLE_TIMEOUT_SECONDS. Both relaunch lazily on the next acquire()."""
        idle_timeout = settings.CAMOUFOX_POOL_IDLE_TIMEOUT_SECONDS
        now = time.monotonic()
        keep: List[_PooledCamoufox] = []
        dead: List[_PooledCamoufox] = []
        retire: List[_PooledCamoufox] = []
        # Drained and refilled with no await in between, so acquire() never sees a missing instance.
        while True:
            try:
                inst = self._idle.get_nowait()
            except asyncio.QueueEmpty:
                break
            if inst is None:
                keep.append(inst)
            elif not browser_is_connected(inst.browser):
                dead.append(inst)
            elif idle_timeout > 0 and now - (inst.last_used_at or inst.created_at) >= idle_timeout:
                retire.append(inst)
            else:
                keep.append(inst)
        for inst in keep:
            self._idle.put_nowait(inst)
        for inst in dead:
            await self._drop_dead(inst)
        for inst in retire:
            self.idle_retired_total += 1
            self._all_instances.discard(inst)
            self._created -= 1
            await self._close_instance(inst)
        if retire:
            logger.info(f"[CamoufoxPool] Retired {len(retire)} idle browser instance(s) after {idle_timeout}s unused.")

    @property
    def idle_count(self) -> int:
        """Idle browser instances, not counting freed-slot wake-ups still in the queue."""
        return max(0, self._idle.qsize() - self._freed_slot_tokens)

    async def _drop_dead(self, inst: _PooledCamoufox) -> None:
        self.dead_reclaimed_total += 1
        logger.warning("[CamoufoxPool] Pooled browser disconnected while idle; dropping it.")
        self._all_instances.discard(inst)
        self._created -= 1
        await self._close_instance(inst)

    def _over_memory_threshold(self) -> bool:
        pct = settings.CAMOUFOX_MEMORY_RECYCLE_PERCENT
        if pct <= 0:
            return False
        usage = self._safe_memory_usage()
        return bool(usage) and usage[0] > usage[1] * pct / 100.0

    def _has_replacement_headroom(self) -> bool:
        usage = self._safe_memory_usage()
        if not usage:
            return True
        working_set, limit = usage
        return limit - working_set >= settings.CAMOUFOX_MIN_REPLACE_HEADROOM_MB * 1024 * 1024

    def _safe_memory_usage(self) -> Optional[Tuple[int, int]]:
        try:
            return self._memory_usage()
        except Exception:
            return None

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
            firefox_user_prefs=firefox_user_prefs(),
            i_know_what_im_doing=True
        )
        try:
            browser = await asyncio.wait_for(cm.__aenter__(), timeout=CAMOUFOX_LAUNCH_TIMEOUT_SECONDS)
        except BaseException as e:
            # Also on cancellation, or the half-launched Firefox process is orphaned.
            try:
                await asyncio.wait_for(cm.__aexit__(None, None, None), timeout=CAMOUFOX_CLOSE_TIMEOUT_SECONDS)
            except BaseException:
                pass
            if isinstance(e, asyncio.TimeoutError):
                raise TimeoutError(
                    f"Camoufox launch did not complete within {CAMOUFOX_LAUNCH_TIMEOUT_SECONDS}s"
                ) from e
            raise
        logger.info(f"[CamoufoxPool] Warmed stealth browser instance ({self._created + 1}/{self.size})")
        return _PooledCamoufox(cm=cm, browser=browser, created_at=time.monotonic())

    async def _close_instance(self, inst: _PooledCamoufox):
        try:
            await asyncio.wait_for(inst.cm.__aexit__(None, None, None), timeout=CAMOUFOX_CLOSE_TIMEOUT_SECONDS)
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
            self._freed_slot_tokens = 0
            logger.info("[CamoufoxPool] Pool stopped.")
