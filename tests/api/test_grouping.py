import sqlite3
import unittest

from api.grouping import (
    GROUP_BY, grouped_stats_by_group, NPI_LIST_CAP, grouped_rates, grouped_stats, search_grouped, stats_for_search, tukey_fences,
)
from api.search import rate_stats, scope_where, search_rates
from etl.schema import create_schema

# The two real files, with the ids they have in data/parsed/rates.db.
DOC = "https://www.centene.com/content/dam/centene/Centene%20Corporate/json/DOCUMENT/"
SOURCE_FILES = [
    (1, DOC + "2026-09-28_centene-management-company-llc_fidelis-ex_in-network.json", "Centene Management Company LLC",
     "Third Party Administrator", "2.0.0", "2026-10-06"),
    (2, DOC + "2026-09-28_centene-management-company-llc_fidelis-es_in-network.json", "Centene Management Company LLC",
     "Third Party Administrator", "2.0.0", "2026-10-06"),
]

V = ("99214", "CPT", "2022", "OFFICE/OUTPATIENT VISIT EST",
     "OFFICE OR OTH OUTPATIENT VISIT FOR THE EVALUATION AND MANAGEMENT OF AN ESTABLISHED PATIENT, WHICH RE",
     "negotiated")

# Real 99214 rows, copied verbatim from the loaded DB: (file, rate, place of service, modifiers, provider reference,
# NPI, EIN, business name). additional_information is NULL, billing class professional, setting outpatient,
# expiration 9999-12-31 for all of them.
ROWS_99214 = [
    # Modifier 25 in the office (11): seven organization offers in 15 rows.
    (2, 161.64, '11', '25', 1355, 1780670919, '11-2475877', 'CS Medical Associates P.C. dba Victory Recovery Partner'),
    (1, 120.7, '11', '25', 253, 1033366059, '11-3188758', 'Mattoo & Bhat Medical Associates, PC'),
    (1, 120.7, '11', '25', 253, 1326068198, '11-3188758', 'Mattoo & Bhat Medical Associates, PC'),
    (1, 120.7, '11', '25', 253, 1346216124, '11-3188758', 'Mattoo & Bhat Medical Associates, PC'),
    # One NPI under two names in one file (two provider references): one NPI offer, two names.
    (2, 265.0, '11', '25', 227, 1073505384, '13-4000192', 'Memorial Sloan Kettering Cancer Center at Basking Ridge'),
    (2, 265.0, '11', '25', 235, 1073505384, '13-4000192', 'Samaritan Hospital of Troy, New York'),
    (2, 137.42, '11', '25', 1690, 1891781688, '14-1338307', 'Westchester Medical Center Advanced Physician Services'),
    (1, 120.7, '11', '25', 253, 1093783078, '20-1453791', 'Jon D Kirwin MD PC'),
    (1, 143.97, '11', '25', 985, 1124012075, '25-0815795', 'Guthrie Medical Group PC'),
    (1, 143.97, '11', '25', 985, 1225383029, '25-0815795', 'Guthrie Medical Group PC'),
    (1, 143.97, '11', '25', 985, 1396373023, '25-0815795', 'Guthrie Medical Group PC'),
    (1, 143.97, '11', '25', 985, 1619287596, '25-0815795', 'Guthrie Medical Group PC'),
    (1, 143.97, '11', '25', 985, 1689977720, '25-0815795', 'Guthrie Medical Group PC'),
    (1, 143.97, '11', '25', 985, 1962478842, '25-0815795', 'Guthrie Medical Group PC'),
    (1, 156.34, '11', '25', 1281, 1427478130, '27-2872791', 'City Medical of Upper East Side, PLLC'),
    # Telehealth (GT, 02), the same $68.82 in both files under different names.
    (1, 68.82, '02', 'GT', 1202, 1295886612, '13-4194078', 'Dmitry K. Nesen, MD'),
    (2, 68.82, '02', 'GT', 1655, 1295886612, '13-4194078', 'Four Season Medical PLLC'),
    (1, 68.82, '02', 'GT', 1202, 1215530670, '81-5263887', 'Kalter Physical Medicine & Rehabilitation PC'),
    (2, 68.82, '02', 'GT', 1655, 1215530670, '81-5263887', 'Rochester General Hospital'),
    (1, 68.82, '02', 'GT', 1202, 1396751558, '81-5263887', 'Kalter Physical Medicine & Rehabilitation PC'),
    (2, 68.82, '02', 'GT', 1655, 1396751558, '81-5263887', 'Rochester General Hospital'),
    (1, 68.82, '02', 'GT', 1202, 1518086222, '81-5263887', 'Kalter Physical Medicine & Rehabilitation PC'),
    (2, 68.82, '02', 'GT', 1655, 1518086222, '81-5263887', 'Rochester General Hospital'),
]

