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
                     outcome: str | None = None, tier: str | None = None,
                     hours: int = 24):
        async with self._lock:
            return await asyncio.to_thread(self._recent, min(max(limit, 1), 500), domain, outcome, tier, hours)

    def _recent(self, limit, domain, outcome, tier, hours):
        sql = "SELECT timestamp,domain,tier,outcome,http_status,duration_ms,challenge,failure_type FROM requests WHERE timestamp >= ?"
        params = [time.time() - min(max(hours, 1), 168) * 3600]
        if domain:
            sql += " AND domain = ?"
            params.append(domain.lower())
        if outcome:
            sql += " AND outcome = ?"
            params.append(outcome)
        if tier:
            sql += " AND tier = ?"
            params.append(tier)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql, params)]

    async def summary(self, domain: str | None = None, outcome: str | None = None,
                      tier: str | None = None, hours: int = 24):
        async with self._lock:
            return await asyncio.to_thread(self._summary, domain, outcome, tier, hours)

    def _summary(self, domain, outcome, tier, window_hours):
        window_hours = min(max(window_hours, 1), 168)
        bucket_count = min(24, window_hours)
        bucket_seconds = window_hours * 3600 / bucket_count
        start = time.time() - window_hours * 3600
        sql = "SELECT timestamp,outcome,duration_ms,tier,domain FROM requests WHERE timestamp >= ?"
        params = [start]
        if domain:
            sql += " AND domain = ?"
            params.append(domain.lower())
        if outcome:
            sql += " AND outcome = ?"
            params.append(outcome)
        if tier:
            sql += " AND tier = ?"
            params.append(tier)
        buckets = [{"timestamp": start + i * bucket_seconds, "success": 0, "failed": 0} for i in range(bucket_count)]
        durations = []
        tier_counts = {}
        failed_domains = {}
        with closing(self._connect()) as conn:
            for timestamp, result, duration, request_tier, request_domain in conn.execute(sql, params):
                index = min(bucket_count - 1, int((timestamp - start) // bucket_seconds))
                if 0 <= index < bucket_count:
                    buckets[index]["failed" if result == "failed" else "success"] += 1
                    durations.append(duration)
                    tier_counts[request_tier] = tier_counts.get(request_tier, 0) + 1
                    if result == "failed":
                        failed_domains[request_domain] = failed_domains.get(request_domain, 0) + 1
        durations.sort()
        count = len(durations)
        return {
            "hours": buckets, "window_hours": window_hours, "total": count,
            "failed": sum(bucket["failed"] for bucket in buckets),
            "p95_ms": round(durations[max(0, (95 * count + 99) // 100 - 1)], 1) if count else None,
            "tier_counts": tier_counts,
            "top_failed_domains": sorted(failed_domains.items(), key=lambda item: (-item[1], item[0]))[:5],
        }


request_history = RequestHistory()
