"""#23 Providers: browse organizations (EIN) with their names, NPIs and codes."""

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import create_app
from api.providers import (
    code_breakdown, counted_median, eins_for_npi, filter_providers, list_providers, normalize_name, provider_detail,
)
from api.errors import NoData
from ui.formatting import label_groups
from ui.provider_page import make_api_router
from tests.api import INSERT_RATE, add_code_731, build_db, insert_real_rows

# Real rows copied from the loaded DB (its file id first, additional_information last).
# EIN 13-4194078: one NPI billed under two names, at the same prices.
NESEN_ROWS = [
    (1, '99213', 'CPT', '2022', 'OFFICE/OUTPATIENT VISIT EST', 'OFFICE OR OTH OUTPATIENT VISIT FOR THE EVALUATION AND MANAGEMENT OF AN ESTABLISHED PATIENT, WHICH RE', 'negotiated', 46.72, 'professional', 'outpatient', '02', 'GT', '9999-12-31', 1202, 1295886612, '13-4194078', 'Dmitry K. Nesen, MD', None),
    (1, '99214', 'CPT', '2022', 'OFFICE/OUTPATIENT VISIT EST', 'OFFICE OR OTH OUTPATIENT VISIT FOR THE EVALUATION AND MANAGEMENT OF AN ESTABLISHED PATIENT, WHICH RE', 'negotiated', 68.82, 'professional', 'outpatient', '02', 'GT', '9999-12-31', 1202, 1295886612, '13-4194078', 'Dmitry K. Nesen, MD', None),
    (2, '99213', 'CPT', '2022', 'OFFICE/OUTPATIENT VISIT EST', 'OFFICE OR OTH OUTPATIENT VISIT FOR THE EVALUATION AND MANAGEMENT OF AN ESTABLISHED PATIENT, WHICH RE', 'negotiated', 46.72, 'professional', 'outpatient', '02', 'GT', '9999-12-31', 1655, 1295886612, '13-4194078', 'Four Season Medical PLLC', None),
    (2, '99214', 'CPT', '2022', 'OFFICE/OUTPATIENT VISIT EST', 'OFFICE OR OTH OUTPATIENT VISIT FOR THE EVALUATION AND MANAGEMENT OF AN ESTABLISHED PATIENT, WHICH RE', 'negotiated', 68.82, 'professional', 'outpatient', '02', 'GT', '9999-12-31', 1655, 1295886612, '13-4194078', 'Four Season Medical PLLC', None),
]
# "Conifer Park Inc" and "Conifer Park, Inc." under EIN 04-2806417 (some of its rows), and the same name under
# 11-3322995 (a network with 53 names; one of its rows).
CONIFER_ROWS = [
    (1, '90837', 'CPT', '2022', 'PSYTX W PT 60 MINUTES', 'PSYCHOTHERAPY, 60 MINUTES WITH PATIENT', 'negotiated', 94.5, 'professional', 'outpatient', '11,02,10', '95,GT', '9999-12-31', 927, 1477588747, '04-2806417', 'Conifer Park, Inc.', None),
    (1, '90785', 'CPT', '2022', 'PSYTX COMPLEX INTERACTIVE', 'INTERACTIVE COMPLEXITY (LIST SEPARATELY IN ADDITION TO THE CODE FOR PRIMARY PROCEDURE)', 'negotiated', 3.36, 'professional', 'outpatient', '10', '95', '9999-12-31', 838, 1073310728, '04-2806417', 'Conifer Park Inc', None),
    (1, '90834', 'CPT', '2022', 'PSYTX W PT 45 MINUTES', 'PSYCHOTHERAPY, 45 MINUTES WITH PATIENT', 'negotiated', 84.0, 'professional', 'outpatient', '02', '95', '9999-12-31', 838, 1073310728, '04-2806417', 'Conifer Park Inc', None),
    (2, '57150', 'CPT', '2022', 'TREAT VAGINA INFECTION', 'IRRIGATION OF VAGINA AND/OR APPLICATION OF MEDICAMENT FOR TREATMENT OF BACTERIAL, PARASITIC, OR FUNG', 'negotiated', 58.66, 'professional', 'outpatient', '11', '59', '9999-12-31', 531, 1972918696, '11-3322995', 'Conifer Park, Inc.', None),
]

