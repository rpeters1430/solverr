import time
import unittest
from unittest.mock import patch

from app.solver.browser import BrowserPool
from app.solver.browser import browser as browser_module
from app.solver.browser.content import decode_browser_body, decode_text_body, is_html_content_type, is_non_html_text, is_text_content_type


class TestContentTypeHelpers(unittest.TestCase):
    def test_text_and_html_classification(self):
        self.assertTrue(is_text_content_type("application/json; charset=utf-8"))
        self.assertTrue(is_text_content_type("application/rss+xml"))
        self.assertTrue(is_text_content_type("text/plain"))
        self.assertFalse(is_text_content_type("image/png"))
        self.assertTrue(is_html_content_type("text/html; charset=UTF-8"))
        self.assertTrue(is_html_content_type("application/xhtml+xml"))
        self.assertTrue(is_non_html_text("application/xml"))
        self.assertFalse(is_non_html_text("text/html"))
        self.assertFalse(is_non_html_text("application/octet-stream"))
        self.assertFalse(is_non_html_text(""))

    def test_decode_honours_declared_charset(self):
        self.assertEqual(decode_text_body("café".encode("latin-1"), "text/plain; charset=ISO-8859-1"), "café")
        self.assertEqual(decode_text_body("café".encode("latin-1"), 'text/plain; charset="iso-8859-1"'), "café")
        self.assertEqual(decode_text_body("café".encode(), "application/json"), "café")

    def test_browser_body_handles_raw_and_transcoded_bytes(self):
        ct = "application/rss+xml; charset=iso-8859-1"
        self.assertEqual(decode_browser_body("Café".encode("latin-1"), ct), "Café")  # as served (Firefox)
        self.assertEqual(decode_browser_body("Café".encode("utf-8"), ct), "Café")  # transcoded (Chromium)

    def test_unknown_charset_falls_back_to_utf8(self):
        self.assertEqual(decode_text_body("ok ✓".encode(), "text/plain; charset=made-up"), "ok ✓")


class _Req:
    resource_type = "document"

    def __init__(self, frame):
        self.frame = frame


class _Resp:
    def __init__(self, frame, status, headers, body):
        self.request = _Req(frame)
        self.status = status
        self.headers = headers
        self._body = body

    async def body(self):
        return self._body


class _ViewerPage:
    """Firefox showing a JSON document: the DOM is its viewer, the response is the real file."""
    url = "https://api.example.com/feed"
    main_frame = object()
    frames = []
    html = "<html><head></head><body><div id='json'>viewer markup</div></body></html>"

    def __init__(self):
        self.handlers = []

    def on(self, event, handler):
        self.handlers.append(handler)

    async def title(self):
        return ""

    async def content(self):
        return self.html

    async def evaluate(self, script, *args):
        # Body-size probes fail, as they would for a short document.
        return False


class _NoCookies:
    async def cookies(self, *args, **kwargs):
        return []


class TestRawNonHtmlBodies(unittest.IsolatedAsyncioTestCase):
    async def _run(self, page, response):
        async def nav(*args, **kwargs):
            for handler in page.handlers:
                handler(response)
            return response, response.status

        start = time.monotonic()
        with patch.object(browser_module, "navigate_to_target", side_effect=nav), \
             patch.object(browser_module, "install_media_blocking", return_value=None), \
             patch.object(browser_module, "dispatch_challenge_click", return_value=(False, False)), \
             patch.multiple(browser_module, SOLVE_FINALIZE_RESERVE_SECONDS=0.2):
            sol = await BrowserPool()._execute_solve_flow(
                context=_NoCookies(), page=page, url=page.url, method="GET", post_data=None, cookies=None,
                timeout_ms=8000, active_ua="ua", headers=None, start_time=start, deadline=start + 8.0,
            )
        return sol, time.monotonic() - start

    async def test_json_document_is_returned_raw_and_promptly(self):
        page = _ViewerPage()
        resp = _Resp(page.main_frame, 200, {"content-type": "application/json; charset=utf-8"}, b'{"ok": true}')
        sol, elapsed = await self._run(page, resp)
        self.assertEqual(sol.response, '{"ok": true}')
        self.assertEqual(sol.headers["content-type"], "application/json; charset=utf-8")
        # A tiny answer must not wait out the budget for a "rendered" body.
        self.assertLess(elapsed, 3.0)

    async def test_xml_in_a_legacy_charset_is_decoded(self):
        page = _ViewerPage()
        body = '<?xml version="1.0"?><rss><title>Café</title></rss>'.encode("latin-1")
        resp = _Resp(page.main_frame, 200, {"Content-Type": "application/rss+xml; charset=iso-8859-1"}, body)
        sol, _ = await self._run(page, resp)
        self.assertIn("<title>Café</title>", sol.response)

    async def test_html_keeps_the_rendered_dom(self):
        page = _ViewerPage()
        page.html = "<html><head><title>x</title></head><body>" + "<p>row</p>" * 100 + "</body></html>"
        resp = _Resp(page.main_frame, 200, {"content-type": "text/html; charset=utf-8"}, b"<html>raw</html>")
        with patch.object(_ViewerPage, "evaluate", return_value=True):
            sol, _ = await self._run(page, resp)
        self.assertEqual(sol.response, page.html)
        self.assertEqual(sol.headers["content-type"], "text/html")



