"""Prices By Procedure (#40, direction B "two doors"): the list page, one procedure's prices with Search in and the
narrow-to-a-provider picker, the no-dead-end notices, and the redirects from /search and /codes. Real fixture rows
only (tests.api); fixture file 1 is fidelis-es, file 2 fidelis-ex."""

import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.datastructures import QueryParams

from app import create_app
from ui.procedure_page import search_redirect
from tests.api import (
    add_clinic_99213, add_code_10040, add_code_731, add_code_97151, add_code_99213, add_network_11_3322995, build_db,
)


class ProcedurePagesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "rates.db"
        conn = sqlite3.connect(db_path)
        build_db(conn)
        for add in (add_code_99213, add_clinic_99213, add_code_10040, add_code_731, add_code_97151,
                    add_network_11_3322995):
            add(conn)
        self.bronze = conn.execute("SELECT id FROM plans WHERE plan_id = '25303NY005'").fetchone()[0]
        conn.close()
        self.client = TestClient(create_app(db_path))

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, path, **params):
        response = self.client.get(path, params=params)
        self.assertEqual(response.status_code, 200, response.text[:300])
        return response.text

    # The empty page: one box over every procedure

    def test_the_empty_page_is_one_box_over_every_procedure(self):
        html = self.get("/by-procedure")
        self.assertIn("<h1>Prices By Procedure</h1>", html)
        self.assertIn('data-door="/api/procedures"', html)
        self.assertIn('href="/by-procedure/99213"', html)
        self.assertIn("Massage therapy", html)
        self.assertIn('href="/by-procedure/731?code_type=RC"', html)  # one number, two codes
        self.assertIn("hospital department", html)

    def test_typing_a_name_filters_the_list_without_script(self):
        html = self.get("/by-procedure", q="massage")
        self.assertIn('href="/by-procedure/97124"', html)
        self.assertNotIn('href="/by-procedure/99213"', html)
        self.assertIn("1 of", html)

    def test_procedures_api_finds_a_code_by_name_or_number(self):
        body = self.client.get("/api/procedures", params={"q": "massage"}).json()
        self.assertEqual([(p["billing_code"], p["href"]) for p in body["procedures"]], [("97124", "/by-procedure/97124")])
        body = self.client.get("/api/procedures", params={"q": "9921", "limit": 1}).json()
        self.assertEqual((body["total"], body["procedures"][0]["billing_code"]), (1, "99213"))

    # Sorting the list: every column, over every match before the list is cut

    def rows(self, html):
        return [href.removeprefix("/by-procedure/") for href in re.findall(r'<tr data-href="(/by-procedure/[^"]+)">', html)]

    def test_the_list_sorts_by_each_column_both_ways(self):
        for sort, direction, expected in (
                ("code", "asc", ["10006", "10040", "510", "521"]),
                ("code", "desc", ["99213", "97151", "97124", "97110"]),
                ("name", "asc", ["10040", "731?code_type=CPT", "97151", "510"]),  # Acne, Anes, Bhv, Clinic
                ("name", "desc", ["97110", "521", "99213", "97124"]),  # Therapeutic, Revenue code 0521, Office, Massage
                ("type", "asc", ["10006", "10040", "731?code_type=CPT", "97110"]),
                ("type", "desc", ["510", "521", "731?code_type=RC", "10006"]),
                ("rates", "desc", ["10040", "97151", "99213", "510"]),
                ("rates", "asc", ["731?code_type=CPT", "731?code_type=RC", "97110", "10006"]),
                ("eins", "desc", ["10040", "97151", "99213", "510"]),
                ("eins", "asc", ["10006", "731?code_type=CPT", "731?code_type=RC", "97110"])):
            with self.subTest(sort=sort, dir=direction):
                html = self.get("/by-procedure", sort=sort, dir=direction)
                self.assertEqual(self.rows(html)[:4], expected)
                body = self.client.get("/api/procedures", params={"sort": sort, "dir": direction, "limit": 4}).json()
                self.assertEqual([p["href"].removeprefix("/by-procedure/") for p in body["procedures"]], expected)
                self.assertEqual(body["total"], 10)

    def test_sorting_comes_before_the_list_is_cut(self):
        body = self.client.get("/api/procedures", params={"sort": "name", "dir": "asc", "limit": 2}).json()
        self.assertEqual([p["name"] for p in body["procedures"]], ["Acne surgery", "Anes upr gi ndsc px nos"])

    def test_a_filtered_list_sorts_too(self):
        self.assertEqual(self.rows(self.get("/by-procedure", q="97")), ["97151", "97124", "97110"])  # most rates first
        self.assertEqual(self.rows(self.get("/by-procedure", q="97", sort="rates", dir="asc")), ["97110", "97124", "97151"])
        self.assertEqual(self.rows(self.get("/by-procedure", q="97", sort="name", dir="asc")), ["97151", "97124", "97110"])
        body = self.client.get("/api/procedures", params={"q": "97", "sort": "code", "dir": "asc"}).json()
        self.assertEqual([p["billing_code"] for p in body["procedures"]], ["97110", "97124", "97151"])

    def test_the_default_order_is_unchanged_and_bad_values_fall_back_to_it(self):
        default = self.rows(self.get("/by-procedure"))
        self.assertEqual(default[:3], ["10040", "97151", "99213"])  # procedures first, most rates first
        self.assertEqual(self.rows(self.get("/by-procedure", sort="price", dir="sideways")), default)
        self.assertEqual(self.rows(self.get("/by-procedure", q="9921", sort="nope")), ["99213"])
        self.assertEqual(self.rows(self.get("/by-procedure", sort="code", dir="sideways"))[0], "10006")  # lowest first
        body = self.client.get("/api/procedures", params={"sort": "nope", "dir": "nope"}).json()
        self.assertEqual([p["href"].removeprefix("/by-procedure/") for p in body["procedures"]], default)

    def test_every_header_sorts_and_the_count_says_how(self):
        html = self.get("/by-procedure", q="97")
        for key, first in (("code", "asc"), ("name", "asc"), ("type", "asc"), ("rates", "desc"), ("eins", "desc")):
            self.assertIn(f'<th data-col="{key}"', html)
            self.assertIn(f'href="/by-procedure?q=97&amp;sort={key}&amp;dir={first}"', html)
        self.assertEqual(html.count('aria-sort="none">'), 5)  # the list's own order: no column is the sort
        self.assertIn(", most rates first.", html)
        html = self.get("/by-procedure", sort="name", dir="asc")
        self.assertEqual(html.count('aria-sort="ascending">'), 1)
        self.assertIn('href="/by-procedure?sort=name&amp;dir=desc">Procedure<span class="arrow" aria-hidden="true">▲', html)
        self.assertIn('href="/by-procedure?sort=rates&amp;dir=desc">Rates<span class="arrow" aria-hidden="true">↕', html)
        self.assertIn(", by name, A to Z.", html)
        self.assertNotIn("most rates first", html)
        html = self.get("/by-procedure", sort="rates", dir="asc")
        self.assertIn('href="/by-procedure?sort=rates&amp;dir=desc">Rates<span class="arrow" aria-hidden="true">▲', html)
        self.assertIn(", fewest rates first.", html)
        body = self.client.get("/api/procedures", params={"sort": "eins", "dir": "desc"}).json()
        self.assertEqual((body["sort"], body["dir"], body["order"]), ("eins", "desc", "most providers first"))
        body = self.client.get("/api/procedures").json()
        self.assertEqual((body["sort"], body["order"]), ("", "most rates first"))

    # One procedure's prices

    def test_a_procedure_page_shows_its_prices_and_states_what_is_searched(self):
        html = self.get("/by-procedure/99213")
        self.assertIn("OFFICE/OUTPATIENT VISIT EST", html.upper())
        self.assertIn("<strong>Searching:</strong> All networks (fidelis-es + fidelis-ex, 3 plans) in the active index", html)
        self.assertIn('role="tablist"', html)

    def test_search_in_is_three_plain_choices_and_a_plan_lookup(self):  # #43
        html = self.get("/by-procedure/99213")
        form = re.search(r'<form class="search-in".*?</form>', html, re.S).group(0)
        radios = re.findall(r'<input type="radio" name="in" value="([^"]*)"( checked)?', form)
        self.assertEqual(radios, [("", " checked"), ("file-1", ""), ("file-2", "")])
        text = " ".join(re.sub(r"<[^>]+>", " ", form).split())
        for label in ("All networks · both files · 3 plans", "Essential Plan · fidelis-es · 1 plan",
                      "Ambetter · fidelis-ex · 2 plans"):
            self.assertIn(label, text)
        self.assertIn('data-plans="/api/plans"', form)
        self.assertIn("Find your plan", text)
        self.assertNotIn("<select", form)
        self.assertNotIn("plan-", form)
        self.assertNotIn("<datalist", html)

    def test_one_plan_searches_its_file_and_says_so(self):  # #39, #43: Bronze is priced in fidelis-ex only
        html = self.get("/by-procedure/99213", **{"in": f"plan-{self.bronze}"})
        self.assertIn("<strong>Searching:</strong> the Ambetter network (fidelis-ex, 2 plans)", html)
        self.assertIn("Ambetter from Fidelis Care Bronze (25303NY005) is in the Ambetter network (fidelis-ex);"
                      " every plan there shares these rates.", html)
        self.assertIn('<input type="radio" name="in" value="file-2" checked', html)
        # fidelis-ex has 3 of the fixture's 99213 rates (4N Medical Care 2, New York Network Management IPA 2 ... ).
        api = self.client.get("/api/search", params={"billing_code": "99213", "plan": self.bronze}).json()
        self.assertIn(f"{api['row_count']:,} rate", html)
        self.assertIn(f"in=plan-{self.bronze}", html)  # links keep the plan

    def test_one_file_says_its_network(self):
        html = self.get("/by-procedure/99213", **{"in": "file-1"})
        self.assertIn("<strong>Searching:</strong> the Essential Plan network (fidelis-es, 1 plan)", html)
        self.assertIn('<input type="radio" name="in" value="file-1" checked', html)
        self.assertNotIn("is in the Essential Plan network", html)

    def test_plans_api_finds_a_plan_by_name_or_id(self):
        body = self.client.get("/api/plans", params={"q": "25303NY005"}).json()
        self.assertEqual(body["plans"], [{"value": f"plan-{self.bronze}", "plan_id": "25303NY005",
                                          "plan_name": "Ambetter from Fidelis Care Bronze", "network": "Ambetter",
                                          "short_name": "fidelis-ex"}])
        self.assertEqual(self.client.get("/api/plans", params={"q": "essential"}).json()["total"], 1)

    def test_links_keep_the_search_in_choice(self):
        html = self.get("/by-procedure/10040", **{"in": "file-1"})
        self.assertIn('href="/by-procedure/10040?in=file-1&amp;modifiers=51&amp;modifiers_op=equals', html)

    def test_hospital_rates_are_their_own_groups_with_hide_and_show(self):
        html = self.get("/by-procedure/99213", view="compare")
        self.assertIn("Hospital Rates For 99213", html)
        self.assertIn('href="/by-procedure/99213?include_facility=false&amp;view=compare"', html)
        own, hospital = html.split("Hospital Rates For 99213", 1)
        self.assertNotIn("RC 0510", own.split('id="p-compare"')[1])
        self.assertIn("RC 0510", hospital)
        head = re.findall(r"<th[^>]*>(.*?)</th>", hospital.split("<thead>", 1)[1].split("</thead>", 1)[0], re.S)
        self.assertEqual([re.sub(r"<[^>]+>|[↕▲▼]", "", h).strip() for h in head[:3]],
                         ["Hospital Department", "Place Of Service", "Min"])  # no modifier on any: no column
        self.assertIn("None has a modifier.", hospital)
        hidden = self.get("/by-procedure/99213", view="compare", include_facility="false")
        self.assertIn("hidden", hidden)
        self.assertIn('href="/by-procedure/99213?view=compare"', hidden)

    def test_provider_names_in_the_rates_link_to_their_prices_by_provider(self):  # cross-link (#40)
        html = self.get("/by-procedure/99213", view="rates", group_by="none", **{"in": "file-1"})
        self.assertIn('<a href="/by-provider/13-4068415?in=file-1" title="This provider’s prices for every code">Forme Medical Center</a>', html)
        self.assertIn('<a href="/by-provider/13-4068415?in=file-1">13-4068415</a>', html)

    # Narrow to a provider

    def test_the_picker_asks_only_for_providers_with_rates_for_this_code_here(self):
        html = self.get("/by-procedure/99213", **{"in": "file-2"})
        self.assertIn('data-picker="/api/procedures/99213/providers?in=file-2"', html)

    def test_narrowed_to_an_ein_compares_with_everyone_in_the_same_group(self):
        html = self.get("/by-procedure/97151", ein="47-3981360", view="compare")
        self.assertIn("Narrowed to</span> <strong>EIN 47-3981360</strong>", html)
        self.assertIn('href="/by-provider/47-3981360"', html)
        self.assertIn('href="/by-procedure/97151?view=compare"', html)  # all providers for the code
        self.assertRegex(html, r'<th class="num sortable" aria-sort="none" title="Everyone’s median in the same group, '
                               r'counted the same way"><a [^>]*>Everyone<span class="arrow"')
        self.assertIn("$19.75", html)  # everyone's median, no modifier, office
        self.assertIn("mostly this provider", html)

    def test_an_npi_without_the_code_says_so_with_alternatives(self):  # dead end 1, on the page itself
        html = self.get("/by-procedure/99213", npi="1003050162")
        self.assertIn("NPI 1003050162 (Hands of Hope PT, PC, EIN 11-3322995) has no rates for 99213.", html)
        self.assertIn('href="/by-procedure/99213?ein=11-3322995"', html)
        self.assertIn('href="/by-procedure/97124?npi=1003050162"', html)

    def test_a_provider_with_only_hospital_rates_hidden_is_offered_them(self):  # dead end 2
        html = self.get("/by-procedure/99213", ein="14-1338544", include_facility="false")
        self.assertIn("has no 99213 rates of its own", html)
        self.assertIn('href="/by-procedure/99213?ein=14-1338544"', html)

    def test_providers_for_code_api(self):
        body = self.client.get("/api/procedures/99213/providers", params={"q": "1003050162"}).json()
        self.assertEqual(body["providers"], [])
        self.assertEqual(body["notice"]["kind"], "none")
        self.assertIn("/by-procedure/99213?ein=11-3322995", [a["href"] for a in body["notice"]["alternatives"]])
        body = self.client.get("/api/procedures/99213/providers", params={"q": "forme", "in": "file-1"}).json()
        self.assertEqual([p["href"] for p in body["providers"]], ["/by-procedure/99213?in=file-1&ein=13-4068415"])
        self.assertEqual(body["searching"], "the Essential Plan network (fidelis-es, 1 plan)")
        self.assertEqual(self.client.get("/api/procedures/99%3B1/providers").status_code, 422)

    def test_unknown_code_says_no_rates(self):
        html = self.get("/by-procedure/99999")
        self.assertIn("No rates for 99999", html)

    # Old links

    def test_old_search_links_redirect_with_the_same_meaning(self):
        cases = {
            "": "/by-procedure",
            "billing_code=10006": "/by-procedure/10006",
            "billing_code=10006&source=2&files=chosen&view=rates": "/by-procedure/10006?in=file-2&view=rates",
            "billing_code=10006&source=1&source=2&files=chosen": "/by-procedure/10006",
            f"billing_code=99213&plan={self.bronze}&npi=1942573167": f"/by-procedure/99213?in=plan-{self.bronze}&npi=1942573167",
            "billing_code=99213&include_facility=false&ein=46-2628853&ein_op=equals":
                "/by-procedure/99213?include_facility=false&ein=46-2628853&ein_op=equals",
            "billing_code=99213&include_facility=false&include_facility=true": "/by-procedure/99213",
        }
        for query, location in cases.items():
            with self.subTest(query=query):
                self.assertEqual(search_redirect(QueryParams(query)), location)
                response = self.client.get(f"/search?{query}", follow_redirects=False)
                self.assertEqual((response.status_code, response.headers["location"]), (307, location))

    def test_old_codes_links_redirect_to_the_list(self):
        for query, location in (("", "/by-procedure"), ("?q=acne", "/by-procedure?q=acne")):
            with self.subTest(query=query):
                response = self.client.get(f"/codes{query}", follow_redirects=False)
                self.assertEqual(response.headers["location"], location)


if __name__ == "__main__":
    unittest.main()
