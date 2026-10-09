"""Opt-in `<meta http-equiv="refresh">` following, shared by Tier 1 and the browser tiers.

Only a short-delay refresh that names a destination counts as a redirect: a bare delay just
reloads the page, and a long one is a "you will be redirected" notice the caller can follow
itself. Every hop still goes through the SSRF check like any other redirect."""
import re
from html.parser import HTMLParser
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

MAX_REFRESH_DELAY_SECONDS = 10.0
MAX_REFRESH_HOPS = 3

_CONTENT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[;,](.*)$", re.S)
_URL_PREFIX_RE = re.compile(r"^url\s*=\s*", re.I)
# Markup inside these is never acted on by a browser.
_INERT_TAGS = frozenset({"template", "noscript"})


class _RefreshCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.inert_depth = 0
        self.base_href: Optional[str] = None
        self.contents: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _INERT_TAGS:
            self.inert_depth += 1
            return
        if self.inert_depth:
            return
        values = {name.lower(): (value or "") for name, value in attrs}
        if tag == "base" and self.base_href is None and "href" in values:
            self.base_href = values["href"]
        elif tag == "meta" and values.get("http-equiv", "").strip().lower() == "refresh":
            self.contents.append(values.get("content", ""))

    def handle_startendtag(self, tag, attrs):
        if tag not in _INERT_TAGS:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in _INERT_TAGS and self.inert_depth:
            self.inert_depth -= 1


def parse_refresh_content(content: str, base_url: str, page_url: str) -> Optional[Tuple[float, str]]:
    """(delay_seconds, absolute_url) for one meta refresh `content` value, or None."""
    match = _CONTENT_RE.match(content or "")
    if not match:
        return None
    delay = float(match.group(1))
    if delay > MAX_REFRESH_DELAY_SECONDS:
        return None
    target = _URL_PREFIX_RE.sub("", match.group(2).strip()).strip()
    if len(target) >= 2 and target[0] == target[-1] and target[0] in "'\"":
        target = target[1:-1].strip()
    if not target:
        return None
    try:
        absolute = urljoin(base_url, target)
        parsed = urlparse(absolute)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc or absolute == page_url:
        return None
    return delay, absolute


def meta_refresh_target(html: str, url: str) -> Optional[Tuple[float, str]]:
    """The first usable refresh redirect in `html` (served from `url`), honouring `<base href>`."""
    if not html or "refresh" not in html.lower():
        return None
    collector = _RefreshCollector()
    try:
        collector.feed(html)
        collector.close()
    except Exception:
        return None
    base_url = url
    if collector.base_href is not None:
        try:
            base_url = urljoin(url, collector.base_href)
        except ValueError:
            base_url = url
    for content in collector.contents:
        target = parse_refresh_content(content, base_url, url)
        if target:
            return target
    return None
