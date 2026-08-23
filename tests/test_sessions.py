import time
import unittest
from unittest.mock import patch
from app.config import settings
from app.models.flaresolverr import CookieModel
from app.solver.sessions import SessionManager

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

if __name__ == "__main__":
    unittest.main()
