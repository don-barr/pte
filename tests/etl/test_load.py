import copy
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from etl.load import load_file, load_in_network, load_index
from etl.run import read_index_info
from etl.schema import create_schema

TEST_DATA = Path(__file__).parent / "test_data"
BASE = "https://www.centene.com/content/dam/centene/Centene%20Corporate/json/DOCUMENT/"
EX_URL = BASE + "2026-09-28_centene-management-company-llc_fidelis-ex_in-network.json"
ES_URL = BASE + "2026-09-28_centene-management-company-llc_fidelis-es_in-network.json"
REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_FILE = TEST_DATA / "2026-09-28_fidelis_index.json"
EX_FILE = TEST_DATA / "2026-09-28_centene-management-company-llc_fidelis-ex_in-network.json"


def loaded_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_schema(conn)
    index = json.loads((TEST_DATA / "2026-09-28_fidelis_index.json").read_text())
    load_index(conn, index, TEST_DATA)
    return conn


class LoadIndexSourceFilesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = loaded_db()

    def source(self, url):
        return self.conn.execute("SELECT * FROM source_files WHERE url = ?", (url,)).fetchone()

    def test_one_source_file_row_per_in_network_url(self):
        urls = [r["url"] for r in self.conn.execute("SELECT url FROM source_files ORDER BY id")]
        self.assertEqual(urls, [EX_URL, ES_URL])

    def test_loaded_file_carries_its_header_and_local_path(self):
        ex = self.source(EX_URL)
        self.assertEqual(ex["local_path"], str(EX_FILE))
        self.assertEqual(ex["reporting_entity_name"], "Centene Management Company LLC")
        self.assertEqual(ex["reporting_entity_type"], "Third Party Administrator")
        self.assertEqual(ex["version"], "2.0.0")
        self.assertEqual(ex["last_updated_on"], "2026-10-06")
        self.assertIsNotNone(ex["loaded_at"])
        self.assertIsNone(ex["error"])

    def test_plans_point_to_their_file(self):
        ex_id = self.source(EX_URL)["id"]
        es_id = self.source(ES_URL)["id"]
        count = lambda sid: self.conn.execute(
            "SELECT COUNT(*) FROM plans WHERE source_file_id = ?", (sid,)
        ).fetchone()[0]
        self.assertEqual(count(ex_id), 88)
        self.assertEqual(count(es_id), 6)
        bronze = self.conn.execute(
            "SELECT * FROM plans WHERE source_file_id = ? AND plan_name = ?",
            (ex_id, "Ambetter from Fidelis Care Bronze"),
        ).fetchone()
        self.assertEqual(bronze["plan_id"], "25303NY005")
        self.assertEqual(bronze["plan_id_type"], "hios")
        self.assertEqual(bronze["plan_market_type"], "individual")
        self.assertEqual(bronze["issuer_name"], "Fidelis Care")

    def test_missing_file_records_an_error_and_loads_nothing(self):
        es = self.source(ES_URL)
        self.assertIn("No such file", es["error"])
        self.assertEqual(es["rate_count"], 0)
        self.assertIsNone(es["loaded_at"])
        rates = self.conn.execute(
            "SELECT COUNT(*) FROM rates WHERE source_file_id = ?", (es["id"],)
        ).fetchone()[0]
        self.assertEqual(rates, 0)

    def test_rate_count_matches_rows(self):
        ex = self.source(EX_URL)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM rates WHERE source_file_id = ?", (ex["id"],)
        ).fetchone()[0]
        self.assertEqual(ex["rate_count"], rows)
        self.assertEqual(rows, 1828)


class LoadIndexRatesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = loaded_db()

    def rates(self, where, params=()):
        return self.conn.execute(f"SELECT * FROM rates WHERE {where}", params).fetchall()

    def test_single_price_row_has_the_real_values(self):
        (row,) = self.rates("billing_code = '10006'")
        ex_id = self.conn.execute("SELECT id FROM source_files WHERE url = ?", (EX_URL,)).fetchone()[0]
        self.assertEqual(
            dict(row) | {"id": None},
            {
                "id": None,
                "source_file_id": ex_id,
                "billing_code": "10006",
                "billing_code_type": "CPT",
                "billing_code_type_version": "2022",
                "name": "FNA BX W/US GDN EA ADDL",
                "description": "FINE NEEDLE ASPIRATION BX W/US GDN EA ADDL",
                "negotiated_type": "negotiated",
                "negotiated_rate": 69.85,
                "billing_class": "professional",
                "setting": "outpatient",
                "service_codes": "11",
                "modifiers": "59",
                "expiration_date": "9999-12-31",
                "provider_reference_id": 1,
                "npi": 1396747861,
                "ein": "11-3471596",
                "business_name": "Minkowitz Consulting Pathologists PC",
                "additional_information": None,  # a CPT price: the file gives none
            },
        )

    def test_revenue_code_prices_keep_the_procedure_code_from_additional_information(self):
        # RC 470 is the audiology department; each price names its procedure, e.g. CPT 92620.
        by_rate = {r["negotiated_rate"]: r["additional_information"] for r in self.rates("billing_code = '470'")}
        self.assertEqual(by_rate[112.98], "92620")
        self.assertIsNone(by_rate[190.44])  # this price has no additional_information

    def test_duplicated_billing_code_items_are_both_loaded(self):
        names = [r["name"] for r in self.rates("billing_code = '470' ORDER BY id")]
        self.assertEqual(len(names), 6)
        self.assertEqual(
            sorted(set(names)), ["AUDITORY FUNCTION, 60 MIN", "Audiology General"]
        )
        self.assertEqual(self.rates("billing_code = '470'")[0]["modifiers"], "")

    def test_npi_zero_is_dropped(self):
        self.assertEqual(self.rates("npi = 0"), [])
        # DCMH lists 0 plus 5 real NPIs: only the 5 become rows.
        dcmh = self.rates("ein = '13-6007353'")
        self.assertEqual(len(dcmh), 5)
        self.assertNotIn(None, [r["npi"] for r in dcmh])

    def test_group_with_only_npi_zero_gets_one_null_npi_row(self):
        rows = self.rates("ein = '13-4107689'")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["npi"])
        self.assertEqual(rows[0]["business_name"], "Hispanic American Consulting Services, Inc.")
        self.assertEqual(rows[0]["provider_reference_id"], 912)
        self.assertEqual(rows[0]["negotiated_rate"], 25.0)
        self.assertEqual(rows[0]["modifiers"], "AJ")

    def test_nine_digit_npi_is_kept(self):
        rows = self.rates("npi = 124507857")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ein"], "13-2828349")
        self.assertEqual(rows[0]["business_name"], "Sun River Health Inc")


class LoadInNetworkTest(unittest.TestCase):
    def test_unknown_provider_reference_is_skipped(self):
        conn = sqlite3.connect(":memory:")
        create_schema(conn)
        conn.execute("INSERT INTO source_files (id, url) VALUES (1, 'u')")
        data = json.loads(EX_FILE.read_text())
        item = next(i for i in data["in_network"] if i["billing_code"] == "10006")
        item["negotiated_rates"][0]["provider_references"] = [999999, 1]
        data["in_network"] = [item]

        count = load_in_network(conn, 1, data)

        self.assertEqual(count, 1)
        self.assertEqual(
            conn.execute("SELECT provider_reference_id FROM rates").fetchall(), [(1,)]
        )



    def test_modifiers_and_places_are_stored_in_one_order_whatever_the_file_says(self):
        # Real 10040 prices carry "79, 51"; a payer listing the same set as "51, 79" (or places as
        # "11, 22" vs "22, 11") must land in the same price group, so lists are stored sorted.
        conn = sqlite3.connect(":memory:")
        create_schema(conn)
        conn.execute("INSERT INTO source_files (id, url) VALUES (1, 'u')")
        data = json.loads(EX_FILE.read_text())
        item = next(i for i in data["in_network"] if i["billing_code"] == "10006")
        first, second = copy.deepcopy(item), copy.deepcopy(item)
        first["negotiated_rates"][0]["negotiated_prices"][0].update(
            billing_code_modifier=["79", "51"], service_code=["22", "11"])
        second["negotiated_rates"][0]["negotiated_prices"][0].update(
            billing_code_modifier=["51", "79"], service_code=["11", "22"])
        data["in_network"] = [first, second]

        load_in_network(conn, 1, data)

        self.assertEqual(conn.execute("SELECT DISTINCT modifiers, service_codes FROM rates").fetchall(),
                         [("51,79", "11,22")])

