import asyncio
import json
import unittest
from app.events import EventBroadcaster
from app.api.dashboard import sse_event_stream


class TestEventBroadcaster(unittest.IsolatedAsyncioTestCase):
    async def test_subscribe_and_broadcast(self):
        broadcaster = EventBroadcaster()
        q1 = broadcaster.subscribe()
        q2 = broadcaster.subscribe()

        await broadcaster.broadcast("solve", {"url": "https://example.com", "status": 200})

        msg1 = json.loads(await q1.get())
        msg2 = json.loads(await q2.get())

        self.assertEqual(msg1["type"], "solve")
        self.assertEqual(msg1["data"]["url"], "https://example.com")
        self.assertEqual(msg1["data"]["status"], 200)

        self.assertEqual(msg2["type"], "solve")
        self.assertEqual(msg2["data"]["url"], "https://example.com")

        broadcaster.unsubscribe(q1)
        broadcaster.unsubscribe(q2)

    async def test_unsubscribe_stops_receiving(self):
        broadcaster = EventBroadcaster()
        q = broadcaster.subscribe()
        broadcaster.unsubscribe(q)

        await broadcaster.broadcast("test", {"hello": "world"})
        self.assertTrue(q.empty())


class TestSSEEventStream(unittest.IsolatedAsyncioTestCase):
    async def test_generator_handles_cancellation_without_nameerror(self):
        # Regression test: event_generator() in app/api/dashboard.py used to
        # reference asyncio.CancelledError without importing asyncio, so a
        # normal client disconnect (which cancels the pending `await q.get()`)
        # raised a masking NameError instead of exiting cleanly.
        response = await sse_event_stream()
        agen = response.body_iterator

        first = await agen.__anext__()
        self.assertIn("connected", first)

        # Cancelling the pending `await q.get()` used to raise a masking
        # NameError (asyncio was unimported); the generator now catches the
        # cancellation, unsubscribes, and the async generator simply ends -
        # surfaced to the caller as StopAsyncIteration, not CancelledError.
        task = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(StopAsyncIteration):
            await task


if __name__ == "__main__":
    unittest.main()
