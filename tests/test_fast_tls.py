import unittest
from app.solver.fast_tls import FastTLSEngine, FIREFOX_PROFILES, CHROME_PROFILES, _sec_ch_ua_for


class TestFastTLSProfileRotation(unittest.TestCase):
    def test_profile_is_deterministic_per_domain(self):
        engine = FastTLSEngine()
        engine.rotate = True
        engine.profiles = FIREFOX_PROFILES
        first = engine._profile_for_domain("https://example.com/page1")
        second = engine._profile_for_domain("https://example.com/other-page")
        self.assertEqual(first, second)

    def test_profile_target_and_ua_always_match_browser_family(self):
        for target, ua in FIREFOX_PROFILES:
            self.assertTrue(target.startswith("firefox"))
            self.assertIn("Firefox/", ua)
            self.assertIn(target.replace("firefox", ""), ua)
        for target, ua in CHROME_PROFILES:
            self.assertTrue(target.startswith("chrome"))
            self.assertIn("Chrome/", ua)
            self.assertIn(target.replace("chrome", ""), ua)

    def test_rotation_disabled_always_returns_first_profile(self):
        engine = FastTLSEngine()
        engine.rotate = False
        engine.profiles = FIREFOX_PROFILES
        for domain in ["a.com", "b.com", "c.com"]:
            self.assertEqual(engine._profile_for_domain(f"https://{domain}"), FIREFOX_PROFILES[0])

    def test_sec_ch_ua_only_generated_for_chrome_targets(self):
        self.assertIsNone(_sec_ch_ua_for("firefox147"))
        header = _sec_ch_ua_for("chrome146")
        self.assertIn("146", header)
        self.assertIn("Chromium", header)


if __name__ == "__main__":
    unittest.main()
