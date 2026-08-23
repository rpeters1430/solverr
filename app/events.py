import asyncio
import json
import time
import logging
from typing import Set, Dict, Any

logger = logging.getLogger("solverr.events")

class EventBroadcaster:
    """Pub/sub event broadcaster for Server-Sent Events (SSE) streaming live activity to dashboard UI."""
    def __init__(self):
        self._subscribers: Set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subscribers.discard(q)

    async def broadcast(self, event_type: str, data: Dict[str, Any]):
        if not self._subscribers:
            return
        payload = json.dumps({"type": event_type, "timestamp": round(time.time(), 3), "data": data})
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except Exception:
                    pass

    def emit(self, event_type: str, data: Dict[str, Any]):
        """Non-blocking event emission helper."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.broadcast(event_type, data))
        except RuntimeError:
            pass

event_broadcaster = EventBroadcaster()
