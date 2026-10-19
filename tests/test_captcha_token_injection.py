import unittest
from app.solver.browser import extract_sitekey, inject_captcha_token


class FakeLocator:
    def __init__(self, count: int, attrs: dict):
        self._count = count
        self._attrs = attrs

    async def count(self):
        return self._count

    async def get_attribute(self, name):
        return self._attrs.get(name)

    @property
    def first(self):
        return self


class FakePage:
    def __init__(self, selector_map: dict, evaluate_recorder=None):
        self._selector_map = selector_map
        self.evaluate_recorder = evaluate_recorder

    def locator(self, sel):
        if sel in self._selector_map:
            return self._selector_map[sel]
        return FakeLocator(count=0, attrs={})

    async def evaluate(self, script, args):
        if self.evaluate_recorder is not None:
            self.evaluate_recorder.append(args)


class TestExtractSitekey(unittest.IsolatedAsyncioTestCase):
    async def test_finds_sitekey_and_callback_on_first_matching_selector(self):
        page = FakePage({
            ".g-recaptcha": FakeLocator(count=1, attrs={"data-sitekey": "SITEKEY123", "data-callback": "onSolved"}),
        })
        result = await extract_sitekey(page, [".g-recaptcha", "div[data-sitekey][class*='recaptcha']"])
        self.assertEqual(result, ("SITEKEY123", "onSolved"))

    async def test_falls_through_to_second_selector_when_first_absent(self):
        page = FakePage({
            ".g-recaptcha": FakeLocator(count=0, attrs={}),
            "div[data-sitekey][class*='recaptcha']": FakeLocator(count=1, attrs={"data-sitekey": "ABC"}),
        })
        result = await extract_sitekey(page, [".g-recaptcha", "div[data-sitekey][class*='recaptcha']"])
        self.assertEqual(result, ("ABC", None))

    async def test_returns_none_when_no_widget_found(self):
        page = FakePage({})
        result = await extract_sitekey(page, [".g-recaptcha", ".h-captcha"])
        self.assertIsNone(result)

    async def test_returns_none_when_element_present_but_no_sitekey_attribute(self):
        page = FakePage({".cf-turnstile": FakeLocator(count=1, attrs={})})
        result = await extract_sitekey(page, [".cf-turnstile"])
        self.assertIsNone(result)


class TestInjectCaptchaToken(unittest.IsolatedAsyncioTestCase):
    async def test_passes_field_names_token_and_callback_to_page_evaluate(self):
        recorder = []
        page = FakePage({}, evaluate_recorder=recorder)
        await inject_captcha_token(page, ["g-recaptcha-response"], "TOKEN_ABC", "onSolved")
        self.assertEqual(len(recorder), 1)
        self.assertEqual(recorder[0]["names"], ["g-recaptcha-response"])
        self.assertEqual(recorder[0]["token"], "TOKEN_ABC")
        self.assertEqual(recorder[0]["callback"], "onSolved")

    async def test_swallows_evaluate_errors(self):
        class ExplodingPage(FakePage):
            async def evaluate(self, script, args):
                raise RuntimeError("page closed")

        page = ExplodingPage({})
        # Should not raise.
        await inject_captcha_token(page, ["g-recaptcha-response"], "TOKEN", None)