MANUAL_THERAPY = ('97140', 'CPT', '2022', 'MANUAL THERAPY 1/> REGIONS', 'MANUAL THERAPY TECHNIQUES (EG, MOBILIZATION/ MANIPULATION, MANUAL LYMPHATIC DRAINAGE, MANUAL TRACTIO', 'negotiated')
# 97140 under the network 11-3322995: four of its names, in two like-for-like groups. With modifiers 59,GP at the
# office (11): Abdul Malik 19.46, New York Network Management IPA 19.19, 19.19 and 19.32, Interfaith 19.19.
# With GP alone: the IPA 29.71, MMC Faculty Practice 19.32.
NETWORK_ROWS = [
    (2, *MANUAL_THERAPY, 19.46, 'professional', 'outpatient', '11', '59,GP', '9999-12-31', 155, 1730072349, '11-3322995', 'Abdul Malik MD, PC', None),
    (1, *MANUAL_THERAPY, 19.19, 'professional', 'outpatient', '11', '59,GP', '9999-12-31', 115, 1013701226, '11-3322995', 'New York Network Management IPA, Inc', None),
    (1, *MANUAL_THERAPY, 19.19, 'professional', 'outpatient', '11', '59,GP', '9999-12-31', 115, 1558775528, '11-3322995', 'New York Network Management IPA, Inc', None),
    (1, *MANUAL_THERAPY, 19.32, 'professional', 'outpatient', '11', '59,GP', '9999-12-31', 114, 1770192262, '11-3322995', 'New York Network Management IPA, Inc', None),
    (2, *MANUAL_THERAPY, 19.19, 'professional', 'outpatient', '11', '59,GP', '9999-12-31', 157, 1013701226, '11-3322995', 'Interfaith Professional Physician Services, PC', None),
    (1, *MANUAL_THERAPY, 29.71, 'professional', 'outpatient', '11', 'GP', '9999-12-31', 1081, 1801419247, '11-3322995', 'New York Network Management IPA, Inc', None),
    (2, *MANUAL_THERAPY, 19.32, 'professional', 'outpatient', '11', 'GP', '9999-12-31', 1431, 1164221024, '11-3322995', 'MMC Faculty Practice', None),
]


def add_providers(conn: sqlite3.Connection) -> None:
    """build_db + 731 (CPT and RC) + the rows above. EINs: 11-3471596 (two names, one NPI), 45-0950946,
    13-4019319, 13-4194078, 04-2806417, 11-3322995 (with 97140 under four names); build_db's 10040 row has
    no EIN."""
    build_db(conn)
    add_code_731(conn)
    insert_real_rows(conn, NESEN_ROWS + CONIFER_ROWS + NETWORK_ROWS)


class ProvidersTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "rates.db"
        with closing(sqlite3.connect(self.db_path)) as conn:
            add_providers(conn)

    def tearDown(self):
        self.tmp.cleanup()

    def conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)


class ListProvidersTest(ProvidersTestBase):
    def test_one_row_per_ein_with_names_counts_and_range(self):
        with closing(self.conn()) as conn:
            providers = list_providers(conn)
        by_ein = {p["ein"]: p for p in providers}
        self.assertEqual(set(by_ein), {"11-3471596", "45-0950946", "13-4019319", "13-4194078", "04-2806417",
                                       "11-3322995"})
        self.assertEqual(by_ein["13-4194078"], {
            "ein": "13-4194078", "names": ["Dmitry K. Nesen, MD", "Four Season Medical PLLC"], "name_count": 2,
            "npi_count": 1, "rate_count": 4, "code_count": 2,
        })
        self.assertEqual(by_ein["04-2806417"]["names"], ["Conifer Park Inc", "Conifer Park, Inc."])
        self.assertEqual(by_ein["04-2806417"]["npi_count"], 2)
        self.assertEqual(by_ein["04-2806417"]["code_count"], 3)

    def test_rates_without_an_ein_are_left_out(self):
        with closing(self.conn()) as conn:
            providers = list_providers(conn)
            total = conn.execute("SELECT COUNT(*) FROM rates").fetchone()[0]
        self.assertEqual(sum(p["rate_count"] for p in providers), total - 1)  # build_db's 10040 row


