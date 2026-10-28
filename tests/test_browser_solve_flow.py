"""Characterization coverage for the browser solve flow using browser doubles."""

from contextlib import ExitStack
import importlib
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.solver.browser.challenges import ChallengeNotSolvedError


browser_module = importlib.import_module("app.solver.browser.browser")


class FakeContext:
    async def cookies(self):
        return []

    async def storage_state(self):
        return {"origins": []}


class FakePage:
    def __init__(self, title="Example", content="<html><body>" + "x" * 350 + "</body></html>"):
        self.url = "https://example.test/"
        self._title = title
        self._content = content
        self.main_frame = object()
        self.handlers = {}
        self.evaluate = AsyncMock(return_value=True)

    def on(self, event, handler):
        self.handlers[event] = handler

    async def title(self):
        return self._title

    async def content(self):
        return self._content


class TestBrowserSolveFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.page = FakePage()
        self.context = FakeContext()
        self.pool = object.__new__(browser_module.BrowserPool)

    async def _solve(self, *, timeout=0.1):
        response = MagicMock(status=200)
        response.body = AsyncMock(return_value=b"<html>" + b"x" * 350 + b"</html>")
        return await self.pool._execute_solve_flow(
            context=self.context,
            page=self.page,
            url="https://example.test/",
            method="GET",
            post_data=None,
            cookies=None,
            timeout_ms=int(timeout * 1000),
            active_ua="FakeBrowser/1.0",
            headers=None,
            start_time=time.monotonic(),
            deadline=time.monotonic() + timeout,
        )

    def _common_patches(self):
        return (
            patch.object(browser_module, "build_playwright_cookies", return_value=[]),
            patch.object(browser_module, "install_media_blocking", new=AsyncMock()),
            patch.object(browser_module, "install_cloudflare_capture", new=AsyncMock(return_value=None)),
            patch.object(browser_module, "read_context_cookies", new=AsyncMock(return_value=[])),
            patch.object(browser_module, "navigate_to_target", new=AsyncMock(return_value=(MagicMock(status=200), 200))),
            patch.object(browser_module, "SOLVE_FINALIZE_RESERVE_SECONDS", 0.02),
            patch.object(browser_module.asyncio, "sleep", new=AsyncMock()),
        )

    def _patch_common(self, stack):
        for patcher in self._common_patches():
            stack.enter_context(patcher)

    async def test_clean_page_returns_rendered_solution(self):
        with ExitStack() as stack:
            self._patch_common(stack)
            stack.enter_context(patch.object(browser_module, "detect_challenge", return_value=None))
            stack.enter_context(patch.object(browser_module, "is_browser_error", return_value=False))
            stack.enter_context(patch.object(browser_module, "is_challenge_title", return_value=False))
            solution = await self._solve()

        self.assertEqual(solution.status, 200)
        self.assertEqual(solution.url, self.page.url)
        self.assertIn("x" * 100, solution.response)
        self.assertEqual(solution.challengeType, None)

    async def test_waf_challenge_that_clears_returns_solution(self):
        detect = MagicMock(side_effect=["cloudflare"] * 4 + [None] * 20)
        with ExitStack() as stack:
            self._patch_common(stack)
            stack.enter_context(patch.object(browser_module, "detect_challenge", detect))
            stack.enter_context(patch.object(
                browser_module, "is_challenge_wall",
                side_effect=lambda challenge, *a, **k: challenge == "cloudflare",
            ))
            stack.enter_context(patch.object(browser_module, "is_browser_error", return_value=False))
            stack.enter_context(patch.object(browser_module, "is_challenge_title", return_value=False))
            stack.enter_context(patch.object(
                browser_module, "dispatch_challenge_click", new=AsyncMock(return_value=(False, False)),
            ))
            solution = await self._solve()

        self.assertEqual(solution.status, 200)
        self.assertEqual(solution.challengeType, "cloudflare")

    async def test_waf_still_present_at_timeout_raises(self):
        self.page.evaluate = AsyncMock(return_value=False)
        with ExitStack() as stack:
            self._patch_common(stack)
            stack.enter_context(patch.object(browser_module, "detect_challenge", return_value="cloudflare"))
            stack.enter_context(patch.object(browser_module, "is_challenge_wall", return_value=True))
            stack.enter_context(patch.object(browser_module, "is_browser_error", return_value=False))
            stack.enter_context(patch.object(browser_module, "is_challenge_title", return_value=False))
            stack.enter_context(patch.object(
                browser_module, "dispatch_challenge_click", new=AsyncMock(return_value=(False, False)),
            ))
            with self.assertRaises(ChallengeNotSolvedError):
                await self._solve()

    async def test_browser_error_page_returns_502_solution(self):
        self.page._title = "This site can’t be reached"
        self.page.url = "about:neterror?e=connectionFailure"
        with ExitStack() as stack:
            self._patch_common(stack)
            stack.enter_context(patch.object(browser_module, "detect_challenge", return_value=None))
            stack.enter_context(patch.object(
                browser_module, "is_browser_error",
                side_effect=lambda title, url: url.startswith("about:neterror"),
            ))
            stack.enter_context(patch.object(browser_module, "is_challenge_title", return_value=False))
            solution = await self._solve()

        self.assertEqual(solution.status, 502)
        self.assertEqual(solution.url, self.page.url)


if __name__ == "__main__":
    unittest.main()
