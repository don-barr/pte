"""Search in (#43): All networks, or one in-network file named by its plans, plus "Find your plan", which only ever
selects that plan's file and says so. Real fixture rows (tests.api: file 1 fidelis-es, file 2 fidelis-ex) and real
plan names from the Fidelis index."""

import sqlite3
import unittest

from api.search_in import find_plans, network_label, resolve_search_in, search_in_choices
from tests.api import build_db


class NetworkLabelTest(unittest.TestCase):
    def test_the_common_leading_words_of_a_files_plans(self):
        # Real names from fidelis-ex, including the index's own "fromFidelis" typo.
        self.assertEqual(network_label(["Ambetter from Fidelis Care Bronze", "Ambetter from Fidelis Care Gold Zero Cost Sharing"],
                                       "fidelis-ex"), "Ambetter")
        self.assertEqual(network_label(["Ambetter from Fidelis Care Bronze", "Ambetter fromFidelis Care Silver 250 Enhanced"],
                                       "fidelis-ex"), "Ambetter")
        self.assertEqual(network_label(["Essential Plan 1", "Essential Plan 4"], "fidelis-es"), "Essential Plan")
        self.assertEqual(network_label(["Essential Plan 1"], "fidelis-es"), "Essential Plan")

    def test_falls_back_to_the_short_file_name(self):
        self.assertEqual(network_label([], "fidelis-es"), "fidelis-es")
        self.assertEqual(network_label(["Essential Plan 1", "Ambetter from Fidelis Care Bronze"], "mixed"), "mixed")


class SearchInTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        build_db(self.conn)
        self.bronze = self.conn.execute("SELECT id FROM plans WHERE plan_id = '25303NY005'").fetchone()[0]
        self.essential = self.conn.execute("SELECT id FROM plans WHERE plan_id = '25303EP000'").fetchone()[0]

    def test_three_choices_all_networks_and_each_file_named_by_its_plans(self):
        choices = search_in_choices(self.conn)
        self.assertEqual([(c["value"], c["label"]) for c in choices], [
            ("", "All networks · both files · 3 plans"),
            ("file-1", "Essential Plan · fidelis-es · 1 plan"),
            ("file-2", "Ambetter · fidelis-ex · 2 plans"),
        ])
        self.assertEqual((choices[2]["name"], choices[2]["detail"]), ("Ambetter", "fidelis-ex · 2 plans"))

    def test_all_networks(self):
        scope = resolve_search_in(self.conn, "")
        self.assertEqual((scope["value"], scope["selected"], scope["file_ids"], scope["error"], scope["plan_note"]),
                         ("", "", None, None, None))
        self.assertEqual(scope["statement"], "All networks (fidelis-es + fidelis-ex, 3 plans) in the active index")
        self.conn.execute("INSERT INTO index_info (id, reporting_entity_name, last_updated_on)"
                          " VALUES (1, 'Centene Management Company LLC', '2026-09-28')")
        self.assertEqual(resolve_search_in(self.conn, None)["statement"],
                         "All networks (fidelis-es + fidelis-ex, 3 plans) in the active index"
                         " (Centene Management Company LLC, 2026-09-28)")

    def test_one_file(self):
        scope = resolve_search_in(self.conn, "file-2")
        self.assertEqual((scope["value"], scope["selected"], scope["file_ids"]), ("file-2", "file-2", [2]))
        self.assertEqual(scope["statement"], "the Ambetter network (fidelis-ex, 2 plans)")
        self.assertIsNone(scope["plan_note"])

    def test_a_plan_selects_its_file_and_says_so(self):
        scope = resolve_search_in(self.conn, f"plan-{self.bronze}")
        self.assertEqual((scope["value"], scope["selected"], scope["file_ids"]), (f"plan-{self.bronze}", "file-2", [2]))
        self.assertEqual(scope["statement"], "the Ambetter network (fidelis-ex, 2 plans)")
        self.assertEqual(scope["plan_note"], "Ambetter from Fidelis Care Bronze (25303NY005) is in the Ambetter network"
                                             " (fidelis-ex); every plan there shares these rates.")

    def test_the_only_plan_in_its_file(self):
        scope = resolve_search_in(self.conn, f"plan-{self.essential}")
        self.assertEqual(scope["selected"], "file-1")
        self.assertEqual(scope["plan_note"], "Essential Plan 1 (25303EP000) is in the Essential Plan network (fidelis-es).")

    def test_an_unknown_choice_says_so_and_searches_all_networks(self):
        for raw in ("plan-999", "file-9", "nonsense"):
            with self.subTest(raw=raw):
                scope = resolve_search_in(self.conn, raw)
                self.assertEqual((scope["value"], scope["selected"], scope["file_ids"]), ("", "", None))
                self.assertIn("isn’t in the active index", scope["error"])

    def test_find_your_plan_by_name_or_id(self):
        found = find_plans(self.conn, "bronze")
        self.assertEqual(found["total"], 1)
        self.assertEqual(found["plans"], [{
            "value": f"plan-{self.bronze}", "plan_id": "25303NY005", "plan_name": "Ambetter from Fidelis Care Bronze",
            "network": "Ambetter", "short_name": "fidelis-ex"}])
        self.assertEqual([p["plan_id"] for p in find_plans(self.conn, "25303ep")["plans"]], ["25303EP000"])
        self.assertEqual([p["network"] for p in find_plans(self.conn, "")["plans"]], ["Ambetter", "Ambetter", "Essential Plan"])
        self.assertEqual(find_plans(self.conn, "", limit=1)["total"], 3)
        self.assertEqual(find_plans(self.conn, "platinum")["plans"], [])


if __name__ == "__main__":
    unittest.main()
