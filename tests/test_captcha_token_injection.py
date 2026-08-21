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


if __name__ == "__main__":
    unittest.main()