class OtherCmsShapesTest(unittest.TestCase):
    """CMS in-network shapes the real slice doesn't use (schema v1.x, see tag v1.6.2 of the
    price-transparency-guide): a negotiated rate with inline provider_groups instead of
    provider_references, and a provider reference pointing at a remote file (location).
    Fixtures are the real EX file reshaped; every value stays real."""

    URL = EX_URL

    def setUp(self):
        self.data = json.loads(EX_FILE.read_text())
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        create_schema(self.conn)

    def load(self, data):
        return load_file(self.conn, self.URL, None, [], json.dumps(data).encode())

    def source(self):
        return self.conn.execute("SELECT * FROM source_files WHERE url = ?", (self.URL,)).fetchone()

    def rows(self, conn):
        cols = "billing_code, name, negotiated_type, negotiated_rate, billing_class, setting, " \
               "service_codes, modifiers, expiration_date, npi, ein, business_name, additional_information"
        return [tuple(r) for r in conn.execute(f"SELECT {cols} FROM rates ORDER BY {cols}")]

    def test_inline_provider_groups_expand_like_referenced_ones(self):
        groups = {ref["provider_group_id"]: ref["provider_groups"] for ref in self.data["provider_references"]}
        inline = copy.deepcopy(self.data)
        del inline["provider_references"]
        for item in inline["in_network"]:
            for rate in item["negotiated_rates"]:
                refs = rate.pop("provider_references")
                rate["provider_groups"] = [g for ref in refs if ref in groups for g in groups[ref]]

        _, count, error = self.load(inline)

        self.assertIsNone(error)
        self.assertEqual(count, 1828)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM rates WHERE provider_reference_id IS NOT NULL").fetchone()[0], 0)
        referenced = sqlite3.connect(":memory:")
        create_schema(referenced)
        load_file(referenced, self.URL, None, [], json.dumps(self.data).encode())
        self.assertEqual(self.rows(self.conn), self.rows(referenced))
        referenced.close()
        # DCMH lists NPI 0 plus 5 real NPIs: inline, it still gives 5 rows and no NPI 0.
        dcmh = self.conn.execute("SELECT npi FROM rates WHERE ein = '13-6007353'").fetchall()
        self.assertEqual(len(dcmh), 5)
        self.assertNotIn((None,), [tuple(r) for r in dcmh])

    def test_remote_location_references_are_skipped_and_counted(self):
        # Reference 1 (Minkowitz Consulting Pathologists) moved to a remote provider-reference file.
        remote = copy.deepcopy(self.data)
        ref = next(r for r in remote["provider_references"] if r["provider_group_id"] == 1)
        del ref["provider_groups"]
        ref["location"] = "https://www.centene.com/provider-reference-1.json"
        full = sqlite3.connect(":memory:")
        create_schema(full)
        load_file(full, self.URL, None, [], json.dumps(self.data).encode())
        (from_ref_1,) = full.execute("SELECT COUNT(*) FROM rates WHERE provider_reference_id = 1").fetchone()
        full.close()
        self.assertGreater(from_ref_1, 0)

        _, count, error = self.load(remote)

        self.assertIsNone(error)
        self.assertEqual(count, 1828 - from_ref_1)
        source = self.source()
        self.assertIsNone(source["error"])
        self.assertEqual(source["rate_count"], count)
        self.assertEqual(
            source["warnings"],
            "skipped 1 provider reference stored in a remote file (location), not fetched",
        )

    def test_only_remote_references_is_an_error(self):
        remote = copy.deepcopy(self.data)
        for ref in remote["provider_references"]:
            del ref["provider_groups"]
            ref["location"] = f"https://www.centene.com/provider-reference-{ref['provider_group_id']}.json"

        _, count, error = self.load(remote)

        self.assertEqual(count, 0)
        self.assertEqual(
            error, "no rates loaded: skipped 9 provider references stored in a remote file "
                   "(location), not fetched")
        source = self.source()
        self.assertEqual(source["error"], error)
        self.assertEqual(source["rate_count"], 0)

    def test_reloading_a_file_clears_its_old_warning(self):
        remote = copy.deepcopy(self.data)
        ref = remote["provider_references"][0]
        del ref["provider_groups"]
        ref["location"] = "https://www.centene.com/provider-reference-1.json"
        self.load(remote)
        self.assertIsNotNone(self.source()["warnings"])

        self.load(self.data)

        self.assertIsNone(self.source()["warnings"])
        self.assertEqual(self.source()["rate_count"], 1828)

