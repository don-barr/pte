import sqlite3
import unittest

from api import pickers
from api.pickers import match_procedures, procedure_list, provider_prices
from ui.formatting import codes_picker, providers_picker
from tests.api import (
    add_clinic_99213, add_code_10040, add_code_260, add_code_731, add_code_97151, add_code_99213,
    add_network_11_3322995, build_db,
)


def fixture() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    build_db(conn)
    for add in (add_code_99213, add_clinic_99213, add_code_260, add_code_10040, add_code_731, add_code_97151,
                add_network_11_3322995):
        add(conn)
    return conn


def providers_for_code(conn, code, q="", scope=(), **kw) -> dict:
    """The API's answer as a page shows it (ui.formatting): links and the notice in words."""
    return providers_picker(pickers.providers_for_code(conn, code, q, **kw), scope)


def codes_for_provider(conn, ein, q, procs, scope=(), **kw) -> dict:
    return codes_picker(pickers.codes_for_provider(conn, ein, q, procs, **kw), scope)


def hrefs(notice: dict) -> list[str]:
    return [a["href"] for a in notice["alternatives"]]


class ProcedureListTest(unittest.TestCase):
    def setUp(self):
        self.conn = fixture()
        self.procs = procedure_list(self.conn)

    def test_one_row_per_code_and_type_procedures_before_hospital_departments(self):
        keys = [(p["billing_code"], p["billing_code_type"]) for p in self.procs]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn(("731", "CPT"), keys)
        self.assertIn(("731", "RC"), keys)
        types = [p["billing_code_type"] for p in self.procs]
        self.assertEqual(types, sorted(types, key=lambda t: t == "RC"))
        cpt = [p for p in self.procs if p["billing_code_type"] != "RC"]
        self.assertEqual(cpt[0]["billing_code"], "10040")  # most rates first: 62
        self.assertEqual(cpt[0]["rate_count"], 62)

    def test_revenue_codes_are_named_by_their_department_never_by_a_procedure_style_name(self):
        by_code = {(p["billing_code"], p["billing_code_type"]): p for p in self.procs}
        self.assertEqual(by_code[("510", "RC")]["name"], "Clinic")
        self.assertEqual(by_code[("521", "RC")]["name"], "Revenue code 0521")  # only "OFFICE/OUTPATIENT VISIT EST"
        self.assertEqual(by_code[("97124", "CPT")]["name"], "Massage therapy")

    def test_a_name_finds_the_procedure_and_a_number_finds_codes_starting_with_it(self):
        self.assertEqual([p["billing_code"] for p in match_procedures(self.procs, "massage")], ["97124"])
        self.assertEqual([p["billing_code"] for p in match_procedures(self.procs, "9721")], [])
        self.assertEqual([p["billing_code"] for p in match_procedures(self.procs, "9712")], ["97124"])
        found = match_procedures(self.procs, "office visit")
        self.assertEqual([p["billing_code"] for p in found], ["99213"])  # RC 521's procedure-style names don't count
        self.assertEqual([(p["billing_code"], p["billing_code_type"]) for p in match_procedures(self.procs, "0510")],
                         [("510", "RC")])

    def test_an_exact_code_comes_first(self):
        found = match_procedures(self.procs, "731")
        self.assertEqual({p["billing_code"] for p in found}, {"731"})


