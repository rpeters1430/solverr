import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.solver.browser import interactions


def _page(widget):
    page = MagicMock()
    page.frames = []
    page.evaluate = AsyncMock(return_value=widget)
    return page


class TestWidgetClickDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_one_evaluate_finds_the_widget_and_clicks_its_checkbox(self):
        page = _page({"selector": "iframe[src*='hcaptcha.com']", "kind": "hCaptcha", "x": 100, "y": 50, "width": 300, "height": 80})
        with patch.object(interactions, "human_click", new=AsyncMock()) as click:
            clicked, _ = await interactions.dispatch_challenge_click(page, "hcaptcha", "", age_gate_clicked=True)
        self.assertTrue(clicked)
        page.evaluate.assert_awaited_once()
        self.assertIs(page.evaluate.await_args.args[1], interactions.WIDGET_LOCATORS)
        click.assert_awaited_once_with(page, 128.0, 90.0)

    async def test_narrow_turnstile_is_clicked_at_its_centre(self):
        page = _page({"selector": ".cf-turnstile", "kind": "Turnstile", "x": 10, "y": 10, "width": 40, "height": 40})
        with patch.object(interactions, "human_click", new=AsyncMock()) as click:
            clicked, _ = await interactions.dispatch_challenge_click(page, "cloudflare_turnstile", "", age_gate_clicked=True)
        self.assertTrue(clicked)
        click.assert_awaited_once_with(page, 30.0, 30.0)

    def test_locator_order_keeps_turnstile_before_recaptcha_and_hcaptcha(self):
        kinds = [kind for _sel, kind in interactions.WIDGET_LOCATORS]
        self.assertEqual(kinds.index("reCAPTCHA"), kinds.count("Turnstile"))
        self.assertEqual(kinds[-1], "hCaptcha")


if __name__ == "__main__":
    unittest.main()