OFFICE_25 = {"modifiers": "25", "modifiers_op": "equals", "service_codes": "11", "service_codes_op": "equals"}
TELEHEALTH_GT = {"modifiers": "GT", "modifiers_op": "equals"}


def build_grouping_db(conn: sqlite3.Connection) -> None:
    create_schema(conn)
    conn.executemany(
        "INSERT INTO source_files (id, url, reporting_entity_name, reporting_entity_type, version, last_updated_on)"
        " VALUES (?, ?, ?, ?, ?, ?)", SOURCE_FILES)
    conn.executemany(
        "INSERT INTO rates (source_file_id, billing_code, billing_code_type, billing_code_type_version, name,"
        " description, negotiated_type, negotiated_rate, billing_class, setting, service_codes, modifiers,"
        " expiration_date, provider_reference_id, npi, ein, business_name, additional_information)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(sid, *V, rate, "professional", "outpatient", pos, mods, "9999-12-31", ref, npi, ein, name, None)
         for sid, rate, pos, mods, ref, npi, ein, name in ROWS_99214])
    conn.commit()


def interpolate(values, p):
    """Hand percentile, inclusive method: position (n - 1) x p."""
    pos = (len(values) - 1) * p
    i = int(pos)
    j = min(i + 1, len(values) - 1)
    return values[i] + (values[j] - values[i]) * (pos - i)


class GroupingTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        build_grouping_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def scope(self, filters):
        return scope_where("99214", filters=filters)


