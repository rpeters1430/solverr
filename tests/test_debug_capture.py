import unittest
from types import SimpleNamespace

from app.solver.browser import debug_capture as dc


class _FakePage:
    def __init__(self):
        self.handlers = {}
        self.main_frame = object()

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event, arg):
        for handler in self.handlers.get(event, []):
            handler(arg)


def _response(page, url, status, resource_type="document", main=True, location=None):
    request = SimpleNamespace(method="GET", resource_type=resource_type, frame=page.main_frame if main else object())
    return SimpleNamespace(url=url, status=status, request=request, headers={"location": location} if location else {})


class TestDebugCapture(unittest.TestCase):
    def test_records_requested_kinds_only(self):
        page = _FakePage()
        capture = dc.DebugCapture(("console", "redirects"))
        capture.attach(page)
        page.emit("console", SimpleNamespace(type="error", text="boom"))
        page.emit("pageerror", ValueError("uncaught"))
        page.emit("response", _response(page, "https://a.test/", 302, location="/next"))
        page.emit("response", _response(page, "https://a.test/next", 200))
        page.emit("response", _response(page, "https://a.test/app.js", 200, resource_type="script"))
        page.emit("response", _response(page, "https://ads.test/", 200, main=False))

        out = capture.to_dict()
        self.assertEqual(set(out), {"console", "redirects"})
        self.assertEqual(out["console"], [{"type": "error", "text": "boom"}, {"type": "pageerror", "text": "uncaught"}])
        self.assertEqual(out["redirects"], [
            {"url": "https://a.test/", "status": 302, "location": "/next"},
            {"url": "https://a.test/next", "status": 200, "location": None},
        ])

    def test_network_includes_failed_requests(self):
        page = _FakePage()
        capture = dc.DebugCapture(("network",))
        capture.attach(page)
        page.emit("response", _response(page, "https://a.test/app.js", 200, resource_type="script"))
        page.emit("requestfailed", SimpleNamespace(url="https://a.test/x.png", method="GET", resource_type="image", failure="NS_ERROR_ABORT"))
        network = capture.to_dict()["network"]
        self.assertEqual(network[0]["status"], 200)
        self.assertEqual(network[1], {"url": "https://a.test/x.png", "method": "GET", "resource_type": "image", "status": None, "failure": "NS_ERROR_ABORT"})

    def test_bounded_with_a_truncation_count(self):
        page = _FakePage()
        capture = dc.DebugCapture(("console",))
        capture.attach(page)
        for i in range(dc.MAX_CONSOLE_ENTRIES + 5):
            page.emit("console", SimpleNamespace(type="log", text="x" * (dc.MAX_TEXT_CHARS + 10)))
        out = capture.to_dict()
        self.assertEqual(len(out["console"]), dc.MAX_CONSOLE_ENTRIES)
        self.assertEqual(len(out["console"][0]["text"]), dc.MAX_TEXT_CHARS)
        self.assertEqual(out["truncated"], {"console": 5})


if __name__ == "__main__":
    unittest.main()