class ProvidersForCodeTest(unittest.TestCase):
    def setUp(self):
        self.conn = fixture()

    def test_the_api_answers_with_facts_no_wording_or_links(self):
        found = pickers.providers_for_code(self.conn, "99213", q="1003050162")
        self.assertNotIn("message", found["notice"])
        self.assertEqual(found["notice"]["who"], {"npi": 1003050162, "name": "Hands of Hope PT, PC", "ein": "11-3322995"})
        self.assertEqual(found["notice"]["under_other_names"], [("11-3322995", 2)])
        self.assertNotIn("href", pickers.providers_for_code(self.conn, "99213")["providers"][0])

    def test_no_text_lists_the_providers_with_rates_for_the_code_most_rates_first(self):
        found = providers_for_code(self.conn, "99213", q="")
        self.assertEqual(found["providers"][0]["name"], "Forme Medical Center")
        self.assertEqual(found["providers"][0]["rate_count"], 3)
        self.assertEqual(found["providers"][0]["href"], "/by-procedure/99213?ein=13-4068415")
        self.assertIsNone(found["notice"])
        names = {p["name"] for p in found["providers"]}
        self.assertNotIn("Hands of Hope PT, PC", names)

    def test_name_and_ein_with_or_without_the_dash_find_the_provider(self):
        for q in ("forme", "FORME medical", "13-4068415", "134068415"):
            with self.subTest(q=q):
                found = providers_for_code(self.conn, "99213", q=q)
                self.assertEqual([p["ein"] for p in found["providers"]], ["13-4068415"])

    def test_an_npi_without_rates_for_the_code_offers_its_codes_and_its_eins_rates(self):  # #40 dead end 1
        found = providers_for_code(self.conn, "99213", q="1003050162")
        self.assertEqual(found["providers"], [])
        notice = found["notice"]
        self.assertEqual(notice["kind"], "none")
        self.assertIn("NPI 1003050162", notice["message"])
        self.assertIn("Hands of Hope PT, PC", notice["message"])
        self.assertIn("/by-procedure/97124?npi=1003050162", hrefs(notice))
        self.assertIn("/by-procedure/99213?ein=11-3322995", hrefs(notice))
        label = next(a["label"] for a in notice["alternatives"] if a["href"] == "/by-procedure/99213?ein=11-3322995")
        self.assertIn("2 rates for 99213 under other names", label)
        self.assertIn("/by-procedure/99213", hrefs(notice))  # keep the code, any provider

    def test_an_npi_with_rates_is_offered(self):
        found = providers_for_code(self.conn, "99213", q="1942573167")
        self.assertEqual([(p["ein"], p["href"]) for p in found["providers"]],
                         [("46-2628853", "/by-procedure/99213?npi=1942573167")])

    def test_a_provider_with_only_hospital_rates_for_the_code_is_told_so_with_a_link_to_them(self):  # dead end 2
        found = providers_for_code(self.conn, "99213", q="14-1338544")
        self.assertEqual([p["hospital_count"] for p in found["providers"]], [1])
        notice = found["notice"]
        self.assertEqual(notice["kind"], "hospital_only")
        self.assertIn("no 99213 rates of its own", notice["message"])
        self.assertIn("revenue code 0510", notice["message"])
        self.assertEqual(notice["alternatives"][0]["href"], "/by-procedure/99213?ein=14-1338544")
        self.assertIn("1 hospital rate", notice["alternatives"][0]["label"])

    def test_with_hospital_rates_hidden_the_link_shows_them_again(self):
        found = providers_for_code(self.conn, "99213", q="14-1338544", include_facility=False,
                                   scope=[("include_facility", "false")])
        self.assertEqual(found["providers"], [])
        self.assertEqual(found["notice"]["kind"], "hospital_only")
        self.assertEqual(found["notice"]["alternatives"][0]["href"], "/by-procedure/99213?ein=14-1338544")

    def test_an_ein_without_the_code_offers_its_codes_its_page_and_the_code_from_anyone(self):
        notice = providers_for_code(self.conn, "99213", q="11-3471596")["notice"]
        self.assertEqual(notice["kind"], "none")
        self.assertIn("EIN 11-3471596", notice["message"])
        self.assertIn("/by-procedure/10006?ein=11-3471596", hrefs(notice))
        self.assertIn("/by-provider/11-3471596", hrefs(notice))
        self.assertIn("/by-procedure/99213", hrefs(notice))

    def test_rates_only_in_another_file_link_to_all_plans(self):
        # Forme Medical Center's 99213 rates are all in fidelis-es (fixture file 1); search fidelis-ex only.
        found = providers_for_code(self.conn, "99213", q="13-4068415", file_ids=[2], scope=[("in", "file-2")])
        self.assertEqual(found["providers"], [])
        self.assertIn("/by-procedure/99213?ein=13-4068415", hrefs(found["notice"]))
        self.assertIn("3 rates for 99213 in other files", " ".join(a["label"] for a in found["notice"]["alternatives"]))

    def test_unknown_ein_and_npi(self):
        for q, word in (("99-9999999", "EIN 99-9999999"), ("1999999999", "NPI 1999999999")):
            with self.subTest(q=q):
                notice = providers_for_code(self.conn, "99213", q=q)["notice"]
                self.assertEqual(notice["kind"], "unknown")
                self.assertIn(word, notice["message"])

    def test_a_name_without_the_code_is_checked_per_ein(self):
        found = providers_for_code(self.conn, "99213", q="hands of hope")
        self.assertEqual(found["providers"], [])
        self.assertIn("/by-procedure/99213?ein=11-3322995", hrefs(found["notice"]))


