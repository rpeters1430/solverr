"""HTML to Markdown or plain text, for /scrape's and MCP's `format` option.

Much cheaper for an LLM to read than raw HTML. Deliberately small and dependency-free beyond
BeautifulSoup: headings, paragraphs, links, images, lists, emphasis, code, quotes and tables.
Scripts, styles and other non-content elements are dropped."""
import re
from typing import List

from bs4 import BeautifulSoup, NavigableString, Tag

DROP_TAGS = ("title", "script", "style", "noscript", "template", "svg", "canvas", "iframe", "head", "button", "select", "input", "textarea")
BLOCK_TAGS = {"p", "div", "section", "article", "main", "header", "footer", "nav", "aside", "figure", "figcaption", "details", "summary", "address"}


def _clean(soup: BeautifulSoup) -> Tag:
    for el in soup.find_all(DROP_TAGS):
        el.decompose()
    return soup.body or soup


def _inline(text: str) -> str:
    return re.sub(r"\s+", " ", text)


class _MarkdownWriter:
    def __init__(self):
        self.list_depth = 0

    def convert(self, node) -> str:
        if isinstance(node, NavigableString):
            if node.__class__.__name__ in ("Comment", "Doctype", "Declaration", "ProcessingInstruction", "CData"):
                return ""
            return _inline(str(node))
        if not isinstance(node, Tag):
            return ""
        name = node.name
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = self.children(node).strip()
            return f"\n\n{'#' * int(name[1])} {text}\n\n" if text else ""
        if name == "br":
            return "  \n"
        if name == "hr":
            return "\n\n---\n\n"
        if name in ("strong", "b"):
            text = self.children(node).strip()
            return f"**{text}**" if text else ""
        if name in ("em", "i"):
            text = self.children(node).strip()
            return f"*{text}*" if text else ""
        if name == "code" and node.parent is not None and node.parent.name != "pre":
            text = node.get_text()
            return f"`{text}`" if text else ""
        if name == "pre":
            return f"\n\n```\n{node.get_text().rstrip()}\n```\n\n"
        if name == "a":
            text = self.children(node).strip()
            href = node.get("href")
            if href and text and not href.startswith(("javascript:", "#")):
                return f"[{text}]({href})"
            return text
        if name == "img":
            src = node.get("src")
            return f"![{_inline(node.get('alt') or '').strip()}]({src})" if src else ""
        if name in ("ul", "ol"):
            return self.list(node, ordered=name == "ol")
        if name == "blockquote":
            inner = self.children(node).strip()
            quoted = "\n".join(f"> {line}" if line else ">" for line in inner.splitlines())
            return f"\n\n{quoted}\n\n"
        if name == "table":
            return self.table(node)
        if name == "p" or name in BLOCK_TAGS:
            text = self.children(node).strip()
            return f"\n\n{text}\n\n" if text else ""
        return self.children(node)

    def children(self, node: Tag) -> str:
        return "".join(self.convert(child) for child in node.children)

    def list(self, node: Tag, ordered: bool) -> str:
        # Nested lists come back unindented; the enclosing item indents its continuation lines.
        lines: List[str] = []
        self.list_depth += 1
        try:
            for index, item in enumerate(node.find_all("li", recursive=False), start=1):
                marker = f"{index}." if ordered else "-"
                body = re.sub(r"\n{3,}", "\n\n", self.children(item).strip())
                body = body.replace("\n", "\n" + " " * (len(marker) + 1))
                lines.append(f"{marker} {body}")
        finally:
            self.list_depth -= 1
        if not lines:
            return ""
        edge = "\n" if self.list_depth else "\n\n"
        return edge + "\n".join(lines) + edge

    def table(self, node: Tag) -> str:
        rows = []
        for tr in node.find_all("tr"):
            cells = [self.children(cell).strip().replace("|", "\\|").replace("\n", " ") for cell in tr.find_all(["th", "td"], recursive=False)]
            if cells:
                rows.append(cells)
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        out = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
        out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n\n" + "\n".join(out) + "\n\n"


def html_to_markdown(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    body = _MarkdownWriter().convert(_clean(soup))
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if title and not body.startswith("# "):
        body = f"# {title}\n\n{body}" if body else f"# {title}"
    return body + "\n" if body else ""


TEXT_BREAK_TAGS = BLOCK_TAGS | {"h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "br", "pre", "blockquote", "table", "ul", "ol", "dt", "dd", "hr"}


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    root = _clean(soup)
    # Line breaks only at block boundaries, so inline markup (<b>, <a>) stays within its line.
    for el in root.find_all(TEXT_BREAK_TAGS):
        el.insert_before("\n")
        el.insert_after("\n")
    for cell in root.find_all(["td", "th"]):
        cell.insert_after(" ")
    lines = [_inline(line).strip() for line in root.get_text().splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text + "\n" if text else ""


def convert_content(content: str, content_type: str, fmt: str) -> str:
    """Convert an HTML body to `fmt` ("html", "markdown" or "text"); non-HTML bodies pass through."""
    if fmt == "html" or not content:
        return content
    ctype = (content_type or "").lower()
    if ctype and "html" not in ctype:
        return content
    return html_to_markdown(content) if fmt == "markdown" else html_to_text(content)