class NotAnInNetworkFileTest(unittest.TestCase):
    """A file without an in_network list (e.g. an index given as an in-network file) is an
    error, not a 0-rate success; an in_network list that is present but empty is valid."""

    URL = EX_URL
    NOT_IN_NETWORK = "not an in-network file: no in_network list"

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        create_schema(self.conn)

    def source(self):
        return self.conn.execute("SELECT * FROM source_files WHERE url = ?", (self.URL,)).fetchone()

    def test_an_index_file_is_an_error(self):
        source_id, count, error = load_file(self.conn, self.URL, INDEX_FILE, [{"plan_name": "x"}])
        self.assertEqual((count, error), (0, self.NOT_IN_NETWORK))
        source = self.source()
        self.assertEqual((source["id"], source["error"], source["rate_count"]),
                         (source_id, self.NOT_IN_NETWORK, 0))
        self.assertIsNone(source["loaded_at"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0], 1)

    def test_other_json_without_an_in_network_list_is_an_error(self):
        data = json.loads(EX_FILE.read_text())
        for content in ([1, 2], {"hello": "world"}, dict(data, in_network={}),
                        dict(data, in_network=None)):
            _, count, error = load_file(self.conn, self.URL, None, [], json.dumps(content).encode())
            self.assertEqual((count, error), (0, self.NOT_IN_NETWORK), content if len(str(content)) < 50 else "")
            self.assertEqual(self.source()["error"], self.NOT_IN_NETWORK)

    def test_an_index_over_a_good_load_keeps_it(self):
        load_file(self.conn, self.URL, EX_FILE, [])
        _, count, error = load_file(self.conn, self.URL, INDEX_FILE, [])
        self.assertEqual((count, error), (0, self.NOT_IN_NETWORK))
        source = self.source()
        self.assertEqual((source["rate_count"], source["error"]), (1828, None))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM rates").fetchone()[0], 1828)

    def test_an_empty_in_network_list_loads_0_rates_with_a_warning(self):
        data = dict(json.loads(EX_FILE.read_text()), in_network=[])
        _, count, error = load_file(self.conn, self.URL, None, [], json.dumps(data).encode())
        self.assertEqual((count, error), (0, None))
        source = self.source()
        self.assertEqual((source["rate_count"], source["error"], source["warnings"]),
                         (0, None, "no rates in file"))
        self.assertIsNotNone(source["loaded_at"])
        self.assertEqual(source["reporting_entity_name"], "Centene Management Company LLC")


