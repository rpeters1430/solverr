import unittest

from app.solver.browser import datadome_action, needs_unsolvable_captcha
from app.solver.browser.clearance import (
    BLOCK_PAGE_COOKIES,
    abck_is_valid,
    cookie_applies_to_host,
    has_earned_sensor_cookie,
    host_of,
    sensor_snapshot,
)


def _cookie(name, value, domain="example.com"):
    return {"name": name, "value": value, "domain": domain, "path": "/"}


class TestHostMatching(unittest.TestCase):
    def test_host_of(self):
        self.assertEqual(host_of("https://Tracker.Example.com:8443/search?q=1"), "tracker.example.com")
        self.assertEqual(host_of("not a url"), "")

    def test_cookie_domain_covers_host_and_its_subdomains_only(self):
        self.assertTrue(cookie_applies_to_host(".example.com", "example.com"))
        self.assertTrue(cookie_applies_to_host("example.com", "www.example.com"))
        self.assertFalse(cookie_applies_to_host("www.example.com", "example.com"))
        # A suffix match that isn't on a label boundary is a different site.
        self.assertFalse(cookie_applies_to_host("ample.com", "example.com"))
        self.assertFalse(cookie_applies_to_host(None, "example.com"))


class TestEarnedSensorCookie(unittest.TestCase):
    HOST = "example.com"

    def _earned(self, challenge, cookies, baseline=frozenset()):
        return has_earned_sensor_cookie(challenge, cookies, self.HOST, set(baseline))

    def test_each_provider_is_proven_by_its_own_cookie(self):
        cases = {
            "cloudflare_turnstile": "cf_clearance",
            "cloudflare_5s": "cf_clearance",
            "imperva": "reese84",
            "datadome": "datadome",
            "aws_waf": "aws-waf-token",
            "ddos_guard": "__ddg2_",
        }
        for challenge, name in cases.items():
            self.assertTrue(self._earned(challenge, [_cookie(name, "v1")]), challenge)
            self.assertFalse(self._earned(challenge, [_cookie("session", "v1")]), challenge)
        self.assertTrue(self._earned("imperva", [_cookie("___utmvc", "legacy")]))

    def test_another_providers_cookie_proves_nothing(self):
        self.assertFalse(self._earned("imperva", [_cookie("cf_clearance", "v1")]))
        self.assertFalse(self._earned("recaptcha", [_cookie("cf_clearance", "v1")]))
        self.assertFalse(self._earned(None, [_cookie("cf_clearance", "v1")]))

    def test_cookie_for_another_site_proves_nothing(self):
        self.assertFalse(self._earned("cloudflare_turnstile", [_cookie("cf_clearance", "v1", domain="other.org")]))
        self.assertTrue(self._earned("cloudflare_turnstile", [_cookie("cf_clearance", "v1", domain=".example.com")]))

    def test_cookie_that_was_already_there_is_not_earned_until_it_changes(self):
        stale = [_cookie("cf_clearance", "cached")]
        baseline = sensor_snapshot(stale, self.HOST)
        self.assertFalse(self._earned("cloudflare_turnstile", stale, baseline))
        self.assertTrue(self._earned("cloudflare_turnstile", [_cookie("cf_clearance", "fresh")], baseline))

    def test_akamai_cookie_only_counts_once_validated(self):
        self.assertFalse(abck_is_valid("ABC123~-1~YAAQ~-1~-1"))
        self.assertTrue(abck_is_valid("ABC123~0~YAAQ~-1~-1"))
        self.assertFalse(abck_is_valid("no-separator"))
        self.assertFalse(self._earned("akamai", [_cookie("_abck", "ABC~-1~YAAQ")]))
        self.assertTrue(self._earned("akamai", [_cookie("_abck", "ABC~0~YAAQ")]))

    def test_ddos_guard_cookies_match_by_prefix(self):
        self.assertTrue(self._earned("ddos_guard", [_cookie("__ddg5_", "x")]))
        self.assertFalse(self._earned("ddos_guard", [_cookie("__ddg1_", "x")]))

    def test_block_page_cookie_snapshot_is_limited_to_the_named_cookies(self):
        cookies = [_cookie("datadome", "wall"), _cookie("aws-waf-token", "wall"), _cookie("cf_clearance", "c")]
        self.assertEqual(
            sensor_snapshot(cookies, self.HOST, BLOCK_PAGE_COOKIES),
            {("datadome", "wall"), ("aws-waf-token", "wall")},
        )
        # The wall's own datadome cookie is baselined, so only the one issued after the check counts.
        baseline = sensor_snapshot(cookies, self.HOST, BLOCK_PAGE_COOKIES)
        self.assertFalse(self._earned("datadome", cookies, baseline))
        self.assertTrue(self._earned("datadome", [_cookie("datadome", "passed")], baseline))


