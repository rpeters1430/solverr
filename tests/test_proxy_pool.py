import unittest
from unittest.mock import patch

from app.config import settings
from app.solver.proxy_pool import FallbackProxyPool, configured_fallback_proxies


class TestFallbackProxyPool(unittest.TestCase):
    def test_rotates_round_robin(self):
        pool = FallbackProxyPool(["http://a:1", "http://b:1", "http://c:1"])
        self.assertEqual([pool.pick() for _ in range(4)], ["http://a:1", "http://b:1", "http://c:1", "http://a:1"])

    def test_failed_proxy_cools_down_and_success_clears_it(self):
        pool = FallbackProxyPool(["http://a:1", "http://b:1"])
        pool.report("http://a:1", ok=False)
        self.assertEqual([pool.pick() for _ in range(3)], ["http://b:1"] * 3)
        pool.report("http://a:1", ok=True)
        self.assertIn("http://a:1", [pool.pick() for _ in range(2)])

    def test_cooldown_doubles_per_consecutive_failure(self):
        pool = FallbackProxyPool(["http://a:1"])
        with patch.object(settings, "FALLBACK_PROXY_COOLDOWN_SECONDS", 10):
            pool.report("http://a:1", ok=False)
            first = pool.stats()[0]["cooldown_remaining_s"]
            pool.report("http://a:1", ok=False)
            second = pool.stats()[0]["cooldown_remaining_s"]
        self.assertAlmostEqual(first, 10, delta=1)
        self.assertAlmostEqual(second, 20, delta=1)

    def test_when_all_are_cooling_down_the_soonest_to_recover_is_used(self):
        pool = FallbackProxyPool(["http://a:1", "http://b:1"])
        with patch.object(settings, "FALLBACK_PROXY_COOLDOWN_SECONDS", 10):
            pool.report("http://a:1", ok=False)
            pool.report("http://a:1", ok=False)
            pool.report("http://b:1", ok=False)
        self.assertEqual(pool.pick(), "http://b:1")

    def test_exclude_skips_proxies_already_tried(self):
        pool = FallbackProxyPool(["http://a:1", "http://b:1"])
        self.assertEqual(pool.pick(exclude=["http://a:1"]), "http://b:1")
        self.assertIsNone(pool.pick(exclude=["http://a:1", "http://b:1"]))

    def test_stats_mask_credentials(self):
        pool = FallbackProxyPool(["http://user:secret@a:1"])
        self.assertEqual(pool.stats()[0]["proxy"], "http://user:***@a:1")

    def test_configured_list_merges_both_settings_without_duplicates(self):
        with patch.object(settings, "FALLBACK_PROXY_URL", "http://a:1"), \
             patch.object(settings, "FALLBACK_PROXY_URLS", " http://b:1 ,http://a:1,, http://c:1"):
            self.assertEqual(configured_fallback_proxies(), ["http://a:1", "http://b:1", "http://c:1"])

    def test_default_pool_follows_settings(self):
        pool = FallbackProxyPool()
        with patch.object(settings, "FALLBACK_PROXY_URL", None), patch.object(settings, "FALLBACK_PROXY_URLS", ""):
            self.assertFalse(pool.enabled)
        with patch.object(settings, "FALLBACK_PROXY_URL", "http://a:1"):
            self.assertTrue(pool.enabled)
            self.assertEqual(pool.pick(), "http://a:1")


if __name__ == "__main__":
    unittest.main()
