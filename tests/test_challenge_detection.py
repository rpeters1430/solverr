import unittest
from app.solver.browser import (
    challenge_from_headers,
    detect_challenge,
    ip_block_provider,
    is_challenge_wall,
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


REAL_PAGE_BODY = "<main>" + "<p>search result row</p>" * 400 + "</main>"


class TestBrandNamesAreNotChallenges(unittest.TestCase):
    def test_datadome_telemetry_tag_is_not_a_challenge(self):
        html = '<html><title>Shop</title><script src="https://js.datadome.co/tags.js"></script>' + REAL_PAGE_BODY
        self.assertIsNone(detect_challenge("Shop", html, check_content=True))

    def test_prose_mentioning_captcha_vendors_is_not_a_challenge(self):
        html = "<p>We compared GeeTest, hCaptcha (hcaptcha.com) and Incapsula for our forum.</p>"
        self.assertIsNone(detect_challenge("Blog", html, check_content=True))

    def test_geetest_widget_markup_is_detected(self):
        self.assertEqual(detect_challenge("Login", '<div class="geetest_btn_click"></div>', check_content=True), "geetest")
        self.assertEqual(detect_challenge("Login", "<script>initGeetest4({captchaId: 'x'})</script>", check_content=True), "geetest")


class TestChallengeFromHeaders(unittest.TestCase):
    def test_cloudflare_mitigated_header(self):
        self.assertEqual(challenge_from_headers({"CF-Mitigated": "challenge"}), "cloudflare_turnstile")
        self.assertEqual(detect_challenge("", "", False, headers={"cf-mitigated": "challenge"}), "cloudflare_turnstile")

    def test_aws_waf_action_needs_its_documented_status(self):
        self.assertEqual(challenge_from_headers({"x-amzn-waf-action": "challenge"}, 202), "aws_waf")
        self.assertEqual(challenge_from_headers({"x-amzn-waf-action": "captcha"}, 405), "aws_waf")
        self.assertIsNone(challenge_from_headers({"x-amzn-waf-action": "challenge"}, 200))

    def test_datadome_block_header_but_not_the_always_on_one(self):
        self.assertEqual(challenge_from_headers({"x-dd-b": "1"}, 403), "datadome")
        self.assertIsNone(challenge_from_headers({"x-datadome": "protected"}, 200))

    def test_ordinary_headers_declare_nothing(self):
        self.assertIsNone(challenge_from_headers({"content-type": "text/html", "server": "cloudflare"}, 200))
        self.assertIsNone(challenge_from_headers(None))


class TestIsChallengeWall(unittest.TestCase):
    def _wall(self, title, html, status=200, headers=None):
        challenge = detect_challenge(title, html, True, headers=headers, status=status)
        return is_challenge_wall(challenge, title, html, status=status, headers=headers)

    def test_cloudflare_interstitial_is_a_wall(self):
        html = '<h2 id="challenge-running">Checking</h2><script>window._cf_chl_opt={}</script>'
        self.assertTrue(self._wall("Just a moment...", html, status=403))
        # Served with a 200 and a neutral title, the orchestration markers still give it away.
        self.assertTrue(self._wall("example.com", html, status=200))

    def test_embedded_widget_on_a_real_page_is_not_a_wall(self):
        for widget in (
            '<div class="cf-turnstile" data-sitekey="x"></div>',
            '<div class="g-recaptcha" data-sitekey="x"></div>',
            '<div class="h-captcha" data-sitekey="x"></div>',
        ):
            self.assertFalse(self._wall("Login", "<form>" + widget + "</form>" + REAL_PAGE_BODY), widget)

    def test_widget_page_served_with_a_block_status_is_a_wall(self):
        self.assertTrue(self._wall("Verify", '<div class="h-captcha" data-sitekey="x"></div>', status=403))

    def test_imperva_telemetry_on_a_real_page_is_not_a_wall(self):
        html = '<script src="/_Incapsula_Resource?SWJIYLWA=719d34d31c8e3a6e6fffd425f7e032f3"></script>' + REAL_PAGE_BODY
        self.assertFalse(self._wall("Search results", html))

    def test_imperva_lean_stub_and_incident_page_are_walls(self):
        self.assertTrue(self._wall("", '<html><script src="/_Incapsula_Resource?SWJIYLWA=1"></script></html>'))
        self.assertTrue(self._wall("", "Request unsuccessful. Incapsula incident ID: 123-456" + REAL_PAGE_BODY))

    def test_aws_waf_sdk_on_a_real_page_is_not_a_wall_but_the_interstitial_is(self):
        sdk = '<script src="https://abc.token.awswaf.com/abc/challenge.js"></script>' + REAL_PAGE_BODY
        self.assertFalse(self._wall("Shop", sdk))
        self.assertTrue(self._wall("", "<script>window.gokuProps = {key: 'a'};</script>"))
        self.assertTrue(self._wall("", "", status=202, headers={"x-amzn-waf-action": "challenge"}))

    def test_datadome_wall_variants(self):
        self.assertTrue(self._wall("", "<script>var dd={'rt':'c','cid':'x','host':'geo.captcha-delivery.com'}</script>", status=403))
        self.assertTrue(self._wall("", '{"url":"https://geo.captcha-delivery.com/captcha/?initialCid=x"}', status=200))

    def test_ddos_guard_footer_mention_is_not_a_wall(self):
        self.assertFalse(self._wall("Tracker", "<footer>Protected by ddos-guard</footer>" + REAL_PAGE_BODY))
        self.assertTrue(self._wall("Tracker", '<script src="https://check.ddos-guard.net/check.js"></script>'))

    def test_clean_page_is_not_a_wall(self):
        self.assertFalse(self._wall("Home", REAL_PAGE_BODY))
        self.assertFalse(is_challenge_wall(None, "Home", "", status=403))

    def test_challenge_title_alone_is_a_wall(self):
        self.assertTrue(is_challenge_wall(None, "Just a moment...", ""))


class TestIpBlockProvider(unittest.TestCase):
    def test_cloudflare_firewall_and_rate_limit_pages(self):
        blocked = '<div id="cf-error-details"><h1>Sorry, you have been blocked</h1></div>'
        self.assertEqual(ip_block_provider(blocked), "cloudflare")
        coded = '<div id="cf-error-details"><span class="cf-error-code">1020</span> Access denied</div>'
        self.assertEqual(ip_block_provider(coded), "cloudflare")
        self.assertEqual(ip_block_provider('<div class="cf-error-details">Error code: 1015</div>'), "cloudflare")
        self.assertEqual(ip_block_provider("", "https://example.com/cdn-cgi/error/1020"), "cloudflare")

    def test_cloudflare_browser_signature_ban_is_left_to_a_fresh_fingerprint(self):
        self.assertIsNone(ip_block_provider('<div id="cf-error-details"><span class="cf-error-code">1010</span></div>'))

    def test_datadome_hard_block_but_not_its_solvable_walls(self):
        hard = "<script>var dd={'rt':'c','t':'bv','host':'geo.captcha-delivery.com'}</script>"
        self.assertEqual(ip_block_provider(hard), "datadome")
        self.assertEqual(ip_block_provider('{"url":"https://geo.captcha-delivery.com/captcha/?cid=x&t=bv"}'), "datadome")
        self.assertIsNone(ip_block_provider("<script>var dd={'rt':'c','t':'fe','host':'geo.captcha-delivery.com'}</script>"))

    def test_ordinary_pages_and_solvable_challenges_are_not_ip_blocks(self):
        self.assertIsNone(ip_block_provider(REAL_PAGE_BODY, "https://example.com/"))
        self.assertIsNone(ip_block_provider("<title>Just a moment...</title><script>window._cf_chl_opt={}</script>"))
        self.assertIsNone(ip_block_provider("<p>Our docs explain Error 1020 and why you have been blocked.</p>"))


if __name__ == "__main__":
    unittest.main()


ANUBIS_CHALLENGE_PAGE = """<!doctype html><html><head><title>Making sure you&#39;re not a bot!</title>
<script id="anubis_version" type="application/json">"v1.21.3"</script>
<script id="anubis_challenge" type="application/json">{"rules":{"algorithm":"fast","difficulty":4},
"challenge":{"id":"0199abcd","randomData":"9f86d081884c7d65"}}</script>
<script async type="module" src="/.within.website/x/cmd/anubis/static/js/main.mjs?cacheBuster=v1.21.3"></script>
</head><body><h1 id="title">Making sure you're not a bot!</h1></body></html>"""

ANUBIS_REJECT_PAGE = """<html><head><title>Oh noes!</title>
<script id="anubis_version" type="application/json">"v1.21.3"</script></head>
<body><img src="/.within.website/x/cmd/anubis/static/img/reject.webp?cacheBuster=v1.21.3"><p>Access denied</p></body></html>"""


class TestAnubisCapGoogle(unittest.TestCase):
    def test_anubis_challenge_page(self):
        from app.solver.browser import anubis_state
        self.assertEqual(anubis_state(ANUBIS_CHALLENGE_PAGE), "challenge")
        self.assertEqual(detect_challenge("", ANUBIS_CHALLENGE_PAGE, check_content=True), "anubis")
        self.assertTrue(is_challenge_wall("anubis", "", ANUBIS_CHALLENGE_PAGE, status=200))

    def test_anubis_detected_in_lowercased_content(self):
        # Fast TLS hands detection a lowercased body.
        self.assertEqual(detect_challenge("", ANUBIS_CHALLENGE_PAGE.lower(), check_content=True), "anubis")

    def test_anubis_bootstrap_with_version_is_a_wall_even_without_challenge_metadata(self):
        from app.solver.browser import anubis_state
        page = ANUBIS_CHALLENGE_PAGE.replace('"randomData":"9f86d081884c7d65"', '"randomData":""')
        self.assertEqual(anubis_state(page), "challenge")

    def test_anubis_reject_page_is_blocked(self):
        from app.solver.browser import anubis_state
        self.assertEqual(anubis_state(ANUBIS_REJECT_PAGE), "blocked")

    def test_page_documenting_anubis_is_not_a_wall(self):
        from app.solver.browser import anubis_state
        docs = (
            "<html><body><p>Anubis serves /.within.website/x/cmd/anubis/static/js/main.mjs and renders "
            "anubis_challenge metadata.</p><noscript>" + ANUBIS_CHALLENGE_PAGE + "</noscript>"
            "<textarea>" + ANUBIS_REJECT_PAGE + "</textarea></body></html>"
        )
        self.assertIsNone(anubis_state(docs))
        self.assertIsNone(detect_challenge("Anubis docs", docs, check_content=True))

    def test_anubis_verification_url(self):
        from app.solver.browser import is_anubis_verification_url
        self.assertTrue(is_anubis_verification_url(
            "https://git.example.org/.within.website/x/cmd/anubis/api/pass-challenge?response=abc"
        ))
        self.assertFalse(is_anubis_verification_url("https://git.example.org/repo"))

    def test_cap_widget_is_an_embedded_widget(self):
        from app.solver.browser import WIDGET_CHALLENGES
        page = '<form><cap-widget data-cap-api-endpoint="/api/"></cap-widget><button>Send</button></form>'
        self.assertEqual(detect_challenge("Contact", page, check_content=True), "cap")
        self.assertIn("cap", WIDGET_CHALLENGES)
        self.assertFalse(is_challenge_wall("cap", "Contact", page, status=200))

    def test_google_sorry_is_an_ip_block(self):
        from app.solver.browser import is_google_sorry_url
        sorry = "https://www.google.com/sorry/index?continue=https://www.google.com/search%3Fq%3Dx"
        self.assertTrue(is_google_sorry_url(sorry))
        self.assertTrue(is_google_sorry_url("https://google.com./sorry"))
        self.assertEqual(ip_block_provider("<html></html>", sorry), "google")

    def test_lookalike_sorry_urls_are_not_google(self):
        from app.solver.browser import is_google_sorry_url
        self.assertFalse(is_google_sorry_url("https://www.google.com/sorrybutnot"))
        self.assertFalse(is_google_sorry_url("https://evil.example/sorry/index"))
        self.assertFalse(is_google_sorry_url("https://www.google.com.evil.example/sorry/"))
        self.assertFalse(is_google_sorry_url("https://www.google.com:8443/sorry/"))
        self.assertFalse(is_google_sorry_url("ftp://www.google.com/sorry/"))
        self.assertIsNone(ip_block_provider("<html></html>", "https://www.google.com/search?q=x"))
