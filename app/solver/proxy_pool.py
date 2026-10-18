"""Tier 4 fallback proxies: rotation with failure-based cooldowns.

FALLBACK_PROXY_URL (one) and FALLBACK_PROXY_URLS (comma-separated) are merged into one pool.
Each pick takes the next proxy in round-robin order that isn't cooling down; a proxy that fails
cools down for FALLBACK_PROXY_COOLDOWN_SECONDS, doubling per consecutive failure (capped at 8x),
and a success clears its record. When every proxy is cooling down, the one that recovers first
is used rather than none, since a Tier 4 attempt is the request's last chance anyway."""
import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from app.config import settings
from app.logging_config import sanitize_proxy_url

logger = logging.getLogger("solverr.proxy_pool")

MAX_COOLDOWN_MULTIPLIER = 8


@dataclass
class _ProxyHealth:
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    last_used_at: float = 0.0


def configured_fallback_proxies() -> List[str]:
    urls: List[str] = []
    for raw in [settings.FALLBACK_PROXY_URL or "", *(settings.FALLBACK_PROXY_URLS or "").split(",")]:
        url = raw.strip()
        if url and url not in urls:
            urls.append(url)
    return urls


class FallbackProxyPool:
    def __init__(self, urls: Optional[List[str]] = None):
        # None reads the settings on every use, so the pool follows them rather than an import-time copy.
        self._fixed_urls = list(urls) if urls is not None else None
        self._health: Dict[str, _ProxyHealth] = {}
        self._next = 0
        self._lock = threading.Lock()

    @property
    def _urls(self) -> List[str]:
        return self._fixed_urls if self._fixed_urls is not None else configured_fallback_proxies()

    def _health_of(self, url: str) -> _ProxyHealth:
        return self._health.setdefault(url, _ProxyHealth())

    @property
    def enabled(self) -> bool:
        return bool(self._urls)

    def __len__(self) -> int:
        return len(self._urls)

    def pick(self, exclude: Optional[List[str]] = None) -> Optional[str]:
        """Next healthy proxy in rotation, skipping `exclude` (already tried for this request)."""
        excluded = set(exclude or [])
        urls = self._urls
        with self._lock:
            candidates = [u for u in urls if u not in excluded]
            if not candidates:
                return None
            now = time.monotonic()
            n = len(urls)
            chosen = None
            for offset in range(n):
                url = urls[(self._next + offset) % n]
                if url in excluded or self._health_of(url).cooldown_until > now:
                    continue
                chosen = url
                self._next = (self._next + offset + 1) % n
                break
            if chosen is None:
                chosen = min(candidates, key=lambda u: self._health_of(u).cooldown_until)
            self._health_of(chosen).last_used_at = now
            return chosen

    def report(self, url: str, ok: bool) -> None:
        with self._lock:
            health = self._health_of(url)
            if ok:
                health.successes += 1
                health.consecutive_failures = 0
                health.cooldown_until = 0.0
                return
            health.failures += 1
            health.consecutive_failures += 1
            multiplier = min(MAX_COOLDOWN_MULTIPLIER, 2 ** (health.consecutive_failures - 1))
            cooldown = settings.FALLBACK_PROXY_COOLDOWN_SECONDS * multiplier
            health.cooldown_until = time.monotonic() + cooldown
        logger.info(f"[ProxyPool] Fallback proxy {sanitize_proxy_url(url)} failed; cooling down for {cooldown:.0f}s")

    def stats(self) -> List[Dict[str, object]]:
        now = time.monotonic()
        urls = self._urls
        with self._lock:
            healths = [(url, self._health_of(url)) for url in urls]
        return [
            {
                "proxy": sanitize_proxy_url(url),
                "healthy": health.cooldown_until <= now,
                "successes": health.successes,
                "failures": health.failures,
                "cooldown_remaining_s": round(max(0.0, health.cooldown_until - now), 1),
            }
            for url, health in healths
        ]


fallback_proxy_pool = FallbackProxyPool()
