import sqlite3
import unittest

from ui.labels import describe_modifiers, describe_places, modifier_tooltip, place_label
from api.search import (
    iqr_spread, code_info, code_types, column_choices, department_names, short_name, filter_codes, find_plans, list_codes,
    list_plans, list_sources, normalize_code, plan_info, price_explained, procedure_names,
    PERCENTILE_MIN_COUNTS, percentile, percentiles, price_groups, quartiles, rate_stats, search_rates, sort_codes,
    stats_by_group, suggest_eins, suggest_npis,
)
from tests.api import (
    BEHAVIOR_ID_ROWS, EX_URL, add_clinic_99213, add_code_10040, add_code_260, add_code_731, add_code_97151, add_code_99213,
    build_db,
)


class SearchRatesTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        build_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_returns_every_rate_for_the_code_cheapest_first(self):
        result = search_rates(self.conn, "10006")
        self.assertEqual(result["total"], 2)
        self.assertEqual([r["negotiated_rate"] for r in result["results"]], [69.85, 76.84])
        self.assertEqual(
            [r["business_name"] for r in result["results"]],
            ["Minkowitz Consulting Pathologists PC", "Samaritan Medical Center"],
        )

    def test_result_carries_rate_provider_and_source_file(self):
        first = search_rates(self.conn, "10006")["results"][0]
        self.assertEqual(first["npi"], 1396747861)
        self.assertEqual(first["ein"], "11-3471596")
        self.assertEqual(first["billing_class"], "professional")
        self.assertEqual(first["setting"], "outpatient")
        self.assertEqual(first["service_codes"], "11")
        self.assertEqual(first["modifiers"], "59")
        self.assertEqual(first["expiration_date"], "9999-12-31")
        self.assertEqual(first["source_file"], "2026-09-28_centene-management-company-llc_fidelis-ex_in-network.json")
        self.assertEqual(first["source_url"], EX_URL)
        self.assertEqual(first["source_last_updated_on"], "2026-10-06")

    def test_code_is_trimmed_and_uppercased(self):
        self.assertEqual(normalize_code("  j1234 "), "J1234")
        result = search_rates(self.conn, " 10006 ")
        self.assertEqual(result["billing_code"], "10006")
        self.assertEqual(result["total"], 2)

    def test_unknown_code_returns_empty(self):
        result = search_rates(self.conn, "99999")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["results"], [])

    def test_limit_and_offset_page_with_accurate_total(self):
        result = search_rates(self.conn, "10006", limit=1, offset=1)
        self.assertEqual(result["total"], 2)
        self.assertEqual((result["limit"], result["offset"]), (1, 1))
        self.assertEqual([r["negotiated_rate"] for r in result["results"]], [76.84])

    def test_code_info_gives_name_and_description(self):
        self.assertEqual(
            code_info(self.conn, "10006"),
            {"billing_code": "10006", "billing_code_type": "CPT", "name": "FNA BX W/US GDN EA ADDL",
             "description": "FINE NEEDLE ASPIRATION BX W/US GDN EA ADDL"},
        )
        self.assertIsNone(code_info(self.conn, "99999"))



class SortRatesTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        build_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_sorts_by_any_column_in_either_direction(self):
        by_provider = search_rates(self.conn, "10006", sort="provider", direction="desc")
        self.assertEqual(
            [r["business_name"] for r in by_provider["results"]],
            ["Samaritan Medical Center", "Minkowitz Consulting Pathologists PC"],
        )
        by_rate = search_rates(self.conn, "10006", sort="rate", direction="desc")
        self.assertEqual([r["negotiated_rate"] for r in by_rate["results"]], [76.84, 69.85])
        self.assertEqual((by_rate["sort"], by_rate["direction"]), ("rate", "desc"))

    def test_every_column_shown_is_sortable(self):
        for column in ("rate", "provider", "ein", "npi", "billing_class", "setting",
                       "service_codes", "modifiers", "source"):
            with self.subTest(column=column):
                self.assertEqual(search_rates(self.conn, "10006", sort=column)["total"], 2)

    def test_unknown_sort_or_direction_is_rejected(self):
        with self.assertRaises(ValueError):
            search_rates(self.conn, "10006", sort="id; DROP TABLE rates")
        with self.assertRaises(ValueError):
            search_rates(self.conn, "10006", direction="sideways")


class SourceFilterTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        build_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_limits_results_to_one_source_file(self):
        result = search_rates(self.conn, "10006", source_file_ids=[2])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["results"][0]["business_name"], "Minkowitz Consulting Pathologists PC")
        self.assertEqual(result["source_file_ids"], [2])

    def test_both_files_or_no_choice_searches_everything(self):
        self.assertEqual(search_rates(self.conn, "10006", source_file_ids=[1, 2])["total"], 2)
        self.assertEqual(search_rates(self.conn, "10006")["total"], 2)

    def test_lists_each_source_file_with_its_plans(self):
        sources = list_sources(self.conn)
        self.assertEqual([s["id"] for s in sources], [1, 2])
        ex = sources[1]
        self.assertEqual(ex["file"], "2026-09-28_centene-management-company-llc_fidelis-ex_in-network.json")
        self.assertEqual(ex["plan_count"], 2)
        self.assertEqual(ex["plan_names"], ["Ambetter from Fidelis Care Bronze",
                                            "Ambetter from Fidelis Care Gold Zero Cost Sharing"])
        self.assertEqual(ex["rate_count"], 2)

    def test_lists_each_source_files_warnings(self):
        # The ETL's text when a file's provider references point at remote files it doesn't fetch (etl/load.py).
        warning = "skipped 1 provider reference stored in a remote file (location), not fetched"
        self.conn.execute("UPDATE source_files SET warnings = ? WHERE id = 2", (warning,))
        self.assertEqual([s["warnings"] for s in list_sources(self.conn)], [None, warning])

    def test_older_databases_without_a_warnings_column_still_list(self):
        self.conn.execute("ALTER TABLE source_files DROP COLUMN warnings")
        sources = list_sources(self.conn)
        self.assertEqual([s["id"] for s in sources], [1, 2])
        self.assertEqual([s["warnings"] for s in sources], [None, None])


