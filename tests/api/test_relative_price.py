"""#44: the relative price ("× ref") reference group is chosen by what Count by counts (provider offers, NPI offers or
rows), within the current scope, and the group table's header says which group it is and why."""

import html
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from api.grouping import grouped_stats_by_group
from app import create_app
from tests.api import FIXTURE_FILE, INSERT_RATE_WITH_INFO, build_db

G2211 = ("G2211", "HCPCS", "2022", "VISIT CPLX INHERENT E&M ASSO", "VISIT CPLX INHERENT E&M ASSOC WITH MED CARE SRVC")

# Real G2211 rates copied from the loaded DB (its file ids: fidelis-ex 1, fidelis-es 2): (file, rate, place of
# service, modifiers, provider reference, NPI, EIN, business name). In fidelis-ex, mod 95 · office (11) is 22 rows but 4
# provider offers (4 EINs); mod GT · telehealth (02) is 27 rows but only 3 offers (one EIN lists 21 NPIs at one price),
# like the full code there (GT · 02: 1,370 rows, 4 offers; 95 · 11: 255 rows, 76 offers). No group has no modifier.
G2211_ROWS = [
    (1, 20.71, '11', '95', 607, 1194205252, '11-3211425', 'Beacon IPA LLC'),
    (1, 20.71, '11', '95', 607, 1447755285, '11-3211425', 'Beacon IPA LLC'),
    (1, 20.71, '11', '95', 607, 1710656178, '11-3211425', 'Beacon IPA LLC'),
    (1, 20.71, '11', '95', 607, 1851977516, '11-3211425', 'Beacon IPA LLC'),
    (1, 24.25, '11', '95', 1197, 1053609982, '13-2739694', 'Charles B. Wang Community Health Center'),
    (1, 24.25, '11', '95', 1197, 1194180661, '13-2739694', 'Charles B. Wang Community Health Center'),
    (1, 24.25, '11', '95', 1197, 1295470805, '13-2739694', 'Charles B. Wang Community Health Center'),
    (1, 24.25, '11', '95', 1197, 1437735297, '13-2739694', 'Charles B. Wang Community Health Center'),
    (1, 24.25, '11', '95', 1197, 1588014039, '13-2739694', 'Charles B. Wang Community Health Center'),
    (1, 24.25, '11', '95', 1197, 1609495423, '13-2739694', 'Charles B. Wang Community Health Center'),
    (1, 24.25, '11', '95', 1197, 1609859750, '13-2739694', 'Charles B. Wang Community Health Center'),
    (1, 24.25, '11', '95', 1197, 1730493305, '13-2739694', 'Charles B. Wang Community Health Center'),
    (1, 24.25, '11', '95', 1197, 1821674557, '13-2739694', 'Charles B. Wang Community Health Center'),
    (1, 24.25, '11', '95', 1197, 1821731894, '13-2739694', 'Charles B. Wang Community Health Center'),
    (1, 24.25, '11', '95', 1197, 1912014945, '13-2739694', 'Charles B. Wang Community Health Center'),
    (1, 24.25, '11', '95', 1197, 1922444710, '13-2739694', 'Charles B. Wang Community Health Center'),
    (1, 20.71, '11', '95', 607, 1013585637, '26-2950781', 'Digestive Disease Care, PC'),
    (1, 20.71, '11', '95', 607, 1093957342, '26-2950781', 'Digestive Disease Care, PC'),
    (1, 20.71, '11', '95', 607, 1902061146, '26-2950781', 'Digestive Disease Care, PC'),
    (1, 20.71, '11', '95', 607, 1063434298, '56-2325021', 'Lefferts Medical Associates, PC'),
    (1, 20.71, '11', '95', 607, 1356586333, '56-2325021', 'Lefferts Medical Associates, PC'),
    (1, 20.71, '11', '95', 607, 1760043970, '56-2325021', 'Lefferts Medical Associates, PC'),
    (1, 17.48, '02', 'GT', 600, 1356398713, '37-1603148', 'Corinthian Medical IPA, Inc'),
    (1, 17.48, '02', 'GT', 1369, 1003126020, '38-3931355', 'Icahn School of Medicine at Mount Sinai'),
    (1, 17.48, '02', 'GT', 600, 1023067576, '38-3931355', 'Cardiology Department of Mount Sinai BI'),
    (1, 17.48, '02', 'GT', 1369, 1053449686, '38-3931355', 'Icahn School of Medicine at Mount Sinai'),
    (1, 17.48, '02', 'GT', 600, 1215921606, '38-3931355', 'Cardiology Department of Mount Sinai BI'),
    (1, 17.48, '02', 'GT', 1369, 1235397613, '38-3931355', 'Icahn School of Medicine at Mount Sinai'),
    (1, 17.48, '02', 'GT', 600, 1346310489, '38-3931355', 'Cardiology Department of Mount Sinai BI'),
    (1, 17.48, '02', 'GT', 1369, 1356503346, '38-3931355', 'Icahn School of Medicine at Mount Sinai'),
    (1, 17.48, '02', 'GT', 1048, 1386957215, '38-3931355', 'Icahn School of Medicine at Mount Sinai'),
    (1, 17.48, '02', 'GT', 600, 1396846929, '38-3931355', 'Cardiology Department of Mount Sinai BI'),
    (1, 17.48, '02', 'GT', 600, 1528206166, '38-3931355', 'Cardiology Department of Mount Sinai BI'),
    (1, 17.48, '02', 'GT', 1048, 1528220712, '38-3931355', 'Icahn School of Medicine at Mount Sinai'),
    (1, 17.48, '02', 'GT', 600, 1649377698, '38-3931355', 'Cardiology Department of Mount Sinai BI'),
    (1, 17.48, '02', 'GT', 600, 1790767762, '38-3931355', 'Cardiology Department of Mount Sinai BI'),
    (1, 17.48, '02', 'GT', 600, 1801845359, '38-3931355', 'Cardiology Department of Mount Sinai BI'),
    (1, 17.48, '02', 'GT', 600, 1861475816, '38-3931355', 'Cardiology Department of Mount Sinai BI'),
    (1, 17.48, '02', 'GT', 600, 1871580761, '38-3931355', 'Cardiology Department of Mount Sinai BI'),
    (1, 17.48, '02', 'GT', 600, 1881687622, '38-3931355', 'Cardiology Department of Mount Sinai BI'),
    (1, 17.48, '02', 'GT', 600, 1891860672, '38-3931355', 'Cardiology Department of Mount Sinai BI'),
    (1, 17.48, '02', 'GT', 600, 1942522511, '38-3931355', 'Cardiology Department of Mount Sinai BI'),
    (1, 17.48, '02', 'GT', 1369, 1962768960, '38-3931355', 'Icahn School of Medicine at Mount Sinai'),
    (1, 17.48, '02', 'GT', 600, 1972761591, '38-3931355', 'Cardiology Department of Mount Sinai BI'),
    (1, 17.48, '02', 'GT', 600, 1316446289, '80-0886397', 'Icahn School Of Medicine At Mount Sinai'),
    (1, 17.48, '02', 'GT', 600, 1609309061, '80-0886397', 'Icahn School Of Medicine At Mount Sinai'),
    (1, 17.48, '02', 'GT', 1369, 1689105736, '80-0886397', 'Anesthesiology Department of Mount Sinai'),
    (1, 17.48, '02', 'GT', 600, 1801237508, '80-0886397', 'Icahn School Of Medicine At Mount Sinai'),
    (1, 17.48, '02', 'GT', 600, 1871524892, '80-0886397', 'Icahn School Of Medicine At Mount Sinai'),
    # fidelis-es: one more GT · 02 offer, so across both files GT · 02 ties 95 · 11 at 4 offers.
    (2, 17.48, '02', 'GT', 1652, 1154908325, '38-3931355', 'Physician Affiliate Group of New York PC'),
]
EX = FIXTURE_FILE[1]  # fidelis-ex in the fixture DB