class CodesForProviderTest(unittest.TestCase):
    def setUp(self):
        self.conn = fixture()
        self.procs = procedure_list(self.conn)

    def test_no_text_lists_the_providers_codes(self):
        found = codes_for_provider(self.conn, "11-3322995", "", self.procs)
        self.assertEqual({c["billing_code"] for c in found["codes"]}, {"97110", "97124", "99213"})
        self.assertEqual(found["total"], 3)

    def test_a_name_finds_its_code_linked_to_this_code_at_this_provider(self):
        found = codes_for_provider(self.conn, "113322995", "massage", self.procs)
        self.assertEqual([(c["billing_code"], c["href"]) for c in found["codes"]],
                         [("97124", "/by-procedure/97124?ein=11-3322995")])

    def test_a_code_it_has_no_rates_for_offers_its_nearest_codes_and_the_code_from_anyone(self):  # dead end 3
        found = codes_for_provider(self.conn, "11-3322995", "10040", self.procs)
        self.assertEqual(found["codes"], [])
        notice = found["notice"]
        self.assertIn("EIN 11-3322995 has no rates for 10040", notice["message"])
        self.assertIn("/by-procedure/97110?ein=11-3322995", hrefs(notice))
        self.assertIn("/by-procedure/10040", hrefs(notice))

    def test_hospital_rates_count_as_the_procedure(self):
        found = codes_for_provider(self.conn, "14-1338544", "99213", self.procs)
        self.assertEqual([(c["billing_code"], c["hospital_count"]) for c in found["codes"]], [("99213", 1)])

    def test_text_that_names_no_code(self):
        notice = codes_for_provider(self.conn, "11-3322995", "zzzz", self.procs)["notice"]
        self.assertEqual(notice["kind"], "unknown")
        self.assertIn("zzzz", notice["message"])


class ProviderPricesTest(unittest.TestCase):
    def test_median_vs_everyones_in_the_same_group_counted_by_offer(self):
        conn = fixture()
        [row] = provider_prices(conn, "47-3981360", [{"billing_code": "97151", "billing_code_type": "CPT"}])
        self.assertEqual((row["group"]["modifiers"], row["group"]["service_codes"]), ("", "11"))
        self.assertEqual(row["count"], 6)  # Encore's six prices in that group
        self.assertEqual(row["median"], 20.375)
        self.assertEqual(row["everyone_count"], 11)
        self.assertEqual(row["everyone_median"], 19.75)
        self.assertTrue(row["mostly_this"])  # 6 of 11 offers are its own
        self.assertAlmostEqual(row["difference"], 20.375 / 19.75 - 1)
        self.assertEqual(row["group_count"], 4)

    def test_not_mostly_this_provider_at_half(self):
        conn = fixture()
        [row] = provider_prices(conn, "13-4068415", [{"billing_code": "99213", "billing_code_type": "CPT"}])
        self.assertEqual((row["median"], row["everyone_median"]), (231.3, (66.14 + 231.3) / 2))
        self.assertFalse(row["mostly_this"])


if __name__ == "__main__":
    unittest.main()