class PlanTest(unittest.TestCase):
    """#1: a plan (a row of plans; plan_id alone isn't unique) limits the search to its in-network file."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        build_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_lists_every_plan_by_name_with_its_id_and_a_label(self):
        plans = list_plans(self.conn)
        self.assertEqual([(p["id"], p["label"]) for p in plans], [
            (2, "Ambetter from Fidelis Care Bronze · 25303NY005"),
            (3, "Ambetter from Fidelis Care Gold Zero Cost Sharing · 25303NY017"),
            (1, "Essential Plan 1 · 25303EP000"),
        ])

    def test_plan_info_gives_its_files_and_how_many_plans_share_each(self):
        info = plan_info(self.conn, 2)
        self.assertEqual((info["plan_name"], info["plan_id"], info["source_file_ids"]),
                         ("Ambetter from Fidelis Care Bronze", "25303NY005", [2]))
        self.assertEqual([(f["short_name"], f["plan_count"]) for f in info["files"]], [("fidelis-ex", 2)])
        self.assertIsNone(plan_info(self.conn, 99))

    def test_find_plans_by_label_id_or_part_of_the_name(self):
        plans = list_plans(self.conn)
        ids = lambda text: [p["id"] for p in find_plans(plans, text)]
        self.assertEqual(ids("Ambetter from Fidelis Care Bronze · 25303NY005"), [2])
        self.assertEqual(ids("25303ny017"), [3])
        self.assertEqual(ids("essential plan 1"), [1])
        self.assertEqual(ids("bronze"), [2])
        self.assertEqual(ids("Ambetter"), [2, 3])
        self.assertEqual(ids("nothing"), [])

    def test_an_exact_name_wins_over_longer_names_containing_it(self):
        self.conn.execute("UPDATE plans SET plan_name = 'Ambetter from Fidelis Care Bronze HSA' WHERE id = 3")
        self.assertEqual([p["id"] for p in find_plans(list_plans(self.conn), "Ambetter from Fidelis Care Bronze")], [2])


class ColumnFilterTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        build_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def names(self, **filters):
        return [r["business_name"] for r in search_rates(self.conn, "10006", filters=filters)["results"]]

    def test_text_filters_match_part_of_the_value_ignoring_case(self):
        self.assertEqual(self.names(provider="samaritan"), ["Samaritan Medical Center"])
        self.assertEqual(self.names(npi="13967"), ["Minkowitz Consulting Pathologists PC", "Samaritan Medical Center"])
        self.assertEqual(self.names(modifiers="59", service_codes="11"),
                         ["Minkowitz Consulting Pathologists PC", "Samaritan Medical Center"])
        self.assertEqual(self.names(provider="nobody"), [])

    def test_ein_matches_with_or_without_the_dash(self):
        self.assertEqual(len(self.names(ein="113471596")), 2)
        self.assertEqual(len(self.names(ein="11-347")), 2)

    def test_exact_filters_and_rate_range(self):
        self.assertEqual(len(self.names(billing_class="professional", setting="outpatient")), 2)
        self.assertEqual(self.names(billing_class="institutional"), [])
        self.assertEqual(self.names(rate_min=70), ["Samaritan Medical Center"])
        self.assertEqual(self.names(rate_max=70), ["Minkowitz Consulting Pathologists PC"])

    def test_like_wildcards_are_taken_literally(self):
        self.assertEqual(self.names(provider="%"), [])
        self.assertEqual(self.names(provider="_"), [])

    def test_blank_filters_are_ignored_and_total_reflects_filters(self):
        result = search_rates(self.conn, "10006", filters={"provider": "  ", "rate_min": 70})
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["filters"], {"rate_min": 70})

    def test_unknown_filter_is_rejected(self):
        with self.assertRaises(ValueError):
            search_rates(self.conn, "10006", filters={"id; DROP": "1"})


class FilterOperatorTest(unittest.TestCase):
    """contains / not_contains / equals / not_equals on every text column."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        build_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def names(self, code="10006", **filters):
        return [r["business_name"] for r in search_rates(self.conn, code, filters=filters)["results"]]

    def test_contains_is_the_default(self):
        self.assertEqual(self.names(provider="samaritan", provider_op="contains"), ["Samaritan Medical Center"])
        self.assertEqual(self.names(provider="samaritan", provider_op=""), ["Samaritan Medical Center"])

    def test_does_not_contain(self):
        self.assertEqual(self.names(provider="samaritan", provider_op="not_contains"),
                         ["Minkowitz Consulting Pathologists PC"])
        self.assertEqual(self.names(npi="13967", npi_op="not_contains"), [])

    def test_equals_matches_the_whole_value_ignoring_case(self):
        self.assertEqual(self.names(provider="samaritan", provider_op="equals"), [])
        self.assertEqual(self.names(provider="samaritan medical center", provider_op="equals"),
                         ["Samaritan Medical Center"])
        self.assertEqual(len(self.names(npi="1396747861", npi_op="equals")), 2)
        self.assertEqual(len(self.names(billing_class="professional", billing_class_op="equals")), 2)

    def test_does_not_equal(self):
        self.assertEqual(self.names(provider="Samaritan Medical Center", provider_op="not_equals"),
                         ["Minkowitz Consulting Pathologists PC"])
        self.assertEqual(self.names(provider="samaritan", provider_op="not_equals"),
                         ["Minkowitz Consulting Pathologists PC", "Samaritan Medical Center"])
        self.assertEqual(self.names(setting="outpatient", setting_op="not_equals"), [])

    def test_negative_operators_keep_rows_with_no_value(self):
        # 10040's only rate has no NPI, EIN or provider name.
        for column, value in (("provider", "samaritan"), ("ein", "11-3471596"), ("npi", "1396747861")):
            with self.subTest(column=column):
                for op in ("not_contains", "not_equals"):
                    self.assertEqual(len(self.names("10040", **{column: value, f"{column}_op": op})), 1)
                for op in ("contains", "equals"):
                    self.assertEqual(self.names("10040", **{column: value, f"{column}_op": op}), [])

    def test_ein_ignores_the_dash_for_every_operator(self):
        for needle in ("113471596", "11-3471596"):
            with self.subTest(needle=needle):
                self.assertEqual(len(self.names(ein=needle, ein_op="equals")), 2)
                self.assertEqual(self.names(ein=needle, ein_op="not_equals"), [])
                self.assertEqual(self.names(ein=needle[:5], ein_op="not_contains"), [])
        self.assertEqual(len(self.names(ein="13-47", ein_op="contains")), 2)  # dash typed in the wrong place

    def test_wildcards_are_literal_for_every_operator(self):
        for op in ("contains", "equals"):
            self.assertEqual(self.names(provider="%", provider_op=op), [])
            self.assertEqual(self.names(provider="_", provider_op=op), [])
        self.assertEqual(len(self.names(provider="%", provider_op="not_contains")), 2)
        self.assertEqual(len(self.names(provider="Samaritan Medical Cente_", provider_op="not_equals")), 2)

    def test_applied_filters_report_the_operator(self):
        result = search_rates(self.conn, "10006", filters={"provider": "samaritan", "provider_op": "not_contains"})
        self.assertEqual(result["filters"], {"provider": "samaritan", "provider_op": "not_contains"})

    def test_unknown_operator_is_rejected(self):
        with self.assertRaises(ValueError):
            search_rates(self.conn, "10006", filters={"provider": "x", "provider_op": "starts_with"})
        with self.assertRaises(ValueError):
            search_rates(self.conn, "10006", filters={"rate_min_op": "equals"})


class ColumnChoicesTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        build_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_few_values_become_a_dropdown_of_the_real_values(self):
        choices = column_choices(self.conn, "10006")
        self.assertEqual(choices["provider"]["control"], "select")
        self.assertEqual(choices["provider"]["values"],
                         ["Minkowitz Consulting Pathologists PC", "Samaritan Medical Center"])
        self.assertEqual(choices["npi"]["values"], ["1396747861"])  # as text, like the box
        self.assertEqual(choices["ein"]["values"], ["11-3471596"])
        self.assertEqual(set(choices), {"provider", "ein", "npi", "billing_class", "setting",
                                        "service_codes", "modifiers", "procedure", "billed_as"})

    def test_control_depends_on_how_many_distinct_values(self):
        choices = column_choices(self.conn, "10006", select_limit=1, suggest_limit=1)
        self.assertEqual(choices["provider"],  # 2 values: over both limits
                         {"distinct": 2, "values": None, "control": "text", "has_empty": False})
        middle = column_choices(self.conn, "10006", select_limit=1, suggest_limit=2)["provider"]
        self.assertEqual(middle, {"distinct": 2, "control": "suggest", "has_empty": False,
                                  "values": ["Minkowitz Consulting Pathologists PC", "Samaritan Medical Center"]})

    def test_a_column_with_one_value_everywhere_has_nothing_to_filter(self):
        choices = column_choices(self.conn, "10006")
        self.assertEqual(choices["setting"], {"distinct": 1, "values": ["outpatient"], "control": "fixed",
                                              "has_empty": False})
        self.assertEqual(choices["billing_class"]["control"], "fixed")

    def test_one_value_plus_empty_rows_can_still_be_filtered(self):
        # 10006 and 10040 together: EIN is 11-3471596 or empty (10040 has no provider).
        self.conn.execute("UPDATE rates SET billing_code = '10006' WHERE billing_code = '10040'")
        ein = column_choices(self.conn, "10006")["ein"]
        self.assertEqual((ein["distinct"], ein["has_empty"], ein["control"]), (1, True, "select"))

    def test_a_column_that_is_always_empty_has_nothing_to_filter(self):
        provider = column_choices(self.conn, "10040")["provider"]
        self.assertEqual((provider["distinct"], provider["has_empty"], provider["control"]), (0, True, "fixed"))

    def test_default_limits_are_50_and_2000(self):
        import inspect
        params = inspect.signature(column_choices).parameters
        self.assertEqual((params["select_limit"].default, params["suggest_limit"].default), (50, 2000))

    def test_scope_is_the_code_and_picked_files_not_other_filters(self):
        self.assertEqual(column_choices(self.conn, "10006", [2])["provider"]["values"],
                         ["Minkowitz Consulting Pathologists PC"])
        # 10040's rate has no provider: nothing to choose, and empty values never count.
        self.assertEqual(column_choices(self.conn, "10040")["provider"],
                         {"distinct": 0, "values": [], "control": "fixed", "has_empty": True})



class SourceFileNamesTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        build_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_short_name_keeps_the_part_that_tells_files_apart(self):
        self.assertEqual(short_name("2026-09-28_centene-management-company-llc_fidelis-ex_in-network.json"), "fidelis-ex")
        self.assertEqual(short_name("rates.json"), "rates.json")

    def test_sources_carry_their_short_name(self):
        self.assertEqual([s["short_name"] for s in list_sources(self.conn)], ["fidelis-es", "fidelis-ex"])

    def test_results_say_how_many_files_they_came_from(self):
        self.assertEqual(search_rates(self.conn, "10006")["file_count"], 2)
        self.assertEqual(search_rates(self.conn, "10006", source_file_ids=[2])["file_count"], 1)
        self.assertEqual(search_rates(self.conn, "10040")["file_count"], 1)


