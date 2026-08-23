import unittest
from unittest.mock import AsyncMock, patch, MagicMock
from app.models.flaresolverr import V1Request, SolutionModel, CookieModel
from app.solver.browser import (
    detect_challenge,
    is_challenge_title,
    has_age_gate_marker,
    CHALLENGE_MARKERS,
    AGE_GATE_MARKERS,
)
from app.solver.cache import CookieCache
from app.solver.fast_tls import FastTLSEngine
from app.solver.engine import HybridSolverEngine


class TestWAFChallengeScenarios(unittest.TestCase):
    """Test challenge detection across varied real-world HTML structures."""

    def test_cloudflare_turnstile_scenarios(self):
        # Scenario 1: Turnstile in title
        self.assertEqual(detect_challenge("Just a moment...", "", check_content=False), "cloudflare_turnstile")
        self.assertEqual(detect_challenge("Checking your browser - Site Name", "", check_content=False), "cloudflare_turnstile")

        # Scenario 2: Turnstile iframe in body
        html_iframe = '<iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/rcv0/0/xxxx"></iframe>'
        self.assertEqual(detect_challenge("Verification Page", html_iframe, check_content=True), "cloudflare_turnstile")

        # Scenario 3: Turnstile class in body
        html_div = '<div class="cf-turnstile" data-sitekey="0x4AAAAAAABBBBBB"></div>'
        self.assertEqual(detect_challenge("Login", html_div, check_content=True), "cloudflare_turnstile")

    def test_cloudflare_5s_interstitial_scenarios(self):
        # 5-second shield page
        self.assertEqual(detect_challenge("Attention Required! | Cloudflare", "", check_content=False), "cloudflare_5s")
        html_5s = '<p>DDoS protection by Cloudflare</p><p>Please wait 5 seconds...</p>'
        self.assertEqual(detect_challenge("Please Wait", html_5s, check_content=True), "cloudflare_5s")

    def test_ddos_guard_scenarios(self):
        # DDoS-Guard interstitial
        self.assertEqual(detect_challenge("DDOS-GUARD", "", check_content=False), "ddos_guard")
        html_ddg = '<script src="https://check.ddos-guard.net/check.js"></script>'
        self.assertEqual(detect_challenge("Security Check", html_ddg, check_content=True), "ddos_guard")

    def test_recaptcha_scenarios(self):
        html_recap = '<div class="g-recaptcha" data-sitekey="6Le-wvkSAAAAAPBorNoEgvd3"></div>'
        self.assertEqual(detect_challenge("Human Verification", html_recap, check_content=True), "recaptcha")
        html_recap_script = '<script src="https://www.google.com/recaptcha/api.js"></script>'
        self.assertEqual(detect_challenge("Sign In", html_recap_script, check_content=True), "recaptcha")

    def test_hcaptcha_scenarios(self):
        html_hcap = '<div class="h-captcha" data-sitekey="10000000-ffff-ffff-ffff-000000000001"></div>'
        self.assertEqual(detect_challenge("Protected Area", html_hcap, check_content=True), "hcaptcha")
        html_cf_hcap = '<div class="cf-hcaptcha" data-sitekey="xxx"></div>'
        self.assertEqual(detect_challenge("Challenge", html_cf_hcap, check_content=True), "hcaptcha")

    def test_imperva_incapsula_scenarios(self):
        html_incap = '<iframe src="/_Incapsula_Resource?SWKMTFCD=1"></iframe>'
        self.assertEqual(detect_challenge("Access Denied", html_incap, check_content=True), "imperva")
        html_visid = '<script>var b = "visid_incap_12345";</script>'
        self.assertEqual(detect_challenge("Please verify", html_visid, check_content=True), "imperva")

    def test_datadome_scenarios(self):
        html_dd = '<script src="https://geo.captcha-delivery.com/captcha/?initialCid=AHrlqAAAAAMA"></script>'
        self.assertEqual(detect_challenge("Security Verification", html_dd, check_content=True), "datadome")

    def test_akamai_scenarios(self):
        html_ak = '<script src="/ak_bmsc/telemetry.js"></script>'
        self.assertEqual(detect_challenge("Verification", html_ak, check_content=True), "akamai")

    def test_age_gate_detection(self):
        html_age = '<div class="disclaimer-dialog"><p>You must be 18 to view this content.</p><button id="enter_site">I AGREE</button></div>'
        self.assertTrue(has_age_gate_marker(html_age.lower(), check_content=True))
        self.assertFalse(has_age_gate_marker(html_age.lower(), check_content=False))

    def test_challenge_title_markers(self):
        self.assertTrue(is_challenge_title("Just a moment..."))
        self.assertTrue(is_challenge_title("Checking your browser before accessing example.com"))
        self.assertTrue(is_challenge_title("Attention Required! | Cloudflare"))
        self.assertTrue(is_challenge_title("DDOS-GUARD Protection"))
        self.assertTrue(is_challenge_title("Loading https://example.com/target"))
        self.assertFalse(is_challenge_title("Welcome to Awesome Site"))
        self.assertFalse(is_challenge_title("Home Page | My Tracker"))


