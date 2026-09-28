"""Bounded, local request history. No URLs, headers, cookies or exception text are stored."""

import asyncio
import os
import sqlite3
import time
from contextlib import closing
from urllib.parse import urlsplit

from app.config import settings


class RequestHistory:
    def __init__(self, path: str | None = None, limit: int | None = None):
        self.path = path or settings.HISTORY_DB_PATH
        self.limit = max(1, limit if limit is not None else settings.HISTORY_MAX_RECORDS)
        self._lock = asyncio.Lock()

    def _connect(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute("""CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY, timestamp REAL NOT NULL, domain TEXT NOT NULL,
            tier TEXT NOT NULL, outcome TEXT NOT NULL, http_status INTEGER,
            duration_ms REAL NOT NULL, challenge TEXT, failure_type TEXT
        )""")
        return conn

    async def record(self, url: str, tier: str, outcome: str, duration_ms: float,
                     http_status: int | None = None, challenge: str | None = None,
                     failure_type: str | None = None):
        # Hostname drops path/query/userinfo; do not persist arbitrary error messages.
        domain = (urlsplit(url).hostname or "unknown").lower()[:253]
        row = (time.time(), domain, tier[:40], outcome[:20], http_status,
               round(duration_ms, 1), (challenge or "")[:40], (failure_type or "")[:80])
        async with self._lock:
            await asyncio.to_thread(self._insert, row)

    def _insert(self, row):
        with closing(self._connect()) as conn:
            with conn:
                conn.execute("INSERT INTO requests (timestamp,domain,tier,outcome,http_status,duration_ms,challenge,failure_type) VALUES (?,?,?,?,?,?,?,?)", row)
                conn.execute("DELETE FROM requests WHERE id <= (SELECT MAX(id) - ? FROM requests)", (self.limit,))

    async def recent(self, limit: int = 100, domain: str | None = None,
                     outcome: str | None = None):
        async with self._lock:
            return await asyncio.to_thread(self._recent, min(max(limit, 1), 500), domain, outcome)

    def _recent(self, limit, domain, outcome):
        sql = "SELECT timestamp,domain,tier,outcome,http_status,duration_ms,challenge,failure_type FROM requests WHERE 1=1"
        params = []
        if domain:
            sql += " AND domain = ?"
            params.append(domain.lower())
        if outcome:
            sql += " AND outcome = ?"
            params.append(outcome)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql, params)]

    async def summary(self, domain: str | None = None, outcome: str | None = None):
        async with self._lock:
            return await asyncio.to_thread(self._summary, domain, outcome)

    def _summary(self, domain, outcome):
        now_hour = int(time.time() // 3600) * 3600
        first_hour = now_hour - 23 * 3600
        sql = "SELECT timestamp,outcome,duration_ms FROM requests WHERE timestamp >= ?"
        params = [first_hour]
        if domain:
            sql += " AND domain = ?"
            params.append(domain.lower())
        if outcome:
            sql += " AND outcome = ?"
            params.append(outcome)
        hours = [{"timestamp": first_hour + i * 3600, "success": 0, "failed": 0} for i in range(24)]
        durations = []
        with closing(self._connect()) as conn:
            for timestamp, result, duration in conn.execute(sql, params):
                index = int((timestamp - first_hour) // 3600)
                if 0 <= index < 24:
                    hours[index]["failed" if result == "failed" else "success"] += 1
                    durations.append(duration)
        durations.sort()
        count = len(durations)
        return {
            "hours": hours, "total": count,
            "failed": sum(hour["failed"] for hour in hours),
            "p95_ms": round(durations[max(0, (95 * count + 99) // 100 - 1)], 1) if count else None,
        }


request_history = RequestHistory()