class FilterProvidersTest(ProvidersTestBase):
    def setUp(self):
        super().setUp()
        with closing(self.conn()) as conn:
            self.providers = list_providers(conn)

    def eins(self, q, npi_eins=None):
        return sorted(p["ein"] for p in filter_providers(self.providers, q, npi_eins))

    def test_blank_query_keeps_everything(self):
        self.assertEqual(len(filter_providers(self.providers, "  ")), 6)

    def test_name_ignores_case_punctuation_and_suffixes(self):
        both = ["04-2806417", "11-3322995"]
        self.assertEqual(self.eins("Conifer Park Inc"), both)
        self.assertEqual(self.eins("conifer park, inc."), both)
        self.assertEqual(self.eins("CONIFER"), both)
        self.assertEqual(self.eins("four season medical"), ["13-4194078"])
        self.assertEqual(self.eins("The Samaritan Medical Center"), ["11-3471596"])

    def test_normalize_name(self):
        self.assertEqual(normalize_name("Conifer Park, Inc."), normalize_name("Conifer Park Inc"))
        self.assertEqual(normalize_name("E & A Medical Solutions, LLC"), "e a medical solutions")
        self.assertEqual(normalize_name("E and A Medical Solutions"), "e a medical solutions")
        self.assertEqual(normalize_name("The Four Season Medical PLLC"), "four season medical")
        self.assertEqual(normalize_name("Lyudmila Sverkunova Medical, PC"), "lyudmila sverkunova medical")

    def test_a_query_that_is_only_a_suffix_still_matches_literally(self):
        self.assertEqual(self.eins("pllc"), ["13-4194078"])

    def test_ein_with_or_without_dash(self):
        self.assertEqual(self.eins("13-4194078"), ["13-4194078"])
        self.assertEqual(self.eins("134194078"), ["13-4194078"])
        self.assertEqual(self.eins("13-4"), ["13-4019319", "13-4194078"])

    def test_npi_matches_the_providers_that_have_it(self):
        with closing(self.conn()) as conn:
            npi_eins = eins_for_npi(conn, "1295886612")
        self.assertEqual(npi_eins, {"13-4194078"})
        self.assertEqual(self.eins("1295886612", npi_eins), ["13-4194078"])

    def test_eins_for_npi_is_only_for_ten_digits(self):
        with closing(self.conn()) as conn:
            self.assertEqual(eins_for_npi(conn, "12958"), set())
            self.assertEqual(eins_for_npi(conn, "Conifer"), set())


