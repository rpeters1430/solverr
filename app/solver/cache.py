import os
import json
import time
import asyncio
import logging
import threading
from typing import Dict, List, Optional
from urllib.parse import urlparse
from app.models.flaresolverr import CookieModel
from app.config import settings

logger = logging.getLogger("solverr.cache")

# Bursts of set_cookies() within this window coalesce into one disk write.
DEBOUNCE_SECONDS = 2.0

# Without a cooldown, a dead Redis would cost a connect timeout on every lookup.
REDIS_RECONNECT_INTERVAL_SECONDS = 30.0

class CookieCache:
    def __init__(self, cache_file: str = settings.CACHE_FILE, redis_url: Optional[str] = settings.REDIS_URL):
        self.cache_file = cache_file
        self.redis_url = redis_url
        self.redis_client = None
        self._redis_last_attempt = 0.0
        self._store: Dict[str, Dict[str, dict]] = {}
        self._write_lock = threading.Lock()
        self._save_pending = False
        self._save_task: Optional[asyncio.Task] = None
        # Async wrappers serialize store access and run blocking I/O off the event loop.
        self._async_lock = asyncio.Lock()
        self._domain_count_cache_value = 0
        self._domain_count_cache_at = 0.0

        if self.redis_url:
            self._redis()
        if not self.redis_client:
            # Local fallback; _redis() migrates it into Redis if Redis comes back.
            self._load_from_disk()

    def _redis(self):
        """Return a live Redis client, retrying a failed connection on a cooldown."""
        if self.redis_client is not None:
            return self.redis_client
        if not self.redis_url:
            return None
        now = time.time()
        if now - self._redis_last_attempt < REDIS_RECONNECT_INTERVAL_SECONDS:
            return None
        self._redis_last_attempt = now
        try:
            import redis
            client = redis.Redis.from_url(self.redis_url, decode_responses=True, socket_connect_timeout=2)
            client.ping()
            self.redis_client = client
            logger.info(f"[CookieCache] Connected to distributed Redis cache backend at {self.redis_url}")
            self._migrate_local_store_to_redis(client)
            # Migration may have invalidated the client, so don't return the stale local.
            return self.redis_client
        except Exception as e:
            logger.warning(f"[CookieCache] Redis connection attempt failed ({e}). Using local disk JSON cache until the next retry.")
            return None

    def _migrate_local_store_to_redis(self, client):
        """Move cookies cached locally during a Redis outage into Redis, which serves all reads once up.

        One pipelined batch, keeping each entry's remaining TTL rather than a fresh one.
        On failure, everything stays local for the next reconnect; re-sending a SET is harmless."""
        if not self._store:
            return
        now = time.time()
        pending: List[tuple] = []  # (domain_key, cookie_key, entry, ttl)
        for domain_key in list(self._store.keys()):
            cookies_dict = self._store[domain_key]
            for cookie_key in list(cookies_dict.keys()):
                entry = cookies_dict[cookie_key]
                remaining_ttl = settings.COOKIE_CACHE_TTL - (now - entry.get("timestamp", 0))
                if remaining_ttl <= 0:
                    del cookies_dict[cookie_key]
                    continue
                pending.append((domain_key, cookie_key, entry, int(remaining_ttl)))
        self._store = {d: c for d, c in self._store.items() if c}

        if pending:
            try:
                pipe = client.pipeline(transaction=False)
                for domain_key, cookie_key, entry, ttl in pending:
                    pipe.set(f"solverr:cookie:{domain_key}:{cookie_key}", json.dumps(entry), ex=ttl)
                pipe.execute()
                for domain_key, cookie_key, _entry, _ttl in pending:
                    cookies_dict = self._store.get(domain_key)
                    if cookies_dict is not None:
                        cookies_dict.pop(cookie_key, None)
                self._store = {d: c for d, c in self._store.items() if c}
                logger.info(f"[CookieCache] Migrated {len(pending)} locally-cached cookie(s) to Redis after (re)connecting")
            except Exception as e:
                logger.warning(f"[CookieCache] Failed to migrate {len(pending)} locally-cached cookie(s) to Redis, will retry on next reconnect: {e}")
                self._invalidate_redis()

        # Persist the compacted store so the disk fallback isn't stale if Redis drops again.
        self._save_to_disk()

    def _invalidate_redis(self):
        """Drop the client after a failed operation so reconnects go through _redis()'s cooldown."""
        self.redis_client = None
        self._redis_last_attempt = time.time()

    def _cookie_key(self, cookie: CookieModel) -> str:
        # Domain is the outer key; path keeps same-name cookies on one domain apart.
        return f"{cookie.name}|{cookie.path or '/'}"

    def _normalize_domain(self, domain_or_url: str) -> str:
        if "://" in domain_or_url:
            parsed = urlparse(domain_or_url)
            domain = parsed.netloc.split(":")[0]
        else:
            domain = domain_or_url.split(":")[0]
        return domain.lstrip(".").lower()

    def _scan_keys(self, pattern: str) -> List[str]:
        # Never KEYS: it blocks the single-threaded Redis server for the whole keyspace walk.
        return list(self.redis_client.scan_iter(match=pattern, count=200))

    def get_cookies(self, url_or_domain: str) -> List[CookieModel]:
        target_domain = self._normalize_domain(url_or_domain)
        result: List[CookieModel] = []
        now = time.time()

        if self._redis():
            try:
                keys = self._scan_keys(f"solverr:cookie:{target_domain}:*")
                # Also check the parent domain.
                parts = target_domain.split(".")
                if len(parts) > 2:
                    parent_domain = ".".join(parts[-2:])
                    keys += self._scan_keys(f"solverr:cookie:{parent_domain}:*")

                unique_keys = list(set(keys))
                raw_values = self.redis_client.mget(unique_keys) if unique_keys else []
                for data_raw in raw_values:
                    if data_raw:
                        data = json.loads(data_raw)
                        c_model = CookieModel(**data["cookie"])
                        if c_model.expires and c_model.expires > 0 and now > c_model.expires:
                            continue
                        result.append(c_model)
                return result
            except Exception as e:
                logger.debug(f"[CookieCache] Redis read error: {e}")
                self._invalidate_redis()

        for domain_key, cookies_dict in self._store.items():
            clean_domain = domain_key.lstrip(".")
            if target_domain == clean_domain or target_domain.endswith("." + clean_domain):
                for cookie_name, data in list(cookies_dict.items()):
                    if now - data.get("timestamp", 0) > settings.COOKIE_CACHE_TTL:
                        continue
                    try:
                        c_model = CookieModel(**data["cookie"])
                        if c_model.expires and c_model.expires > 0 and now > c_model.expires:
                            continue
                        result.append(c_model)
                    except Exception:
                        pass
        # Deduplicate by cookie name (last/most-specific wins)
        deduped: Dict[str, CookieModel] = {}
        for c in result:
            deduped[c.name] = c
        return list(deduped.values())

    def get_cookie_dict(self, url_or_domain: str) -> Dict[str, str]:
        cookies = self.get_cookies(url_or_domain)
        return {c.name: c.value for c in cookies}

    def set_cookies(self, url_or_domain: str, cookies: List[CookieModel], schedule_save: bool = True):
        domain = self._normalize_domain(url_or_domain)
        now = time.time()

        if self._redis():
            try:
                pipe = self.redis_client.pipeline(transaction=False)
                for c in cookies:
                    c_dict = c.model_dump()
                    c_domain = c.domain.lstrip(".") if c.domain else domain
                    key = f"solverr:cookie:{c_domain}:{self._cookie_key(c)}"
                    val = json.dumps({"cookie": c_dict, "timestamp": now})
                    pipe.set(key, val, ex=settings.COOKIE_CACHE_TTL)
                pipe.execute()
                self._domain_count_cache_at = 0.0
                logger.debug(f"[CookieCache] Saved {len(cookies)} cookie(s) to Redis for domain '{domain}'")
                return
            except Exception as e:
                logger.debug(f"[CookieCache] Redis write error: {e}")
                self._invalidate_redis()

        for c in cookies:
            c_dict = c.model_dump()
            c_domain = c.domain.lstrip(".") if c.domain else domain
            if c_domain not in self._store:
                self._evict_domain_if_at_capacity()
                self._store[c_domain] = {}
            self._store[c_domain][self._cookie_key(c)] = {
                "cookie": c_dict,
                "timestamp": now
            }
            self._evict_cookies_if_at_capacity(c_domain)
        self._domain_count_cache_at = 0.0
        logger.debug(f"[CookieCache] Saved {len(cookies)} cookie(s) to local cache for domain '{domain}'")
        if schedule_save:
            self._schedule_save()

    def _evict_domain_if_at_capacity(self):
        """Drop the domain whose freshest cookie is oldest once MAX_CACHE_DOMAINS is reached."""
        if len(self._store) < settings.MAX_CACHE_DOMAINS:
            return
        oldest_domain = min(
            self._store,
            key=lambda d: max((v.get("timestamp", 0) for v in self._store[d].values()), default=0),
        )
        del self._store[oldest_domain]
        logger.info(f"[CookieCache] Evicted domain '{oldest_domain}' (MAX_CACHE_DOMAINS={settings.MAX_CACHE_DOMAINS} reached)")

    def _evict_cookies_if_at_capacity(self, domain: str):
        cookies_for_domain = self._store.get(domain, {})
        overflow = len(cookies_for_domain) - settings.MAX_COOKIES_PER_DOMAIN
        if overflow <= 0:
            return
        oldest_keys = sorted(cookies_for_domain, key=lambda k: cookies_for_domain[k].get("timestamp", 0))[:overflow]
        for key in oldest_keys:
            del cookies_for_domain[key]

    def clear(self):
        if self._redis():
            try:
                keys = self._scan_keys("solverr:cookie:*")
                if keys:
                    self.redis_client.delete(*keys)
                logger.info("[CookieCache] Cleared all cached cookies from Redis")
            except Exception as e:
                logger.warning(f"[CookieCache] Redis clear error: {e}")
                self._invalidate_redis()

        self._store = {}
        self._domain_count_cache_value = 0
        self._domain_count_cache_at = time.monotonic()
        logger.info("[CookieCache] Cleared all local cached cookies")
        self._save_to_disk()

    def get_all_entries(self) -> Dict[str, List[dict]]:
        out = {}
        now = time.time()

        if self._redis():
            try:
                keys = self._scan_keys("solverr:cookie:*")
                raw_values = self.redis_client.mget(keys) if keys else []
                for key, data_raw in zip(keys, raw_values):
                    parts = key.split(":")
                    if len(parts) >= 4:
                        domain = parts[2]
                        if data_raw:
                            data = json.loads(data_raw)
                            age = int(now - data.get("timestamp", 0))
                            item = dict(data["cookie"])
                            item["age_seconds"] = age
                            if domain not in out:
                                out[domain] = []
                            out[domain].append(item)
                return out
            except Exception as e:
                logger.debug(f"[CookieCache] Redis get_all error: {e}")
                self._invalidate_redis()

        for domain, cookies in self._store.items():
            valid_list = []
            for name, data in cookies.items():
                age = int(now - data.get("timestamp", 0))
                if age <= settings.COOKIE_CACHE_TTL:
                    item = dict(data["cookie"])
                    item["age_seconds"] = age
                    valid_list.append(item)
            if valid_list:
                out[domain] = valid_list
        return out

    def count_domains(self) -> int:
        """Return a cheap cached domain count without fetching cookie values."""
        now = time.monotonic()
        if now - self._domain_count_cache_at < 5.0:
            return self._domain_count_cache_value
        if self._redis():
            try:
                domains = {
                    key.split(":", 3)[2]
                    for key in self._scan_keys("solverr:cookie:*")
                    if len(key.split(":", 3)) >= 4
                }
                value = len(domains)
            except Exception:
                self._invalidate_redis()
                value = self._count_live_local_domains()
        else:
            value = self._count_live_local_domains()
        self._domain_count_cache_value = value
        self._domain_count_cache_at = now
        return value

    def _count_live_local_domains(self) -> int:
        now = time.time()
        value = 0
        for cookies in self._store.values():
            for data in cookies.values():
                if now - data.get("timestamp", 0) > settings.COOKIE_CACHE_TTL:
                    continue
                try:
                    cookie = CookieModel(**data["cookie"])
                except Exception:
                    continue
                if cookie.expires and cookie.expires > 0 and now > cookie.expires:
                    continue
                value += 1
                break
        return value

    async def get_cookies_async(self, url_or_domain: str) -> List[CookieModel]:
        async with self._async_lock:
            return await asyncio.to_thread(self.get_cookies, url_or_domain)

    async def set_cookies_async(self, url_or_domain: str, cookies: List[CookieModel]) -> None:
        async with self._async_lock:
            # Schedule the flush here: in the worker thread there's no loop, so it would write immediately.
            await asyncio.to_thread(self.set_cookies, url_or_domain, cookies, False)
            if not self.redis_client:
                self._schedule_save()

    async def get_all_entries_async(self) -> Dict[str, List[dict]]:
        async with self._async_lock:
            return await asyncio.to_thread(self.get_all_entries)

    async def count_domains_async(self) -> int:
        async with self._async_lock:
            return await asyncio.to_thread(self.count_domains)

    async def clear_async(self) -> None:
        async with self._async_lock:
            await asyncio.to_thread(self.clear)

    async def export_netscape_async(self, domain_filter: Optional[str] = None) -> str:
        async with self._async_lock:
            return await asyncio.to_thread(self.export_netscape, domain_filter)

    def export_netscape(self, domain_filter: Optional[str] = None) -> str:
        """Export cached cookies in Netscape format for curl, yt-dlp, wget, etc."""
        lines = [
            "# Netscape HTTP Cookie File",
            "# https://curl.se/docs/http-cookies.html",
            "# Exported from Solverr",
            ""
        ]
        all_entries = self.get_all_entries()
        filter_norm = self._normalize_domain(domain_filter) if domain_filter else None
        
        for dom, cookies in all_entries.items():
            if filter_norm and dom != filter_norm and not dom.endswith("." + filter_norm):
                continue
            for c in cookies:
                c_dom = c.get("domain") or dom
                if not c_dom.startswith(".") and not c_dom.startswith("http"):
                    include_sub = "TRUE" if "." in c_dom else "FALSE"
                    export_dom = f".{c_dom}" if include_sub == "TRUE" and not c_dom.startswith(".") else c_dom
                else:
                    include_sub = "TRUE" if c_dom.startswith(".") else "FALSE"
                    export_dom = c_dom
                path = c.get("path") or "/"
                secure = "TRUE" if c.get("secure") else "FALSE"
                expires = int(c.get("expires") if c.get("expires") and c.get("expires") > 0 else 0)
                name = c.get("name", "")
                value = c.get("value", "")
                lines.append(f"{export_dom}\t{include_sub}\t{path}\t{secure}\t{expires}\t{name}\t{value}")
        return "\n".join(lines) + "\n"

    def _schedule_save(self):
        """Debounce disk writes, or save synchronously when no event loop is running."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._save_to_disk()
            return

        self._save_pending = True
        if self._save_task is None or self._save_task.done():
            self._save_task = loop.create_task(self._debounced_flush(loop))

    async def _debounced_flush(self, loop: asyncio.AbstractEventLoop):
        while True:
            await asyncio.sleep(DEBOUNCE_SECONDS)
            self._save_pending = False
            await loop.run_in_executor(None, self._save_to_disk)
            if not self._save_pending:
                break
            # A write landed during the flush; loop so it isn't dropped.

    def _load_from_disk(self):
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    self._store = json.load(f)
                logger.info(f"[CookieCache] Loaded cached cookies for {len(self._store)} domain(s) from '{self.cache_file}'")
            except Exception as e:
                logger.warning(f"[CookieCache] Could not load cache file '{self.cache_file}': {e}")
                self._store = {}

    def _save_to_disk(self):
        with self._write_lock:
            try:
                cache_dir = os.path.dirname(self.cache_file)
                if cache_dir:
                    os.makedirs(cache_dir, exist_ok=True)
                tmp_file = f"{self.cache_file}.tmp"
                with open(tmp_file, "w", encoding="utf-8") as f:
                    json.dump(self._store, f, indent=2)
                try:
                    # Group-writable for PUID/PGID bind mounts, never world-readable: it holds live cookies.
                    os.chmod(tmp_file, 0o660)  # nosec B103
                except Exception:
                    pass
                os.replace(tmp_file, self.cache_file)
            except Exception as e:
                logger.warning(f"[CookieCache] Could not save cache to disk: {e}")

cookie_cache = CookieCache()
