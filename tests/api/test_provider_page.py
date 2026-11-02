"""Prices By Provider (#40, direction B "two doors"): the list page, one provider's prices per code against
everyone's, the narrow-to-a-procedure picker with no dead ends, and the redirects from /providers. Real fixture rows
only (tests.api); fixture file 1 is fidelis-es, file 2 fidelis-ex."""

import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import create_app
from tests.api import (
    add_clinic_99213, add_code_10040, add_code_731, add_code_97151, add_code_99213, add_network_11_3322995, build_db,
)


class ProviderPagesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "rates.db"
        conn = sqlite3.connect(db_path)
        build_db(conn)
        for add in (add_code_99213, add_clinic_99213, add_code_10040, add_code_731, add_code_97151,
                    add_network_11_3322995):
            add(conn)
        conn.close()
        self.client = TestClient(create_app(db_path))

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, path, status=200, **params):
        response = self.client.get(path, params=params)
        self.assertEqual(response.status_code, status, response.text[:300])
        return response.text

    # The empty page

    def test_the_empty_page_is_one_box_over_every_provider_most_rates_first(self):
        html = self.get("/by-provider")
        self.assertIn("<h1>Prices By Provider</h1>", html)
        self.assertIn('data-door="/api/providers?sort=rates&amp;dir=desc"', html)
        rows = re.findall(r'<tr data-href="(/by-provider/[^"]+)">', html)
        self.assertEqual(rows[0], "/by-provider/45-1535116")  # Mount Sinai's anesthesiology: 21 rates

    def test_a_name_ein_or_npi_filters_the_list(self):
        for q in ("mount sinai", "45-1535116", "451535116", "1033684915"):
            with self.subTest(q=q):
                rows = re.findall(r'<tr data-href="(/by-provider/[^"]+)">', self.get("/by-provider", q=q))
                self.assertEqual(rows, ["/by-provider/45-1535116"])
        html = self.get("/by-provider", q="hands of hope")
        self.assertIn("Hands of Hope PT, PC", html)  # the matching name shows, not just the EIN's first

    def test_providers_api_gives_matched_names_and_links(self):
        body = self.client.get("/api/providers", params={"q": "hands of hope"}).json()
        [p] = body["providers"]
        self.assertEqual((p["matched_names"], p["href"]), (["Hands of Hope PT, PC"], "/by-provider/11-3322995"))

    # Sorting the list: every column, over every match before the list is cut

    def rows(self, html):
        return [href.removeprefix("/by-provider/") for href in re.findall(r'<tr data-href="(/by-provider/[^"]+)">', html)]

    def test_the_list_sorts_by_each_column_both_ways(self):
        for sort, direction, expected in (
                ("name", "asc", ["46-2628853", "46-2142743", "11-3322995"]),  # 4N, A and J, Abdul Malik
                ("name", "desc", ["46-1255974", "45-3148303", "11-2560903"]),  # Trustees, Trinity, Teladoc
                ("ein", "asc", ["11-2560903", "11-2997945", "11-3211425"]),
                ("ein", "desc", ["85-3378316", "85-1911011", "84-2625611"]),
                ("npis", "desc", ["11-2560903", "45-1535116", "11-2997945"]),
                ("npis", "asc", ["11-3211425", "11-3460764", "11-3471596"]),
                ("codes", "desc", ["11-3322995", "13-4019319", "85-3378316"]),
                ("codes", "asc", ["11-2560903", "11-2997945", "11-3211425"]),
                ("rates", "desc", ["45-1535116", "11-2560903", "47-3981360"]),
                ("rates", "asc", ["11-3211425", "11-3460764", "13-3186117"])):
            with self.subTest(sort=sort, dir=direction):
                self.assertEqual(self.rows(self.get("/by-provider", sort=sort, dir=direction))[:3], expected)
                body = self.client.get("/api/providers", params={"sort": sort, "dir": direction, "limit": 3}).json()
                self.assertEqual([p["ein"] for p in body["providers"]], expected)

    def test_a_filtered_list_sorts_too(self):
        self.assertEqual(self.rows(self.get("/by-provider", q="behavior")),
                         ["47-3981360", "82-3396945", "82-2669171", "46-2142743"])  # most rates first
        self.assertEqual(self.rows(self.get("/by-provider", q="behavior", sort="name", dir="asc")),
                         ["46-2142743", "82-3396945", "47-3981360", "82-2669171"])  # A and J, Comprehensive, Encore, Rachael

    def test_the_default_order_is_unchanged_and_bad_values_fall_back_to_it(self):
        default = self.rows(self.get("/by-provider"))
        self.assertEqual(default[:2], ["45-1535116", "11-2560903"])
        self.assertEqual(self.rows(self.get("/by-provider", sort="price", dir="sideways")), default)
        self.assertEqual(self.rows(self.get("/by-provider", sort="name", dir="sideways"))[0], "46-2628853")  # A to Z
        self.assertEqual(self.rows(self.get("/by-provider", sort="npis", dir="sideways"))[0], "11-2560903")  # most first

    def test_every_header_sorts_and_the_count_says_how(self):
        html = self.get("/by-provider", q="behavior")
        for key, first in (("name", "asc"), ("ein", "asc"), ("npis", "desc"), ("codes", "desc")):
            self.assertIn(f'<th data-col="{key}"', html)
            self.assertIn(f'href="/by-provider?q=behavior&amp;sort={key}&amp;dir={first}"', html)
        self.assertIn('href="/by-provider?q=behavior&amp;sort=rates&amp;dir=asc">Rates<span class="arrow" aria-hidden="true">▼',
                      html)  # the default: most rates first, so Rates is the active column
        self.assertEqual(html.count('aria-sort="descending">'), 1)
        self.assertIn(", most rates first.", html)
        html = self.get("/by-provider", sort="ein", dir="asc")
        self.assertIn('href="/by-provider?sort=ein&amp;dir=desc">EIN<span class="arrow" aria-hidden="true">▲', html)
        self.assertIn('href="/by-provider?sort=rates&amp;dir=desc">Rates<span class="arrow" aria-hidden="true">↕', html)
        self.assertIn(", by EIN, lowest first.", html)
        self.assertNotIn("most rates first", html)
        body = self.client.get("/api/providers", params={"sort": "name", "dir": "desc", "limit": 1}).json()
        self.assertEqual(([p["ein"] for p in body["providers"]], body["order"]), (["46-1255974"], "by name, Z to A"))

    # One provider's prices

    def test_a_provider_page_compares_each_code_with_everyone_in_the_same_group(self):
        html = self.get("/by-provider/47-3981360")
        self.assertIn("<h1>EIN 47-3981360</h1>", html)
        row = re.search(r'<tr data-href="/by-procedure/97151\?ein=47-3981360">(.*?)</tr>\s*(?=<tr data-href|</tbody>)',
                        html, re.S).group(1)
        self.assertIn("no modifier · office (11)", row)
        self.assertIn("$20.38", row)   # its median, 20.375
        self.assertIn("$19.75", row)   # everyone's
        self.assertIn("mostly this provider", row)
        self.assertIn("+3%", row)
        self.assertIn('href="/by-procedure/97151">all providers</a>', html)  # this code, all providers
        self.assertIn("<strong>Searching:</strong> All networks (fidelis-es + fidelis-ex, 3 plans)", html)

    def code_row_and_next(self, html, href):
        """A code's own row (up to its first </tr>) and the row straight after it."""
        start = html.index(f'<tr data-href="{href}">')
        end = html.index("</tr>", start) + len("</tr>")
        after = re.match(r"\s*(<tr[^>]*>.*?</tr>)\s*(?=<tr data-href|</tbody>\s*</table>\s*(?:<nav|<div class=\"sech\"))",
                         html[end:], re.S)
        return html[start:end], after.group(1) if after else ""

    def test_the_all_providers_link_sits_in_the_code_cell_not_its_own_column(self):
        html = self.get("/by-provider/47-3981360")
        row, _ = self.code_row_and_next(html, "/by-procedure/97151?ein=47-3981360")
        code_cell = re.search(r'<td class="code">(.*?)</td>', row, re.S).group(1)
        self.assertIn('<a class="all" href="/by-procedure/97151">all providers</a>', code_cell)
        head = re.search(r'<table class="list" id="codes-table">\s*<thead>(.*?)</thead>', html, re.S).group(1)
        self.assertEqual(re.findall(r"<th(?:\s[^>]*)?>(.*?)</th>", head),
                         ["Code", "Procedure", "Rates", "Most Common Group", "This Provider", "Everyone", "Difference",
                          "Prices By Name"])  # no empty last column

    def test_prices_by_name_opens_into_a_full_width_row_beneath_the_code(self):
        html = self.get("/by-provider/47-3981360")
        row, names_row = self.code_row_and_next(html, "/by-procedure/97151?ein=47-3981360")
        self.assertIn('<details class="breakdown"><summary>4 groups · 1 name</summary></details>', row)
        self.assertNotIn("names-in-group", row)  # the breakdown isn't squeezed into the summary's cell
        self.assertTrue(names_row.startswith('<tr class="names-row">'), names_row[:80])
        self.assertIn('<td colspan="8">', names_row)  # the table's whole width
        self.assertEqual(re.findall(r"<th(?:\s[^>]*)?>(.*?)</th>", names_row), ["Group", "Name", "Median", "Rates"])
        self.assertIn("office (11)</span> · 12 rates", names_row)
        self.assertIn('<td>Encore Licensed Behavior Analyst, PLLC</td><td class="num">$20.38</td><td class="num muted">12</td>',
                      names_row)
        self.assertEqual(names_row.count('<tbody class="group">'), 4)  # one block per group

    def test_names_and_npis_are_listed(self):
        html = self.get("/by-provider/11-3322995")
        self.assertIn("3 provider names bill under this tax ID", html)
        for name in ("Hands of Hope PT, PC", "Abdul Malik MD, PC", "New York Network Management IPA, Inc"):
            self.assertIn(name, html)
        self.assertIn("1003050162", html)

    def test_search_in_narrows_the_codes_and_links_keep_it(self):
        html = self.get("/by-provider/11-3322995", **{"in": "file-1"})  # fidelis-es: Abdul Malik's 97124 only
        self.assertIn('href="/by-procedure/97124?in=file-1&amp;ein=11-3322995"', html)
        self.assertNotIn("/by-procedure/99213?in=file-1&amp;ein=11-3322995", html)
        self.assertIn('data-picker="/api/providers/11-3322995/codes?in=file-1"', html)

    def test_no_rates_in_the_chosen_plans_offers_all_plans(self):
        html = self.get("/by-provider/13-4068415", **{"in": "file-2"})  # Forme Medical Center: fidelis-es only
        self.assertIn("EIN 13-4068415 has no rates in the Ambetter network (fidelis-ex, 2 plans)", html)
        self.assertIn('href="/by-provider/13-4068415"', html)

    def test_unknown_or_invalid_ein(self):
        self.assertIn("No provider has", self.get("/by-provider/99-9999999", status=404).replace(
            "EIN 99-9999999 has no rates in the loaded files", "No provider has"))
        self.assertIn("isn’t an EIN", self.get("/by-provider/12345", status=404))

    def test_the_picker_offers_only_its_codes_and_says_when_a_code_has_none(self):  # dead end 3
        body = self.client.get("/api/providers/11-3322995/codes", params={"q": "10040"}).json()
        self.assertEqual(body["codes"], [])
        self.assertIn("EIN 11-3322995 has no rates for 10040", body["notice"]["message"])
        self.assertIn("/by-procedure/10040", [a["href"] for a in body["notice"]["alternatives"]])
        body = self.client.get("/api/providers/11-3322995/codes", params={"q": "massage", "in": "file-1"}).json()
        self.assertEqual([c["href"] for c in body["codes"]], ["/by-procedure/97124?in=file-1&ein=11-3322995"])
        self.assertEqual(self.client.get("/api/providers/nope/codes").status_code, 422)

    # Old links

    def test_old_provider_links_redirect(self):
        for old, new in (("/providers", "/by-provider"), ("/providers?q=sinai", "/by-provider?q=sinai"),
                         ("/providers/113322995", "/by-provider/11-3322995"),
                         ("/providers/11-3322995?name=Hands+of+Hope+PT%2C+PC",
                          "/by-provider/11-3322995?name=Hands+of+Hope+PT%2C+PC")):
            with self.subTest(old=old):
                response = self.client.get(old, follow_redirects=False)
                self.assertEqual((response.status_code, response.headers["location"]), (307, new))


if __name__ == "__main__":
    unittest.main()
