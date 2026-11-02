"""Every title is Title Case, each word capitalised ("Load Data", "Prices By Procedure"): the nav, the page title,
headings, tab labels, column headers and the Docs pages' headings. Data shown in a title (a code's own name) is left as published."""

import html
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import create_app
from tests.api import add_clinic_99213, add_code_10040, add_code_99213, build_db

TITLE_PATTERNS = (
    r"<title>(.*?)</title>",
    r'<nav aria-label="Main">(.*?)</nav>',
    r"<h[1-4][^>]*>(.*?)</h[1-4]>",
    r'<a role="tab"[^>]*>(.*?)</a>',
    r"<th(?:\s[^>]*)?>(.*?)</th>",
    r'<summary>((?i:How these numbers).*?)</summary>',
)


def titles(page: str) -> list[str]:
    page = re.sub(r'<span class="(cname|count|refnote)">.*?</span>|<code>.*?</code>|<svg.*?</svg>|<select.*?</select>|<th[^>]*class="fixed"[^>]*>.*?</th>', " ", page, flags=re.S)  # data, not words
    found = []
    for pattern in TITLE_PATTERNS:
        for chunk in re.findall(pattern, page, re.S):
            parts = chunk.split("</a>") if chunk.lstrip().startswith("<a") else [chunk]
            found += [t for t in (" ".join(html.unescape(re.sub(r"<[^>]+>", " ", p)).split()) for p in parts) if t]
    return found


def lowercase_words(title: str) -> list[str]:
    """Words (and hyphenated parts) that start with a lowercase letter; file names (rates.db) are data."""
    words = re.findall(r"[^\W\d_][\w’']*", re.sub(r"\S+\.(db|json|md)\b", " ", title))
    return [w for w in words if w[0].islower()]


class TitleCaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "rates.db"
        conn = sqlite3.connect(db_path)
        build_db(conn)
        add_code_10040(conn)
        add_code_99213(conn)
        add_clinic_99213(conn)
        ein = conn.execute("SELECT ein FROM rates WHERE ein IS NOT NULL LIMIT 1").fetchone()[0]
        conn.close()
        self.client = TestClient(create_app(db_path))
        self.paths = ["/", "/by-procedure", "/by-provider", "/by-procedure/10040", "/by-procedure/99213",
                      "/by-procedure/99213?view=stats&modifiers=25&modifiers_op=equals",
                      "/by-procedure/99213?view=rates", f"/by-provider/{ein}", "/docs", "/docs/modifiers"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_nav_names_each_page_in_title_case(self):
        nav = titles(self.client.get("/").text)
        for name in ("Load Data", "Prices By Procedure", "Prices By Provider", "Docs"):
            self.assertIn(name, nav)

    def test_every_title_on_every_page_capitalises_each_word(self):
        for path in self.paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                found = titles(response.text)
                self.assertTrue(found)
                self.assertEqual([t for t in found if lowercase_words(t)], [])


if __name__ == "__main__":
    unittest.main()
