import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import create_app
from tests.api import (
    add_clinic_99213, add_code_10040, add_code_260, add_code_731, add_code_97151, add_code_99213, build_db,
)


class WithDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "rates.db"
        conn = sqlite3.connect(db_path)
        build_db(conn)
        conn.close()
        self.db_path = db_path
        self.client = TestClient(create_app(db_path))

    def tearDown(self):
        self.tmp.cleanup()

    def test_api_search_returns_rates_cheapest_first(self):
        response = self.client.get("/api/search", params={"billing_code": " 10006 "})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["billing_code"], "10006")
        self.assertEqual(body["total"], 2)
        self.assertEqual([r["negotiated_rate"] for r in body["results"]], [69.85, 76.84])
        self.assertEqual(body["code"]["name"], "FNA BX W/US GDN EA ADDL")

    def test_api_search_unknown_code_is_empty_not_an_error(self):
        response = self.client.get("/api/search", params={"billing_code": "99999"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 0)

    def test_api_search_rejects_empty_or_invalid_code(self):
        for params in ({}, {"billing_code": "   "}, {"billing_code": "10006; DROP TABLE"}):
            with self.subTest(params=params):
                response = self.client.get("/api/search", params=params)
                self.assertEqual(response.status_code, 422)
                self.assertIn("detail", response.json())

    def test_api_search_rejects_bad_paging(self):
        response = self.client.get("/api/search", params={"billing_code": "10006", "view": "rates", "limit": 0})
        self.assertEqual(response.status_code, 422)

    def test_health_reports_each_source_file(self):
        body = self.client.get("/api/health").json()
        self.assertTrue(body["db_present"])
        self.assertEqual(body["rate_count"], 3)
        counts = {f["file"]: (f["rate_count"], f["error"]) for f in body["source_files"]}
        self.assertEqual(counts, {
            "2026-09-28_centene-management-company-llc_fidelis-es_in-network.json": (1, None),
            "2026-09-28_centene-management-company-llc_fidelis-ex_in-network.json": (2, None),
        })

    def test_procedure_list_links_every_code_to_its_prices(self):  # was the Billing Codes page (#40)
        response = self.client.get("/codes")
        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/by-procedure/10006"', response.text)
        self.assertIn("Acne surgery", response.text)

    def test_billing_codes_is_never_written_with_a_small_c(self):  # #41.4
        for path in ("/", "/by-procedure", "/by-provider", "/docs"):
            with self.subTest(path=path):
                self.assertNotIn("Billing codes", self.client.get(path).text)

    def test_procedure_list_filter_narrows_the_list(self):
        response = self.client.get("/codes", params={"q": "acne"})
        self.assertIn("Acne surgery", response.text)
        self.assertNotIn('href="/by-procedure/10006"', response.text)

    def test_api_codes_returns_filtered_codes(self):
        body = self.client.get("/api/codes", params={"q": "fine needle"}).json()
        self.assertEqual([c["billing_code"] for c in body["codes"]], ["10006"])

    def test_results_page_sorts_by_the_chosen_column(self):
        text = self.client.get("/search", params={"billing_code": "10006", "view": "rates", "sort": "provider", "dir": "desc"}).text
        cell = '<td data-col="provider" class="provider"><a href="/by-provider/11-3471596" title="This provider’s prices for every code">{}'  # result cells
        self.assertLess(text.index(cell.format("Samaritan")), text.index(cell.format("Minkowitz")))

    def test_results_headers_link_to_sorting_and_name_their_column(self):
        text = self.client.get("/search", params={"billing_code": "10006", "view": "rates"}).text
        self.assertIn("sort=provider", text)
        self.assertIn('data-col="ein"', text)
        self.assertIn('id="columns"', text)

    def test_bad_sort_is_rejected(self):
        response = self.client.get("/api/search", params={"billing_code": "10006", "view": "rates", "sort": "bogus"})
        self.assertEqual(response.status_code, 422)
        page = self.client.get("/search", params={"billing_code": "10006", "view": "rates", "sort": "bogus"})
        self.assertEqual(page.status_code, 200)
        self.assertIn("Minkowitz", page.text)

    def test_procedure_list_puts_most_rates_first(self):
        text = self.client.get("/by-procedure").text
        self.assertLess(text.index('href="/by-procedure/10006"'), text.index('href="/by-procedure/10040"'))

    def test_page_filters_by_one_source_file(self):
        text = self.client.get("/search", params={"billing_code": "10006", "view": "rates", "source": "2"}).text
        self.assertIn("Minkowitz Consulting Pathologists PC", text)
        self.assertNotIn("Samaritan Medical Center", text)
        self.assertIn("in=file-2", text)  # sort links keep the choice

    def test_page_searches_both_files_when_both_are_picked(self):
        text = self.client.get("/search", params=[("billing_code", "10006"), ("view", "rates"), ("source", "1"), ("source", "2")]).text
        self.assertIn("Minkowitz Consulting Pathologists PC", text)
        self.assertIn("Samaritan Medical Center", text)

    def test_page_searches_all_plans_at_first(self):
        text = self.client.get("/by-procedure/10006").text
        self.assertIn('<input type="radio" name="in" value="" checked', text)
        self.assertIn("Ambetter · fidelis-ex · 2 plans", " ".join(re.sub(r"<[^>]+>", " ", text).split()))

    def test_page_ignores_an_unknown_source(self):
        text = self.client.get("/search", params={"billing_code": "10006", "view": "rates", "source": "abc"}).text
        self.assertIn("Samaritan Medical Center", text)

    def test_api_filters_by_source_files(self):
        body = self.client.get("/api/search", params={"billing_code": "10006", "view": "rates", "source_file_id": 1}).json()
        self.assertEqual([r["business_name"] for r in body["results"]], ["Samaritan Medical Center"])
        both = self.client.get("/api/search", params=[("billing_code", "10006"), ("view", "rates"), ("source_file_id", 1), ("source_file_id", 2)])
        self.assertEqual(both.json()["total"], 2)
        bad = self.client.get("/api/search", params={"billing_code": "10006", "view": "rates", "source_file_id": "x"})
        self.assertEqual(bad.status_code, 422)

    def test_page_filters_by_any_column(self):
        text = self.client.get("/search", params={"billing_code": "10006", "view": "rates", "provider": "minkowitz"}).text
        cell = '<td data-col="provider" class="provider"><a href="/by-provider/11-3471596" title="This provider’s prices for every code">{}'  # result cells
        self.assertIn(cell.format("Minkowitz Consulting Pathologists PC"), text)
        self.assertNotIn(cell.format("Samaritan Medical Center"), text)
        self.assertIn("provider=minkowitz", text)  # sort links keep the filter
        self.assertIn('value="minkowitz"', text)  # the filter shows what was typed
        # Provider is a dropdown here (two values), but a typed "contains" filter still says contains.
        self.assertIn('<option value="contains" selected>contains</option>', text)

    def test_page_has_a_filter_for_each_column_that_varies(self):
        text = self.client.get("/search", params={"billing_code": "10006", "view": "rates"}).text
        for name in ("provider", "rate_min", "rate_max"):  # 2 providers, any rate
            with self.subTest(name=name):
                self.assertIn(f'name="{name}"', text)
        self.assertIn('<option value="Samaritan Medical Center"', text)

    def test_a_column_with_one_value_shows_it_instead_of_a_filter(self):
        text = self.client.get("/search", params={"billing_code": "10006", "view": "rates"}).text
        self.assertNotIn('name="setting"', text)
        self.assertIn("Same for every row: professional · outpatient", text)

    def test_a_one_value_column_keeps_its_filter_while_it_is_being_used(self):
        text = self.client.get("/search", params={"billing_code": "10006", "view": "rates", "setting_op": "not_empty"}).text
        self.assertIn('name="setting"', text)

    def test_page_ignores_a_rate_that_is_not_a_number(self):
        text = self.client.get("/search", params={"billing_code": "10006", "view": "rates", "rate_min": "abc"}).text
        self.assertIn("Samaritan Medical Center", text)

    def test_api_filters_by_any_column(self):
        body = self.client.get("/api/search", params={"billing_code": "10006", "view": "rates", "rate_min": 70}).json()
        self.assertEqual([r["business_name"] for r in body["results"]], ["Samaritan Medical Center"])
        bad = self.client.get("/api/search", params={"billing_code": "10006", "view": "rates", "rate_min": "abc"})
        self.assertEqual(bad.status_code, 422)

    def test_page_filters_with_an_operator_and_keeps_it(self):
        text = self.client.get("/search", params={"billing_code": "10006", "view": "rates", "provider": "samaritan",
                                                  "provider_op": "not_contains"}).text
        self.assertIn("Minkowitz Consulting Pathologists PC", text)
        self.assertNotIn("<td data-col=\"provider\" class=\"provider\">Samaritan", text)
        self.assertIn("provider=samaritan&amp;provider_op=not_contains", text)  # sort links keep both
        self.assertIn('<option value="not_contains" selected>', text)  # the chosen operator shows

    def test_page_ignores_an_unknown_operator(self):
        response = self.client.get("/search", params={"billing_code": "10006", "view": "rates", "provider": "samaritan",
                                                      "provider_op": "bogus"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("1 rate match", response.text)  # fell back to contains
        self.assertNotIn("provider_op=bogus", response.text)

    def test_page_picks_the_control_from_the_number_of_values(self):
        # Fixture's 10006: one billing class (nothing to filter), two providers, so tiny limits show each control.
        text = self.client.get("/search", params={"billing_code": "10006", "view": "rates"}).text
        self.assertIn('<select form="filters" name="provider"', text)  # 2 values <= 50
        self.assertIn('<option value="equals" selected>is</option><option value="not_equals">is not</option>', text)
        self.assertNotIn('<option value="contains">', text.split('name="provider_op"')[1].split("</select>")[0])

        mid = TestClient(create_app(self.db_path, select_limit=1)).get(
            "/search", params={"billing_code": "10006", "view": "rates"}).text
        self.assertIn('<input form="filters" name="provider" value="" data-options="choices-provider"', mid)
        self.assertIn('<script type="application/json" id="choices-provider">["Minkowitz Consulting Pathologists PC",', mid)
        self.assertIn("Same for every row: professional", mid)  # still 1 value
        self.assertIn('<option value="not_contains">does not contain</option>', mid)

        many = TestClient(create_app(self.db_path, select_limit=1, suggest_limit=1)).get(
            "/search", params={"billing_code": "10006", "view": "rates"}).text
        self.assertIn('<input form="filters" name="provider" value=""', many)
        self.assertNotIn('choices-provider', many)


    def test_api_takes_operators(self):
        body = self.client.get("/api/search", params={"billing_code": "10006", "view": "rates", "provider": "Samaritan Medical Center",
                                                      "provider_op": "not_equals"}).json()
        self.assertEqual([r["business_name"] for r in body["results"]], ["Minkowitz Consulting Pathologists PC"])
        self.assertEqual(body["filters"], {"provider": "Samaritan Medical Center", "provider_op": "not_equals"})
        kept = self.client.get("/api/search", params={"billing_code": "10040", "ein": "113471596",
                                                      "ein_op": "not_contains"}).json()
        self.assertEqual(kept["total"], 1)
        bad = self.client.get("/api/search", params={"billing_code": "10006", "view": "rates", "npi": "1", "npi_op": "like"})
        self.assertEqual(bad.status_code, 422)
        self.assertIn("npi_op", bad.json()["detail"])

    def test_source_file_column_only_when_results_span_files(self):
        both = self.client.get("/search", params={"billing_code": "10006", "view": "rates"}).text
        self.assertIn('<td data-col="source"', both)
        one = self.client.get("/search", params={"billing_code": "10006", "view": "rates", "source": "2"}).text
        self.assertNotIn('data-col="source"', one)
        self.assertNotIn("Source file", one)

    def test_search_in_uses_the_files_short_names(self):
        text = " ".join(re.sub(r"<[^>]+>", " ", self.client.get("/by-procedure/10006").text).split())
        self.assertIn("Essential Plan · fidelis-es · 1 plan", text)

    def test_filters_apply_without_a_button_and_one_clear_all_drops_them(self):
        text = self.client.get("/search", params={"billing_code": "10006", "view": "rates"}).text
        self.assertNotIn("Apply filters", text)
        self.assertNotIn('class="clear-all"', text)  # nothing to clear
        filtered = self.client.get("/search", params={"billing_code": "10006", "view": "rates", "provider": "minkowitz"}).text
        clear = re.search(r'class="clear-all" href="([^"]+)"', filtered).group(1)
        self.assertNotIn("provider", clear)
        self.assertEqual(clear, "/by-procedure/10006")

    def test_providers_pages_are_part_of_the_app_and_in_the_nav(self):
        self.assertIn('<a href="/by-provider"', self.client.get("/").text)
        page = self.client.get("/providers")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Minkowitz Consulting Pathologists PC", page.text)
        self.assertEqual(self.client.get("/api/providers/11-3471596").status_code, 200)

    def test_stats_show_iqr_over_median(self):
        text = self.client.get("/search", params={"billing_code": "10006"}).text
        self.assertIn("IQR ÷ median", text)
        self.assertIn("−2.4% / +2.4%", text)  # Q1 $71.60, median $73.345, Q3 $75.09

    def test_a_result_page_keeps_the_procedure_box(self):
        self.assertIn('data-door="/api/procedures"', self.client.get("/by-procedure/10006").text)

    def test_page_without_query_shows_the_box(self):
        response = self.client.get("/search")
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="q"', response.text)
        self.assertIn("autofocus", response.text)

    def test_page_renders_results(self):
        response = self.client.get("/search", params={"billing_code": "10006", "view": "rates"})
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("FINE NEEDLE ASPIRATION BX W/US GDN EA ADDL", html)
        self.assertIn("2 rates", html)
        self.assertIn("$69.85", html)
        self.assertIn("Minkowitz Consulting Pathologists PC", html)
        self.assertIn("11-3471596", html)
        self.assertIn("1396747861", html)
        self.assertIn("fidelis-ex_in-network.json", html)
        self.assertLess(html.index("$69.85"), html.index("$76.84"))

    def test_page_unknown_code_says_no_rates_found(self):
        response = self.client.get("/search", params={"billing_code": "99999"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("No rates for 99999 in the active index", response.text)

    def test_page_invalid_code_shows_friendly_message(self):
        response = self.client.get("/search", params={"billing_code": "<script>"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("isn’t a valid billing code", response.text)
        self.assertNotIn("<script>", response.text)

    def test_page_empty_code_asks_for_one(self):
        response = self.client.get("/by-procedure/%20%20")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Enter a billing code", response.text)


class IdentitySearchTest(unittest.TestCase):
    """#17: NPI and EIN boxes in the search form: an exact match, the same as the NPI / EIN column filters."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "rates.db"
        conn = sqlite3.connect(db_path)
        build_db(conn)
        conn.close()
        self.client = TestClient(create_app(db_path))

    def tearDown(self):
        self.tmp.cleanup()

    def page(self, **params):
        return self.client.get("/search", params={"billing_code": "10006", "view": "rates", **params}).text

    def test_the_page_offers_one_provider_box_for_a_name_npi_or_ein(self):  # #40: replaces the NPI and EIN boxes
        text = self.client.get("/by-procedure/10006").text
        self.assertIn('placeholder="Provider name, NPI or EIN"', text)
        self.assertIn('data-picker="/api/procedures/10006/providers?"', text)

    def test_npi_is_an_exact_match_and_the_page_says_it_is_narrowed(self):
        text = self.page(npi="1396747861")
        self.assertIn("2 rates match your filters", text)
        self.assertIn("Narrowed to</span> <strong>NPI 1396747861</strong>", text)
        self.assertIn('href="/by-procedure/10006?view=rates"', text)  # all providers again
        self.assertIn("npi=1396747861&amp;npi_op=equals", text)  # sort links carry the exact match

    def test_part_of_an_npi_is_not_a_match(self):
        text = self.page(npi="139674786")
        self.assertIn("No provider has NPI 139674786 in the loaded files.", text)
        self.assertNotIn('<table id="results"', text)

    def test_ein_matches_with_or_without_the_dash(self):
        self.assertIn("2 rates match your filters", self.page(ein="11-3471596"))
        self.assertIn("2 rates match your filters", self.page(ein="113471596"))
        self.assertIn("No rates for 10006 from NPI 1396747861 and EIN 45-0934668.",
                      self.page(npi="1396747861", ein="45-0934668"))

    def test_an_exact_filter_narrows_but_a_partial_one_is_a_chip(self):
        self.assertIn("<strong>NPI 1396747861</strong>", self.page(npi="1396747861", npi_op="equals"))
        partial = self.page(npi="13967", npi_op="contains")
        self.assertNotIn("Narrowed to", partial)
        self.assertIn('<span class="chip">NPI contains 13967<a', partial)
        self.assertIn("2 rates match your filters", partial)  # a partial filter-row value is not validated

    def test_invalid_npi_or_ein_gets_a_friendly_message(self):
        cases = {
            ("npi", "12ab"): "“12ab” isn’t a valid NPI. An NPI is a number of up to 10 digits, such as 1801055561.",
            ("npi", "12345678901"): "“12345678901” isn’t a valid NPI.",
            ("ein", "11-34"): "“11-34” isn’t a valid EIN. Use 9 digits, with or without the dash, such as 45-0934668.",
            ("ein", "1-13471596"): "“1-13471596” isn’t a valid EIN.",
        }
        for (name, value), message in cases.items():
            with self.subTest(name=name, value=value):
                text = self.page(**{name: value})
                self.assertIn(message, text)
                self.assertNotIn('<table id="results"', text)
                api = self.client.get("/api/search", params={"billing_code": "10006", name: value})
                self.assertEqual(api.status_code, 422)
                self.assertTrue(api.json()["detail"].startswith(message.split(" An")[0].split(" Use")[0]))

    def test_api_npi_and_ein_are_exact_matches(self):
        body = self.client.get("/api/search", params={"billing_code": "10006", "npi": "1396747861",
                                                      "ein": "113471596"}).json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["filters"]["npi_op"], "equals")
        partial = self.client.get("/api/search", params={"billing_code": "10006", "npi": "139674786"}).json()
        self.assertEqual(partial["total"], 0)
        contains = self.client.get("/api/search", params={"billing_code": "10006", "npi": "13967",
                                                          "npi_op": "contains"}).json()
        self.assertEqual(contains["total"], 2)


class PlanSearchPageTest(unittest.TestCase):
    """#1: pick a plan, and the search covers only its in-network file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "rates.db"
        conn = sqlite3.connect(db_path)
        build_db(conn)
        conn.close()
        self.client = TestClient(create_app(db_path))

    def tearDown(self):
        self.tmp.cleanup()

    def page(self, **params):
        return self.client.get("/by-procedure/10006", params={"view": "rates", **params}).text

    def test_search_in_offers_networks_not_plans(self):  # #43: a plan is only ever a way to pick its file
        text = self.client.get("/by-procedure/10006").text
        self.assertEqual(re.findall(r'<input type="radio" name="in" value="([^"]*)"', text), ["", "file-1", "file-2"])
        self.assertNotIn('value="plan-', text)

    def test_a_plan_limits_the_search_to_its_file_and_says_so(self):
        text = self.page(**{"in": "plan-2"})
        self.assertIn("Minkowitz Consulting Pathologists PC", text)
        self.assertNotIn("Samaritan Medical Center", text)
        self.assertIn("<strong>Searching:</strong> the Ambetter network (fidelis-ex, 2 plans)", text)
        self.assertIn("Ambetter from Fidelis Care Bronze (25303NY005) is in the Ambetter network (fidelis-ex);"
                      " every plan there shares these rates.", text)
        self.assertIn("in=plan-2", text)  # sort links keep the plan
        self.assertIn('<input type="radio" name="in" value="file-2" checked', text)

    def test_an_old_plan_link_means_the_same_plan(self):
        text = self.client.get("/search", params={"billing_code": "10006", "plan": "2", "source": "1"}).text
        self.assertNotIn("Samaritan Medical Center", text)
        self.assertIn('<input type="radio" name="in" value="file-2" checked', text)

    def test_the_only_plan_in_a_file(self):
        text = self.page(**{"in": "plan-1"})
        self.assertIn("<strong>Searching:</strong> the Essential Plan network (fidelis-es, 1 plan)</p>", text)
        self.assertIn("Essential Plan 1 (25303EP000) is in the Essential Plan network (fidelis-es).", text)

    def test_unknown_plan_says_so_and_searches_every_plan(self):
        for plan in ("plan-999", "plan-abc"):
            with self.subTest(plan=plan):
                text = self.page(**{"in": plan})
                self.assertIn("That plan or file isn’t in the active index, so all networks are searched.", text)
                self.assertIn("Samaritan Medical Center", text)
                self.assertIn('<input type="radio" name="in" value="" checked', text)

    def test_api_takes_a_plan(self):
        body = self.client.get("/api/search", params={"billing_code": "10006", "plan": 2,
                                                      "source_file_id": 1}).json()
        self.assertEqual([r["business_name"] for r in body["results"]], ["Minkowitz Consulting Pathologists PC"])
        self.assertEqual((body["plan"]["plan_id"], body["plan"]["source_file_ids"]), ("25303NY005", [2]))
        self.assertNotIn("plan", self.client.get("/api/search", params={"billing_code": "10006"}).json())
        for plan in ("999", "abc"):
            with self.subTest(plan=plan):
                bad = self.client.get("/api/search", params={"billing_code": "10006", "plan": plan})
                self.assertEqual(bad.status_code, 422)
                self.assertEqual(bad.json()["detail"], "That plan isn’t in the loaded data. Type a plan name or ID "
                                                       "and pick it from the list.")


class CodeTypePageTest(unittest.TestCase):
    """731 is two codes: CPT anesthesia (fidelis-ex) and RC ECG monitoring (fidelis-es)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "rates.db"
        conn = sqlite3.connect(db_path)
        build_db(conn)
        add_code_731(conn)
        conn.close()
        self.client = TestClient(create_app(db_path))

    def tearDown(self):
        self.tmp.cleanup()

    def test_page_shows_one_type_and_offers_the_other(self):
        text = self.client.get("/search", params={"billing_code": "731"}).text
        self.assertIn("ANES UPR GI NDSC PX NOS", text)
        self.assertNotIn("Med Alliance Medical Health Services", text)
        self.assertIn("code_type=RC", text)
        self.assertIn("EKG/ECG Electrocardiogram Holter monitor", text)  # named in the chooser

    def test_page_shows_the_chosen_type(self):
        text = self.client.get("/search", params={"billing_code": "731", "code_type": "RC", "view": "rates"}).text
        self.assertIn("Med Alliance Medical Health Services", text)
        self.assertNotIn("Chinese American IPA", text)
        self.assertIn("code_type=RC&amp;view=rates&amp;sort=", text)  # sort links keep the chosen type

    def test_no_chooser_for_a_number_with_one_type(self):
        self.assertNotIn("code_type=", self.client.get("/search", params={"billing_code": "10006"}).text)

    def test_api_takes_a_code_type_and_lists_the_types(self):
        body = self.client.get("/api/search", params={"billing_code": "731", "code_type": "RC"}).json()
        self.assertEqual(body["total"], 1)
        self.assertEqual([t["billing_code_type"] for t in body["code_types"]], ["CPT", "RC"])
        default = self.client.get("/api/search", params={"billing_code": "731"}).json()
        self.assertEqual(default["code_type"], "CPT")
        self.assertEqual(default["total"], 1)

    def test_code_list_links_each_type_to_its_own_search(self):
        text = self.client.get("/codes", params={"q": "731"}).text
        self.assertIn('href="/by-procedure/731?code_type=RC"', text)
        self.assertIn('href="/by-procedure/731?code_type=CPT"', text)


class PriceGroupPageTest(unittest.TestCase):
    """Chips that narrow the results to one (modifiers, place of service) group, so prices compare like with like."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "rates.db"
        conn = sqlite3.connect(db_path)
        build_db(conn)
        add_code_10040(conn)
        add_code_99213(conn)
        add_code_731(conn)
        add_code_97151(conn)
        conn.close()
        self.db_path = db_path
        self.client = TestClient(create_app(db_path))

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def plain(html):
        return " ".join(re.sub(r"<[^>]+>", " ", html).split())

    def options(self, text):
        """The group picker's options as text: label, then count and median."""
        return [self.plain(o) for o in re.findall(r'<a class="group-option[^"]*"[^>]*>(.*?)</a>', text, re.S)]

    def rows(self, text):
        """The Compare Groups table's cells, per row."""
        return [[self.plain(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)]
                for r in re.findall(r'<tr class="group-row[^"]*"[^>]*>(.*?)</tr>', text, re.S)]

    def page(self, **params):
        return self.client.get("/search", params={"billing_code": "10040", **params}).text

    def test_picker_lists_each_group_with_its_count_and_median(self):
        text = self.page(group_by="none")
        self.assertIn("All 4 groups", self.plain(re.search(r'<summary class="picker-btn">(.*?)</summary>', text).group(1)))
        self.assertEqual(self.options(text), [
            "mod 51 · office (11) 23 rates · $145.97",
            "mod 59 · office (11) 16 rates · $69.67",
            "mods 79, 51 · office (11) 16 rates · $144.12",
            "mod XS · office (11) 7 rates · $145.97",
        ])

    def test_group_links_set_both_group_filters_and_keep_the_rest(self):
        text = self.page(provider="teladoc", sort="provider", dir="desc", source="1")
        self.assertIn('href="/by-procedure/10040?in=file-1&amp;provider=teladoc'
                      '&amp;modifiers=79%2C51&amp;modifiers_op=equals&amp;service_codes=11&amp;service_codes_op=equals'
                      '&amp;view=stats&amp;sort=provider&amp;dir=desc"', text)

    def test_choosing_a_group_marks_it_and_all_groups_clears_only_the_group(self):
        text = self.page(provider="mount", modifiers="79,51", modifiers_op="equals",
                         service_codes="11", service_codes_op="equals")
        active = re.findall(r'<a class="group-option on"[^>]*>(.*?)</a>', text, re.S)
        self.assertEqual(len(active), 1)
        self.assertIn("79, 51", active[0])
        self.assertIn('<a class="group-all" href="/by-procedure/10040?provider=mount">', text)
        self.assertIn("11 rates match your filters", text)

    def test_one_group_needs_no_picker_and_nothing_to_compare(self):
        text = self.client.get("/search", params={"billing_code": "10006"}).text
        self.assertEqual(self.options(text), [])
        self.assertNotIn(">Compare Groups<", text)

    def test_place_of_service_makes_separate_groups(self):
        text = self.client.get("/search", params={"billing_code": "99213", "group_by": "none"}).text
        self.assertEqual(self.options(text), [
            "mod 25 · office (11) 4 rates · $231.30",
            "mod 25 · telehealth (02) 2 rates · $59.10",
            "mods 25, 95 · telehealth (02), telehealth at home (10) 1 rate · $51.45",
        ])

    def test_no_modifier_group_uses_the_empty_operator(self):
        text = self.client.get("/search", params={"billing_code": "97151", "group_by": "none"}).text
        self.assertEqual(self.options(text)[0], "no modifier · office (11) 21 rates · $19.75")
        self.assertIn('href="/by-procedure/97151?modifiers_op=empty&amp;service_codes=11'
                      '&amp;service_codes_op=equals&amp;group_by=none&amp;view=stats"', text)
        chosen = self.client.get("/search", params={"billing_code": "97151", "modifiers_op": "empty",
                                                    "service_codes": "11", "service_codes_op": "equals"}).text
        self.assertIn("21 rates match your filters", chosen)
        self.assertRegex(chosen, r'<a role="tab" aria-selected="true" href="[^"]*view=stats">Statistics</a>')

    def test_many_groups_show_the_biggest_and_an_expander(self):
        client = TestClient(create_app(self.db_path, group_limit=2))
        text = client.get("/search", params={"billing_code": "10040"}).text
        self.assertIn("Show all 4 groups", text)
        self.assertEqual(len(re.findall(r'<tbody class="more-groups" hidden>', text)), 1)
        self.assertEqual(len(self.rows(text)), 4)  # two of them behind the expander
        self.assertEqual(len(self.options(text)), 4)  # the picker lists them all

    def test_groups_and_modifier_cells_explain_each_modifier_on_hover(self):
        self.assertIn('title="79 · unrelated procedure in the follow-up period — surgical role', self.page())
        rates = self.page(view="rates")
        self.assertIn('<td data-col="modifiers" title="XS · separate structure', rates)
        self.assertIn("Same for every row: professional · outpatient · office (11)", rates)  # one place: folded

    def test_dropdown_shows_comma_joined_codes_with_a_space(self):
        text = self.client.get("/search", params={"billing_code": "99213", "view": "rates"}).text
        self.assertIn('<option value="02,10">02, 10</option>', text)

    def test_api_returns_the_groups(self):
        body = self.client.get("/api/search", params={"billing_code": "10040", "modifiers": "51",
                                                      "modifiers_op": "equals"}).json()
        self.assertEqual(body["total"], 23)
        self.assertEqual([(g["modifiers"], g["service_codes"], g["rate_count"]) for g in body["price_groups"]],
                         [("51", "11", 23), ("59", "11", 16), ("79,51", "11", 16), ("XS", "11", 7)])

    def test_api_takes_the_empty_operators(self):
        body = self.client.get("/api/search", params={"billing_code": "731", "code_type": "RC",
                                                      "modifiers_op": "empty"}).json()
        self.assertEqual(body["total"], 1)
        body = self.client.get("/api/search", params={"billing_code": "10040", "provider_op": "not_empty"}).json()
        self.assertEqual(body["total"], 61)

    def test_page_offers_the_empty_operators(self):
        text = self.page(view="rates")
        self.assertIn('<option value="empty">is empty</option>', text)
        self.assertIn('<option value="not_empty">is not empty</option>', text)


class ProcedurePageTest(unittest.TestCase):
    """#14: the procedure on revenue-code rates, and hospital (facility) rates in a CPT search."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "rates.db"
        conn = sqlite3.connect(db_path)
        build_db(conn)
        add_code_99213(conn)
        add_clinic_99213(conn)
        add_code_260(conn)
        conn.close()
        self.client = TestClient(create_app(db_path))

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def plain(html):
        return " ".join(re.sub(r"<[^>]+>", " ", html).split())

    def options(self, text):
        """The group picker's options as text: label, then count and median."""
        return [self.plain(o) for o in re.findall(r'<a class="group-option[^"]*"[^>]*>(.*?)</a>', text, re.S)]

    def rows(self, text):
        """The Compare Groups table's cells, per row."""
        return [[self.plain(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)]
                for r in re.findall(r'<tr class="group-row[^"]*"[^>]*>(.*?)</tr>', text, re.S)]

    def page(self, code, **params):
        return self.client.get("/search", params={"billing_code": code, **params}).text

    def test_rc_groups_name_the_procedure(self):
        options = self.options(self.page("260", group_by="none"))
        self.assertIn("96365 · THER/PROPH/DIAG IV INF INIT · no modifier · emergency room (23) 2 rates · $452.87", options)
        self.assertIn("96361 · no modifier · emergency room (23) 3 rates · $215.43", options)  # no name here
        self.assertIn("no procedure code · no modifier · on-campus hospital outpatient (22) 1 rate · $7.96", options)

    def test_rc_results_have_a_filterable_procedure_column(self):
        text = self.page("260", view="rates")
        self.assertIn('data-col="procedure"', text)
        self.assertIn('<select form="filters" name="procedure"', text)
        self.assertIn('<td data-col="procedure">96365<div class="muted">THER/PROPH/DIAG IV INF INIT</div></td>', text)
        self.assertIn("procedure=96365&amp;procedure_op=equals", text)  # the group picks the procedure
        chosen = self.page("260", procedure="96365", procedure_op="equals", service_codes="23",
                           service_codes_op="equals")
        self.assertIn("2 rates match your filters", chosen)
        self.assertIn('<div class="big">$452.87</div>', chosen)
        self.assertNotIn('data-col="procedure"', self.page("10006", view="rates"))
        self.assertNotIn('data-col="billed_as"', self.page("260", view="rates"))

    def test_a_cpt_search_shows_hospital_rates_billed_under_a_revenue_code(self):
        text = self.page("99213", view="rates", group_by="none")
        self.assertIn("16 rates", text)
        self.assertIn("RC 0510 · Clinic · no modifier · FQHC (50) 3 rates · $118.20", self.options(text))
        self.assertIn("RC 0521 · no modifier · FQHC (50) 4 rates · $90.95", self.options(text))
        self.assertIn("mod 25 · office (11) 4 rates · $231.30", self.options(text))
        self.assertIn('<td data-col="billed_as">RC 0510 · Clinic</td>', text)
        self.assertIn('<td data-col="billed_as">CPT 99213</td>', text)
        self.assertIn("includes 9 hospital rates billed under a revenue code", text)
        self.assertIn("billed_as=RC+0510&amp;billed_as_op=equals", text)

    def test_a_facility_group_gets_its_own_stats(self):
        text = self.page("99213", billed_as="RC 0521", billed_as_op="equals", service_codes="50",
                         service_codes_op="equals", modifiers_op="empty", group_by="none")
        self.assertIn('<div class="big">$90.95</div>', text)

    def test_facility_rates_can_be_left_out(self):
        text = self.page("99213", include_facility="false", view="rates")
        self.assertIn("7 rates", text)
        self.assertNotIn('data-col="billed_as"', text)
        self.assertIn("include_facility=false&amp;view=", text)  # links keep the choice
        self.assertIn("9 hospital rates hidden", text)
        both = self.client.get("/search", params=[("billing_code", "99213"), ("include_facility", "false"),
                                                  ("include_facility", "true")]).text  # hidden + checked box
        self.assertIn("16 rates", both)

    def test_api_takes_include_facility(self):
        body = self.client.get("/api/search", params={"billing_code": "99213"}).json()
        self.assertEqual((body["total"], body["include_facility"]), (16, True))
        self.assertIn(("RC 0510", "99213", "22"),
                      [(g["billed_as"], g["procedure"], g["service_codes"]) for g in body["price_groups"]])
        self.assertEqual(body["procedure_names"], {"99213": "OFFICE/OUTPATIENT VISIT EST"})
        self.assertEqual(body["department_names"], {"510": "Clinic"})
        own = self.client.get("/api/search", params={"billing_code": "99213", "include_facility": "false"}).json()
        self.assertEqual(own["total"], 7)


class StatsPageTest(unittest.TestCase):
    """Statistics and a distribution chart, only for results that are one like-for-like group."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "rates.db"
        conn = sqlite3.connect(db_path)
        build_db(conn)
        add_code_10040(conn)
        add_code_97151(conn)
        conn.close()
        self.client = TestClient(create_app(db_path))

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def plain(html):
        return " ".join(re.sub(r"<[^>]+>", " ", html).split())

    def options(self, text):
        """The group picker's options as text: label, then count and median."""
        return [self.plain(o) for o in re.findall(r'<a class="group-option[^"]*"[^>]*>(.*?)</a>', text, re.S)]

    def rows(self, text):
        """The Compare Groups table's cells, per row."""
        return [[self.plain(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)]
                for r in re.findall(r'<tr class="group-row[^"]*"[^>]*>(.*?)</tr>', text, re.S)]

    def page(self, code, **params):
        return self.client.get("/search", params={"billing_code": code, "group_by": "none", **params}).text

    def tiles(self, text):
        return dict(re.findall(r'<div class="stat"><dt>(.*?)</dt><dd>(.*?)</dd></div>', text, re.S))

    def stats_panel(self, text):
        return text[text.index('id="p-stats"'):text.index("</section>", text.index('id="p-stats"'))]

    def test_one_group_shows_the_median_first_then_spread_and_a_chart(self):
        text = self.page("97151", modifiers_op="empty", service_codes="11", service_codes_op="equals")
        self.assertIn('<div class="big">$19.75</div><div class="biglbl">median of 21 rates</div>', text)
        self.assertEqual(self.tiles(text), {"Middle half (Q1–Q3)": "$10.00 – $23.00", "Range": "$1.00 – $26.69",
                                            "Providers": "4", "Distinct prices": "11", "EINs · NPIs": "4 · 4"})
        self.assertIn('class="chartbox"', text)
        self.assertIn("one price for one provider group and NPI", text)  # the weighting note
        self.assertNotIn("mean", self.stats_panel(text).lower().replace("median", ""))
        self.assertIn("No averages are shown: an average can be a price nobody negotiated. The median is the standard "
                      "median (with an even number of values it is the midpoint of the two middle prices).", text)
        self.assertNotIn("Statistics compare like with like", text)

    def test_percentiles_name_the_tail_they_cannot_estimate(self):
        text = self.page("10006")
        pcts = dict(re.findall(r'<div class="stat pct"(?: title="[^"]*")?><dt>(.*?)</dt><dd(?: class="na")?>(.*?)</dd>',
                               text))
        self.assertEqual(pcts, {"P1": "$69.92", "P90": "$76.14", "P95": "$76.49", "P99": "$76.77", "P99.9": "n/a",
                                "P99.99": "n/a"})
        self.assertIn('title="P99.9 needs at least 1,000 rates; these are 2."', text)

    def test_a_code_with_one_group_shows_stats_without_choosing(self):
        text = self.page("10006")
        self.assertIn('<div class="big">$73.35</div>', text)  # (69.85 + 76.84) / 2, rounded for display
        self.assertIn('class="chartbox"', text)

    def test_identical_rates_say_so_instead_of_a_chart(self):
        text = self.page("10040", modifiers="51", modifiers_op="equals")
        self.assertIn("All 23 rates are $145.97", text)
        self.assertNotIn('class="chartbox"', text)

    def test_mixed_groups_compare_per_group_and_statistics_ask_for_one(self):
        stats = self.page("10040", view="stats")
        self.assertEqual(self.tiles(stats), {})
        self.assertNotIn('class="chartbox"', stats)
        self.assertIn("Statistics compare like with like.", stats)
        text = self.page("10040")
        rows = self.rows(text)
        self.assertEqual(rows[1], ["mod 59", "office (11)", "$69.67", "$69.67", "$69.67", "$140.17", "$140.17", "16", "14", "15", "0.48×", "—"])
        self.assertIn('data-href="/by-procedure/10040?modifiers=59&amp;modifiers_op=equals'
                      '&amp;service_codes=11&amp;service_codes_op=equals&amp;group_by=none&amp;view=stats"', text)
        self.assertIn("No rate is published for this code without a modifier.", text)
        self.assertIn('>Modifier<span class="arrow"', text)  # each half of a group in its own column
        self.assertIn('>Place Of Service<span class="arrow"', text)

    def test_no_missing_base_rate_line_when_there_is_one(self):
        text = self.page("97151")
        self.assertIn('id="p-compare"', text)
        self.assertNotIn("No rate is published for this code without a modifier.", text)

    def test_api_returns_stats_and_stats_by_group(self):
        body = self.client.get("/api/search", params={"billing_code": "10040", "modifiers": "59",
                                                      "modifiers_op": "equals"}).json()
        self.assertEqual((body["stats"]["count"], body["stats"]["median"]), (16, 69.67))
        self.assertEqual(sum(b["count"] for b in body["stats"]["histogram"]), 16)
        self.assertEqual(body["stats"]["p99"], 140.17)
        self.assertIsNone(body["stats"]["p99_9"])
        self.assertIsNone(body["stats"]["p99_99"])
        self.assertEqual([(g["modifiers"], g["median"]) for g in body["stats_by_group"]],
                         [("51", 145.97), ("59", 69.67), ("79,51", 144.12), ("XS", 145.97)])
        self.assertNotIn("mean", body["stats"])
        empty = self.client.get("/api/search", params={"billing_code": "99999"}).json()
        self.assertIsNone(empty["stats"])
        self.assertEqual(empty["stats_by_group"], [])


class PriceDriversPageTest(unittest.TestCase):
    """#22A relative price per group, #22A+ what explains price, in the mixed-groups summary."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "rates.db"
        conn = sqlite3.connect(db_path)
        build_db(conn)
        add_code_10040(conn)
        add_code_97151(conn)
        conn.close()
        self.client = TestClient(create_app(db_path))

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def plain(html):
        return " ".join(re.sub(r"<[^>]+>", " ", html).split())

    def options(self, text):
        """The group picker's options as text: label, then count and median."""
        return [self.plain(o) for o in re.findall(r'<a class="group-option[^"]*"[^>]*>(.*?)</a>', text, re.S)]

    def rows(self, text):
        """The Compare Groups table's cells, per row."""
        return [[self.plain(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)]
                for r in re.findall(r'<tr class="group-row[^"]*"[^>]*>(.*?)</tr>', text, re.S)]

    def test_group_table_shows_each_groups_price_relative_to_the_reference(self):
        text = self.client.get("/search", params={"billing_code": "10040", "group_by": "none"}).text
        self.assertIn('title="Median vs the reference group, mod 51 · office (11), 23 rates: no professional group without a '
                      'modifier, so the professional group with the most rates (Count by). For this code only; '
                      'differences mix the modifier/place effect with which providers publish each group"', text)
        rows = self.rows(text)
        self.assertEqual(rows[0][-2], "1.00× ref")
        self.assertEqual(rows[1][-2], "0.48×")  # 59: $69.67 / $145.97

    def test_what_explains_price_is_one_bar_adding_to_100(self):
        text = self.client.get("/search", params={"billing_code": "10040"}).text
        legend = self.plain(re.search(r'<div class="xlegend">(.*?)</div>', text, re.S).group(1))
        self.assertEqual(legend, "Modifier 63% Place of service adds 0% Other, mostly which provider 37%")
        self.assertIn('aria-label="Modifier 63%, Place of service adds 0%, Other, mostly which provider 37%"', text)
        self.assertIn("measured in percentages", text)
        self.assertNotIn("log price", text)  # plain words on the page
        self.assertNotIn('class="card explain"', self.client.get("/search", params={"billing_code": "97151"}).text)

    def test_api_returns_relative_prices_and_what_explains_price(self):
        body = self.client.get("/api/search", params={"billing_code": "10040"}).json()
        self.assertEqual([(g["modifiers"], g["reference"]) for g in body["stats_by_group"]][:2],
                         [("51", True), ("59", False)])
        self.assertAlmostEqual(body["stats_by_group"][1]["relative"], 69.67 / 145.97)
        self.assertEqual(body["price_explained"]["count"], 62)
        self.assertAlmostEqual(body["price_explained"]["modifier"], 0.634813, places=5)
        self.assertIsNone(self.client.get("/api/search", params={"billing_code": "97151"}).json()["price_explained"])


class OutlierPageTest(unittest.TestCase):
    """#25: outliers (Tukey's fences) in the stats panel, the chart and the per-group table."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "rates.db"
        conn = sqlite3.connect(db_path)
        build_db(conn)
        add_code_99213(conn)
        add_code_97151(conn)
        conn.close()
        self.client = TestClient(create_app(db_path))

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def plain(html):
        return " ".join(re.sub(r"<[^>]+>", " ", html).split())

    def options(self, text):
        """The group picker's options as text: label, then count and median."""
        return [self.plain(o) for o in re.findall(r'<a class="group-option[^"]*"[^>]*>(.*?)</a>', text, re.S)]

    def rows(self, text):
        """The Compare Groups table's cells, per row."""
        return [[self.plain(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)]
                for r in re.findall(r'<tr class="group-row[^"]*"[^>]*>(.*?)</tr>', text, re.S)]

    def office(self, **params):
        return self.client.get("/search", params={"billing_code": "99213", "modifiers": "25", "modifiers_op": "equals",
                                                  "service_codes": "11", "service_codes_op": "equals",
                                                  "group_by": "none", **params}).text

    def outlier_buttons(self, text):
        return self.plain(re.search(r'<div class="outbtns">(.*?)</div>', text, re.S).group(1))

    def test_rail_counts_outliers_beyond_each_fence(self):
        text = self.office()  # fences 128.075 and 293.235; 66.14 below
        self.assertEqual(self.outlier_buttons(text), "1 below $128.08 Show these 0 above $293.24")
        # Said in plain words, never by the statistician's name (nobody says "Tukey's fences").
        self.assertNotIn("Tukey", text)
        self.assertNotIn("fence", self.plain(text).lower())
        self.assertIn("1.5 times the Q1–Q3 range", self.plain(text))

    def test_clicking_a_count_shows_those_rates(self):
        link = re.search(r'<a class="outbtn" href="([^"]+)"', self.office()).group(1)
        self.assertIn("rate_max=128.07", link)  # the highest cent below the fence
        self.assertIn("modifiers=25", link)  # keeps the group
        self.assertIn("view=rates", link)
        shown = self.client.get(link.replace("&amp;", "&")).text
        self.assertIn("1 rate matches your filters", shown)
        self.assertIn("$66.14", shown)
        self.assertIn('<span class="chip">Rate ≤ $128.07<a', shown)

    def test_a_negative_low_fence_means_no_low_outliers(self):
        text = self.client.get("/search", params={"billing_code": "97151", "modifiers_op": "empty", "group_by": "none",
                                                  "service_codes": "11", "service_codes_op": "equals"}).text
        self.assertEqual(self.outlier_buttons(text), "No low outliers possible 0 above $42.50")

    def test_chart_draws_fences_inside_the_axis_and_marks_bars_beyond_them(self):
        text = self.office()
        self.assertEqual(text.count('class="fence"'), 1)  # the low fence; the high one is past the max
        self.assertEqual(text.count('class="bar outlier"'), 1)  # the bar holding $66.14

    def test_group_table_has_quartiles_and_outliers(self):
        text = self.client.get("/search", params={"billing_code": "99213", "group_by": "none"}).text
        self.assertIn('>Q1<span class="arrow"', text)
        self.assertIn('>Q3<span class="arrow"', text)
        rows = self.rows(text)
        self.assertEqual(rows[0][2:], ["$66.14", "$190.01", "$231.30", "$231.30", "$231.30", "4", "2", "4", "1.00× ref", "1 low"])
        self.assertEqual(rows[1][-1], "—")

    def test_api_returns_fences_and_counts(self):
        body = self.client.get("/api/search", params={"billing_code": "99213", "modifiers": "25",
                                                      "modifiers_op": "equals", "service_codes": "11",
                                                      "service_codes_op": "equals"}).json()
        self.assertAlmostEqual(body["stats"]["low_fence"], 128.075)
        self.assertAlmostEqual(body["stats"]["high_fence"], 293.235)
        self.assertEqual((body["stats"]["low_outliers"], body["stats"]["high_outliers"]), (1, 0))
        office = body["stats_by_group"][0]
        self.assertEqual((office["low_outliers"], office["high_outliers"]), (1, 0))
        self.assertAlmostEqual(office["q1"], 190.01)


class SearchRedesignTest(unittest.TestCase):
    """#26: one-line search bar, results header, scope bar, and three tabs (Compare Groups, Statistics, Rates)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "rates.db"
        conn = sqlite3.connect(db_path)
        build_db(conn)
        add_code_10040(conn)
        add_code_99213(conn)
        add_clinic_99213(conn)
        add_code_731(conn)
        conn.close()
        self.client = TestClient(create_app(db_path))

    def tearDown(self):
        self.tmp.cleanup()

    def page(self, code="10040", **params):
        return self.client.get("/search", params={"billing_code": code, **params}).text

    def section(self, text, start, end):
        i = text.index(start)
        return text[i:text.index(end, i)]

    @staticmethod
    def plain(html):
        return " ".join(re.sub(r"<[^>]+>", " ", html).split())

    # Search bar
    def test_no_big_title_or_lede_and_no_datalists(self):
        text = self.client.get("/search").text
        self.assertNotIn("<h1>Price Transparency Explorer</h1>", text)
        self.assertNotIn('class="lede"', text)
        self.assertNotIn("<datalist", self.page())
        self.assertNotIn("<datalist", self.page(view="rates"))

    def test_searching_states_the_scope_in_words(self):
        searching = lambda t: self.plain(self.section(t, '<p class="searching"', "</p>"))
        self.assertEqual(searching(self.page()), "Searching: All networks (fidelis-es + fidelis-ex, 3 plans) in the active index")
        self.assertEqual(searching(self.page(source="2")), "Searching: the Ambetter network (fidelis-ex, 2 plans)")
        self.assertEqual(searching(self.page(plan="2")), "Searching: the Ambetter network (fidelis-ex, 2 plans)")
        self.assertIn("hospital rates hidden", self.plain(self.section(self.page("99213", include_facility="false"),
                                                                       '<p class="meta"', "</p>")))

    # Results header
    def test_header_names_the_code_and_switches_type(self):
        text = self.page("731")
        title = self.plain(self.section(text, '<h1 class="code"', "</h1>"))
        self.assertTrue(title.startswith("731 Anesthesia for upper gastrointestinal endoscopic procedures"), title)
        switch = self.section(text, '<nav class="typeswitch"', "</nav>")
        self.assertIn('aria-current="true"', switch)
        self.assertIn("code_type=RC", switch)
        self.assertIn('title="EKG/ECG Electrocardiogram Holter monitor"', switch)

    def test_meta_line_counts_rates_files_and_hospital_rates_with_a_hide_link(self):
        meta = self.section(self.page("99213"), '<p class="meta"', "</p>")
        self.assertIn("16 rates", self.plain(meta))
        self.assertIn("includes 9 hospital rates billed under a revenue code", self.plain(meta))
        self.assertIn("include_facility=false", meta)

    # Scope bar
    def test_group_picker_lists_every_group_and_links_to_its_statistics(self):
        picker = self.section(self.page(), '<details class="pop picker"', "</details>")
        self.assertIn("All 4 groups", self.plain(picker))
        options = re.findall(r'<a class="group-option[^"]*" href="([^"]+)"[^>]*>(.*?)</a>', picker, re.S)
        # Most provider offers first: 59 has 15 offers in 16 rows, 51 has 2 in 23.
        self.assertEqual([self.plain(o[1]) for o in options][:2],
                         ["mod 59 · office (11) 15 provider prices · $69.67", "mod 51 · office (11) 2 provider prices · $145.97"])
        self.assertIn("modifiers=59&amp;modifiers_op=equals&amp;service_codes=11&amp;service_codes_op=equals"
                      "&amp;view=stats", options[0][0])

    def test_picked_group_names_itself_in_the_picker_not_as_chips(self):
        text = self.page(modifiers="59", modifiers_op="equals", service_codes="11", service_codes_op="equals")
        self.assertIn("mod 59 · office (11)", self.plain(self.section(text, '<summary class="picker-btn"', "</summary>")))
        self.assertNotIn('class="chip"', text)

    def test_count_by_is_a_segmented_control_defaulting_to_provider(self):
        seg = self.section(self.page(), '<nav class="seg countby"', "</nav>")
        self.assertEqual(re.findall(r'aria-current="true"[^>]*>([^<]+)<', seg), ["Provider"])
        self.assertIn("group_by=npi", seg)
        self.assertIn("group_by=none", seg)
        self.assertEqual(re.findall(r'aria-current="true"[^>]*>([^<]+)<',
                                    self.section(self.page(group_by="none"), '<nav class="seg countby"', "</nav>")),
                         ["Every row"])

    def test_every_active_filter_is_a_removable_chip_with_one_clear_all(self):
        text = self.page(npi="1265164024", rate_min="60", rate_max="100", provider="mmc", provider_op="contains")
        chips = re.findall(r'<span class="chip">(.*?)<a href="([^"]+)"', text, re.S)
        labels = [self.plain(c[0]) for c in chips]
        self.assertEqual(labels, ["Provider contains mmc", "Rate $60.00–$100.00"])  # the NPI narrows the page instead
        provider_removed = chips[0][1]
        self.assertNotIn("provider=", provider_removed)
        self.assertIn("npi=1265164024", provider_removed)
        self.assertNotIn("rate_", chips[1][1])
        clear = re.search(r'<a class="clear-all" href="([^"]+)"', text).group(1)
        self.assertEqual(clear, "/by-procedure/10040")
        self.assertEqual(text.count("Clear all"), 1)
        self.assertIn("<strong>NPI 1265164024</strong>", text)

    # Tabs
    def test_tabs_default_to_compare_for_many_groups_and_statistics_for_one(self):
        selected = lambda t: re.findall(r'<a role="tab" aria-selected="true"[^>]*>(.*?)<', t)
        self.assertEqual(selected(self.page()), ["Compare Groups"])
        self.assertEqual(selected(self.page("10006")), ["Statistics"])
        self.assertNotIn(">Compare Groups<", self.page("10006"))  # one group: nothing to compare
        self.assertEqual(selected(self.page(view="rates")), ["Rates"])
        self.assertEqual(selected(self.page(view="bogus")), ["Compare Groups"])

    def test_only_the_active_tab_is_rendered(self):
        compare = self.page()
        self.assertIn('id="p-compare"', compare)
        self.assertNotIn('<table id="results"', compare)
        rates = self.page(view="rates")
        self.assertIn('<table id="results"', rates)
        self.assertNotIn('class="group-row"', rates)

    # Compare Groups
    def test_compare_shows_what_explains_price_as_one_bar(self):
        text = self.page()
        legend = self.plain(self.section(text, '<div class="xlegend"', "</div>\n"))
        self.assertEqual(legend, "Modifier 63% Place of service adds 0% Other, mostly which provider 37%")

    def test_compare_rows_count_by_the_chosen_unit_and_open_statistics(self):
        text = self.page()
        rows = re.findall(r'<tr class="group-row[^"]*" data-href="([^"]+)"[^>]*>(.*?)</tr>', text, re.S)
        self.assertEqual(len(rows), 4)
        cells = [self.plain(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", rows[0][1], re.S)]
        # modifier, place of service, min, Q1, median, Q3, max, provider prices, providers, clinicians (distinct
        # NPIs), x ref, outliers
        self.assertEqual(cells[:2], ["mod 59", "office (11)"])
        # The reference by provider offers (#44): mod 59 · office, 15 offers, over mod 51 · office's 23 rates but 2 offers.
        self.assertEqual(cells[2:], ["$69.67", "$69.67", "$69.67", "$121.84", "$140.17", "15", "14", "15", "1.00× ref", "—"])
        self.assertIn("view=stats", rows[0][0])
        self.assertNotIn("<svg", rows[0][1])  # no distribution strip: the numbers say it
        head = self.compare_head(text)
        self.assertEqual(head[2:7], ["Min", "Q1", "Median", "Q3", "Max"])
        self.assertEqual(head[8:10], ["Providers", "Clinicians"])
        self.assertEqual(len(re.findall(r'<td class="num med">', rows[0][1])), 1)  # the median stands out
        none = self.page(group_by="none")
        first = re.findall(r'<tr class="group-row[^"]*"[^>]*>(.*?)</tr>', none, re.S)[0]
        self.assertEqual(self.plain(re.findall(r"<td[^>]*>(.*?)</td>", first, re.S)[7]), "23")

    def compare_rows(self, text):
        return [[self.plain(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)]
                for r in re.findall(r'<tr class="group-row[^"]*"[^>]*>(.*?)</tr>', text, re.S)]

    def compare_head(self, text):
        return [self.plain(h).rstrip("↕▲▼ ") for h in re.findall(r"<th[^>]*>(.*?)</th>", self.section(text, "<thead>", "</thead>"), re.S)]

    def test_compare_has_modifier_and_place_of_service_in_columns_of_their_own(self):
        text = self.page()
        self.assertEqual(self.compare_head(text)[:2], ["Modifier", "Place Of Service"])
        self.assertEqual(self.compare_rows(text)[0][:2], ["mod 59", "office (11)"])

    def test_compare_sorts_by_modifier_and_back(self):
        up = [r[0] for r in self.compare_rows(self.page(gsort="modifier"))]
        self.assertEqual(up, ["mod 51", "mod 59", "mods 79, 51", "mod XS"])
        down = [r[0] for r in self.compare_rows(self.page(gsort="modifier", gdir="desc"))]
        self.assertEqual(down, list(reversed(up)))

    def test_compare_sorts_by_every_number_column(self):
        money = lambda c: float(c.strip("$").replace(",", ""))
        for key, column, convert in (("min", 2, money), ("median", 4, money), ("max", 6, money),
                                     ("count", 7, int), ("providers", 8, int), ("clinicians", 9, int)):
            for direction in ("asc", "desc"):
                with self.subTest(key=key, direction=direction):
                    values = [convert(r[column]) for r in self.compare_rows(self.page(gsort=key, gdir=direction))]
                    self.assertEqual(values, sorted(values, reverse=direction == "desc"))

    def test_compare_headers_link_to_their_sort_and_say_the_current_one(self):
        text = self.page()
        head = self.section(text, "<thead>", "</thead>")
        # Biggest group first by default: the count column is the sort, largest first.
        self.assertRegex(head, r'<th class="num sortable" aria-sort="descending"[^>]*><a [^>]*>Provider Prices')
        self.assertIn("gsort=modifier", head)
        self.assertIn("gsort=median", head)
        sorted_page = self.section(self.page(gsort="median"), "<thead>", "</thead>")
        self.assertRegex(sorted_page, r'aria-sort="ascending"[^>]*><a [^>]*>Median')
        self.assertIn("gsort=median&amp;gdir=desc", sorted_page)  # clicking again flips it

    def test_compare_ignores_an_unknown_sort(self):
        self.assertEqual(self.compare_rows(self.page(gsort="bogus")), self.compare_rows(self.page()))

    def test_both_networks_say_a_price_in_each_counts_twice(self):
        # A provider's same price in both files is one provider price per file: say so where it shows, only when
        # more than one file is searched and something is counted once (not Every row).
        note = "counts once per network"
        both = self.client.get("/by-procedure/10040").text
        self.assertIn(note, self.plain(self.section(both, 'id="p-compare"', "</section>")))
        self.assertIn("once per network", self.section(both, "<thead>", "</thead>"))  # the column's tooltip
        one_file = re.search(r'name="in" value="(file-\d+)"', both).group(1)
        self.assertNotIn(note, self.client.get("/by-procedure/10040", params={"in": one_file}).text)
        self.assertNotIn(note, self.client.get("/by-procedure/10040", params={"group_by": "none"}).text)
        stats = self.client.get("/by-procedure/10040", params={"view": "stats", "modifiers": "59", "modifiers_op": "equals",
                                                               "service_codes": "11", "service_codes_op": "equals"}).text
        self.assertIn("once per network", self.plain(self.section(stats, '<details class="how">', "</details>")))

    def test_compare_count_column_is_named_by_what_count_by_counts(self):
        # Each provider's price counted once is "Provider Prices", not "Offers" (an app-made word nobody uses).
        headers = {by: [self.plain(h).rstrip("↕▲▼ ") for h in re.findall(r'<th class="num[^"]*"[^>]*>(.*?)</th>',
                                                                          self.page(group_by=by), re.S)]
                   for by in ("provider", "npi", "none")}
        self.assertIn("Provider Prices", headers["provider"])
        self.assertIn("NPI Prices", headers["npi"])
        self.assertIn("Rates", headers["none"])
        self.assertFalse(any("Offer" in h for hs in headers.values() for h in hs))

    # Statistics
    def test_statistics_need_one_group(self):
        text = self.page(view="stats")
        self.assertIn("Statistics compare like with like.", text)
        self.assertIn("view=stats", self.section(text, 'class="card emptystate"', "</section>"))

    def test_statistics_lead_with_the_median_counted_by_provider(self):
        office = dict(modifiers="25", modifiers_op="equals", service_codes="11", service_codes_op="equals")
        text = self.page("99213", **office)
        hero = self.plain(self.section(text, '<div class="card hero"', "</dl>"))
        self.assertTrue(hero.startswith("$148.72 median of 2 provider prices (4 rates)"), hero)
        rows = self.plain(self.section(self.page("99213", group_by="none", **office), '<div class="card hero"', "</dl>"))
        self.assertTrue(rows.startswith("$231.30 median of 4 rates"), rows)
        self.assertIn("How These Numbers Are Computed", text)

    def test_outlier_counts_are_links_that_filter_the_rates(self):
        text = self.page("99213", group_by="none", modifiers="25", modifiers_op="equals", service_codes="11",
                         service_codes_op="equals")
        low = re.search(r'<a class="outbtn" href="([^"]+)"[^>]*><b>1</b> below \$128\.08', text)
        self.assertIsNotNone(low)
        self.assertIn("rate_max=128.07", low.group(1))
        self.assertIn("view=rates", low.group(1))

    def test_chart_can_be_dragged_to_pick_a_range_and_reset(self):
        text = self.page("10040", modifiers="59", modifiers_op="equals")
        box = re.search(r'<div class="chartbox"([^>]*)>', text).group(1)
        self.assertIn('data-min="69.67"', box)
        self.assertIn('data-max="140.17"', box)
        self.assertIn('data-href="/by-procedure/10040?modifiers=59&amp;modifiers_op=equals&amp;view=stats"', box)
        self.assertNotIn('class="fullrange"', text)
        ranged = self.page("10040", modifiers="59", modifiers_op="equals", rate_min="100", view="stats")
        reset = re.search(r'<a class="fullrange" href="([^"]+)"', ranged).group(1)
        self.assertNotIn("rate_min", reset)
        self.assertIn("modifiers=59", reset)

    def test_identical_rates_say_so(self):
        text = self.page(modifiers="51", modifiers_op="equals", group_by="none")
        self.assertIn("All 23 rates are $145.97", text)
        self.assertNotIn('class="chartbox"', text)

    # Rates
    def test_filter_row_is_behind_a_button_and_npi_ein_live_in_the_search_bar(self):
        text = self.page(view="rates")
        self.assertIn('<tr class="filter-row" id="filter-row" hidden>', text)
        self.assertIn('aria-controls="filter-row"', text)
        self.assertNotIn('name="npi_op"', text)
        self.assertNotIn('name="ein_op"', text)
        shown = self.page(view="rates", provider="mmc")
        self.assertIn('<tr class="filter-row" id="filter-row">', shown)

    def test_one_value_columns_fold_into_a_same_for_every_row_line(self):
        text = self.page(view="rates", modifiers="59", modifiers_op="equals", service_codes="11",
                         service_codes_op="equals")
        same = self.plain(self.section(text, '<p class="same"', "</p>"))
        self.assertEqual(same, "Same for every row: professional · outpatient · office (11) · mod 59")
        self.assertNotIn('<th data-col="setting"', text)
        self.assertNotIn('<td data-col="modifiers"', text)

    def test_provider_offers_list_their_npis(self):
        text = self.page("99213", view="rates", modifiers="25", modifiers_op="equals", service_codes="11",
                         service_codes_op="equals")
        self.assertIn("1–2 of 2 provider prices (4 rates)", self.plain(text))
        self.assertIn("<summary>3 NPIs</summary>", text)
        self.assertEqual(text.count('<td data-col="provider"'), 2)
        rows = self.page("99213", view="rates", group_by="none", modifiers="25", modifiers_op="equals",
                         service_codes="11", service_codes_op="equals")
        self.assertEqual(rows.count('<td data-col="provider"'), 4)

    def test_file_update_date_moves_to_the_file_tooltip(self):
        text = self.page("10006", view="rates")
        self.assertNotIn("updated 2026-10-06</div>", text)
        self.assertIn('title="2026-09-28_centene-management-company-llc_fidelis-ex_in-network.json · updated 2026-10-06"',
                      text)


class FileChoiceTest(unittest.TestCase):
    """#34 (an empty choice of files silently searched every file) can't happen since Search in is one choice (#40);
    old links that said "no file" search every plan, and the API still refuses an explicitly empty choice."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "rates.db"
        conn = sqlite3.connect(db_path)
        build_db(conn)
        conn.close()
        self.client = TestClient(create_app(db_path))

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_old_empty_choice_searches_every_plan_and_says_so(self):
        text = self.client.get("/search", params={"billing_code": "10006", "files": "chosen", "view": "rates"}).text
        self.assertIn("Samaritan Medical Center", text)
        self.assertIn("Minkowitz Consulting Pathologists PC", text)
        self.assertIn("<strong>Searching:</strong> All networks (fidelis-es + fidelis-ex, 3 plans)", text)

    def test_one_file_searches_only_that_file(self):
        text = self.client.get("/by-procedure/10006", params={"in": "file-2", "view": "rates"}).text
        self.assertIn("Minkowitz Consulting Pathologists PC", text)
        self.assertNotIn("Samaritan Medical Center", text)

    def test_api_rejects_an_explicitly_empty_file_choice(self):
        bad = self.client.get("/api/search", params={"billing_code": "10006", "files": "chosen"})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(bad.json()["detail"], "Pick at least one file to search in.")
        self.assertEqual(self.client.get("/api/search", params={"billing_code": "10006"}).json()["total"], 2)
        one = self.client.get("/api/search", params={"billing_code": "10006", "files": "chosen", "source_file_id": 1})
        self.assertEqual(one.json()["total"], 1)


class CountByApiTest(unittest.TestCase):
    """#24 in the API: group_by counts offers instead of rows; the default stays every row (none)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "rates.db"
        conn = sqlite3.connect(db_path)
        build_db(conn)
        add_code_10040(conn)
        conn.close()
        self.client = TestClient(create_app(db_path))

    def tearDown(self):
        self.tmp.cleanup()

    def api(self, **params):
        return self.client.get("/api/search", params={"billing_code": "10040", **params})

    def test_default_is_every_row(self):
        body = self.api().json()
        self.assertEqual((body["group_by"], body["total"], body["row_count"]), ("none", 62, 62))

    def test_provider_counts_organization_offers(self):
        body = self.api(group_by="provider").json()
        self.assertEqual((body["group_by"], body["total"], body["row_count"]), ("provider", 20, 62))
        self.assertIn("npis", body["results"][0])
        self.assertEqual([(g["modifiers"], g["count"]) for g in body["stats_by_group"]],
                         [("51", 2), ("59", 15), ("79,51", 2), ("XS", 1)])
        self.assertEqual(self.api(group_by="provider", modifiers="59", modifiers_op="equals").json()["stats"]["count"],
                         15)
        self.assertEqual(len(body["price_groups"]), 4)  # fields kept

    def test_bad_group_by_is_rejected(self):
        response = self.api(group_by="ein")
        self.assertEqual(response.status_code, 422)
        self.assertIn("group by", response.json()["detail"])


class SuggestApiTest(unittest.TestCase):
    """#23: /api/suggest/npi and /api/suggest/ein feed the search bar's NPI and EIN boxes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "rates.db"
        conn = sqlite3.connect(self.db_path)
        build_db(conn)
        add_code_10040(conn)
        conn.close()
        self.client = TestClient(create_app(self.db_path))

    def tearDown(self):
        self.tmp.cleanup()

    def test_npi_suggestions_for_a_code(self):
        body = self.client.get("/api/suggest/npi", params={"billing_code": " 10006 ", "q": "139"}).json()
        self.assertEqual(body["billing_code"], "10006")
        self.assertEqual([(s["value"], s["label"]) for s in body["suggestions"]],
                         [("1396747861", "Minkowitz Consulting Pathologists PC")])

    def test_ein_suggestions_without_a_code(self):
        body = self.client.get("/api/suggest/ein", params={"q": "11-34"}).json()
        self.assertIsNone(body["billing_code"])
        self.assertEqual([s["value"] for s in body["suggestions"]], ["11-3471596", "11-3460764"])

    def test_a_code_being_typed_that_is_not_valid_yet_means_every_code(self):
        body = self.client.get("/api/suggest/ein", params={"billing_code": "10006;", "q": "11-347"}).json()
        self.assertEqual([s["value"] for s in body["suggestions"]], ["11-3471596"])

    def test_limit_and_no_data(self):
        self.assertEqual(len(self.client.get("/api/suggest/npi", params={"q": "1", "limit": 2}).json()["suggestions"]), 2)
        self.assertEqual(self.client.get("/api/suggest/npi", params={"limit": 0}).status_code, 422)
        empty = TestClient(create_app(Path(self.tmp.name) / "missing.db"))
        self.assertEqual(empty.get("/api/suggest/npi", params={"q": "1"}).status_code, 503)


class DevRestartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "rates.db"
        self.trigger = Path(self.tmp.name) / "_dev_reload_trigger.py"

    def tearDown(self):
        self.tmp.cleanup()

    def test_restart_button_touches_the_reload_trigger(self):
        client = TestClient(create_app(self.db_path, dev_tools=True, restart_trigger=self.trigger))
        self.assertIn('action="/dev/restart"', client.get("/search").text)
        response = client.post("/dev/restart")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Restarting", response.text)
        self.assertTrue(self.trigger.exists())

    def test_no_restart_unless_dev_tools_are_on(self):
        client = TestClient(create_app(self.db_path, restart_trigger=self.trigger))
        self.assertNotIn("/dev/restart", client.get("/search").text)
        self.assertEqual(client.post("/dev/restart").status_code, 404)
        self.assertFalse(self.trigger.exists())


class WithoutDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "rates.db"

    def tearDown(self):
        self.tmp.cleanup()

    def assert_no_data(self, client):
        page = client.get("/search", params={"billing_code": "10006"})
        self.assertEqual(page.status_code, 200)
        self.assertIn("No data loaded yet", page.text)
        api = client.get("/api/search", params={"billing_code": "10006"})
        self.assertEqual(api.status_code, 503)
        health = client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertFalse(health.json()["db_present"])
        codes = client.get("/codes")
        self.assertEqual(codes.status_code, 200)
        self.assertIn("No data loaded yet", codes.text)
        self.assertEqual(client.get("/api/codes").status_code, 503)

    def test_missing_db(self):
        self.assert_no_data(TestClient(create_app(self.db_path)))
        self.assertFalse(self.db_path.exists(), "the app must not create the DB")

    def test_empty_db_file(self):
        self.db_path.touch()
        self.assert_no_data(TestClient(create_app(self.db_path)))


class LoadDataHomeTest(unittest.TestCase):
    """#37.1: "/" is the Load Data page; the nav starts with it as a plain link."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "rates.db"
        conn = sqlite3.connect(self.db_path)
        build_db(conn)
        conn.close()
        self.client = TestClient(create_app(self.db_path))

    def tearDown(self):
        self.tmp.cleanup()

    def nav(self, path: str) -> str:
        html = self.client.get(path).text
        return html[html.index("<nav"):html.index("</nav>")]

    def test_home_is_the_load_data_page_listing_the_loaded_files(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("<h1>Load Data</h1>", html)
        self.assertIn("Loaded indexes", html)
        self.assertIn("Choose An Index File", html)
        self.assertIn("2026-09-28_centene-management-company-llc_fidelis-es_in-network.json", html)
        self.assertIn("2026-09-28_centene-management-company-llc_fidelis-ex_in-network.json", html)
        self.assertIn("2 plans", html)
        self.assertIn("1 plan<", html)
        self.assertIn("1 rate<", html)
        self.assertIn("2 rates<", html)

    def test_no_search_card_and_no_totals(self):
        html = self.client.get("/").text
        self.assertNotIn("Search loaded data", html)
        self.assertNotIn("data-total", html)
        self.assertNotIn('action="/search"', html)
        self.assertNotIn("Admin dashboard", html)
        self.assertNotIn("Nothing is loaded yet", html)

    def test_a_load_error_shows(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE source_files SET error = 'HTTP 404 fetching file' WHERE id = 1")
        conn.commit()
        conn.close()
        self.assertIn("HTTP 404 fetching file", self.client.get("/").text)

    def test_old_search_links_redirect_to_search(self):
        response = self.client.get("/", params={"billing_code": "10006", "sort": "provider"},
                                   follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/search?billing_code=10006&sort=provider")
        followed = self.client.get("/", params={"billing_code": "10006"})
        self.assertIn("FINE NEEDLE ASPIRATION BX W/US GDN EA ADDL", followed.text)

    def test_load_redirects_home_keeping_the_query(self):
        response = self.client.get("/load", follow_redirects=False)
        self.assertEqual((response.status_code, response.headers["location"]), (307, "/"))
        response = self.client.get("/load", params={"index": "/a b.json"}, follow_redirects=False)
        self.assertEqual(response.headers["location"], "/?index=%2Fa+b.json")
        self.assertIn("<h1>Load Data</h1>", self.client.get("/load").text)

    def test_every_page_has_the_two_doors_in_the_nav(self):  # #40
        for path in ("/", "/by-procedure", "/by-procedure/10006", "/by-provider", "/by-provider/11-3471596", "/docs"):
            with self.subTest(path=path):
                nav = self.nav(path)
                self.assertEqual(re.findall(r'<a href="([^"]+)"[^>]*>([^<]+)</a>', nav),
                                 [("/", "Load Data"), ("/by-procedure", "Prices By Procedure"),
                                  ("/by-provider", "Prices By Provider"), ("/docs", "Docs")])

    def test_each_door_is_highlighted_on_its_pages(self):
        for path, section in (("/by-procedure", "Prices By Procedure"), ("/by-procedure/10006", "Prices By Procedure"),
                              ("/search?billing_code=10006", "Prices By Procedure"), ("/codes", "Prices By Procedure"),
                              ("/by-provider", "Prices By Provider"), ("/by-provider/11-3471596", "Prices By Provider"),
                              ("/providers", "Prices By Provider"), ("/", "Load Data"), ("/docs", "Docs")):
            with self.subTest(path=path):
                self.assertEqual(re.findall(r'<a href="[^"]*" aria-current="page">([^<]+)</a>', self.nav(path)), [section])

    def test_nav_has_load_data_first_as_a_plain_link_and_no_dropdown(self):
        for path in ("/", "/by-procedure", "/by-provider", "/docs"):
            with self.subTest(path=path):
                nav = self.nav(path)
                self.assertNotIn("<details", nav)
                self.assertNotIn("Admin", nav)
                self.assertNotIn('href="/load"', nav)
                self.assertNotIn("Using:", nav)
                self.assertRegex(nav, r'<a href="/"( aria-current="page")?>Load Data</a>')
                links = [nav.index(f'href="{h}"', nav.index("</a>")) for h in ("/", "/by-procedure", "/by-provider", "/docs")]
                self.assertEqual(links, sorted(links))  # Load Data first, then the two doors, then Docs

    def test_load_data_is_highlighted_on_its_pages(self):
        for path in ("/", "/load", "/load/status/nope"):
            with self.subTest(path=path):
                self.assertIn('<a href="/" aria-current="page">Load Data</a>', self.nav(path))
        self.assertNotIn('<a href="/" aria-current="page">', self.nav("/search"))

    def test_dev_restart_returns_to_search(self):
        trigger = Path(self.tmp.name) / "_dev_reload_trigger.py"
        client = TestClient(create_app(self.db_path, dev_tools=True, restart_trigger=trigger))
        self.assertIn('content="2;url=/by-procedure"', client.post("/dev/restart").text)


class LoadDataHomeWithoutDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "rates.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_state_with_no_db(self):
        response = TestClient(create_app(self.db_path)).get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Nothing is loaded yet", response.text)
        self.assertIn("Choose An Index File", response.text)
        self.assertFalse(self.db_path.exists())

    def test_empty_state_with_empty_db_file(self):
        self.db_path.touch()
        self.assertIn("Nothing is loaded yet", TestClient(create_app(self.db_path)).get("/").text)


class EveryFileTest(unittest.TestCase):
    """What stays of #29 now that files in use are gone (dropped for Search in, #40): pages and the API cover
    every loaded file unless told otherwise, and the Load Data page shows each loaded file's warnings.
    Fixture (build_db + add_code_731): file 1 is fidelis-es, file 2 fidelis-ex."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "rates.db"
        conn = sqlite3.connect(self.db_path)
        build_db(conn)
        add_code_731(conn)
        conn.close()
        self.client = TestClient(create_app(self.db_path))

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_in_use_toggles_and_an_old_cookie_changes_nothing(self):
        self.client.cookies.set("files_in_use", "rates.db:2")
        html = self.client.get("/").text
        self.assertNotIn('name="toggle"', html)
        self.assertNotIn("In use", html)
        self.assertEqual(self.client.post("/files-in-use", data={"toggle": "1"}).status_code, 404)
        self.assertEqual(len(self.client.get("/api/codes").json()["codes"]), 4)

    def test_a_loaded_file_shows_its_warnings(self):
        warning = "skipped 1 provider reference stored in a remote file (location), not fetched"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE source_files SET warnings = ? WHERE id = ?", (warning, 2))
        cards = re.findall(r'<article class="file.*?</article>', self.client.get("/").text, re.S)
        self.assertEqual(len(cards), 2)
        self.assertNotIn("Warning:", cards[0])
        self.assertIn(f"<strong>Warning:</strong> {warning}", cards[1])

    def test_codes_api_covers_every_file_unless_given_one(self):
        self.assertEqual(len(self.client.get("/api/codes").json()["codes"]), 4)
        self.assertEqual(len(self.client.get("/api/codes", params={"source_file_id": 2}).json()["codes"]), 3)

    def test_providers_api_covers_every_file_unless_given_one(self):
        self.assertEqual(self.client.get("/api/providers").json()["total"], 3)
        self.assertEqual(self.client.get("/api/providers", params={"source_file_id": 2}).json()["total"], 2)
        one = self.client.get("/api/providers/11-3471596", params={"source_file_id": 2}).json()
        self.assertEqual([n["name"] for n in one["names"]], ["Minkowitz Consulting Pathologists PC"])


if __name__ == "__main__":
    unittest.main()
