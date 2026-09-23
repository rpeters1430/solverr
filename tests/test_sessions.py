import time
import unittest
from unittest.mock import patch
from app.config import settings
from app.models.flaresolverr import CookieModel
from app.solver.sessions import REDIS_KEY_PREFIX, SessionManager


class _FakePipeline:
    """Mirrors tests/test_cache.py's _FakePipeline: commands queue locally,
    a connection failure surfaces only in execute() and fails the batch."""

    def __init__(self, client):
        self._client = client
        self._commands = []

    def set(self, key, val, ex=None):
        self._commands.append((key, val))
        return self

    def execute(self):
        if not self._client.healthy:
            raise ConnectionError("redis down")
        for key, val in self._commands:
            self._client.store[key] = val
        return [True] * len(self._commands)


class TestSessionManager(unittest.TestCase):
    def setUp(self):
        self.mgr = SessionManager()

    def test_create_and_get_session(self):
        sid = self.mgr.create_session(proxy="http://proxy.example.com:8080")
        sess = self.mgr.get_session(sid)
        self.assertIsNotNone(sess)
        self.assertEqual(sess.proxy, "http://proxy.example.com:8080")

    def test_update_cookies(self):
        sid = self.mgr.create_session()
        sess = self.mgr.get_session(sid)
        c1 = CookieModel(name="cf_clearance", value="val1", domain="test.com")
        sess.update_cookies([c1])
        self.assertEqual(len(sess.cookies), 1)

        c2 = CookieModel(name="cf_clearance", value="val2_updated", domain="test.com")
        sess.update_cookies([c2])
        self.assertEqual(len(sess.cookies), 1)
        self.assertEqual(sess.cookies[0].value, "val2_updated")

    def test_session_ttl_pruning(self):
        # Create session with 1 second TTL
        sid = self.mgr.create_session(ttl=1)
        sess = self.mgr.get_session(sid)
        self.assertIsNotNone(sess)

        # Fast forward time
        sess.last_accessed = time.time() - 5
        self.assertTrue(sess.is_expired())

        # Prune expired
        pruned_count = self.mgr.prune_expired_sessions()
        self.assertEqual(pruned_count, 1)
        self.assertIsNone(self.mgr.get_session(sid))

    def test_destroy_session(self):
        sid = self.mgr.create_session()
        self.assertTrue(self.mgr.destroy_session(sid))
        self.assertFalse(self.mgr.destroy_session(sid))

    def test_evicts_oldest_session_when_over_capacity(self):
        with patch.object(settings, "MAX_SESSIONS", 2):
            sid1 = self.mgr.create_session()
            self.mgr._sessions[sid1].last_accessed = time.time() - 10
            sid2 = self.mgr.create_session()
            self.mgr._sessions[sid2].last_accessed = time.time() - 5
            sid3 = self.mgr.create_session()

            active = set(self.mgr.list_sessions())
            self.assertEqual(len(active), 2)
            self.assertNotIn(sid1, active)
            self.assertIn(sid3, active)

    def test_redis_reconnects_after_initial_failure(self):
        # A Redis outage at startup must be retried, not permanent.
        class FakeRedisClient:
            def __init__(self, healthy):
                self.healthy = healthy

            def ping(self):
                if not self.healthy:
                    raise ConnectionError("redis down")

        attempts = {"n": 0}

        def fake_from_url(*args, **kwargs):
            attempts["n"] += 1
            return FakeRedisClient(healthy=attempts["n"] > 1)

        with patch("redis.Redis.from_url", side_effect=fake_from_url):
            mgr = SessionManager(redis_url="redis://fake-host:6379/0")
            self.assertIsNone(mgr.redis_client)
            self.assertEqual(attempts["n"], 1)

            self.assertIsNone(mgr._redis())
            self.assertEqual(attempts["n"], 1)

            mgr._redis_last_attempt = 0
            client = mgr._redis()
            self.assertIsNotNone(client)
            self.assertEqual(attempts["n"], 2)
            self.assertIs(mgr.redis_client, client)

    def test_local_sessions_are_migrated_to_redis_on_reconnect(self):
        # Outage-era sessions exist only in memory until migrated.
        class FakeRedisClient:
            def __init__(self):
                self.healthy = False
                self.store = {}

            def ping(self):
                if not self.healthy:
                    raise ConnectionError("redis down")

            def set(self, key, val, ex=None):
                if not self.healthy:
                    raise ConnectionError("redis down")
                self.store[key] = val

            def pipeline(self, transaction=True):
                return _FakePipeline(self)

        client = FakeRedisClient()
        with patch("redis.Redis.from_url", return_value=client):
            mgr = SessionManager(redis_url="redis://fake-host:6379/0")
            self.assertIsNone(mgr.redis_client)

            sid = mgr.create_session()
            self.assertIn(sid, mgr._sessions)
            self.assertNotIn(f"{REDIS_KEY_PREFIX}{sid}", client.store)

            client.healthy = True
            mgr._redis_last_attempt = 0
            mgr._redis()
            self.assertIs(mgr.redis_client, client)
            self.assertIn(f"{REDIS_KEY_PREFIX}{sid}", client.store)

    def test_migration_skips_already_expired_sessions(self):
        class FakeRedisClient:
            def ping(self):
                pass

            def set(self, key, val, ex=None):
                raise AssertionError("an already-expired session must not be written to Redis")

        client = FakeRedisClient()
        mgr = SessionManager(redis_url=None)
        sid = mgr.create_session(ttl=1)
        mgr._sessions[sid].last_accessed = time.time() - 10
        self.assertTrue(mgr._sessions[sid].is_expired())

        mgr.redis_url = "redis://fake-host:6379/0"
        with patch("redis.Redis.from_url", return_value=client):
            mgr._redis_last_attempt = 0
            mgr._redis()
        self.assertIs(mgr.redis_client, client)

    def test_redis_invalidated_and_retried_after_post_connect_outage(self):
        # A connection that drops later must go through the reconnect cooldown too.
        class FlakyRedisClient:
            def __init__(self):
                self.healthy = True

            def ping(self):
                pass

            def get(self, key):
                if not self.healthy:
                    raise ConnectionError("redis down")
                return None

        client = FlakyRedisClient()
        with patch("redis.Redis.from_url", return_value=client):
            mgr = SessionManager(redis_url="redis://fake-host:6379/0")
            self.assertIs(mgr.redis_client, client)

            client.healthy = False
            self.assertIsNone(mgr._load_from_redis("some-session-id"))
            self.assertIsNone(mgr.redis_client, "a failed operation must invalidate the stale client")

            self.assertIsNone(mgr._redis())

            client.healthy = True
            mgr._redis_last_attempt = 0
            self.assertIs(mgr._redis(), client)

if __name__ == "__main__":
    unittest.main()
