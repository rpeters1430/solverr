"""Opt-in debug capture for a browser solve: console messages, network requests, and the
main-frame redirect chain (the request's `capture` field). Bounded so a chatty page can't
grow the response without limit; `truncated` counts what was dropped."""
import logging
from typing import Any, Dict, Iterable, List

from app.models.flaresolverr import CAPTURE_KINDS

logger = logging.getLogger("solverr.browser")

MAX_CONSOLE_ENTRIES = 200
MAX_NETWORK_ENTRIES = 500
MAX_REDIRECT_ENTRIES = 50
MAX_TEXT_CHARS = 1000


class DebugCapture:
    def __init__(self, kinds: Iterable[str]):
        self.kinds = frozenset(k for k in kinds if k in CAPTURE_KINDS)
        self.console: List[Dict[str, Any]] = []
        self.network: List[Dict[str, Any]] = []
        self.redirects: List[Dict[str, Any]] = []
        self.truncated: Dict[str, int] = {}

    def _append(self, kind: str, items: List[Dict[str, Any]], limit: int, entry: Dict[str, Any]) -> None:
        if len(items) < limit:
            items.append(entry)
        else:
            self.truncated[kind] = self.truncated.get(kind, 0) + 1

    def attach(self, page: Any) -> None:
        if "console" in self.kinds:
            page.on("console", self._on_console)
            page.on("pageerror", self._on_page_error)
        if "network" in self.kinds:
            page.on("requestfailed", self._on_request_failed)
        if "network" in self.kinds or "redirects" in self.kinds:
            page.on("response", lambda resp: self._on_response(page, resp))

    def _on_console(self, msg: Any) -> None:
        try:
            self._append("console", self.console, MAX_CONSOLE_ENTRIES, {
                "type": msg.type,
                "text": (msg.text or "")[:MAX_TEXT_CHARS],
            })
        except Exception as e:
            logger.debug(f"[DebugCapture] Console notice: {e}")

    def _on_page_error(self, error: Any) -> None:
        self._append("console", self.console, MAX_CONSOLE_ENTRIES, {
            "type": "pageerror",
            "text": str(error)[:MAX_TEXT_CHARS],
        })

    def _on_response(self, page: Any, resp: Any) -> None:
        try:
            req = resp.request
            if "network" in self.kinds:
                self._append("network", self.network, MAX_NETWORK_ENTRIES, {
                    "url": resp.url,
                    "method": req.method,
                    "resource_type": req.resource_type,
                    "status": resp.status,
                })
            if "redirects" in self.kinds and req.resource_type == "document" and req.frame == page.main_frame:
                self._append("redirects", self.redirects, MAX_REDIRECT_ENTRIES, {
                    "url": resp.url,
                    "status": resp.status,
                    "location": resp.headers.get("location"),
                })
        except Exception as e:
            logger.debug(f"[DebugCapture] Response notice: {e}")

    def _on_request_failed(self, req: Any) -> None:
        try:
            self._append("network", self.network, MAX_NETWORK_ENTRIES, {
                "url": req.url,
                "method": req.method,
                "resource_type": req.resource_type,
                "status": None,
                "failure": req.failure,
            })
        except Exception as e:
            logger.debug(f"[DebugCapture] Request-failed notice: {e}")

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {kind: getattr(self, kind) for kind in CAPTURE_KINDS if kind in self.kinds}
        if self.truncated:
            out["truncated"] = dict(self.truncated)
        return out
