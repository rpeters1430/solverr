import asyncio
import json
import unittest
from app.events import EventBroadcaster


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


if __name__ == "__main__":
    unittest.main()
