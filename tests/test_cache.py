import os
import time
import tempfile
import unittest
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

if __name__ == "__main__":
    unittest.main()