class CodeTypeTest(unittest.TestCase):
    """731 is two codes: CPT anesthesia and RC (revenue code) ECG monitoring."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        build_db(self.conn)
        add_code_731(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_lists_each_type_a_number_has_with_its_name(self):
        self.assertEqual(code_types(self.conn, "731"), [
            {"billing_code_type": "CPT", "name": "ANES UPR GI NDSC PX NOS", "rate_count": 1},
            {"billing_code_type": "RC", "name": "EKG/ECG Electrocardiogram Holter monitor", "rate_count": 1},
        ])
        self.assertEqual(len(code_types(self.conn, "10006")), 1)

    def test_search_keeps_to_one_type(self):
        result = search_rates(self.conn, "731", code_type="RC")
        self.assertEqual([r["business_name"] for r in result["results"]], ["Med Alliance Medical Health Services"])
        self.assertEqual(result["code_type"], "RC")
        self.assertEqual(code_info(self.conn, "731", "RC")["name"], "EKG/ECG Electrocardiogram Holter monitor")
        self.assertEqual(column_choices(self.conn, "731", code_type="RC")["billing_class"]["values"], ["institutional"])

    def test_no_type_means_every_type(self):
        self.assertEqual(search_rates(self.conn, "731")["total"], 2)

class ListCodesTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        build_db(self.conn)
        self.codes = list_codes(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_lists_each_code_once_with_its_name_and_rate_count(self):
        self.assertEqual(
            [(c["billing_code"], c["billing_code_type"], c["name"], c["rate_count"]) for c in self.codes],
            [("10006", "CPT", "FNA BX W/US GDN EA ADDL", 2), ("10040", "CPT", "ACNE SURGERY", 1)],
        )

    def test_filter_matches_code_or_name_or_description_ignoring_case(self):
        self.assertEqual([c["billing_code"] for c in filter_codes(self.codes, "1000")], ["10006"])
        self.assertEqual([c["billing_code"] for c in filter_codes(self.codes, "acne")], ["10040"])
        self.assertEqual([c["billing_code"] for c in filter_codes(self.codes, "needle aspiration")], ["10006"])

    def test_codes_sort_by_any_column(self):
        self.assertEqual([c["billing_code"] for c in sort_codes(self.codes, "rates", "desc")], ["10006", "10040"])
        self.assertEqual([c["billing_code"] for c in sort_codes(self.codes, "name", "asc")], ["10040", "10006"])
        self.assertEqual([c["billing_code"] for c in sort_codes(self.codes, "code", "desc")], ["10040", "10006"])
        with self.assertRaises(ValueError):
            sort_codes(self.codes, "bogus", "asc")

    def test_blank_filter_keeps_every_code(self):
        self.assertEqual(len(filter_codes(self.codes, "  ")), 2)



class SuggestTest(unittest.TestCase):
    """#23: the NPI and EIN boxes suggest real values, limited to the billing code typed so far."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        build_db(self.conn)
        add_code_10040(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_npis_start_with_the_digits_typed_and_carry_a_name(self):
        found = suggest_npis(self.conn, "13967")
        self.assertEqual([s["value"] for s in found], ["1396747861"])
        # Two names in two files: the first, and how many there are.
        self.assertEqual((found[0]["label"], found[0]["names"], found[0]["rates"]),
                         ("Minkowitz Consulting Pathologists PC", 2, 2))

    def test_npis_limited_to_the_code_most_rates_first(self):
        self.assertEqual([s["value"] for s in suggest_npis(self.conn, "", billing_code="10006")], ["1396747861"])
        self.assertEqual(suggest_npis(self.conn, "13967", billing_code="10040"), [])
        top = suggest_npis(self.conn, "1", billing_code="10040", limit=3)
        self.assertEqual([(s["value"], s["rates"]) for s in top],
                         [("1033684915", 2), ("1215566856", 2), ("1417360249", 2)])  # ties by NPI
        self.assertGreater(len(suggest_npis(self.conn, "1", limit=100)), len(suggest_npis(self.conn, "1", billing_code="10040", limit=100)))

    def test_eins_match_with_or_without_the_dash(self):
        self.assertEqual([s["value"] for s in suggest_eins(self.conn, "11-3")],
                         ["11-3471596", "11-3211425", "11-3460764"])  # most rates first
        for q in ("11-34", "11347", "113471596"):
            with self.subTest(q=q):
                self.assertEqual([s["value"] for s in suggest_eins(self.conn, q, billing_code="10006")],
                                 ["11-3471596"])
        self.assertEqual(suggest_eins(self.conn, "11346", billing_code="10006"), [])
        self.assertEqual(suggest_eins(self.conn, "x"), [])
        self.assertEqual(suggest_npis(self.conn, "12a"), [])


if __name__ == "__main__":
    unittest.main()


class PriceGroupsTest(unittest.TestCase):
    """Rates for one code grouped by (modifiers, billing class, place of service): each group prices one kind of
    service (see docs/modifiers.md, "Comparison rules")."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        build_db(self.conn)
        add_code_10040(self.conn)
        add_code_99213(self.conn)
        add_code_731(self.conn)

    def tearDown(self):
        self.conn.close()

    def summary(self, groups):
        return [(g["modifiers"], g["service_codes"], g["rate_count"], g["provider_count"], g["min_rate"], g["max_rate"])
                for g in groups]

    def test_each_group_names_its_billing_class(self):
        self.assertEqual({g["billing_class"] for g in price_groups(self.conn, "10040")}, {"professional"})
        self.assertEqual([g["billing_class"] for g in price_groups(self.conn, "731", code_type="RC")], ["institutional"])

    def test_real_10040_rates_fall_into_four_modifier_groups_most_rates_first(self):
        self.assertEqual(self.summary(price_groups(self.conn, "10040")), [
            ("51", "11", 23, 2, 145.97, 145.97),
            ("59", "11", 16, 14, 69.67, 140.17),  # the one fidelis-ex rate has no provider name
            ("79,51", "11", 16, 2, 144.12, 144.12),
            ("XS", "11", 7, 1, 145.97, 145.97),
        ])

    def test_place_of_service_splits_a_modifier_group(self):
        self.assertEqual(self.summary(price_groups(self.conn, "99213")), [
            ("25", "11", 4, 2, 66.14, 231.3),
            ("25", "02", 2, 2, 51.45, 66.74),
            ("25,95", "02,10", 1, 1, 51.45, 51.45),
        ])

    def test_groups_ignore_the_modifier_and_place_filters_but_respect_the_others(self):
        filters = {"modifiers": "51", "modifiers_op": "equals", "service_codes": "99", "service_codes_op": "equals",
                   "billing_class": "institutional", "billing_class_op": "equals", "provider": "teladoc"}
        self.assertEqual(self.summary(price_groups(self.conn, "10040", filters=filters)), [
            ("51", "11", 13, 1, 145.97, 145.97),
            ("79,51", "11", 5, 1, 144.12, 144.12),
        ])
        self.assertEqual(len(price_groups(self.conn, "10040", filters={"rate_max": 100})), 1)

    def test_groups_respect_files_and_code_type(self):
        self.assertEqual(self.summary(price_groups(self.conn, "10040", source_file_ids=[2])),
                         [("59", "11", 1, 0, 103.51, 103.51)])
        self.assertEqual(self.summary(price_groups(self.conn, "731", code_type="RC")),
                         [("", "50", 1, 1, 167.81, 167.81)])  # no modifier is a group of its own

    def test_keep_group_filters_gives_the_groups_in_the_results(self):
        filters = {"modifiers": "51", "modifiers_op": "contains"}
        self.assertEqual([g["modifiers"] for g in price_groups(self.conn, "10040", filters=filters,
                                                                keep_group_filters=True)], ["51", "79,51"])

    def test_no_modifier_is_a_group_of_its_own(self):
        add_code_97151(self.conn)
        self.assertEqual(self.summary(price_groups(self.conn, "97151")), [
            ("", "11", 21, 4, 1.0, 26.69),
            ("GT", "11,12,02", 2, 1, 22.25, 22.25),
            ("GT", "11,99,12,02", 2, 1, 26.0, 26.0),
            ("", "12", 1, 1, 19.07, 19.07),
            ("GT", "02", 1, 1, 26.0, 26.0),
        ])

    def test_unknown_code_has_no_groups(self):
        self.assertEqual(price_groups(self.conn, "99999"), [])


class ProcedureTest(unittest.TestCase):
    """#14: revenue-code (RC) rates name their procedure (CPT/HCPCS) in additional_information. In an RC search it
    is part of the like-for-like group; a CPT search also finds the hospital rates filed under an RC with it."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        build_db(self.conn)
        add_code_99213(self.conn)
        add_clinic_99213(self.conn)
        add_code_260(self.conn)
        add_code_731(self.conn)

    def tearDown(self):
        self.conn.close()

    def summary(self, groups):
        return [(g["billed_as"], g["procedure"], g["service_codes"], g["rate_count"], g["min_rate"], g["max_rate"])
                for g in groups]

    def test_rc_groups_split_by_procedure(self):
        self.assertEqual(self.summary(price_groups(self.conn, "260", code_type="RC")), [
            ("RC 0260", "96361", "23", 3, 192.44, 223.53),
            ("RC 0260", "96361", "22,23", 2, 130.06, 246.52),
            ("RC 0260", "96365", "23", 2, 355.92, 549.82),
            ("RC 0260", "", "22", 1, 7.96, 7.96),
            ("RC 0260", "", "23", 1, 7.79, 7.79),
            ("RC 0260", "96360", "22,23", 1, 564.28, 564.28),
            ("RC 0260", "96366", "22,23", 1, 78.94, 78.94),
            ("RC 0260", "96366", "23", 1, 111.18, 111.18),
        ])

    def test_procedure_is_a_filter_and_a_result_column(self):
        result = search_rates(self.conn, "260", code_type="RC", filters={"procedure": "96365", "procedure_op": "equals"})
        self.assertEqual([(r["procedure"], r["negotiated_rate"]) for r in result["results"]],
                         [("96365", 355.92), ("96365", 549.82)])
        choices = column_choices(self.conn, "260", code_type="RC")["procedure"]
        self.assertEqual((choices["values"], choices["has_empty"]), (["96360", "96361", "96365", "96366"], True))
        by_procedure = search_rates(self.conn, "260", code_type="RC", sort="procedure", direction="desc")
        self.assertEqual(by_procedure["results"][0]["procedure"], "96366")

    def test_a_cpt_search_also_finds_hospital_rates_filed_under_an_rc(self):
        result = search_rates(self.conn, "99213")
        self.assertEqual(result["total"], 7 + 9)
        billed = {(r["billed_as"], r["procedure"]) for r in result["results"]}
        self.assertEqual(billed, {("CPT 99213", None), ("RC 0510", "99213"), ("RC 0521", "99213")})
        self.assertEqual(search_rates(self.conn, "99213", include_facility=False)["total"], 7)

    def test_hospital_rates_are_groups_of_their_own(self):
        self.assertEqual(self.summary(price_groups(self.conn, "99213", code_type="CPT")), [
            ("CPT 99213", "", "11", 4, 66.14, 231.3),
            ("RC 0521", "99213", "50", 4, 77.71, 104.19),
            ("RC 0510", "99213", "50", 3, 113.25, 118.2),
            ("CPT 99213", "", "02", 2, 51.45, 66.74),
            ("RC 0510", "99213", "22", 2, 264.81, 269.06),
            ("CPT 99213", "", "02,10", 1, 51.45, 51.45),
        ])
        medians = {(g["billed_as"], g["service_codes"]): g["median"]
                   for g in stats_by_group(self.conn, "99213", code_type="CPT")}
        self.assertAlmostEqual(medians[("RC 0521", "50")], 90.95)
        only = rate_stats(self.conn, "99213", code_type="CPT",
                          filters={"billed_as": "RC 0510", "billed_as_op": "equals", "service_codes": "22",
                                   "service_codes_op": "equals"})
        self.assertEqual(only["count"], 2)
        self.assertAlmostEqual(only["median"], 266.935)
        self.assertEqual(len(price_groups(self.conn, "99213", code_type="CPT", include_facility=False)), 3)
        self.assertEqual(column_choices(self.conn, "99213", code_type="CPT")["billed_as"]["values"],
                         ["CPT 99213", "RC 0510", "RC 0521"])

    def test_an_rc_search_never_adds_other_codes(self):
        self.assertEqual(search_rates(self.conn, "731", code_type="RC")["total"], 1)
        self.assertEqual(search_rates(self.conn, "731", code_type="CPT")["total"], 1)

    def test_procedure_names_come_from_the_codes_own_cpt_or_hcpcs_rates(self):
        self.assertEqual(procedure_names(self.conn, ["96365", "96361", "99213", ""]),
                         {"96365": "THER/PROPH/DIAG IV INF INIT", "99213": "OFFICE/OUTPATIENT VISIT EST"})

    def test_department_names_come_from_the_rcs_own_mixed_case_name(self):
        # 510's "Clinic General" is the department; 521 has only procedure-style (upper-case) names here.
        self.assertEqual(department_names(self.conn, ["510", "521", "260"]), {"510": "Clinic", "260": "IV Therapy"})
        self.assertEqual(department_names(self.conn), {"510": "Clinic", "260": "IV Therapy",
                                                      "731": "EKG/ECG Electrocardiogram Holter monitor"})


class PriceDriversTest(unittest.TestCase):
    """#22A: each group's median relative to a reference group. #22A+: how much of the spread in a code's prices
    (log scale) place of service and modifier account for (eta squared)."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        build_db(self.conn)
        add_code_10040(self.conn)
        add_code_97151(self.conn)
        add_code_99213(self.conn)
        add_clinic_99213(self.conn)
        add_code_260(self.conn)

    def tearDown(self):
        self.conn.close()

    def relative(self, code, **kwargs):
        return [(g["modifiers"], g["procedure"], g["service_codes"], round(g["relative"], 4), g["reference"])
                for g in stats_by_group(self.conn, code, **kwargs)]

    def test_reference_is_the_largest_no_modifier_group_of_the_codes_own_billing_class(self):
        self.assertEqual(self.relative("97151"), [
            ("", "", "11", 1.0, True),  # median $19.75
            ("GT", "", "11,12,02", round(22.25 / 19.75, 4), False),
            ("GT", "", "11,99,12,02", round(26.0 / 19.75, 4), False),
            ("", "", "12", round(19.07 / 19.75, 4), False),
            ("GT", "", "02", round(26.0 / 19.75, 4), False),
        ])

    def test_a_revenue_codes_reference_is_institutional(self):
        rows = self.relative("260", code_type="RC")
        self.assertEqual(rows[0], ("", "96361", "23", 1.0, True))  # 3 rates, median $215.43
        self.assertIn(("", "96365", "23", round(452.87 / 215.43, 4), False), rows)

    def test_without_a_no_modifier_group_the_largest_group_is_the_reference(self):
        rows = self.relative("10040")
        self.assertEqual(rows[0], ("51", "", "11", 1.0, True))  # 23 rates, median $145.97
        self.assertEqual(rows[1], ("59", "", "11", round(69.67 / 145.97, 4), False))
        # 99213 here: no no-modifier group; the largest, 25 in the office (4 rates, median $231.30), ties with a
        # hospital group and comes first.
        self.assertEqual(self.relative("99213", code_type="CPT")[0], ("25", "", "11", 1.0, True))

    def test_a_hospital_group_is_never_the_reference_for_a_cpt(self):
        # Rates from $70: the largest group is RC 0521 (4 rates), but the reference is the code's own 25 in the
        # office (3 rates of $231.30), its own billing class.
        rows = stats_by_group(self.conn, "99213", code_type="CPT", filters={"rate_min": 70})
        self.assertEqual((rows[0]["billed_as"], rows[0]["reference"]), ("RC 0521", False))
        reference = [g for g in rows if g["reference"]]
        self.assertEqual([(g["billed_as"], g["modifiers"], g["service_codes"]) for g in reference],
                         [("CPT 99213", "25", "11")])
        self.assertAlmostEqual(rows[0]["relative"], 90.95 / 231.3)

    def test_share_of_log_price_variation_explained(self):
        # 99213's 7 own rates (hospital rates left out), hand-computed: modifier 18.14%, place 63.44%, both 63.44%.
        explained = price_explained(self.conn, "99213", code_type="CPT", min_count=5)
        self.assertEqual(explained["count"], 7)
        self.assertAlmostEqual(explained["modifier"], 0.181444, places=5)
        self.assertAlmostEqual(explained["place"], 0.634382, places=5)
        self.assertAlmostEqual(explained["both"], 0.634382, places=5)
        self.assertAlmostEqual(explained["other"], 1 - 0.634382, places=5)
        # All 62 rates of 10040 are in the office: place explains nothing, the modifier 63.48%.
        acne = price_explained(self.conn, "10040")
        self.assertEqual((acne["count"], acne["place"]), (62, 0.0))
        self.assertAlmostEqual(acne["modifier"], 0.634813, places=5)

    def test_fully_explained_is_exactly_all_and_never_past_it(self):
        # Teladoc's 10040 rates: one price per modifier group, so the groups explain everything.
        teladoc = price_explained(self.conn, "10040", filters={"provider": "teladoc"}, min_count=5)
        self.assertEqual((teladoc["modifier"], teladoc["both"], teladoc["other"]), (1.0, 1.0, 0.0))

    def test_nothing_to_explain_with_few_rates_or_one_group(self):
        self.assertIsNone(price_explained(self.conn, "99213", code_type="CPT"))  # 7 rates < 30
        self.assertIsNone(price_explained(self.conn, "10040", filters={"provider": "ramapo"}, min_count=5))  # all XS
        self.assertIsNone(price_explained(self.conn, "10040", filters={"provider": "teladoc", "modifiers": "79"},
                                          min_count=50))

    def test_explained_follows_the_other_filters_but_not_the_group_filters(self):
        whole = price_explained(self.conn, "10040")
        self.assertEqual(price_explained(self.conn, "10040", filters={"modifiers": "51", "modifiers_op": "equals"}),
                         whole)
        mount = price_explained(self.conn, "10040", filters={"provider": "mount"}, min_count=5)
        self.assertEqual(mount["count"], 21)


class OutlierTest(unittest.TestCase):
    """#25: Tukey's fences, 1.5 x IQR beyond the quartiles, in rate_stats and stats_by_group."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        build_db(self.conn)
        add_code_10040(self.conn)
        add_code_97151(self.conn)
        add_code_99213(self.conn)
        add_code_260(self.conn)

    def tearDown(self):
        self.conn.close()

    def fences(self, stats):
        return (round(stats["low_fence"], 6), round(stats["high_fence"], 6), stats["low_outliers"],
                stats["high_outliers"])

    def test_high_outliers(self):
        # RC 260's 12 rates: Q1 = 78.94 + .75 x (111.18 - 78.94) = 103.12, Q3 = 246.52 + .25 x (355.92 - 246.52)
        # = 273.87, IQR 170.75: fences 103.12 - 256.125 = -153.005 and 273.87 + 256.125 = 529.995; 549.82 and
        # 564.28 lie above.
        self.assertEqual(self.fences(rate_stats(self.conn, "260", code_type="RC")), (-153.005, 529.995, 0, 2))

    def test_low_outliers(self):
        # 10040's 62 rates: Q1 = 140.17 + .25 x (144.12 - 140.17) = 141.1575, Q3 = 145.97, IQR 4.8125: fences
        # 133.93875 and 153.18875; the 10 rates of $69.67 and the one of $103.51 lie below.
        self.assertEqual(self.fences(rate_stats(self.conn, "10040")), (133.93875, 153.18875, 11, 0))

    def test_each_group_has_quartiles_and_outliers(self):
        groups = stats_by_group(self.conn, "99213", code_type="CPT")
        office = groups[0]  # 25 in the office: 66.14 and 3 x 231.30
        self.assertEqual((office["modifiers"], office["service_codes"]), ("25", "11"))
        # Q1 = 66.14 + .75 x (231.30 - 66.14) = 190.01, Q3 = 231.30, IQR 41.29: fences 128.075 and 293.235.
        self.assertEqual((round(office["q1"], 6), office["q3"]), (190.01, 231.3))
        self.assertEqual(self.fences(office), (128.075, 293.235, 1, 0))
        self.assertEqual(self.fences(groups[1]), (43.805, 74.385, 0, 0))  # 25 by telehealth: 51.45 and 66.74


class EmptyOperatorTest(unittest.TestCase):
    """empty / not_empty: whether a text column has a value at all (NULL and "" both count as empty)."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        build_db(self.conn)
        add_code_731(self.conn)

    def tearDown(self):
        self.conn.close()

    def rates(self, code, **filters):
        return [r["negotiated_rate"] for r in search_rates(self.conn, code, filters=filters)["results"]]

    def test_empty_matches_null(self):
        # Fixture 10040's fidelis-ex rate has no NPI, EIN or provider name (NULL).
        self.assertEqual(self.rates("10040", provider_op="empty"), [103.51])
        self.assertEqual(self.rates("10040", npi_op="not_empty"), [])
        self.assertEqual(self.rates("10006", ein_op="empty"), [])
        self.assertEqual(self.rates("10006", ein_op="not_empty"), [69.85, 76.84])

    def test_empty_matches_an_empty_string(self):
        # 731 RC (ECG monitoring) has no modifier, stored as "".
        self.assertEqual(self.rates("731", modifiers_op="empty"), [167.81])
        self.assertEqual(self.rates("731", modifiers_op="not_empty"), [23.81])

    def test_the_value_is_ignored(self):
        self.assertEqual(self.rates("731", modifiers="AA", modifiers_op="empty"), [167.81])
        result = search_rates(self.conn, "731", filters={"modifiers_op": "empty", "modifiers": ""})
        self.assertEqual(result["filters"], {"modifiers_op": "empty"})

    def test_existing_operators_still_need_a_value(self):
        result = search_rates(self.conn, "731", filters={"modifiers_op": "equals"})
        self.assertEqual((result["total"], result["filters"]), (2, {}))


class MeaningsTest(unittest.TestCase):
    def test_modifiers_are_described_one_by_one(self):
        self.assertEqual(describe_modifiers("79,51"), [
            ("79", "unrelated procedure in the follow-up period", "surgical_role"),
            ("51", "multiple procedures", "work_amount"),
        ])
        self.assertEqual(describe_modifiers("ZZ"), [("ZZ", None, None)])  # unknown: just the code
        self.assertEqual(describe_modifiers(""), [])

    def test_tooltip_gives_label_and_category_one_line_per_modifier(self):
        self.assertEqual(modifier_tooltip("26"),
                         "26 · professional component (reading the result) — component: a fraction of the whole service")
        self.assertEqual(modifier_tooltip("26,ZZ").splitlines()[1], "ZZ")
        self.assertEqual(modifier_tooltip(""), "no modifier")

    def test_places_of_service_have_a_short_label_and_a_meaning(self):
        self.assertEqual(place_label("11"), "office (11)")
        self.assertEqual(place_label("02,10"), "telehealth (02), telehealth at home (10)")
        self.assertEqual(place_label("98"), "98")
        self.assertEqual(describe_places("02"), [("02", "Telehealth provided other than in patient's home")])


class RateStatsTest(unittest.TestCase):
    """Statistics over every rate that matches the search, not just the page shown."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        build_db(self.conn)
        add_code_10040(self.conn)
        add_code_97151(self.conn)
        add_code_731(self.conn)

    def tearDown(self):
        self.conn.close()

    # 97151 with no modifier in the office: 21 real rates from $1.00 to $26.69.
    OFFICE_NO_MODIFIER = {"modifiers_op": "empty", "service_codes": "11", "service_codes_op": "equals"}

    def test_counts_and_quartiles_of_real_rates(self):
        stats = rate_stats(self.conn, "97151", filters=self.OFFICE_NO_MODIFIER)
        self.assertEqual(
            {k: stats[k] for k in ("count", "provider_count", "ein_count", "npi_count", "distinct_rates",
                                   "min", "q1", "median", "q3", "iqr", "max")},
            {"count": 21, "provider_count": 4, "ein_count": 4, "npi_count": 4, "distinct_rates": 11,
             "min": 1.0, "q1": 10.0, "median": 19.75, "q3": 23.0, "iqr": 13.0, "max": 26.69})
        self.assertNotIn("mean", stats)  # no averages: an average can be a price nobody negotiated

    def test_histogram_bins_cover_min_to_max_and_count_every_rate(self):
        stats = rate_stats(self.conn, "97151", filters=self.OFFICE_NO_MODIFIER)
        bins = stats["histogram"]
        self.assertEqual(len(bins), 20)
        self.assertEqual(sum(b["count"] for b in bins), 21)
        self.assertEqual((bins[0]["low"], bins[-1]["high"]), (1.0, 26.69))
        self.assertEqual(bins[0]["count"], 2)   # the two $1.00 rates
        self.assertEqual(bins[-1]["count"], 2)  # the two $26.69 rates, max included in the last bin

    def test_density_curve_spans_the_range_in_count_units(self):
        stats = rate_stats(self.conn, "97151", filters=self.OFFICE_NO_MODIFIER)
        curve = stats["density"]
        self.assertEqual((curve[0]["x"], curve[-1]["x"]), (1.0, 26.69))
        self.assertTrue(all(p["y"] >= 0 for p in curve))
        # Scaled to the histogram: its area is close to count x bin width (a little leaks past min and max).
        width = (26.69 - 1.0) / 20
        step = curve[1]["x"] - curve[0]["x"]
        area = sum(p["y"] for p in curve) * step
        self.assertLess(area, 21 * width)
        self.assertGreater(area, 0.6 * 21 * width)

    def test_identical_rates_have_one_bin(self):
        stats = rate_stats(self.conn, "10040", filters={"modifiers": "51", "modifiers_op": "equals"})
        self.assertEqual((stats["count"], stats["min"], stats["q1"], stats["median"], stats["q3"], stats["max"],
                          stats["iqr"], stats["distinct_rates"]), (23, 145.97, 145.97, 145.97, 145.97, 145.97, 0, 1))
        self.assertEqual(stats["histogram"], [{"low": 145.97, "high": 145.97, "count": 23}])

    def test_a_single_rate(self):
        stats = rate_stats(self.conn, "731", code_type="RC")
        self.assertEqual((stats["count"], stats["median"], stats["iqr"]), (1, 167.81, 0))

    def test_follows_filters_files_and_the_chosen_group(self):
        stats = rate_stats(self.conn, "10040", filters={"modifiers": "59", "modifiers_op": "equals"})
        self.assertEqual((stats["count"], stats["min"], stats["q1"], stats["median"], stats["q3"], stats["max"]),
                         (16, 69.67, 69.67, 69.67, 140.17, 140.17))
        self.assertEqual(stats["npi_count"], 15)  # the fidelis-ex rate has no NPI
        self.assertEqual(rate_stats(self.conn, "10040", source_file_ids=[2])["count"], 1)
        self.assertEqual(rate_stats(self.conn, "10040", filters={"provider": "teladoc"})["count"], 18)

    def test_no_rates_no_stats(self):
        self.assertIsNone(rate_stats(self.conn, "99999"))
        self.assertIsNone(rate_stats(self.conn, "10040", filters={"rate_min": 1000}))

    def test_stats_by_group_summarise_each_group_with_its_median(self):
        rows = stats_by_group(self.conn, "97151")
        self.assertEqual([(g["modifiers"], g["service_codes"], g["rate_count"], g["provider_count"], g["min_rate"],
                           g["median"], g["max_rate"]) for g in rows], [
            ("", "11", 21, 4, 1.0, 19.75, 26.69),
            ("GT", "11,12,02", 2, 1, 22.25, 22.25, 22.25),
            ("GT", "11,99,12,02", 2, 1, 26.0, 26.0, 26.0),
            ("", "12", 1, 1, 19.07, 19.07, 19.07),
            ("GT", "02", 1, 1, 26.0, 26.0, 26.0),
        ])
        # Like price_groups: the group filters are ignored, the others are not.
        self.assertEqual(len(stats_by_group(self.conn, "97151", filters={"modifiers": "GT"})), 5)
        self.assertEqual([g["rate_count"] for g in stats_by_group(self.conn, "97151", filters={"rate_max": 5})], [2])


class PercentileTest(unittest.TestCase):
    """High (and one low) percentiles, by the same inclusive interpolation as the quartiles."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        build_db(self.conn)
        add_code_97151(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_exact_values_on_two_real_rates(self):
        # Fixture 10006: $69.85 and $76.84, so each percentile is 69.85 + p x 6.99.
        stats = rate_stats(self.conn, "10006")
        for key, expected in (("p1", 69.9199), ("p90", 76.141), ("p95", 76.4905), ("p99", 76.7701)):
            with self.subTest(key=key):
                self.assertAlmostEqual(stats[key], expected)
        self.assertIsNone(stats["p99_9"])   # 2 rates: far too few
        self.assertIsNone(stats["p99_99"])

    def test_same_method_as_the_quartiles(self):
        values = sorted(rate for rate, *_ in BEHAVIOR_ID_ROWS)  # all 27 real 97151 rates
        self.assertEqual((percentile(values, .25), percentile(values, .5), percentile(values, .75)), quartiles(values))
        self.assertEqual((percentile(values, 0), percentile(values, 1)), (values[0], values[-1]))
        self.assertEqual(percentile([145.97], .999), 145.97)

    def test_the_tail_percentiles_need_enough_rates(self):
        self.assertEqual(PERCENTILE_MIN_COUNTS, {"p99_9": 1000, "p99_99": 10000})
        values = sorted(rate for rate, *_ in BEHAVIOR_ID_ROWS)
        self.assertEqual(len(values), 27)
        shown = percentiles(values, min_counts={"p99_9": 27, "p99_99": 28})
        self.assertAlmostEqual(shown["p99_9"], percentile(values, .999))
        self.assertIsNone(shown["p99_99"])
        defaults = percentiles(values)
        self.assertIsNone(defaults["p99_9"])
        self.assertIsNone(defaults["p99_99"])
        self.assertEqual(defaults["p99"], percentile(values, .99))


class IqrSpreadTest(unittest.TestCase):
    def test_iqr_over_median_with_the_side_each_quartile_sits(self):
        # 99213 · mod 25 · office (11), real quartiles: the middle half spans 6.9% of the median, lopsided upward.
        spread = iqr_spread(98.44, 99.88, 105.37)
        self.assertAlmostEqual(spread["iqr_over_median"], 6.93 / 99.88)
        self.assertAlmostEqual(spread["q1_below_median"], 1.44 / 99.88)
        self.assertAlmostEqual(spread["q3_above_median"], 5.49 / 99.88)

    def test_no_ratio_without_a_median_to_divide_by(self):
        self.assertEqual(iqr_spread(0, 0, 0),
                         {"iqr_over_median": None, "q1_below_median": None, "q3_above_median": None})


class NamesInScopeTest(unittest.TestCase):
    """names_in_scope: who a narrowed procedure page names (an EIN or NPI's business names for the code here)."""

    def test_an_eins_names_for_the_code_with_their_rate_counts(self):
        from api.search import names_in_scope
        from tests.api import add_code_97151
        conn = sqlite3.connect(":memory:")
        build_db(conn)
        add_code_97151(conn)
        self.assertEqual(names_in_scope(conn, "97151", filters={"ein": "47-3981360"}),
                         [("Encore Licensed Behavior Analyst, PLLC", "47-3981360", 17)])
        self.assertEqual(names_in_scope(conn, "97151", filters={"ein": "99-9999999"}), [])
