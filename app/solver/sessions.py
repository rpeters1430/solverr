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
        existing_names = {c.name for c in new_cookies}
        self.cookies = [c for c in self.cookies if c.name not in existing_names] + new_cookies

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
    """In-memory session store with optional Redis-backed persistence.

    Sessions live in-memory for fast access, mirroring CookieCache's design.
    When REDIS_URL is configured, sessions are also durably persisted so
    they survive a process restart and are visible across instances -
    without Redis, sessions are lost on restart and are per-process only.
    """

    def __init__(self, redis_url: Optional[str] = None):
        self._sessions: Dict[str, Session] = {}
        self.redis_client = None
        redis_url = redis_url if redis_url is not None else settings.REDIS_URL
        if redis_url:
            self._init_redis(redis_url)

    def _init_redis(self, redis_url: str):
        try:
            import redis
            self.redis_client = redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=2)
            self.redis_client.ping()
            logger.info(f"[SessionManager] Connected to distributed Redis session backend at {redis_url}")
        except Exception as e:
            logger.warning(f"[SessionManager] Redis connection failed ({e}). Sessions will be in-memory only for this process.")
            self.redis_client = None

    def _persist(self, sess: Session):
        if not self.redis_client:
            return
        try:
            key = f"{REDIS_KEY_PREFIX}{sess.session_id}"
            self.redis_client.set(key, json.dumps(sess.to_dict()), ex=sess.ttl)
        except Exception as e:
            logger.debug(f"[SessionManager] Redis persist error for '{sess.session_id}': {e}")

    def _load_from_redis(self, session_id: str) -> Optional[Session]:
        if not self.redis_client:
            return None
        try:
            key = f"{REDIS_KEY_PREFIX}{session_id}"
            raw = self.redis_client.get(key)
            if not raw:
                return None
            sess = Session.from_dict(json.loads(raw))
            if sess.is_expired():
                self.redis_client.delete(key)
                return None
            return sess
        except Exception as e:
            logger.debug(f"[SessionManager] Redis load error for '{session_id}': {e}")
            return None

    def create_session(self, session_id: Optional[str] = None, proxy: Optional[str] = None, ttl: int = 7200) -> str:
        sid = session_id or str(uuid.uuid4())
        sess = Session(session_id=sid, proxy=proxy, ttl=ttl)
        self._sessions[sid] = sess
        self._persist(sess)
        proxy_desc = f" (proxy: {sanitize_proxy_url(proxy)})" if proxy else ""
        logger.info(f"[SessionManager] Created session '{sid}'{proxy_desc}")
        return sid

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
        if self.redis_client:
            try:
                self.redis_client.delete(f"{REDIS_KEY_PREFIX}{session_id}")
            except Exception as e:
                logger.debug(f"[SessionManager] Redis delete error for '{session_id}': {e}")

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
        if self.redis_client:
            try:
                for key in self.redis_client.keys(f"{REDIS_KEY_PREFIX}*"):
                    session_ids.add(key[len(REDIS_KEY_PREFIX):])
            except Exception as e:
                logger.debug(f"[SessionManager] Redis list error: {e}")
        return list(session_ids)

    def prune_expired_sessions(self) -> int:
        expired = [sid for sid, sess in self._sessions.items() if sess.is_expired()]
        for sid in expired:
            self._delete(sid)
            logger.info(f"[SessionManager] Pruned expired session '{sid}'")
        return len(expired)

session_manager = SessionManager()
