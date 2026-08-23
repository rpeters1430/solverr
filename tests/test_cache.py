import os
import time
import tempfile
import unittest
from unittest.mock import patch
from app.config import settings
from app.models.flaresolverr import CookieModel
from app.solver.cache import CookieCache

class TestCookieCache(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cache_file = os.path.join(self.temp_dir, "test_cookies.json")
        self.cache = CookieCache(cache_file=self.cache_file)

    def tearDown(self):
        if os.path.exists(self.cache_file):
            try:
                os.remove(self.cache_file)
            except Exception:
                pass
        if os.path.exists(f"{self.cache_file}.tmp"):
            try:
                os.remove(f"{self.cache_file}.tmp")
            except Exception:
                pass

    def test_set_and_get_cookies(self):
        cookies = [
            CookieModel(name="cf_clearance", value="test_val_123", domain=".example.com"),
            CookieModel(name="session_id", value="sess_abc", domain="sub.example.com")
        ]
        self.cache.set_cookies("https://sub.example.com/page", cookies)

        fetched = self.cache.get_cookies("https://sub.example.com")
        cookie_names = {c.name for c in fetched}
        self.assertIn("cf_clearance", cookie_names)
        self.assertIn("session_id", cookie_names)

    def test_atomic_disk_persistence(self):
        cookies = [CookieModel(name="cf_clearance", value="val_persistent", domain=".example.com")]
        self.cache.set_cookies("https://example.com", cookies)
        self.assertTrue(os.path.exists(self.cache_file))

        # Re-load from disk in new instance
        new_cache = CookieCache(cache_file=self.cache_file)
        fetched = new_cache.get_cookies("https://example.com")
        self.assertEqual(len(fetched), 1)
        self.assertEqual(fetched[0].value, "val_persistent")

    def test_clear_cache(self):
        cookies = [CookieModel(name="cf_clearance", value="val_clear", domain=".example.com")]
        self.cache.set_cookies("https://example.com", cookies)
        self.cache.clear()
        self.assertEqual(len(self.cache.get_cookies("https://example.com")), 0)

    def test_flat_cache_file_path(self):
        flat_file = "test_flat_cookies_tmp.json"
        try:
            cache = CookieCache(cache_file=flat_file)
            cookies = [CookieModel(name="cf_clearance", value="val_flat", domain=".example.com")]
            cache.set_cookies("https://example.com", cookies)
            self.assertTrue(os.path.exists(flat_file))
            fetched = cache.get_cookies("https://example.com")
            self.assertEqual(len(fetched), 1)
        finally:
            if os.path.exists(flat_file):
                os.remove(flat_file)

    def test_cookie_deduplication_by_name(self):
        cookies = [
            CookieModel(name="cf_clearance", value="old_val", domain=".example.com"),
            CookieModel(name="cf_clearance", value="new_val", domain=".example.com"),
        ]
        self.cache.set_cookies("https://example.com", cookies)
        fetched = self.cache.get_cookies("https://example.com")
        self.assertEqual(len(fetched), 1)
        self.assertEqual(fetched[0].value, "new_val")

    def test_export_netscape_format(self):
        cookies = [
            CookieModel(name="cf_clearance", value="token123", domain=".example.com", path="/", secure=True, expires=1800000000)
        ]
        self.cache.set_cookies("https://example.com", cookies)
        netscape_txt = self.cache.export_netscape()
        self.assertIn("# Netscape HTTP Cookie File", netscape_txt)
        self.assertIn(".example.com", netscape_txt)
        self.assertIn("TRUE\t/\tTRUE\t1800000000\tcf_clearance\ttoken123", netscape_txt)

    def test_expired_cookie_is_not_returned(self):
        expired_ts = time.time() - 60  # Expired 1 minute ago
        valid_ts = time.time() + 3600  # Valid for 1 hour
        cookies = [
            CookieModel(name="expired_token", value="old", domain=".example.com", expires=expired_ts),
            CookieModel(name="valid_token", value="good", domain=".example.com", expires=valid_ts),
        ]
        self.cache.set_cookies("https://example.com", cookies)
        fetched = self.cache.get_cookies("https://example.com")
        names = {c.name: c.value for c in fetched}
        self.assertNotIn("expired_token", names)
        self.assertIn("valid_token", names)


    def test_evicts_oldest_domain_when_over_capacity(self):
        with patch.object(settings, "MAX_CACHE_DOMAINS", 2):
            self.cache.set_cookies("https://a.com", [CookieModel(name="x", value="1", domain="a.com")])
            time.sleep(0.01)
            self.cache.set_cookies("https://b.com", [CookieModel(name="x", value="1", domain="b.com")])
            time.sleep(0.01)
            self.cache.set_cookies("https://c.com", [CookieModel(name="x", value="1", domain="c.com")])

            self.assertEqual(len(self.cache._store), 2)
            self.assertNotIn("a.com", self.cache._store)
            self.assertIn("c.com", self.cache._store)

    def test_evicts_oldest_cookies_when_domain_over_capacity(self):
        with patch.object(settings, "MAX_COOKIES_PER_DOMAIN", 2):
            for i in range(3):
                self.cache.set_cookies("https://example.com", [CookieModel(name=f"c{i}", value="v", domain="example.com")])
                time.sleep(0.01)

            fetched = {c.name for c in self.cache.get_cookies("https://example.com")}
            self.assertEqual(len(fetched), 2)
            self.assertNotIn("c0", fetched)
            self.assertIn("c2", fetched)

    def test_cookie_identity_includes_path(self):
        # Two same-name cookies on different paths must not overwrite
        # each other in the local store.
        self.cache.set_cookies("https://example.com", [
            CookieModel(name="token", value="root", domain="example.com", path="/"),
        ])
        self.cache.set_cookies("https://example.com", [
            CookieModel(name="token", value="admin", domain="example.com", path="/admin"),
        ])
        stored = self.cache._store["example.com"]
        self.assertEqual(len(stored), 2)


if __name__ == "__main__":
    unittest.main()
