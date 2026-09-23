import asyncio
import json
import uuid
import time
import logging
from typing import Dict, List, Optional
from app.models.flaresolverr import CookieModel
from app.config import settings
from app.logging_config import sanitize_proxy_url

logger = logging.getLogger("solverr.sessions")

REDIS_KEY_PREFIX = "solverr:session:"

# Same reconnect cooldown as CookieCache.
REDIS_RECONNECT_INTERVAL_SECONDS = 30.0

class Session:
    def __init__(self, session_id: str, proxy: Optional[str] = None, ttl: int = 7200):
        self.session_id: str = session_id
        self.proxy: Optional[str] = proxy
        self.created_at: float = time.time()
        self.last_accessed: float = time.time()
        self.ttl: int = ttl
        self.cookies: List[CookieModel] = []

    def touch(self):
        self.last_accessed = time.time()

    def is_expired(self) -> bool:
        return (time.time() - self.last_accessed) > self.ttl

    def update_cookies(self, new_cookies: List[CookieModel]):
        self.touch()
        # Identity is domain + path + name: a same-name cookie on a
        # different path must not replace an unrelated cookie.
        existing_keys = {(c.domain, c.path, c.name) for c in new_cookies}
        self.cookies = [
            c for c in self.cookies if (c.domain, c.path, c.name) not in existing_keys
        ] + new_cookies

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "proxy": self.proxy,
            "created_at": self.created_at,
            "last_accessed": self.last_accessed,
            "ttl": self.ttl,
            "cookies": [c.model_dump() for c in self.cookies]
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        sess = cls(session_id=data["session_id"], proxy=data.get("proxy"), ttl=data.get("ttl", 7200))
        sess.created_at = data.get("created_at", time.time())
        sess.last_accessed = data.get("last_accessed", time.time())
        sess.cookies = [CookieModel(**c) for c in data.get("cookies", [])]
        return sess

