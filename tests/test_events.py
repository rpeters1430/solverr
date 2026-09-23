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
        # A client disconnect once raised NameError because asyncio wasn't imported.
        response = await sse_event_stream()
        agen = response.body_iterator

        first = await agen.__anext__()
        self.assertIn("connected", first)

        # Cancellation should unsubscribe and end the generator (StopAsyncIteration).
        task = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(StopAsyncIteration):
            await task


if __name__ == "__main__":
    unittest.main()
