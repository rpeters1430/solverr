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

# Minimum interval between disk flushes when running inside an event loop.
# Rapid successive set_cookies() calls (e.g. many concurrent solves) coalesce
# into a single write instead of one blocking json.dump() per call.
DEBOUNCE_SECONDS = 2.0

class CookieCache:
    def __init__(self, cache_file: str = settings.CACHE_FILE, redis_url: Optional[str] = settings.REDIS_URL):
        self.cache_file = cache_file
        self.redis_url = redis_url
        self.redis_client = None
        self._store: Dict[str, Dict[str, dict]] = {}
        self._write_lock = threading.Lock()
        self._save_pending = False
        self._save_task: Optional[asyncio.Task] = None

        if self.redis_url:
            self._init_redis()
        else:
            self._load_from_disk()

    def _init_redis(self):
        try:
            import redis
            self.redis_client = redis.Redis.from_url(self.redis_url, decode_responses=True, socket_connect_timeout=2)
            self.redis_client.ping()
            logger.info(f"[CookieCache] Connected to distributed Redis cache backend at {self.redis_url}")
        except Exception as e:
            logger.warning(f"[CookieCache] Redis connection failed ({e}). Falling back to local disk JSON cache.")
            self.redis_client = None
            self._load_from_disk()

    def _normalize_domain(self, domain_or_url: str) -> str:
        if "://" in domain_or_url:
            parsed = urlparse(domain_or_url)
            domain = parsed.netloc.split(":")[0]
        else:
            domain = domain_or_url.split(":")[0]
        return domain.lstrip(".").lower()

    def get_cookies(self, url_or_domain: str) -> List[CookieModel]:
        target_domain = self._normalize_domain(url_or_domain)
        result: List[CookieModel] = []
        now = time.time()

        if self.redis_client:
            try:
                keys = self.redis_client.keys(f"solverr:cookie:{target_domain}:*")
                # Also check wildcard parent domains
                parts = target_domain.split(".")
                if len(parts) > 2:
                    parent_domain = ".".join(parts[-2:])
                    keys += self.redis_client.keys(f"solverr:cookie:{parent_domain}:*")

                for key in set(keys):
                    data_raw = self.redis_client.get(key)
                    if data_raw:
                        data = json.loads(data_raw)
                        c_model = CookieModel(**data["cookie"])
                        result.append(c_model)
                return result
            except Exception as e:
                logger.debug(f"[CookieCache] Redis read error: {e}")

        for domain_key, cookies_dict in self._store.items():
            clean_domain = domain_key.lstrip(".")
            if target_domain == clean_domain or target_domain.endswith("." + clean_domain) or clean_domain.endswith("." + target_domain):
                for cookie_name, data in list(cookies_dict.items()):
                    if now - data.get("timestamp", 0) > settings.COOKIE_CACHE_TTL:
                        continue
                    try:
                        c_model = CookieModel(**data["cookie"])
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

    def set_cookies(self, url_or_domain: str, cookies: List[CookieModel]):
        domain = self._normalize_domain(url_or_domain)
        now = time.time()

        if self.redis_client:
            try:
                for c in cookies:
                    c_dict = c.model_dump()
                    c_domain = c.domain.lstrip(".") if c.domain else domain
                    key = f"solverr:cookie:{c_domain}:{c.name}"
                    val = json.dumps({"cookie": c_dict, "timestamp": now})
                    self.redis_client.set(key, val, ex=settings.COOKIE_CACHE_TTL)
                logger.debug(f"[CookieCache] Saved {len(cookies)} cookie(s) to Redis for domain '{domain}'")
                return
            except Exception as e:
                logger.debug(f"[CookieCache] Redis write error: {e}")

        if domain not in self._store:
            self._store[domain] = {}
        
        for c in cookies:
            c_dict = c.model_dump()
            c_domain = c.domain.lstrip(".") if c.domain else domain
            if c_domain not in self._store:
                self._store[c_domain] = {}
            self._store[c_domain][c.name] = {
                "cookie": c_dict,
                "timestamp": now
            }
        logger.debug(f"[CookieCache] Saved {len(cookies)} cookie(s) to local cache for domain '{domain}'")
        self._schedule_save()

    def clear(self):
        if self.redis_client:
            try:
                keys = self.redis_client.keys("solverr:cookie:*")
                if keys:
                    self.redis_client.delete(*keys)
                logger.info("[CookieCache] Cleared all cached cookies from Redis")
            except Exception as e:
                logger.warning(f"[CookieCache] Redis clear error: {e}")

        self._store = {}
        logger.info("[CookieCache] Cleared all local cached cookies")
        self._save_to_disk()

    def get_all_entries(self) -> Dict[str, List[dict]]:
        out = {}
        now = time.time()

        if self.redis_client:
            try:
                keys = self.redis_client.keys("solverr:cookie:*")
                for key in keys:
                    parts = key.split(":")
                    if len(parts) >= 4:
                        domain = parts[2]
                        data_raw = self.redis_client.get(key)
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

    def _schedule_save(self):
        """Debounce disk writes: coalesce bursts of set_cookies() into one
        background flush instead of a blocking write per call. Falls back to
        an immediate synchronous save when there's no running event loop
        (e.g. sync test/CLI usage)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._save_to_disk()
            return

        self._save_pending = True
        if self._save_task is None or self._save_task.done():
            self._save_task = loop.create_task(self._debounced_flush(loop))

    async def _debounced_flush(self, loop: asyncio.AbstractEventLoop):
        # Throttle: wait once, then flush whatever accumulated in _store,
        # regardless of how many set_cookies() calls arrived during the wait.
        await asyncio.sleep(DEBOUNCE_SECONDS)
        self._save_pending = False
        await loop.run_in_executor(None, self._save_to_disk)

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
                os.replace(tmp_file, self.cache_file)
            except Exception as e:
                logger.warning(f"[CookieCache] Could not save cache to disk: {e}")

cookie_cache = CookieCache()
