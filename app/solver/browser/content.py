import codecs
import re
from typing import Optional

# Media types whose body is text even without a text/ prefix.
_TEXT_MARKERS = ("html", "xml", "json", "javascript", "ecmascript", "x-www-form-urlencoded")
_CHARSET_RE = re.compile(r"""(?:^|;)\s*charset\s*=\s*(?:"([^"]*)"|'([^']*)'|([^;\s]+))""", re.I)


def media_type(content_type: Optional[str]) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def is_text_content_type(content_type: Optional[str]) -> bool:
    mt = media_type(content_type)
    return mt.startswith("text/") or any(marker in mt for marker in _TEXT_MARKERS)


def is_html_content_type(content_type: Optional[str]) -> bool:
    return media_type(content_type) in ("text/html", "application/xhtml+xml")


def is_non_html_text(content_type: Optional[str]) -> bool:
    """JSON, XML, RSS, plain text...: what Firefox wraps in a viewer page instead of rendering as-is."""
    return bool(content_type) and is_text_content_type(content_type) and not is_html_content_type(content_type)


def decode_text_body(body: bytes, content_type: Optional[str]) -> str:
    """Decode with the response's declared charset, falling back to UTF-8 for an unknown one."""
    match = _CHARSET_RE.search(content_type or "")
    charset = next((g for g in match.groups() if g), "") if match else ""
    try:
        codecs.lookup(charset)
    except LookupError:
        charset = "utf-8"
    return body.decode(charset or "utf-8", errors="replace")


def decode_browser_body(body: bytes, content_type: Optional[str]) -> str:
    """Decode a Playwright response body. Chromium hands back documents already transcoded to
    UTF-8 while Firefox can hand back the bytes as served, so valid UTF-8 is taken as such and
    anything else is read with the declared charset. Legacy-charset text with non-ASCII bytes is
    almost never valid UTF-8, so the two cases don't collide in practice."""
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return decode_text_body(body, content_type)
