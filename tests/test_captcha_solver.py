import unittest
from contextlib import contextmanager
from unittest.mock import patch
import httpx
from app.solver.captcha_solver import CaptchaSolverClient


@contextmanager
def mocked_http(handler):
    """Patch httpx.AsyncClient so every instantiation routes through a
    MockTransport - no real network calls, fully offline test."""
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_async_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    with patch("httpx.AsyncClient", side_effect=factory):
        yield


def make_client() -> CaptchaSolverClient:
    client = CaptchaSolverClient()
    client.api_key = "test-api-key"
    client.base_url = "https://fake-solver.test"
    client.poll_interval = 0  # no real waiting in tests
    client.timeout = 5
    return client


class TestCaptchaSolverClient(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_without_api_key_returns_none_immediately(self):
        client = CaptchaSolverClient()
        client.api_key = None
        result = await client.solve_recaptcha_v2("sitekey123", "https://example.com")
        self.assertIsNone(result)

    async def test_successful_submit_and_poll_returns_token(self):
        calls = {"in": 0, "res": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/in.php":
                calls["in"] += 1
                return httpx.Response(200, json={"status": 1, "request": "task-42"})
            calls["res"] += 1
            return httpx.Response(200, json={"status": 1, "request": "SOLVED_TOKEN"})

        client = make_client()
        with mocked_http(handler):
            token = await client.solve_recaptcha_v2("sitekey123", "https://example.com")
        self.assertEqual(token, "SOLVED_TOKEN")
        self.assertEqual(calls["in"], 1)
        self.assertGreaterEqual(calls["res"], 1)

    async def test_submit_rejected_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": 0, "request": "ERROR_WRONG_USER_KEY"})

        client = make_client()
        with mocked_http(handler):
            token = await client.solve_hcaptcha("sitekey123", "https://example.com")
        self.assertIsNone(token)

    async def test_poll_failure_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/in.php":
                return httpx.Response(200, json={"status": 1, "request": "task-99"})
            return httpx.Response(200, json={"status": 0, "request": "ERROR_CAPTCHA_UNSOLVABLE"})

        client = make_client()
        with mocked_http(handler):
            token = await client.solve_turnstile("sitekey123", "https://example.com")
        self.assertIsNone(token)

    async def test_not_ready_then_solved(self):
        state = {"polls": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/in.php":
                return httpx.Response(200, json={"status": 1, "request": "task-7"})
            state["polls"] += 1
            if state["polls"] < 3:
                return httpx.Response(200, json={"status": 0, "request": "CAPCHA_NOT_READY"})
            return httpx.Response(200, json={"status": 1, "request": "FINAL_TOKEN"})

        client = make_client()
        with mocked_http(handler):
            token = await client.solve_recaptcha_v2("sitekey123", "https://example.com")
        self.assertEqual(token, "FINAL_TOKEN")
        self.assertEqual(state["polls"], 3)

    async def test_enabled_property_reflects_api_key(self):
        client = CaptchaSolverClient()
        client.api_key = None
        self.assertFalse(client.enabled)
        client.api_key = "abc"
        self.assertTrue(client.enabled)


class TestCaptchaSolverTaskOptions(unittest.IsolatedAsyncioTestCase):
    async def _submitted(self, call):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/in.php":
                from urllib.parse import parse_qs
                seen.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
                return httpx.Response(200, json={"status": 1, "request": "t"})
            return httpx.Response(200, json={"status": 1, "request": "TOKEN"})

        client = make_client()
        with mocked_http(handler):
            self.assertEqual(await call(client), "TOKEN")
        return seen

    async def test_recaptcha_enterprise_invisible_and_data_s(self):
        seen = await self._submitted(lambda c: c.solve_recaptcha_v2(
            "k", "https://e.test", enterprise=True, invisible=True, data_s="S"))
        self.assertEqual((seen["enterprise"], seen["invisible"], seen["data-s"]), ("1", "1", "S"))

    async def test_plain_recaptcha_sends_no_extra_fields(self):
        seen = await self._submitted(lambda c: c.solve_recaptcha_v2("k", "https://e.test"))
        self.assertNotIn("enterprise", seen)
        self.assertNotIn("data-s", seen)

    async def test_turnstile_action_and_cdata(self):
        seen = await self._submitted(lambda c: c.solve_turnstile("k", "https://e.test", action="login", cdata="xyz"))
        self.assertEqual((seen["action"], seen["data"]), ("login", "xyz"))


class TestDataDomeSliderTask(unittest.IsolatedAsyncioTestCase):
    async def test_creates_v2_task_through_the_proxy_and_returns_cookie(self):
        import json
        bodies = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            bodies.append((request.url.path, body))
            if request.url.path == "/createTask":
                return httpx.Response(200, json={"errorId": 0, "taskId": 7})
            return httpx.Response(200, json={"errorId": 0, "status": "ready",
                                             "solution": {"cookie": "datadome=abc; Domain=.e.test; Path=/; Secure"}})

        client = make_client()
        client.api_v2_url = "https://fake-v2.test"
        with mocked_http(handler):
            cookie = await client.solve_datadome_slider(
                "https://geo.captcha-delivery.com/captcha/?initialCid=x", "https://e.test/", "UA",
                "http://user:p%40ss@proxy.test:3128")
        self.assertEqual(cookie, "datadome=abc; Domain=.e.test; Path=/; Secure")
        task = bodies[0][1]["task"]
        self.assertEqual(task["type"], "DataDomeSliderTask")
        self.assertEqual((task["proxyType"], task["proxyAddress"], task["proxyPort"]), ("http", "proxy.test", 3128))
        self.assertEqual((task["proxyLogin"], task["proxyPassword"]), ("user", "p@ss"))

    async def test_no_proxy_means_no_task(self):
        client = make_client()

        def handler(request):
            raise AssertionError("must not call the service")

        with mocked_http(handler):
            self.assertIsNone(await client.solve_datadome_slider("u", "https://e.test/", "UA", ""))

    async def test_task_error_returns_none(self):
        def handler(request):
            return httpx.Response(200, json={"errorId": 1, "errorCode": "ERROR_PROXY_CONNECTION_FAILED"})

        client = make_client()
        with mocked_http(handler):
            self.assertIsNone(await client.solve_datadome_slider("u", "https://e.test/", "UA", "http://p.test:8080"))

    def test_proxy_task_fields(self):
        from app.solver.captcha_solver import proxy_task_fields
        self.assertEqual(proxy_task_fields("socks5://h.test:1080"),
                         {"proxyType": "socks5", "proxyAddress": "h.test", "proxyPort": 1080})
        self.assertIsNone(proxy_task_fields("http://h.test"))  # no port
        self.assertIsNone(proxy_task_fields("ftp://h.test:21"))
        self.assertIsNone(proxy_task_fields(None))


if __name__ == "__main__":
    unittest.main()