class GroupedRatesTest(GroupingTestCase):
    def test_group_by_choices(self):
        self.assertEqual(GROUP_BY, ("provider", "npi", "none"))

    def test_line_counts_per_group_by(self):
        where, args = self.scope(OFFICE_25)
        self.assertEqual(grouped_rates(self.conn, where, args, "provider")["total"], 7)
        self.assertEqual(grouped_rates(self.conn, where, args, "npi")["total"], 14)
        self.assertEqual(grouped_rates(self.conn, where, args, "none")["total"], 15)
        where, args = self.scope(TELEHEALTH_GT)
        # The same EIN and price in two files is two offers (one per file).
        self.assertEqual(grouped_rates(self.conn, where, args, "provider")["total"], 4)
        self.assertEqual(grouped_rates(self.conn, where, args, "npi")["total"], 8)
        self.assertEqual(grouped_rates(self.conn, where, args, "none")["total"], 8)

    def test_provider_line_lists_its_npis_names_and_rows(self):
        where, args = self.scope(TELEHEALTH_GT)
        lines = grouped_rates(self.conn, where, args, "provider", sort="ein")["results"]
        kalter = [l for l in lines if l["ein"] == "81-5263887" and l["source_file_id"] == 1]
        self.assertEqual(len(kalter), 1)
        line = kalter[0]
        self.assertEqual(line["names"], ["Kalter Physical Medicine & Rehabilitation PC"])
        self.assertEqual(line["npis"], [1215530670, 1396751558, 1518086222])
        self.assertEqual(line["npi_count"], 3)
        self.assertEqual(line["row_count"], 3)
        self.assertEqual(line["negotiated_rate"], 68.82)
        self.assertEqual(line["modifiers"], "GT")
        self.assertEqual(line["service_codes"], "02")
        self.assertEqual(line["billing_class"], "professional")
        self.assertEqual(line["source_file"],
                         "2026-09-28_centene-management-company-llc_fidelis-ex_in-network.json")

    def test_names_merged_within_one_offer(self):
        where, args = self.scope(OFFICE_25)
        for group_by in ("provider", "npi"):
            lines = grouped_rates(self.conn, where, args, group_by)["results"]
            msk = [l for l in lines if l["ein"] == "13-4000192"]
            self.assertEqual(len(msk), 1, group_by)
            self.assertEqual(msk[0]["names"], ["Memorial Sloan Kettering Cancer Center at Basking Ridge",
                                               "Samaritan Hospital of Troy, New York"])
            self.assertEqual(msk[0]["row_count"], 2)
            self.assertEqual(msk[0]["npi"], 1073505384)

    def test_npi_lines_are_one_per_npi(self):
        where, args = self.scope(OFFICE_25)
        lines = grouped_rates(self.conn, where, args, "npi")["results"]
        guthrie = [l for l in lines if l["ein"] == "25-0815795"]
        self.assertEqual(sorted(l["npi"] for l in guthrie),
                         [1124012075, 1225383029, 1396373023, 1619287596, 1689977720, 1962478842])
        self.assertTrue(all(l["row_count"] == 1 and l["names"] == ["Guthrie Medical Group PC"] for l in guthrie))

    def test_sorts_and_pages(self):
        where, args = self.scope(OFFICE_25)
        result = grouped_rates(self.conn, where, args, "provider", sort="rate", direction="desc", limit=3)
        self.assertEqual([l["negotiated_rate"] for l in result["results"]], [265.0, 161.64, 156.34])
        self.assertEqual(result["total"], 7)
        page2 = grouped_rates(self.conn, where, args, "provider", sort="rate", direction="desc", limit=3, offset=3)
        self.assertEqual([l["negotiated_rate"] for l in page2["results"]], [143.97, 137.42, 120.7])
        by_npis = grouped_rates(self.conn, where, args, "provider", sort="npi_count", direction="desc")
        self.assertEqual(by_npis["results"][0]["ein"], "25-0815795")
        by_name = grouped_rates(self.conn, where, args, "provider", sort="provider")
        self.assertEqual(by_name["results"][0]["names"], ["CS Medical Associates P.C. dba Victory Recovery Partner"])
        with self.assertRaises(ValueError):
            grouped_rates(self.conn, where, args, "provider", sort="nonsense")
        with self.assertRaises(ValueError):
            grouped_rates(self.conn, where, args, "clinic")

    def test_none_returns_rows(self):
        where, args = self.scope(OFFICE_25)
        result = grouped_rates(self.conn, where, args, "none", limit=100)
        self.assertEqual(len(result["results"]), 15)
        self.assertEqual(result["results"][0]["negotiated_rate"], 120.7)
        self.assertTrue(all(l["row_count"] == 1 for l in result["results"]))

    def test_npi_list_is_capped(self):
        self.assertEqual(NPI_LIST_CAP, 50)


