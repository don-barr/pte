"""#30 Docs: the project's Markdown docs (README, docs/modifiers.md) rendered inside the app. requirements.md is a
local working file, not published, so it is not served."""

import tempfile
import re
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from ui.docs_pages import make_router

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = REPO_ROOT / "ui" / "templates"

# A small doc in the style of docs/modifiers.md, with a table, a fenced code block, links to the other docs,
# raw HTML that must not run and a comment that must stay hidden.
SAMPLE_MODIFIERS = """# Billing Code Modifiers

A modifier is a two-character code after a billing code. More in the [README](../README.md#quick-start)
and the [requirements](../requirements.md); CMS lists them in the
[HCPCS update](https://www.cms.gov/medicare/coding/hcpcsreleasecodesets/hcpcs-quarterly-update.html).

<script>alert("modifiers")</script>

<!-- TODO add the 2023 modifier list -->

## Categories

| Modifier | Meaning |
|---|---|
| 26 | Professional component |
| TC | Technical component |

```sql
SELECT modifiers, COUNT(*) FROM rates GROUP BY modifiers;
```

A chart: ![Search for 99213](screenshot-search.png)

Inline <b onclick="alert(1)">bold</b> and a [bad link](javascript:alert(1)).
"""

SAMPLE_README = """# Price Transparency Explorer

See [modifiers](docs/modifiers.md#categories) and [requirements](requirements.md).

![Search for 99213](docs/screenshot-search.png)

## Quick start

Run it.
"""

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c63000100000500010d0a2db40000000049454e44ae426082")


def make_client(root: Path | None = None) -> TestClient:
    # FastAPI's own Swagger UI lives at /docs by default; the app moves it to /api/docs so these routes win.
    app = FastAPI(docs_url="/api/docs", redoc_url=None)
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    templates.env.globals["static_version"] = lambda name="tables.js": 0
    app.include_router(make_router(templates, root=root) if root else make_router(templates))
    return TestClient(app)


class RealDocsTest(unittest.TestCase):
    """The repo's own docs, read from the root resolved from api/docs_pages.py."""

    def setUp(self):
        self.client = make_client()

    def test_docs_root_renders_the_readme(self):
        resp = self.client.get("/docs")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Price Transparency Explorer", resp.text)
        self.assertIn('id="getting-started"', resp.text)

    def test_the_top_of_every_doc_offers_both_documents_and_says_which_is_open(self):
        for path, current in (("/docs", "README"), ("/docs/modifiers", "Billing Code Modifiers")):
            with self.subTest(path=path):
                text = self.client.get(path).text
                switcher = re.search(r'<nav class="doc-tabs" aria-label="Documents">(.*?)</nav>', text, re.S)
                self.assertIsNotNone(switcher, "a document switcher at the top")
                cards = re.findall(r'<a href="(/docs/[a-z]+)"( aria-current="page")?>(.*?)</a>', switcher.group(1), re.S)
                self.assertEqual([c[0] for c in cards], ["/docs/readme", "/docs/modifiers"])
                self.assertIn("what each one means", cards[1][2])  # each says what is inside
                self.assertEqual([re.sub(r"<[^>]+>.*", "", c[2].split("<strong>")[1]) for c in cards if c[1]], [current])
                self.assertLess(text.index('class="doc-tabs"'), text.index('<article class="doc">'))

    def test_readme_slug_is_the_same_page(self):
        resp = self.client.get("/docs/readme")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('id="architecture-and-data-flow"', resp.text)

    def test_requirements_is_not_served(self):
        self.assertEqual(self.client.get("/docs/requirements").status_code, 404)

    def test_modifiers_renders(self):
        resp = self.client.get("/docs/modifiers")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('id="comparison-rules"', resp.text)

    def test_readme_comments_stay_hidden(self):
        self.assertNotIn("TODO screenshot", self.client.get("/docs").text)

    def test_index_lists_every_doc(self):
        resp = self.client.get("/docs/index")
        self.assertEqual(resp.status_code, 200)
        for href in ('href="/docs/readme"', 'href="/docs/modifiers"'):
            self.assertIn(href, resp.text)
        self.assertNotIn("/docs/requirements", resp.text)

    def test_unknown_slug_is_404(self):
        resp = self.client.get("/docs/etl")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("No Such Doc", resp.text)

    def test_paths_are_not_served(self):
        for path in ("/docs/..%2FREADME.md", "/docs/README.md", "/docs/assets/..%2Frequirements.md",
                     "/docs/assets/modifiers.md"):
            self.assertEqual(self.client.get(path).status_code, 404, path)


