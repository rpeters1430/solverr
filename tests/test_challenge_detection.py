import unittest
from app.solver.browser import (
    detect_challenge,
    is_challenge_title,
    has_age_gate_marker,
    is_browser_error,
)


class TestChallengeDetection(unittest.TestCase):
    def test_detects_turnstile_from_title(self):
        self.assertEqual(detect_challenge("Just a moment...", "", check_content=False), "cloudflare_turnstile")

    def test_detects_recaptcha_from_content_only_when_checked(self):
        content = "<div class='g-recaptcha'></div>"
        self.assertIsNone(detect_challenge("Example Site", content, check_content=False))
        self.assertEqual(detect_challenge("Example Site", content, check_content=True), "recaptcha")

    def test_detects_hcaptcha(self):
        self.assertEqual(detect_challenge("Verify", "<script src='hcaptcha.com/1/api.js'></script>", check_content=True), "hcaptcha")

    def test_detects_datadome(self):
        self.assertEqual(detect_challenge("", "geo.captcha-delivery.com", check_content=True), "datadome")

    def test_detects_aws_waf(self):
        content = "<script>window.gokuProps = {key: 'abc'};</script>"
        self.assertIsNone(detect_challenge("Request Blocked", content, check_content=False))
        self.assertEqual(detect_challenge("Request Blocked", content, check_content=True), "aws_waf")

    def test_detects_aws_waf_challenge_script_marker(self):
        self.assertEqual(detect_challenge("", "<script src='/awswaf/challenge.js'></script>", check_content=True), "aws_waf")

    def test_aws_waf_cookie_name_alone_is_not_detected(self):
        # Detection never sees cookies, so the aws-waf-token name alone must not match.
        self.assertIsNone(detect_challenge("", "document.cookie contains aws-waf-token=...", check_content=True))

    def test_clean_page_has_no_challenge(self):
        self.assertIsNone(detect_challenge("My Cool Blog", "<h1>Welcome</h1>", check_content=True))

    def test_first_matching_marker_wins(self):
        # Title contains both a turnstile marker and a recaptcha content marker;
        # turnstile is checked first in CHALLENGE_MARKERS so it should win.
        result = detect_challenge("Just a moment...", "g-recaptcha", check_content=True)
        self.assertEqual(result, "cloudflare_turnstile")

    def test_is_challenge_title(self):
        self.assertTrue(is_challenge_title("Just a moment..."))
        self.assertTrue(is_challenge_title("Attention Required! | Cloudflare"))
        self.assertFalse(is_challenge_title("Welcome to my site"))
        self.assertFalse(is_challenge_title(""))

    def test_legitimate_pages_with_cloudflare_in_title_are_not_challenge_titles(self):
        # Titles mentioning "Cloudflare" in legitimate contexts must not trigger challenge loops
        self.assertFalse(is_challenge_title("Home – Cloudflare Tools"))
        self.assertFalse(is_challenge_title("Cloudflare Turnstile demo: Sample Form with Cloudflare Turnstile"))
        self.assertFalse(is_challenge_title("Cloudflare - Wikipedia"))
        self.assertFalse(is_challenge_title("What is Cloudflare? | Cloudflare Learning"))

    def test_bare_turnstile_in_text_does_not_trigger_challenge(self):
        self.assertIsNone(detect_challenge("News", "The subway turnstile was broken today", check_content=True))

    def test_turnstile_cf_turnstile_class_triggers_challenge(self):
        self.assertEqual(detect_challenge("Login", "<div class='cf-turnstile'></div>", check_content=True), "cloudflare_turnstile")
        self.assertEqual(
            detect_challenge("Login", "<script src='https://challenges.cloudflare.com/turnstile/v0/api.js'></script>", check_content=True),
            "cloudflare_turnstile"
        )

    def test_bare_akamai_in_json_text_does_not_trigger_challenge(self):
        self.assertIsNone(detect_challenge("", '{"akamai_fingerprint": "1:65536;2:0"}', check_content=True))

    def test_cloudflare_loading_redirect_title_is_still_a_challenge(self):
        # Treating this mid-redirect title as cleared once returned the transitional page's 403.
        self.assertTrue(is_challenge_title("Loading https://eztvx.to/home"))
        self.assertTrue(is_challenge_title("Loading http://example.com/page"))

    def test_unrelated_loading_title_without_url_is_not_a_challenge(self):
        self.assertFalse(is_challenge_title("Loading..."))
        self.assertFalse(is_challenge_title("Loading your dashboard"))

    def test_age_gate_marker_only_checked_when_content_checked(self):
        content_lower = "please confirm you are 18 <div class='disclaimer-dialog'>"
        self.assertFalse(has_age_gate_marker(content_lower, check_content=False))
        self.assertTrue(has_age_gate_marker(content_lower, check_content=True))
        self.assertFalse(has_age_gate_marker("nothing interesting here", check_content=True))

    def test_is_browser_error(self):
        self.assertTrue(is_browser_error("Problem loading page", "https://torrentgalaxy.to"))
        self.assertTrue(is_browser_error("Warning: Security Risk Ahead", "https://nyaa.si"))
        self.assertTrue(is_browser_error("Server Not Found", "https://example.com"))
        self.assertTrue(is_browser_error("Address Not Found", "https://example.com"))
        self.assertTrue(is_browser_error("Anything", "about:neterror?e=dnsNotFound"))
        self.assertTrue(is_browser_error("Anything", "about:certerror?e=nssBadCert"))
        self.assertFalse(is_browser_error("Search results - Example Indexer", "https://example.com"))
        self.assertFalse(is_browser_error("Home", "https://cloudflare.manfredi.io/"))


if __name__ == "__main__":
    unittest.main()