class TestCookieScopingAndIsolation(unittest.TestCase):
    """Test cookie caching domain isolation and inheritance rules."""

    def setUp(self):
        import tempfile, os
        self.temp_dir = tempfile.mkdtemp()
        self.cache_file = os.path.join(self.temp_dir, "test_scope_cookies.json")
        self.cache = CookieCache(cache_file=self.cache_file)

    def tearDown(self):
        import os
        if os.path.exists(self.cache_file):
            try:
                os.remove(self.cache_file)
            except Exception:
                pass

    def test_parent_domain_cookies_propagate_to_subdomain(self):
        # A cookie set for parent .example.com should be available on sub.example.com
        parent_cookies = [CookieModel(name="cf_clearance", value="parent_token", domain=".example.com")]
        self.cache.set_cookies("https://example.com", parent_cookies)

        sub_cookies = self.cache.get_cookies("https://sub.example.com")
        names = {c.name: c.value for c in sub_cookies}
        self.assertIn("cf_clearance", names)
        self.assertEqual(names["cf_clearance"], "parent_token")

    def test_subdomain_cookies_do_not_leak_to_parent_domain(self):
        # A cookie scoped specifically to sub.example.com MUST NOT leak to parent example.com
        sub_cookies = [CookieModel(name="sub_only_token", value="secret_sub", domain="sub.example.com")]
        self.cache.set_cookies("https://sub.example.com", sub_cookies)

        parent_cookies = self.cache.get_cookies("https://example.com")
        names = {c.name: c.value for c in parent_cookies}
        self.assertNotIn("sub_only_token", names)

    def test_subdomain_cookies_do_not_leak_to_peer_subdomain(self):
        # A cookie for api.example.com MUST NOT leak to web.example.com
        api_cookies = [CookieModel(name="api_session", value="api_val", domain="api.example.com")]
        self.cache.set_cookies("https://api.example.com", api_cookies)

        web_cookies = self.cache.get_cookies("https://web.example.com")
        names = {c.name: c.value for c in web_cookies}
        self.assertNotIn("api_session", names)


class TestFastTLSEdgeCases(unittest.TestCase):
    """Test Fast TLS WAF challenge detection and profile consistency."""

    def test_fast_tls_domain_profile_consistency(self):
        engine = FastTLSEngine()
        p1 = engine._profile_for_domain("https://indexer1.org/search?q=test")
        p2 = engine._profile_for_domain("https://indexer1.org/details/123")
        self.assertEqual(p1, p2)

        # Different domain may yield same or different deterministic profile
        p3 = engine._profile_for_domain("https://another-domain.net")
        self.assertIn(p3[0], [p[0] for p in engine.profiles])


if __name__ == "__main__":
    unittest.main()
