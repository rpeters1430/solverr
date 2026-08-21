import unittest
from app.solver.browser import detect_challenge, is_challenge_title, has_age_gate_marker


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

    def test_cloudflare_loading_redirect_title_is_still_a_challenge(self):
        # Cloudflare shows "Loading <target-url>" as a transitional title
        # while its JS challenge finishes and window.location redirects -
        # treating this as cleared snapshots the page mid-transition
        # (observed live: a real solve broke out here and reported the
        # transitional page's 403 instead of waiting for the real result).
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


if __name__ == "__main__":
    unittest.main()