class GroupedStatsTest(GroupingTestCase):
    # Office, modifier 25: the values each way, by hand from ROWS_99214.
    ROWS = sorted([161.64, 120.7, 120.7, 120.7, 265.0, 265.0, 137.42, 120.7] + [143.97] * 6 + [156.34])
    NPI_OFFERS = sorted([161.64, 120.7, 120.7, 120.7, 265.0, 137.42, 120.7] + [143.97] * 6 + [156.34])
    ORG_OFFERS = [120.7, 120.7, 137.42, 143.97, 156.34, 161.64, 265.0]

    def test_counts_and_quartiles_by_group_by(self):
        where, args = self.scope(OFFICE_25)
        provider = grouped_stats(self.conn, where, args, "provider")
        self.assertEqual((provider["count"], provider["row_count"], provider["group_by"]), (7, 15, "provider"))
        # Organization offers: Q1 at position 1.5 = 129.06, median 143.97, Q3 at 4.5 = 158.99.
        self.assertAlmostEqual(provider["q1"], 129.06)
        self.assertAlmostEqual(provider["median"], 143.97)
        self.assertAlmostEqual(provider["q3"], 158.99)
        self.assertNotIn("mean", provider)  # no averages: every statistic is a price or between two
        self.assertEqual((provider["min"], provider["max"]), (120.7, 265.0))

        npi = grouped_stats(self.conn, where, args, "npi")
        self.assertEqual((npi["count"], npi["row_count"]), (14, 15))
        # 14 NPI offers: Q1 at 3.25 = 124.88, median at 6.5 = 143.97, Q3 at 9.75 = 143.97.
        self.assertAlmostEqual(npi["q1"], 124.88)
        self.assertAlmostEqual(npi["median"], 143.97)
        self.assertAlmostEqual(npi["q3"], 143.97)

        rows = grouped_stats(self.conn, where, args, "none")
        self.assertEqual((rows["count"], rows["row_count"]), (15, 15))
        # 15 rows: Q1 at 3.5 = 129.06, Q3 at 10.5 = 150.155.
        self.assertAlmostEqual(rows["q1"], 129.06)
        self.assertAlmostEqual(rows["q3"], 150.155)

    def test_none_matches_rate_stats(self):
        where, args = self.scope(OFFICE_25)
        mine = grouped_stats(self.conn, where, args, "none")
        theirs = rate_stats(self.conn, "99214", filters=OFFICE_25)
        for key in ("count", "provider_count", "ein_count", "npi_count", "min", "q1", "median", "q3", "max",
                    "distinct_rates", "p1", "p90", "p95", "p99", "p99_9", "p99_99", "histogram", "density"):
            self.assertEqual(mine[key], theirs[key], key)

    def test_distinct_entity_counts_are_over_rows(self):
        where, args = self.scope(OFFICE_25)
        stats = grouped_stats(self.conn, where, args, "provider")
        self.assertEqual((stats["ein_count"], stats["npi_count"], stats["provider_count"]), (7, 14, 8))
        self.assertEqual(stats["distinct_rates"], 6)
        self.assertEqual(sum(b["count"] for b in stats["histogram"]), 7)

    def test_percentiles_over_offers(self):
        where, args = self.scope(OFFICE_25)
        stats = grouped_stats(self.conn, where, args, "provider")
        self.assertAlmostEqual(stats["p90"], interpolate(self.ORG_OFFERS, .90))
        self.assertAlmostEqual(stats["p1"], interpolate(self.ORG_OFFERS, .01))
        # Thresholds still apply, to the number of offers: 7 offers is too few for p99.9 / p99.99.
        self.assertIsNone(stats["p99_9"])
        self.assertIsNone(stats["p99_99"])

    def test_percentile_threshold_counts_offers_not_rows(self):
        where, args = self.scope(OFFICE_25)
        min_counts = {"p99_9": 15}  # met by the 15 rows, not by 14 NPI offers or 7 organization offers
        self.assertIsNotNone(grouped_stats(self.conn, where, args, "none", min_counts=min_counts)["p99_9"])
        self.assertIsNone(grouped_stats(self.conn, where, args, "npi", min_counts=min_counts)["p99_9"])
        self.assertIsNone(grouped_stats(self.conn, where, args, "provider", min_counts=min_counts)["p99_9"])

    def test_tukey_fences_and_outliers(self):
        where, args = self.scope(OFFICE_25)
        # Organization offers: IQR 158.99 - 129.06 = 29.93; fences 129.06 - 44.895, 158.99 + 44.895.
        provider = grouped_stats(self.conn, where, args, "provider")
        self.assertAlmostEqual(provider["low_fence"], 84.165)
        self.assertAlmostEqual(provider["high_fence"], 203.885)
        self.assertEqual((provider["low_outliers"], provider["high_outliers"]), (0, 1))
        # NPI offers: IQR 143.97 - 124.88 = 19.09; fences 96.245 and 172.605; one $265 offer above.
        npi = grouped_stats(self.conn, where, args, "npi")
        self.assertAlmostEqual(npi["low_fence"], 96.245)
        self.assertAlmostEqual(npi["high_fence"], 172.605)
        self.assertEqual((npi["low_outliers"], npi["high_outliers"]), (0, 1))
        # Rows: IQR 150.155 - 129.06 = 21.095; fences 97.4175 and 181.7975; the $265 NPI's two rows above.
        rows = grouped_stats(self.conn, where, args, "none")
        self.assertAlmostEqual(rows["low_fence"], 97.4175)
        self.assertAlmostEqual(rows["high_fence"], 181.7975)
        self.assertEqual((rows["low_outliers"], rows["high_outliers"]), (0, 2))

    def test_tukey_fences_helper(self):
        fences = tukey_fences([1.0, 2.0, 3.0, 4.0, 100.0], 2.0, 4.0)
        self.assertEqual(fences, {"low_fence": -1.0, "high_fence": 7.0, "low_outliers": 0, "high_outliers": 1})

    def test_same_price_everywhere(self):
        where, args = self.scope(TELEHEALTH_GT)
        stats = grouped_stats(self.conn, where, args, "provider")
        self.assertEqual((stats["count"], stats["row_count"]), (4, 8))
        self.assertEqual((stats["min"], stats["median"], stats["max"]), (68.82, 68.82, 68.82))
        self.assertEqual((stats["low_outliers"], stats["high_outliers"]), (0, 0))

    def test_nothing_matches(self):
        where, args = scope_where("00000")
        self.assertIsNone(grouped_stats(self.conn, where, args, "provider"))


