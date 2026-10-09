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

    def test_session_update_cookies_normalizes_leading_dot(self):
        sid = self.mgr.create_session()
        sess = self.mgr.get_session(sid)
        c1 = CookieModel(name="test_cookie", value="val1", domain=".example.com")
        sess.update_cookies([c1])
        self.assertEqual(len(sess.cookies), 1)

        c2 = CookieModel(name="test_cookie", value="val2_updated", domain="example.com")
        sess.update_cookies([c2])
        self.assertEqual(len(sess.cookies), 1)
        self.assertEqual(sess.cookies[0].value, "val2_updated")


if __name__ == "__main__":
    unittest.main()


class TestSessionBrowserStorage(unittest.TestCase):
    def _session(self):
        from app.solver.sessions import Session
        return Session("s1")

    def test_snapshot_replaces_each_named_origin_and_keeps_others(self):
        sess = self._session()
        sess.update_storage({"local": {"https://a.test": {"x": "1", "y": "2"}, "https://b.test": {"k": "v"}}})
        sess.update_storage({"local": {"https://a.test": {"x": "3"}}, "session": {"https://a.test": {"s": "1"}}})
        self.assertEqual(sess.local_storage, {"https://a.test": {"x": "3"}, "https://b.test": {"k": "v"}})
        self.assertEqual(sess.storage_payload()["session"], {"https://a.test": {"s": "1"}})

    def test_round_trips_through_redis_json(self):
        import json
        from app.solver.sessions import Session
        sess = self._session()
        sess.update_storage({"local": {"https://a.test": {"x": "1"}}, "session": {"https://a.test": {"y": "2"}}})
        restored = Session.from_dict(json.loads(json.dumps(sess.to_dict())))
        self.assertEqual(restored.storage_payload(), sess.storage_payload())

    def test_older_snapshot_without_storage_loads(self):
        from app.solver.sessions import Session
        restored = Session.from_dict({"session_id": "old", "cookies": []})
        self.assertIsNone(restored.storage_payload())

    def test_oversized_snapshot_is_refused(self):
        sess = self._session()
        sess.update_storage({"local": {"https://a.test": {"x": "1"}}})
        with patch.object(settings, "SESSION_STORAGE_MAX_KB", 1):
            self.assertFalse(sess.update_storage({"local": {"https://a.test": {"big": "z" * 4096}}}))
        self.assertEqual(sess.local_storage, {"https://a.test": {"x": "1"}})

    def test_malformed_snapshot_is_cleaned(self):
        sess = self._session()
        sess.update_storage({"local": {"https://a.test": ["not", "a", "map"], "https://b.test": {"n": 1}}})
        self.assertEqual(sess.local_storage, {"https://b.test": {"n": "1"}})


class TestApplyAndPersistSession(unittest.IsolatedAsyncioTestCase):
    async def test_session_feeds_the_request_and_learns_from_the_solution(self):
        from app.models.flaresolverr import SolutionModel, V1Request
        from app.solver import sessions as sessions_module
        manager = SessionManager(redis_url="")
        with patch.object(sessions_module, "session_manager", manager):
            sid = await manager.create_session_async("s1", proxy="http://proxy.test:8080")
            await manager.update_session_storage_async(sid, {"local": {"https://a.test": {"k": "v"}}})
            req = V1Request(cmd="request.get", url="https://a.test/", session=sid)
            storage = await sessions_module.apply_session(req)
            self.assertEqual(req.get_proxy_url(), "http://proxy.test:8080")
            self.assertEqual(storage["local"], {"https://a.test": {"k": "v"}})

            sol = SolutionModel(url="https://a.test/", status=200, storage={"local": {"https://a.test": {"k": "new"}}})
            await sessions_module.persist_session(req, sol)
            sess = await manager.get_session_async(sid)
            self.assertEqual(sess.local_storage, {"https://a.test": {"k": "new"}})

    def test_storage_is_never_serialized_to_clients(self):
        from app.models.flaresolverr import SolutionModel, V1Response
        sol = SolutionModel(url="https://a.test/", status=200, storage={"local": {"o": {"k": "v"}}})
        self.assertNotIn("storage", V1Response(solution=sol).model_dump()["solution"])
        self.assertNotIn("storage", V1Response(solution=sol).model_dump_json())

    def test_clients_cannot_inject_browser_storage(self):
        from app.models.flaresolverr import V1Request
        req = V1Request(cmd="request.get", url="https://a.test/", browser_storage={"local": {"x": {}}})
        self.assertNotIn("browser_storage", req.model_dump())
