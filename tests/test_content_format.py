import unittest

from app.content_format import convert_content, html_to_markdown, html_to_text

PAGE = """<html><head><title>Ignored</title><script>var secret = 1;</script><style>p{}</style></head>
<body><form><h1>Hello</h1><p>Some <b>bold</b>, <em>em</em> and <a href="/x">a link</a>.<br>Line two</p>
<ul><li>one</li><li>two<ul><li>nested</li></ul></li></ul><ol><li>first</li><li>second</li></ol>
<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2|3</td></tr></table>
<pre>x = 1
y = 2</pre><blockquote><p>quoted</p></blockquote><img src="/i.png" alt="pic"></form></body></html>"""


class TestHtmlToMarkdown(unittest.TestCase):
    def setUp(self):
        self.md = html_to_markdown(PAGE)

    def test_structure(self):
        self.assertTrue(self.md.startswith("# Hello\n"))
        self.assertIn("Some **bold**, *em* and [a link](/x).", self.md)
        self.assertIn("- one\n- two\n  - nested", self.md)
        self.assertIn("1. first\n2. second", self.md)
        self.assertIn("| A | B |\n| --- | --- |\n| 1 | 2\\|3 |", self.md)
        self.assertIn("```\nx = 1\ny = 2\n```", self.md)
        self.assertIn("> quoted", self.md)
        self.assertIn("![pic](/i.png)", self.md)

    def test_drops_scripts_and_styles(self):
        self.assertNotIn("secret", self.md)
        self.assertNotIn("p{}", self.md)

    def test_title_is_used_when_the_page_has_no_h1(self):
        self.assertTrue(html_to_markdown("<title>Doc</title><p>body</p>").startswith("# Doc\n\nbody"))


class TestHtmlToText(unittest.TestCase):
    def test_inline_markup_stays_on_its_line(self):
        text = html_to_text(PAGE)
        self.assertIn("Some bold, em and a link.", text)
        self.assertIn("Line two", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("\n\n\n", text)


class TestConvertContent(unittest.TestCase):
    def test_non_html_passes_through(self):
        self.assertEqual(convert_content('{"a": 1}', "application/json", "markdown"), '{"a": 1}')

    def test_html_format_is_unchanged(self):
        self.assertEqual(convert_content(PAGE, "text/html", "html"), PAGE)

    def test_missing_content_type_is_treated_as_html(self):
        self.assertTrue(convert_content("<h1>T</h1>", "", "markdown").startswith("# T"))


if __name__ == "__main__":
    unittest.main()