class SampleDocsTest(unittest.TestCase):
    """A temp root with small docs, to check rendering, link rewriting and that raw HTML never comes through."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "modifiers.md").write_text(SAMPLE_MODIFIERS, encoding="utf-8")
        (self.root / "README.md").write_text(SAMPLE_README, encoding="utf-8")
        (self.root / "docs" / "screenshot-search.png").write_bytes(PNG)
        (self.root / "docs" / "notes.txt").write_text("private", encoding="utf-8")
        self.client = make_client(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def page(self, path: str) -> str:
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        return resp.text

    def test_table_is_rendered(self):
        html = self.page("/docs/modifiers")
        self.assertIn("<table>", html)
        self.assertIn("<th>Modifier</th>", html)
        self.assertIn("<td>Professional component</td>", html)

    def test_fenced_code_is_rendered(self):
        html = self.page("/docs/modifiers")
        self.assertIn('<code class="language-sql">SELECT modifiers, COUNT(*) FROM rates GROUP BY modifiers;',
                      html)

    def test_headings_have_anchor_links_and_a_toc(self):
        html = self.page("/docs/modifiers")
        self.assertIn('<h2 id="categories">', html)
        self.assertIn('href="#categories"', html)
        self.assertIn('class="toc"', html)

    def test_script_tag_does_not_come_through(self):
        html = self.page("/docs/modifiers")
        self.assertNotIn('alert("modifiers")</script>', html)
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<b onclick", html)

    def test_comment_is_hidden(self):
        html = self.page("/docs/modifiers")
        self.assertNotIn("TODO add the 2023 modifier list", html)

    def test_javascript_link_is_dropped(self):
        self.assertNotIn("javascript:", self.page("/docs/modifiers"))

    def test_relative_doc_links_are_rewritten(self):
        html = self.page("/docs/modifiers")
        self.assertIn('href="/docs/readme#quick-start"', html)
        self.assertNotIn('href="/docs/requirements"', html)  # not served, so no link to it
        readme = self.page("/docs")
        self.assertIn('href="/docs/modifiers#categories"', readme)
        self.assertNotIn('href="/docs/requirements"', readme)

    def test_external_links_are_kept(self):
        self.assertIn('href="https://www.cms.gov/medicare/coding/hcpcsreleasecodesets/hcpcs-quarterly-update.html"',
                      self.page("/docs/modifiers"))

    def test_images_under_docs_are_served(self):
        self.assertIn('src="/docs/assets/screenshot-search.png"', self.page("/docs"))
        self.assertIn('src="/docs/assets/screenshot-search.png"', self.page("/docs/modifiers"))
        resp = self.client.get("/docs/assets/screenshot-search.png")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "image/png")
        self.assertEqual(resp.content, PNG)

    def test_only_images_are_served_from_docs(self):
        self.assertEqual(self.client.get("/docs/assets/notes.txt").status_code, 404)
        self.assertEqual(self.client.get("/docs/assets/missing.png").status_code, 404)

    def test_missing_file_is_not_written_yet(self):
        (self.root / "docs" / "modifiers.md").unlink()
        resp = self.client.get("/docs/modifiers")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("not written yet", resp.text)

    def test_file_is_read_on_each_request(self):
        self.assertNotIn("Freshly added", self.page("/docs/modifiers"))
        with open(self.root / "docs" / "modifiers.md", "a", encoding="utf-8") as f:
            f.write("\n## Freshly added\n")
        self.assertIn("Freshly added", self.page("/docs/modifiers"))


if __name__ == "__main__":
    unittest.main()


class AppWiringTest(unittest.TestCase):
    """The real app (#30): /docs is the README page, Swagger moved to /api/docs, and the nav links to Docs."""

    def setUp(self):
        from app import create_app
        self.tmp = tempfile.TemporaryDirectory()
        self.client = TestClient(create_app(Path(self.tmp.name) / "missing.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_docs_is_the_readme(self):
        resp = self.client.get("/docs")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('id="getting-started"', resp.text)
        self.assertNotIn("swagger-ui", resp.text)

    def test_swagger_lives_at_api_docs(self):
        resp = self.client.get("/api/docs")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("swagger-ui", resp.text)
        self.assertEqual(self.client.get("/redoc").status_code, 404)

    def test_nav_links_to_docs_and_marks_it_current(self):
        self.assertIn('<a href="/docs" aria-current="page">Docs</a>', self.client.get("/docs/modifiers").text)
        self.assertIn('<a href="/docs">Docs</a>', self.client.get("/codes").text)