class TestWidgetParamsAndSolverCookie(unittest.IsolatedAsyncioTestCase):
    async def test_extracts_render_parameters(self):
        from app.solver.browser.captcha import extract_widget_params
        page = FakePage({".cf-turnstile": FakeLocator(count=1, attrs={
            "data-sitekey": "K", "data-action": "login", "data-cdata": "C", "data-callback": "cb"})})
        params = await extract_widget_params(page, [".cf-turnstile"])
        self.assertEqual((params["sitekey"], params["action"], params["cdata"], params["callback"]), ("K", "login", "C", "cb"))
        self.assertIsNone(params["s"])

    async def test_escalation_passes_turnstile_action_and_cdata(self):
        from unittest.mock import AsyncMock, patch
        from app.solver.browser import captcha as captcha_module
        recorder = []
        page = FakePage({".cf-turnstile": FakeLocator(count=1, attrs={"data-sitekey": "K", "data-action": "login"})},
                        evaluate_recorder=recorder)
        solve = AsyncMock(return_value="TOKEN")
        with patch.object(captcha_module.captcha_solver, "solve_turnstile", solve):
            self.assertTrue(await captcha_module.try_captcha_solver_escalation(page, "https://e.test", "cloudflare_turnstile"))
        solve.assert_awaited_once_with("K", "https://e.test", action="login", cdata=None)

    def test_parse_solver_cookie(self):
        from app.solver.browser.captcha import parse_solver_cookie
        cookie = parse_solver_cookie("datadome=abc~1; Max-Age=31536000; Domain=.e.test; Path=/; Secure; SameSite=Lax", "www.e.test")
        self.assertEqual(cookie, {"name": "datadome", "value": "abc~1", "domain": ".e.test", "path": "/",
                                  "secure": True, "sameSite": "Lax"})
        self.assertEqual(parse_solver_cookie("datadome=x", "www.e.test")["domain"], "www.e.test")
        self.assertIsNone(parse_solver_cookie("", "e.test"))


if __name__ == "__main__":
    unittest.main()


class _CloudflarePage:
    def __init__(self, url, results):
        self.url = url
        self.results = list(results)
        self.scripts = []

    async def evaluate(self, script, *args):
        self.scripts.append(script)
        return self.results.pop(0)


RENDER = {
    "websiteURL": "https://e.test/", "websiteKey": "0xKEY", "action": "managed",
    "data": "cdata", "pagedata": "chlpagedata", "userAgent": "UA",
}


class TestCloudflareChallengeCapture(unittest.IsolatedAsyncioTestCase):
    async def test_install_uses_a_random_main_world_key(self):
        from app.solver.browser import captcha as captcha_module

        class Ctx:
            scripts = []

            async def add_init_script(self, script):
                self.scripts.append(script)

        ctx = Ctx()
        key1 = await captcha_module.install_cloudflare_capture(ctx)
        key2 = await captcha_module.install_cloudflare_capture(ctx)
        self.assertNotEqual(key1, key2)
        self.assertTrue(ctx.scripts[0].startswith("mw:("))
        self.assertIn(key1, ctx.scripts[0])

    async def test_read_rejects_incomplete_renders(self):
        from app.solver.browser.captcha import read_cloudflare_render
        self.assertEqual(await read_cloudflare_render(_CloudflarePage("https://e.test/", [dict(RENDER)]), "k"), RENDER)
        self.assertIsNone(await read_cloudflare_render(_CloudflarePage("https://e.test/", [None]), "k"))
        self.assertIsNone(await read_cloudflare_render(_CloudflarePage("https://e.test/", [{**RENDER, "pagedata": ""}]), "k"))

    async def test_token_is_delivered_through_the_page_callback(self):
        from unittest.mock import AsyncMock, patch
        from app.solver.browser import captcha as captcha_module
        page = _CloudflarePage("https://e.test/", [True])
        with patch.object(captcha_module.captcha_solver, "solve_cloudflare_challenge", AsyncMock(return_value="TOKEN")) as solve:
            self.assertTrue(await captcha_module.try_cloudflare_challenge_solver(page, "k", RENDER, "http://p.test:1", 30.0))
        self.assertEqual(solve.call_args.kwargs, {"proxy_url": "http://p.test:1", "timeout": 30.0})
        self.assertTrue(page.scripts[0].startswith("mw:"))
        self.assertIn('"TOKEN"', page.scripts[0])

    async def test_token_for_a_replaced_challenge_is_discarded(self):
        from unittest.mock import AsyncMock, patch
        from app.solver.browser import captcha as captcha_module
        page = _CloudflarePage("https://e.test/?__cf_chl_rt_tk=new", [])
        with patch.object(captcha_module.captcha_solver, "solve_cloudflare_challenge", AsyncMock(return_value="TOKEN")):
            self.assertFalse(await captcha_module.try_cloudflare_challenge_solver(page, "k", RENDER, None, 30.0))
        self.assertEqual(page.scripts, [])
