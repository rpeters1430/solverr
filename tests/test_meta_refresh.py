import time
import unittest
from unittest.mock import AsyncMock, patch

from app.solver.meta_refresh import MAX_REFRESH_DELAY_SECONDS, meta_refresh_target


class TestMetaRefreshParsing(unittest.TestCase):
    URL = "https://example.com/dir/page"

    def test_relative_destination_and_delay(self):
        html = '<html><head><meta http-equiv="Refresh" content="2; url=next"></head></html>'
        self.assertEqual(meta_refresh_target(html, self.URL), (2.0, "https://example.com/dir/next"))

    def test_quoted_and_comma_forms(self):
        self.assertEqual(
            meta_refresh_target("<meta http-equiv=refresh content=\"0,URL='/x?a=1&amp;b=2'\">", self.URL),
            (0.0, "https://example.com/x?a=1&b=2"),
        )

    def test_base_href_is_honoured(self):
        html = '<base href="https://cdn.example.org/root/"><meta http-equiv="refresh" content="0;url=go">'
        self.assertEqual(meta_refresh_target(html, self.URL), (0.0, "https://cdn.example.org/root/go"))

    def test_reload_without_destination_is_not_a_redirect(self):
        self.assertIsNone(meta_refresh_target('<meta http-equiv="refresh" content="5">', self.URL))

    def test_long_delay_is_not_followed(self):
        html = f'<meta http-equiv="refresh" content="{int(MAX_REFRESH_DELAY_SECONDS) + 1};url=/later">'
        self.assertIsNone(meta_refresh_target(html, self.URL))

    def test_non_http_schemes_and_self_refresh_are_ignored(self):
        self.assertIsNone(meta_refresh_target('<meta http-equiv="refresh" content="0;url=javascript:alert(1)">', self.URL))
        self.assertIsNone(meta_refresh_target('<meta http-equiv="refresh" content="0;url=file:///etc/passwd">', self.URL))
        self.assertIsNone(meta_refresh_target(f'<meta http-equiv="refresh" content="0;url={self.URL}">', self.URL))

    def test_inert_markup_is_ignored(self):
        html = (
            '<noscript><meta http-equiv="refresh" content="0;url=/nojs"></noscript>'
            '<template><meta http-equiv="refresh" content="0;url=/tpl"></template>'
            '<script>document.write(\'<meta http-equiv="refresh" content="0;url=/js">\')</script>'
        )
        self.assertIsNone(meta_refresh_target(html, self.URL))

    def test_first_usable_refresh_wins(self):
        html = '<meta http-equiv="refresh" content="60;url=/slow"><meta http-equiv="refresh" content="1;url=/fast">'
        self.assertEqual(meta_refresh_target(html, self.URL), (1.0, "https://example.com/fast"))



class _RefreshPage:
    """Pages by URL; Firefox 'fires' a refresh only when auto_fire is set."""

    def __init__(self, pages, url, auto_fire):
        self.pages = pages
        self.url = url
        self.auto_fire = auto_fire
        self.gotos = []

    async def content(self):
        return self.pages[self.url]

    async def wait_for_url(self, predicate, wait_until=None, timeout=None):
        target = meta_refresh_target(self.pages[self.url], self.url)
        if self.auto_fire and target:
            self.url = target[1]
            return
        raise TimeoutError("refresh never fired")

    async def goto(self, url, wait_until=None, timeout=None):
        self.gotos.append(url)
        self.url = url


class TestBrowserMetaRefresh(unittest.IsolatedAsyncioTestCase):
    PAGES = {
        "https://example.com/a": '<meta http-equiv="refresh" content="0;url=/b">',
        "https://example.com/b": '<meta http-equiv="refresh" content="1;url=/c">',
        "https://example.com/c": "<html><body>final</body></html>",
    }

    async def test_waits_for_firefox_to_fire_the_refresh(self):
        from app.solver.browser.navigation import follow_meta_refresh
        page = _RefreshPage(self.PAGES, "https://example.com/a", auto_fire=True)
        with patch("app.solver.browser.navigation.check_target_url_async", new=AsyncMock()):
            await follow_meta_refresh(page, time.monotonic() + 10)
        self.assertEqual(page.url, "https://example.com/c")
        self.assertEqual(page.gotos, [])

    async def test_loads_a_refresh_that_never_fires(self):
        from app.solver.browser.navigation import follow_meta_refresh
        page = _RefreshPage(self.PAGES, "https://example.com/a", auto_fire=False)
        with patch("app.solver.browser.navigation.check_target_url_async", new=AsyncMock()):
            await follow_meta_refresh(page, time.monotonic() + 10)
        self.assertEqual(page.gotos, ["https://example.com/b", "https://example.com/c"])

    async def test_blocked_destination_is_not_followed(self):
        from app.security import SSRFBlockedError
        from app.solver.browser.navigation import follow_meta_refresh
        pages = {"https://example.com/a": '<meta http-equiv="refresh" content="0;url=http://127.0.0.1/admin">'}
        page = _RefreshPage(pages, "https://example.com/a", auto_fire=False)
        with patch("app.solver.browser.navigation.check_target_url_async", new=AsyncMock(side_effect=SSRFBlockedError("no"))):
            await follow_meta_refresh(page, time.monotonic() + 10)
        self.assertEqual(page.url, "https://example.com/a")
        self.assertEqual(page.gotos, [])


if __name__ == "__main__":
    unittest.main()