class TestSessionStorageInBrowser(unittest.IsolatedAsyncioTestCase):
    def test_context_options_restore_local_storage(self):
        from app.solver.browser.browser import _context_options
        self.assertEqual(_context_options(None), {"service_workers": "block"})
        opts = _context_options({"local": {"https://a.test": {"k": "v"}}, "session": {}})
        self.assertEqual(opts["storage_state"]["origins"], [
            {"origin": "https://a.test", "localStorage": [{"name": "k", "value": "v"}]}
        ])

    async def test_capture_reads_local_and_session_storage(self):
        from app.solver.browser.browser import _capture_storage

        class Ctx:
            async def storage_state(self):
                return {"cookies": [], "origins": [
                    {"origin": "https://a.test", "localStorage": [{"name": "k", "value": "v"}]},
                    {"origin": "https://empty.test", "localStorage": []},
                ]}

        class Page:
            async def evaluate(self, script):
                return {"origin": "https://a.test", "items": {"s": "1"}}

        self.assertEqual(await _capture_storage(Ctx(), Page()), {
            "local": {"https://a.test": {"k": "v"}},
            "session": {"https://a.test": {"s": "1"}},
        })

    async def test_capture_ignores_opaque_origins_and_failures(self):
        from app.solver.browser.browser import _capture_storage

        class Ctx:
            async def storage_state(self):
                raise RuntimeError("closed")

        class Page:
            async def evaluate(self, script):
                return {"origin": "null", "items": {"s": "1"}}

        self.assertIsNone(await _capture_storage(Ctx(), Page()))

    async def test_saved_session_storage_is_installed_as_init_script(self):
        scripts = []

        class Ctx(_NoCookies):
            async def add_init_script(self, script=None, path=None):
                scripts.append(script)

            async def storage_state(self):
                return {"origins": [{"origin": "https://api.example.com", "localStorage": [{"name": "a", "value": "b"}]}]}

        page = _ViewerPage()
        resp = _Resp(page.main_frame, 200, {"content-type": "application/json"}, b"{}")

        async def nav(*args, **kwargs):
            for handler in page.handlers:
                handler(resp)
            return resp, 200

        start = time.monotonic()
        with patch.object(browser_module, "navigate_to_target", side_effect=nav), \
             patch.object(browser_module, "install_media_blocking", return_value=None), \
             patch.multiple(browser_module, SOLVE_FINALIZE_RESERVE_SECONDS=0.2):
            sol = await BrowserPool()._execute_solve_flow(
                context=Ctx(), page=page, url=page.url, method="GET", post_data=None, cookies=None,
                timeout_ms=5000, active_ua="ua", headers=None, start_time=start, deadline=start + 5.0,
                browser_storage={"session": {"https://api.example.com": {"tok": "x\"y"}}}, capture_storage=True,
            )
        self.assertEqual(len(scripts), 1)
        self.assertIn('"tok": "x\\"y"', scripts[0])
        self.assertEqual(sol.storage["local"], {"https://api.example.com": {"a": "b"}})


if __name__ == "__main__":
    unittest.main()
