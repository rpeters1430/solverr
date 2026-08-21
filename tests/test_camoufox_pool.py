import asyncio
import unittest
from unittest.mock import patch
from app.solver.browser import CamoufoxPool, _PooledCamoufox
from app.config import settings


class FakeCamoufoxPool(CamoufoxPool):
    """CamoufoxPool with the real AsyncCamoufox launch/close swapped for
    cheap fakes, so pool bookkeeping (acquire/release/recycle/capacity) can
    be tested without spawning a real browser process."""

    def __init__(self, size):
        super().__init__(size)
        self.launch_count = 0
        self.close_count = 0

    async def _launch_instance(self) -> _PooledCamoufox:
        self.launch_count += 1
        return _PooledCamoufox(cm=object(), browser=f"fake-browser-{self.launch_count}", created_at=__import__("time").time())

    async def _close_instance(self, inst: _PooledCamoufox):
        self.close_count += 1


class TestCamoufoxPool(unittest.IsolatedAsyncioTestCase):
    async def test_pool_grows_lazily_up_to_size(self):
        pool = FakeCamoufoxPool(3)
        self.assertEqual(pool.launch_count, 0)
        inst1 = await pool.acquire()
        self.assertEqual(pool.launch_count, 1)
        inst2 = await pool.acquire()
        self.assertEqual(pool.launch_count, 2)
        self.assertNotEqual(inst1.browser, inst2.browser)

    async def test_released_instance_is_reused_not_recreated(self):
        with patch.object(settings, "CAMOUFOX_POOL_RECYCLE_USES", 100), \
             patch.object(settings, "CAMOUFOX_POOL_RECYCLE_SECONDS", 100000):
            pool = FakeCamoufoxPool(2)
            inst = await pool.acquire()
            await pool.release(inst)
            self.assertEqual(pool.launch_count, 1)
            reused = await pool.acquire()
            self.assertEqual(pool.launch_count, 1)
            self.assertEqual(reused.browser, inst.browser)

    async def test_instance_recycled_after_use_budget_exhausted(self):
        with patch.object(settings, "CAMOUFOX_POOL_RECYCLE_USES", 2), \
             patch.object(settings, "CAMOUFOX_POOL_RECYCLE_SECONDS", 100000):
            pool = FakeCamoufoxPool(1)
            inst = await pool.acquire()  # uses=1
            await pool.release(inst)
            inst = await pool.acquire()  # uses=2, hits budget
            await pool.release(inst)     # should recycle: close old, launch new
            self.assertEqual(pool.close_count, 1)
            self.assertEqual(pool.launch_count, 2)

    async def test_acquire_blocks_at_capacity_until_release(self):
        pool = FakeCamoufoxPool(1)
        inst = await pool.acquire()
        self.assertEqual(pool.launch_count, 1)

        acquired_second = asyncio.Event()

        async def acquire_second():
            await pool.acquire()
            acquired_second.set()

        task = asyncio.create_task(acquire_second())
        await asyncio.sleep(0.05)
        self.assertFalse(acquired_second.is_set())  # still waiting, pool at capacity

        await pool.release(inst)
        await asyncio.wait_for(acquired_second.wait(), timeout=1.0)
        self.assertEqual(pool.launch_count, 1)  # reused, no new launch
        task.cancel()

    async def test_close_drains_and_closes_idle_instances(self):
        pool = FakeCamoufoxPool(3)
        i1 = await pool.acquire()
        i2 = await pool.acquire()
        await pool.release(i1)
        await pool.release(i2)
        await pool.close()
        self.assertEqual(pool.close_count, 2)
        self.assertEqual(pool._created, 0)


if __name__ == "__main__":
    unittest.main()