class ProviderDetailTest(ProvidersTestBase):
    def test_names_npis_and_codes(self):
        with closing(self.conn()) as conn:
            detail = provider_detail(conn, "13-4194078")
        self.assertEqual(detail["ein"], "13-4194078")
        self.assertEqual(detail["rate_count"], 4)
        # No price range across a name's codes: prices only compare within one code.
        self.assertEqual(detail["names"], [
            {"name": "Dmitry K. Nesen, MD", "rate_count": 2, "npi_count": 1, "code_count": 2},
            {"name": "Four Season Medical PLLC", "rate_count": 2, "npi_count": 1, "code_count": 2},
        ])
        self.assertEqual(detail["npis"], [
            {"npi": 1295886612, "rate_count": 4, "names": ["Dmitry K. Nesen, MD", "Four Season Medical PLLC"]},
        ])
        self.assertEqual([c["billing_code"] for c in detail["codes"]], ["99213", "99214"])
        self.assertEqual(detail["codes"][0], {
            "billing_code": "99213", "billing_code_type": "CPT", "name": "OFFICE/OUTPATIENT VISIT EST",
            "rate_count": 2, "name_count": 2, "min_rate": 46.72, "max_rate": 46.72, "multi_type": False,
        })

    def test_codes_sorted_by_rate_count_desc_then_code(self):
        with closing(self.conn()) as conn:
            detail = provider_detail(conn, "04-2806417")
        self.assertEqual([(c["billing_code"], c["rate_count"]) for c in detail["codes"]],
                         [("90785", 1), ("90834", 1), ("90837", 1)])
        self.assertEqual([(n["name"], n["rate_count"]) for n in detail["names"]],
                         [("Conifer Park Inc", 2), ("Conifer Park, Inc.", 1)])

    def test_number_that_is_two_codes_is_flagged(self):
        with closing(self.conn()) as conn:
            detail = provider_detail(conn, "45-0950946")
        self.assertEqual(detail["codes"][0]["billing_code_type"], "CPT")
        self.assertTrue(detail["codes"][0]["multi_type"])

    def test_codes_for_one_name(self):
        with closing(self.conn()) as conn:
            detail = provider_detail(conn, "11-3322995", name="MMC Faculty Practice")
        self.assertEqual(detail["name"], "MMC Faculty Practice")
        self.assertEqual(len(detail["names"]), 5)  # the names table still lists every name
        self.assertEqual([(c["billing_code"], c["rate_count"], c["min_rate"], c["max_rate"]) for c in detail["codes"]],
                         [("97140", 1, 19.32, 19.32)])
        with closing(self.conn()) as conn:
            self.assertEqual(provider_detail(conn, "11-3322995", name="Nobody")["codes"], [])

    def test_breakdown_by_like_for_like_group_then_name(self):
        with closing(self.conn()) as conn:
            detail = provider_detail(conn, "11-3322995")
            code = next(c for c in detail["codes"] if c["billing_code"] == "97140")
            self.assertEqual((code["rate_count"], code["name_count"], code["min_rate"], code["max_rate"]),
                             (7, 4, 19.19, 29.71))
            code_breakdown(conn, "11-3322995", [code])
        groups = code["groups"]
        self.assertEqual([(g["modifiers"], g["rate_count"]) for g in groups], [("59,GP", 5), ("GP", 2)])
        first = groups[0]
        self.assertEqual((first["billed_as"], first["procedure"], first["billing_class"], first["service_codes"]),
                         ("CPT 97140", "", "professional", "11"))
        self.assertNotIn("modifier_label", first)  # the API gives codes; the UI puts them in words
        label_groups([code])
        self.assertEqual(first["place_label"], "office (11)")
        self.assertEqual(first["modifier_label"], "mods 59, GP")
        self.assertIn("physical therapy plan of care", first["modifier_tooltip"])
        # Each name with its median price and rate count, most rates first.
        self.assertEqual(first["names"], [
            {"name": "New York Network Management IPA, Inc", "median": 19.19, "rate_count": 3},
            {"name": "Abdul Malik MD, PC", "median": 19.46, "rate_count": 1},
            {"name": "Interfaith Professional Physician Services, PC", "median": 19.19, "rate_count": 1},
        ])
        self.assertEqual((first["name_count"], first["more_names"]), (3, 0))
        self.assertEqual(code["breakdown_name_count"], 4)
        self.assertEqual(groups[1]["names"], [
            {"name": "MMC Faculty Practice", "median": 19.32, "rate_count": 1},
            {"name": "New York Network Management IPA, Inc", "median": 29.71, "rate_count": 1},
        ])

    def test_counted_median_is_the_median_written_out(self):
        import statistics
        for counts in ({19.19: 2, 19.32: 1}, {19.19: 1, 29.71: 1}, {5.0: 3, 7.0: 1, 9.0: 2}, {46.72: 1}):
            values = [v for v, n in counts.items() for _ in range(n)]
            self.assertEqual(counted_median(counts), statistics.median(values), counts)

    def test_breakdown_caps_the_names_per_group(self):
        with closing(self.conn()) as conn:
            code = next(c for c in provider_detail(conn, "11-3322995")["codes"] if c["billing_code"] == "97140")
            code_breakdown(conn, "11-3322995", [code], names_cap=1)
        self.assertEqual([n["name"] for n in code["groups"][0]["names"]], ["New York Network Management IPA, Inc"])
        self.assertEqual((code["groups"][0]["name_count"], code["groups"][0]["more_names"]), (3, 2))
        self.assertEqual(code["breakdown_name_count"], 4)  # counts every name, not just those listed

    def test_breakdown_keeps_two_types_of_one_number_apart(self):
        with closing(self.conn()) as conn:
            code = provider_detail(conn, "45-0950946")["codes"][0]
            code_breakdown(conn, "45-0950946", [code])
        self.assertEqual([g["billed_as"] for g in code["groups"]], ["CPT 731"])
        self.assertEqual(code["groups"][0]["names"], [
            {"name": "Chinese American IPA Assoc/CAIPA", "median": 23.81, "rate_count": 1}])

    def test_ein_without_dash_and_unknown_ein(self):
        with closing(self.conn()) as conn:
            self.assertEqual(provider_detail(conn, "134194078")["ein"], "13-4194078")
            self.assertIsNone(provider_detail(conn, "99-9999999"))
            self.assertIsNone(provider_detail(conn, "nonsense"))