class SessionManager:
    """In-memory session store, also persisted to Redis when REDIS_URL is set.

    Without Redis, sessions are per-process and lost on restart.
    """

    def __init__(self, redis_url: Optional[str] = None):
        self._sessions: Dict[str, Session] = {}
        self.redis_client = None
        self._redis_last_attempt = 0.0
        self.redis_url = redis_url if redis_url is not None else settings.REDIS_URL
        self._async_lock = asyncio.Lock()
        if self.redis_url:
            self._redis()

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
            logger.info(f"[SessionManager] Connected to distributed Redis session backend at {self.redis_url}")
            self._migrate_local_sessions_to_redis(client)
            # Migration may have invalidated the client, so don't return the stale local.
            return self.redis_client
        except Exception as e:
            logger.warning(f"[SessionManager] Redis connection attempt failed ({e}). Sessions are in-memory only for this process until the next retry.")
            return None

    def _migrate_local_sessions_to_redis(self, client):
        """Persist sessions created during a Redis outage, in one pipelined batch.

        On failure the whole batch is retried on the next reconnect."""
        pending = [(sid, sess) for sid, sess in self._sessions.items() if not sess.is_expired()]
        if not pending:
            return
        try:
            pipe = client.pipeline(transaction=False)
            for sid, sess in pending:
                pipe.set(f"{REDIS_KEY_PREFIX}{sid}", json.dumps(sess.to_dict()), ex=sess.ttl)
            pipe.execute()
            logger.info(f"[SessionManager] Migrated {len(pending)} locally-held session(s) to Redis after (re)connecting")
        except Exception as e:
            logger.warning(f"[SessionManager] Failed to migrate {len(pending)} locally-held session(s) to Redis, will retry on next reconnect: {e}")
            self._invalidate_redis()

    def _invalidate_redis(self):
        """Drop the client after a failed operation so reconnects go through _redis()'s cooldown."""
        self.redis_client = None
        self._redis_last_attempt = time.time()

    def _persist(self, sess: Session):
        redis_client = self._redis()
        if not redis_client:
            return
        try:
            key = f"{REDIS_KEY_PREFIX}{sess.session_id}"
            redis_client.set(key, json.dumps(sess.to_dict()), ex=sess.ttl)
        except Exception as e:
            logger.debug(f"[SessionManager] Redis persist error for '{sess.session_id}': {e}")
            self._invalidate_redis()

    def _load_from_redis(self, session_id: str) -> Optional[Session]:
        redis_client = self._redis()
        if not redis_client:
            return None
        try:
            key = f"{REDIS_KEY_PREFIX}{session_id}"
            raw = redis_client.get(key)
            if not raw:
                return None
            sess = Session.from_dict(json.loads(raw))
            if sess.is_expired():
                redis_client.delete(key)
                return None
            return sess
        except Exception as e:
            logger.debug(f"[SessionManager] Redis load error for '{session_id}': {e}")
            self._invalidate_redis()
            return None

    def create_session(self, session_id: Optional[str] = None, proxy: Optional[str] = None, ttl: int = 7200) -> str:
        sid = session_id or str(uuid.uuid4())
        self._evict_oldest_if_at_capacity()
        sess = Session(session_id=sid, proxy=proxy, ttl=ttl)
        self._sessions[sid] = sess
        self._persist(sess)
        proxy_desc = f" (proxy: {sanitize_proxy_url(proxy)})" if proxy else ""
        logger.info(f"[SessionManager] Created session '{sid}'{proxy_desc}")
        return sid

    def _evict_oldest_if_at_capacity(self):
        # In-memory bound only; Redis sessions expire via their TTL.
        if len(self._sessions) < settings.MAX_SESSIONS:
            return
        self.prune_expired_sessions()
        if len(self._sessions) < settings.MAX_SESSIONS:
            return
        oldest_id = min(self._sessions, key=lambda sid: self._sessions[sid].last_accessed)
        logger.info(f"[SessionManager] Evicting oldest session '{oldest_id}' (MAX_SESSIONS={settings.MAX_SESSIONS} reached)")
        self._delete(oldest_id)

    def get_session(self, session_id: str) -> Optional[Session]:
        sess = self._sessions.get(session_id)
        if not sess:
            sess = self._load_from_redis(session_id)
            if sess:
                self._sessions[session_id] = sess
        if sess:
            if sess.is_expired():
                logger.info(f"[SessionManager] Pruning expired session '{session_id}' on access")
                self._delete(session_id)
                return None
            sess.touch()
            self._persist(sess)
        return sess

    def update_session_cookies(self, session_id: str, cookies: List[CookieModel]) -> Optional[Session]:
        sess = self.get_session(session_id)
        if not sess:
            return None
        sess.update_cookies(cookies)
        self._persist(sess)
        return sess

    def _delete(self, session_id: str):
        self._sessions.pop(session_id, None)
        redis_client = self._redis()
        if redis_client:
            try:
                redis_client.delete(f"{REDIS_KEY_PREFIX}{session_id}")
            except Exception as e:
                logger.debug(f"[SessionManager] Redis delete error for '{session_id}': {e}")
                self._invalidate_redis()

    def destroy_session(self, session_id: str) -> bool:
        existed = session_id in self._sessions or self._load_from_redis(session_id) is not None
        if existed:
            self._delete(session_id)
            logger.info(f"[SessionManager] Destroyed session '{session_id}'")
            return True
        logger.warning(f"[SessionManager] Cannot destroy session '{session_id}' - not found")
        return False

    def list_sessions(self) -> List[str]:
        self.prune_expired_sessions()
        session_ids = set(self._sessions.keys())
        redis_client = self._redis()
        if redis_client:
            try:
                # Never KEYS: it blocks the single-threaded Redis server for the whole keyspace walk.
                for key in redis_client.scan_iter(match=f"{REDIS_KEY_PREFIX}*", count=200):
                    session_ids.add(key[len(REDIS_KEY_PREFIX):])
            except Exception as e:
                logger.debug(f"[SessionManager] Redis list error: {e}")
                self._invalidate_redis()
        return list(session_ids)

    def prune_expired_sessions(self) -> int:
        expired = [sid for sid, sess in self._sessions.items() if sess.is_expired()]
        for sid in expired:
            self._delete(sid)
            logger.info(f"[SessionManager] Pruned expired session '{sid}'")
        return len(expired)

    async def get_session_async(self, session_id: str) -> Optional[Session]:
        async with self._async_lock:
            return await asyncio.to_thread(self.get_session, session_id)

    async def update_session_cookies_async(self, session_id: str, cookies: List[CookieModel]) -> Optional[Session]:
        async with self._async_lock:
            return await asyncio.to_thread(self.update_session_cookies, session_id, cookies)

    async def create_session_async(self, session_id: Optional[str] = None, proxy: Optional[str] = None, ttl: int = 7200) -> str:
        async with self._async_lock:
            return await asyncio.to_thread(self.create_session, session_id, proxy, ttl)

    async def destroy_session_async(self, session_id: str) -> bool:
        async with self._async_lock:
            return await asyncio.to_thread(self.destroy_session, session_id)

    async def list_sessions_async(self) -> List[str]:
        async with self._async_lock:
            return await asyncio.to_thread(self.list_sessions)

    async def prune_expired_sessions_async(self) -> int:
        async with self._async_lock:
            return await asyncio.to_thread(self.prune_expired_sessions)


session_manager = SessionManager()
