import unittest
from unittest.mock import AsyncMock

from app.models.flaresolverr import ScrapeRequest
from app.solver.browser.browser import extract_rendered_records


class TestRenderedExtraction(unittest.IsolatedAsyncioTestCase):
    async def test_browser_extraction_is_bounded_and_forces_browser(self):
        spec = {"container": ".result", "fields": {"title": ".title", "link": "a@href"}}
        page = type("Page", (), {"evaluate": AsyncMock(return_value=[{"title": "Example", "link": "/item"}])})()
        result = await extract_rendered_records(page, spec)
        self.assertEqual(result["records"][0]["title"], "Example")
        self.assertIn("slice(0, 50)", page.evaluate.await_args.args[0])
        self.assertTrue(ScrapeRequest(url="https://example.com", extract_records=spec).to_v1_request().forceBrowser)

    async def test_rejects_oversized_field_map(self):
        with self.assertRaises(ValueError):
            await extract_rendered_records(AsyncMock(), {"container": ".x", "fields": {str(n): ".x" for n in range(13)}})


if __name__ == "__main__":
    unittest.main()