class LoadCommandTest(unittest.TestCase):
    """`python -m etl.load --no-download` run as a real process against fixtures in a temp dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.raw_dir = self.tmp / "in_network_files"
        self.raw_dir.mkdir()
        self.db = self.tmp / "parsed" / "rates.db"

    def tearDown(self):
        self._tmp.cleanup()

    def run_load(self, index_path, *, db=True):
        """--db self.db, or (db=False) the default: <--parsed-dir>/<index stem>.db."""
        target = ["--db", str(self.db)] if db else ["--parsed-dir", str(self.tmp / "parsed")]
        return subprocess.run(
            [sys.executable, "-m", "etl.load", "--index", str(index_path),
             "--index-dir", str(self.tmp / "index_files"), "--raw-dir", str(self.raw_dir),
             "--backup-dir", str(self.tmp / "parsed" / "backups"), *target, "--no-download"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
        )

    def write_index(self, index):
        path = self.tmp / "index.json"
        path.write_text(json.dumps(index))
        return path

    def last_line(self, proc):
        return proc.stdout.strip().splitlines()[-1]

    def test_every_file_loaded_exits_0(self):
        index = json.loads(INDEX_FILE.read_text())
        index["reporting_structure"] = [
            s for s in index["reporting_structure"]
            if all(f["location"] == EX_URL for f in s["in_network_files"])
        ]
        shutil.copy(EX_FILE, self.raw_dir / EX_FILE.name)
        proc = self.run_load(self.write_index(index))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.last_line(proc), "1 of 1 files loaded")

    def test_some_files_failed_exits_3(self):
        shutil.copy(EX_FILE, self.raw_dir / EX_FILE.name)
        proc = self.run_load(INDEX_FILE)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertTrue(self.last_line(proc).startswith("1 of 2 files loaded: "), proc.stdout)

    def test_no_file_loaded_exits_4(self):
        proc = self.run_load(INDEX_FILE)
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertTrue(self.last_line(proc).startswith("0 of 2 files loaded: "), proc.stdout)

    def test_unreadable_index_exits_2(self):
        proc = self.run_load(self.tmp / "no-such-index.json")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("not found", proc.stderr)
        self.assertFalse(self.db.exists())

    def test_index_failing_validation_exits_2(self):
        index = copy.deepcopy(json.loads(INDEX_FILE.read_text()))
        del index["reporting_structure"][0]["reporting_plans"]
        shutil.copy(EX_FILE, self.raw_dir / EX_FILE.name)
        proc = self.run_load(self.write_index(index))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("reporting_plans", proc.stderr)
        self.assertFalse(self.db.exists())

    def test_default_db_is_named_after_the_index_records_it_and_becomes_active(self):
        shutil.copy(EX_FILE, self.raw_dir / EX_FILE.name)
        index = self.tmp / "2026-09-28_fidelis_index.json"
        shutil.copy(INDEX_FILE, index)
        proc = self.run_load(index, db=False)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)  # fidelis-es has no local copy
        db = self.tmp / "parsed" / "2026-09-28_fidelis_index.db"
        self.assertIn(str(db), proc.stdout)
        with sqlite3.connect(db) as conn:
            info = read_index_info(conn)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM rates").fetchone()[0], 1828)
        self.assertEqual(info["file_name"], "2026-09-28_fidelis_index.json")
        self.assertEqual(len(info["files"]), 2)
        self.assertEqual((self.tmp / "parsed" / "active_index").read_text().strip(), db.name)
        # Loading it again rebuilds it, backing up the old one first.
        proc = self.run_load(index, db=False)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("Backed up", proc.stdout)
        (backup,) = (self.tmp / "parsed" / "backups").glob("2026-09-28_fidelis_index-*.db")
        with sqlite3.connect(backup) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM rates").fetchone()[0], 1828)

    def test_db_override_also_records_the_index(self):
        shutil.copy(EX_FILE, self.raw_dir / EX_FILE.name)
        self.run_load(INDEX_FILE)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(read_index_info(conn)["location"], str(INDEX_FILE))
        self.assertFalse((self.tmp / "parsed" / "active_index").exists())

    def test_help_documents_the_exit_codes(self):
        proc = subprocess.run(
            [sys.executable, "-m", "etl.load", "--help"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0)
        for code in ("0", "2", "3", "4"):
            self.assertRegex(proc.stdout, rf"(?m)^\s+{code}\s")


if __name__ == "__main__":
    unittest.main()