class TestDataDomeAction(unittest.TestCase):
    def test_declared_variant_wins(self):
        self.assertEqual(datadome_action("var dd={'rt':'i','cid':'x','host':'geo.captcha-delivery.com'}"), "interstitial")
        self.assertEqual(datadome_action("var dd={'rt':'c','cid':'x','host':'geo.captcha-delivery.com'}"), "captcha")
        # The interstitial script tag is on the page too, but `rt` says slider.
        both = "var dd={'rt':'c','host':'geo.captcha-delivery.com'}</script><script src='https://ct.captcha-delivery.com/i.js'>"
        self.assertEqual(datadome_action(both), "captcha")

    def test_hard_block_outranks_the_variant(self):
        self.assertEqual(datadome_action("var dd={'rt':'c','t':'bv','host':'geo.captcha-delivery.com'}"), "blocked")
        self.assertEqual(datadome_action('{"url":"https://geo.captcha-delivery.com/captcha/?cid=x&t=bv"}'), "blocked")

    def test_script_path_fallbacks(self):
        self.assertEqual(datadome_action("<script src='https://ct.captcha-delivery.com/i.js'></script>"), "interstitial")
        self.assertEqual(datadome_action('{"url":"https://geo.captcha-delivery.com/captcha/?initialCid=x"}'), "captcha")

    def test_header_only_and_no_wall(self):
        self.assertEqual(datadome_action("", {"x-dd-b": "1"}), "interstitial")
        self.assertIsNone(datadome_action('<script src="https://js.datadome.co/tags.js"></script>'))
        self.assertIsNone(datadome_action("", {"x-datadome": "protected"}))


class TestNeedsUnsolvableCaptcha(unittest.TestCase):
    def test_datadome_slider_but_not_its_device_check(self):
        slider = "var dd={'rt':'c','cid':'x','host':'geo.captcha-delivery.com'}"
        device_check = "var dd={'rt':'i','cid':'x','host':'geo.captcha-delivery.com'}"
        self.assertTrue(needs_unsolvable_captcha("datadome", slider))
        self.assertFalse(needs_unsolvable_captcha("datadome", device_check))

    def test_aws_waf_captcha_but_not_its_silent_challenge(self):
        captcha = "<script>window.gokuProps={}</script><script src='https://x.token.awswaf.com/x/captcha.js'></script>"
        challenge = "<script>window.gokuProps={}</script><script src='https://x.token.awswaf.com/x/challenge.js'></script>"
        self.assertTrue(needs_unsolvable_captcha("aws_waf", captcha))
        self.assertFalse(needs_unsolvable_captcha("aws_waf", challenge))
        self.assertTrue(needs_unsolvable_captcha("aws_waf", "", {"x-amzn-waf-action": "captcha"}, 405))
        self.assertFalse(needs_unsolvable_captcha("aws_waf", "", {"x-amzn-waf-action": "challenge"}, 202))

    def test_challenges_with_a_solve_path_are_never_written_off(self):
        for challenge in ("cloudflare_turnstile", "recaptcha", "hcaptcha", "imperva", "akamai", None):
            self.assertFalse(needs_unsolvable_captcha(challenge, "<div class='g-recaptcha'></div>captcha.js"), challenge)


if __name__ == "__main__":
    unittest.main()