def make_client(connect) -> TestClient:
    app = FastAPI()
    app.include_router(make_api_router(connect))
    return TestClient(app)


class ApiTest(ProvidersTestBase):
    """/api/providers and /api/providers/{ein}."""

    def setUp(self):
        super().setUp()
        self.client = make_client(self.conn)

    def test_api_lists_and_filters(self):
        body = self.client.get("/api/providers").json()
        self.assertEqual(body["total"], 6)
        self.assertEqual(len(body["providers"]), 6)
        body = self.client.get("/api/providers", params={"q": "conifer park inc"}).json()
        self.assertEqual(sorted(p["ein"] for p in body["providers"]), ["04-2806417", "11-3322995"])
        body = self.client.get("/api/providers", params={"q": "1295886612"}).json()
        self.assertEqual([p["ein"] for p in body["providers"]], ["13-4194078"])

    def test_api_detail_and_unknown(self):
        response = self.client.get("/api/providers/13-4194078")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["names"]), 2)
        response = self.client.get("/api/providers/99-9999999")
        self.assertEqual(response.status_code, 404)
        self.assertIn("99-9999999", response.json()["detail"])

    def test_api_pages_after_filtering_and_sorting(self):
        body = self.client.get("/api/providers").json()
        self.assertEqual((body["limit"], body["offset"]), (100, 0))
        body = self.client.get("/api/providers", params={"limit": 2}).json()
        self.assertEqual((body["total"], body["all"]), (6, 6))
        self.assertEqual([p["ein"] for p in body["providers"]], ["04-2806417", "11-3322995"])
        body = self.client.get("/api/providers", params={"limit": 2, "offset": 4}).json()
        self.assertEqual([p["ein"] for p in body["providers"]], ["13-4194078", "45-0950946"])
        # The filter searches every provider, then pages.
        body = self.client.get("/api/providers", params={"q": "conifer", "limit": 1, "offset": 1}).json()
        self.assertEqual((body["total"], body["all"]), (2, 6))
        self.assertEqual([p["ein"] for p in body["providers"]], ["11-3322995"])
        # Sorting happens before paging: the most rates first.
        body = self.client.get("/api/providers", params={"sort": "rates", "dir": "desc", "limit": 2}).json()
        self.assertEqual([p["ein"] for p in body["providers"]], ["11-3322995", "13-4194078"])

    def test_api_rejects_limits_out_of_range(self):
        for params in ({"limit": 0}, {"limit": 501}, {"offset": -1}):
            self.assertEqual(self.client.get("/api/providers", params=params).status_code, 422, params)
        self.assertEqual(self.client.get("/api/providers", params={"limit": 500}).status_code, 200)

    def test_api_detail_mirrors_the_page(self):
        body = self.client.get("/api/providers/11-3322995").json()
        self.assertNotIn("min_rate", body["names"][0])
        self.assertEqual((body["code_count"], body["limit"], body["codes_offset"]), (2, 100, 0))
        code = next(c for c in body["codes"] if c["billing_code"] == "97140")
        self.assertEqual([g["modifiers"] for g in code["groups"]], ["59,GP", "GP"])
        body = self.client.get("/api/providers/11-3322995", params={"name": "MMC Faculty Practice"}).json()
        self.assertEqual([c["billing_code"] for c in body["codes"]], ["97140"])
        self.assertEqual(body["name"], "MMC Faculty Practice")
        body = self.client.get("/api/providers/11-3322995", params={"limit": 1, "codes_offset": 1}).json()
        self.assertEqual((body["code_count"], len(body["codes"])), (2, 1))
        self.assertEqual(self.client.get("/api/providers/11-3322995", params={"limit": 0}).status_code, 422)

    def test_list_follows_a_reloaded_db(self):
        self.assertEqual(self.client.get("/api/providers").json()["total"], 6)
        with closing(sqlite3.connect(self.db_path)) as conn:
            # A real row (from build_db's file 2) under a new EIN: Lifeline Tests' 96365 rate.
            conn.execute(INSERT_RATE, (
                2, '96365', 'CPT', '2022', 'THER/PROPH/DIAG IV INF INIT',
                'INTRAVENOUS INFUSION, FOR THERAPY, PROPHYLAXIS, OR DIAGNOSIS (SPECIFY SUBSTANCE OR DRUG); INITIAL, U',
                'negotiated', 54.29, 'professional', 'outpatient', '11', 'XE', '9999-12-31', 1074, 1245442490,
                '33-3404574', 'Lifeline Tests Incorporated'))
            conn.commit()
        stat = self.db_path.stat()
        os.utime(self.db_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        self.assertEqual(self.client.get("/api/providers").json()["total"], 7)


class PagesTest(ProvidersTestBase):
    """The same providers on Prices By Provider (#40; the pages that were /providers)."""

    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(self.db_path))

    def test_list_links_every_ein_to_its_page(self):
        html = self.client.get("/by-provider").text
        self.assertIn('href="/by-provider/13-4194078"', html)
        self.assertIn("Four Season Medical PLLC", html)
        self.assertIn("All 6 providers (EINs)", html)
        self.assertNotIn("Lowest", html)  # no price range across codes
        self.assertNotIn("Highest", html)

    def test_list_filter_and_order(self):
        html = self.client.get("/by-provider", params={"q": "conifer park inc"}).text
        self.assertIn("2 of 6 providers (EINs) match", html)
        self.assertNotIn("/by-provider/13-4194078", html)
        html = self.client.get("/by-provider").text
        self.assertLess(html.index("/by-provider/11-3322995"), html.index("/by-provider/45-0950946"))  # most rates first

    def test_list_no_match(self):
        self.assertIn("No provider name, EIN or NPI matches “zzzz”.", self.client.get("/by-provider", params={"q": "zzzz"}).text)

    def test_provider_page_shows_each_name_npis_and_code_links(self):
        response = self.client.get("/by-provider/13-4194078")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("Dmitry K. Nesen, MD", html)
        self.assertIn("Four Season Medical PLLC", html)
        self.assertIn("1295886612", html)
        self.assertIn('href="/by-procedure/99213?ein=13-4194078"', html)
        self.assertNotIn("code_type=", html)

    def test_provider_page_links_a_two_type_code_with_its_type(self):
        html = self.client.get("/by-provider/45-0950946").text
        self.assertIn('href="/by-procedure/731?ein=45-0950946&amp;code_type=CPT"', html)

    def test_provider_page_unknown_is_a_friendly_404(self):
        response = self.client.get("/by-provider/99-9999999")
        self.assertEqual(response.status_code, 404)
        self.assertIn("EIN 99-9999999 has no rates in the loaded files.", response.text)
        self.assertIn('href="/by-provider"', response.text)

    def test_provider_page_pages_its_codes(self):
        # 04-2806417 has three codes: 90785, 90834, 90837 (one rate each).
        html = self.client.get("/by-provider/04-2806417", params={"limit": 2}).text
        self.assertIn("1–2 of 3 codes", html)
        self.assertIn(">90785</a>", html)
        self.assertNotIn(">90837</a>", html)
        self.assertIn('href="/by-provider/04-2806417?limit=2&amp;codes_offset=2#codes"', html)
        html = self.client.get("/by-provider/04-2806417", params={"limit": 2, "codes_offset": 2}).text
        self.assertIn("3–3 of 3 codes", html)
        self.assertIn(">90837</a>", html)
        self.assertNotIn(">90785</a>", html)
        self.assertIn('href="/by-provider/04-2806417?limit=2#codes"', html)
        self.assertIn("Conifer Park Inc", html)  # names are not paged
        self.assertIn("Conifer Park, Inc.", html)
        html = self.client.get("/by-provider/04-2806417").text  # 25 a page: three codes need no pager
        self.assertIn(">90837</a>", html)
        self.assertNotIn("Next", html)

    def test_provider_page_pages_its_npis_separately(self):
        # 04-2806417 has two NPIs: 1073310728 (Conifer Park Inc) and 1477588747 (Conifer Park, Inc.).
        html = self.client.get("/by-provider/04-2806417", params={"limit": 1, "codes_offset": 1}).text
        self.assertIn("1–1 of 2 NPIs", html)
        self.assertIn('<td class="code">1073310728</td>', html)
        self.assertNotIn('<td class="code">1477588747</td>', html)
        self.assertIn('href="/by-provider/04-2806417?limit=1&amp;codes_offset=1&amp;npis_offset=1#npis"', html)
        html = self.client.get("/by-provider/04-2806417", params={"limit": 1, "npis_offset": 1}).text
        self.assertIn("2–2 of 2 NPIs", html)
        self.assertIn('<td class="code">1477588747</td>', html)
        self.assertIn(">90785</a>", html)  # codes still on their first page

    def test_provider_page_past_the_end_and_bad_paging(self):
        html = self.client.get("/by-provider/04-2806417", params={"codes_offset": 50}).text
        self.assertIn("No codes on this page", html)
        for params in ({"limit": "x", "codes_offset": "y", "npis_offset": "-3"}, {"limit": 0}, {"limit": 9999}):
            self.assertEqual(self.client.get("/by-provider/04-2806417", params=params).status_code, 200, params)

    def test_names_link_to_their_codes(self):
        html = self.client.get("/by-provider/11-3322995").text
        self.assertIn('href="/by-provider/11-3322995?name=MMC+Faculty+Practice#codes"', html)
        html = self.client.get("/by-provider/11-3322995", params={"name": "MMC Faculty Practice"}).text
        codes = html[html.index('id="codes-table"'):html.index('id="names"')]
        self.assertIn(">97140</a>", codes)
        self.assertNotIn(">57150</a>", codes)
        self.assertIn("showing the codes billed under <strong>MMC Faculty Practice</strong>", html)
        self.assertIn('href="/by-provider/11-3322995">every code</a>', html)

    def test_each_code_breaks_down_by_group_and_name(self):
        html = self.client.get("/by-provider/11-3322995").text
        codes = html[html.index('id="codes-table"'):html.index('id="names"')]
        self.assertIn("2 groups · 4 names", codes)
        self.assertIn("office (11)", codes)
        self.assertIn("New York Network Management IPA, Inc", codes)
        self.assertIn("$19.19", codes)
        self.assertIn("$29.71", codes)

    def test_the_name_is_kept_by_the_pagers(self):
        # 11-3322995 has seven NPIs here; the IPA bills one code (97140).
        html = self.client.get("/by-provider/11-3322995", params={"name": "New York Network Management IPA, Inc",
                                                                  "limit": 1, "npis_offset": 1}).text
        self.assertIn("2–2 of 7 NPIs", html)
        self.assertIn('href="/by-provider/11-3322995?name=New+York+Network+Management+IPA%2C+Inc&amp;limit=1#npis"', html)


class NoDataTest(unittest.TestCase):
    def setUp(self):
        def connect():
            raise NoData
        self.client = make_client(connect)

    def test_api_is_503(self):
        for path in ("/api/providers", "/api/providers/13-4194078"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 503, path)
            self.assertIn("No data loaded yet", response.json()["detail"])

    def test_pages_say_no_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(Path(tmp) / "missing.db"))
            for path in ("/by-provider", "/by-provider/13-4194078"):
                response = client.get(path)
                self.assertEqual(response.status_code, 200, path)
                self.assertIn("No data loaded yet", response.text)


if __name__ == "__main__":
    unittest.main()