class AdapterTest(GroupingTestCase):
    def test_search_grouped_takes_search_rates_arguments(self):
        result = search_grouped(self.conn, "99214", filters=OFFICE_25, group_by="provider", limit=5)
        self.assertEqual((result["total"], result["row_count"], result["group_by"]), (7, 15, "provider"))
        self.assertEqual(len(result["results"]), 5)
        self.assertEqual(result["filters"], search_rates(self.conn, "99214", filters=OFFICE_25)["filters"])
        self.assertEqual(result["billing_code"], "99214")

    def test_search_grouped_none_is_search_rates(self):
        mine = search_grouped(self.conn, "99214", filters=OFFICE_25, group_by="none")
        theirs = search_rates(self.conn, "99214", filters=OFFICE_25)
        self.assertEqual(mine["total"], theirs["total"])
        self.assertEqual([r["npi"] for r in mine["results"]], [r["npi"] for r in theirs["results"]])

    def test_stats_for_search(self):
        stats = stats_for_search(self.conn, "99214", filters=OFFICE_25, group_by="npi")
        self.assertEqual((stats["count"], stats["row_count"], stats["group_by"]), (14, 15, "npi"))



class StatsByGroupTest(GroupingTestCase):
    """The Compare Groups table counts what Count by counts: one value per offer in each group."""

    def by_group(self, group_by):
        return {(g["modifiers"], g["service_codes"]): g for g in grouped_stats_by_group(
            self.conn, "99214", group_by=group_by)}

    def test_each_group_matches_its_own_grouped_stats(self):
        for group_by in GROUP_BY:
            groups = self.by_group(group_by)
            for key, filters in ((("25", "11"), OFFICE_25), (("GT", "02"), TELEHEALTH_GT)):
                with self.subTest(group_by=group_by, group=key):
                    expected = stats_for_search(self.conn, "99214", filters=filters, group_by=group_by)
                    g = groups[key]
                    self.assertEqual(g["count"], expected["count"])
                    self.assertEqual((g["q1"], g["median"], g["q3"]),
                                     (expected["q1"], expected["median"], expected["q3"]))
                    self.assertEqual((g["low_outliers"], g["high_outliers"]),
                                     (expected["low_outliers"], expected["high_outliers"]))
                    self.assertEqual(g["min_rate"], expected["min"])
                    self.assertEqual(g["rate_count"], expected["row_count"])  # rows behind it, always

    def test_offer_counts_and_relative_price(self):
        groups = self.by_group("provider")
        self.assertEqual((groups[("25", "11")]["count"], groups[("GT", "02")]["count"]), (7, 4))
        self.assertTrue(groups[("25", "11")]["reference"])  # the largest group, no unmodified one here
        self.assertAlmostEqual(groups[("GT", "02")]["relative"],
                               groups[("GT", "02")]["median"] / groups[("25", "11")]["median"])
        self.assertEqual(self.by_group("none")[("25", "11")]["count"], 15)


if __name__ == "__main__":
    unittest.main()