def add_code_g2211(conn: sqlite3.Connection) -> None:
    conn.executemany(INSERT_RATE_WITH_INFO, [
        (FIXTURE_FILE[f], *G2211, "negotiated", rate, "professional", "outpatient", pos, modifiers, "9999-12-31",
         ref, npi, ein, name, None)
        for f, rate, pos, modifiers, ref, npi, ein, name in G2211_ROWS])
    conn.commit()


class ReferenceByCountUnitTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        build_db(self.conn)
        add_code_g2211(self.conn)

    def tearDown(self):
        self.conn.close()

    def groups(self, group_by, files=(EX,), **kwargs):
        return {(g["modifiers"], g["service_codes"]): g for g in grouped_stats_by_group(
            self.conn, "G2211", list(files) if files else None, group_by=group_by, **kwargs)}

    def reference(self, group_by, files=(EX,), **kwargs):
        return [k for k, g in self.groups(group_by, files, **kwargs).items() if g["reference"]]

    def test_by_provider_offers_the_group_with_more_offers_is_the_reference(self):
        groups = self.groups("provider")
        self.assertEqual({k: (g["rate_count"], g["count"]) for k, g in groups.items()},
                         {("95", "11"): (22, 4), ("GT", "02"): (27, 3)})
        self.assertEqual(self.reference("provider"), [("95", "11")])
        # Median of the 4 offers $20.71, $20.71, $20.71, $24.25.
        self.assertEqual(groups[("95", "11")]["median"], 20.71)
        self.assertAlmostEqual(groups[("GT", "02")]["relative"], 17.48 / 20.71)
        self.assertEqual(groups[("95", "11")]["relative"], 1.0)

    def test_by_npi_offers_and_by_rows_the_unit_decides(self):
        self.assertEqual({k: g["count"] for k, g in self.groups("npi").items()}, {("95", "11"): 22, ("GT", "02"): 27})
        self.assertEqual(self.reference("npi"), [("GT", "02")])
        self.assertEqual(self.reference("none"), [("GT", "02")])

    def test_reference_is_chosen_within_the_scope(self):
        # Both files: GT · 02 gains fidelis-es's offer, 4 to 4, and a tie goes to the group with more rows.
        self.assertEqual({k: g["count"] for k, g in self.groups("provider", files=None).items()},
                         {("95", "11"): 4, ("GT", "02"): 4})
        self.assertEqual(self.reference("provider", files=None), [("GT", "02")])
        # A price filter is scope too: from $20, only 95 · 11 is left.
        self.assertEqual(self.reference("provider", filters={"rate_min": 20}), [("95", "11")])

    def test_the_reference_says_why_it_was_chosen(self):
        g = self.groups("provider")[("95", "11")]
        self.assertEqual(g["reference_rule"], "own_class")  # no professional group without a modifier


class ReferenceHeaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "rates.db"
        conn = sqlite3.connect(db_path)
        build_db(conn)
        add_code_g2211(conn)
        conn.close()
        self.client = TestClient(create_app(db_path))

    def tearDown(self):
        self.tmp.cleanup()

    def header(self, **params):
        text = self.client.get("/by-procedure/G2211", params={"in": f"file-{EX}", **params}).text
        th = re.search(r'<th class="num ref sortable"[^>]*title="([^"]*)"[^>]*>(.*?)</th>', text, re.S)
        self.assertIsNotNone(th, "the × ref header")
        return html.unescape(th.group(1)), " ".join(re.sub(r"<[^>]+>|[↕▲▼]", " ", th.group(2)).split())

    def test_header_names_the_reference_and_its_tooltip_says_why(self):
        title, label = self.header()
        self.assertEqual(label, "× Ref mod 95 · office (11)")
        self.assertIn("mod 95 · office (11), 4 provider prices", title)
        self.assertIn("no professional group without a modifier, so the professional group with the most "
                      "provider prices", title)

    def test_counted_by_npi_the_reference_follows(self):
        title, label = self.header(group_by="npi")
        self.assertEqual(label, "× Ref mod GT · telehealth (02)")
        self.assertIn("27 NPI prices", title)
        self.assertIn("with the most NPI prices", title)


if __name__ == "__main__":
    unittest.main()
