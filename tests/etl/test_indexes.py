"""#37.5: one database per index. Each index database records its index (index_info); an IndexStore lists the
databases in data/parsed/, keeps which one is active, and removes one after backing it up; migrate_rates_db turns
the old single rates.db into its index's database without re-parsing anything."""

import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from etl.indexes import ACTIVE_FILE, IndexStore, db_name_for, migrate_rates_db
from etl.run import read_index_info, run_load, write_index_info

TEST_DATA = Path(__file__).parent / "test_data"
INDEX_NAME = "2026-09-28_fidelis_index.json"
EX_NAME = "2026-09-28_centene-management-company-llc_fidelis-ex_in-network.json"
ES_NAME = "2026-09-28_centene-management-company-llc_fidelis-es_in-network.json"
REAL_INDEX = json.loads((TEST_DATA / INDEX_NAME).read_text())
BASE = "https://www.centene.com/content/dam/centene/Centene%20Corporate/json/DOCUMENT/"
EX_URL = BASE + EX_NAME
ES_URL = BASE + ES_NAME
EX_RATES = 1828


class TempDirs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.parsed = self.tmp / "parsed"
        self.backups = self.parsed / "backups"
        self.raw_dir = self.tmp / "raw"
        self.raw_dir.mkdir()
        shutil.copy(TEST_DATA / EX_NAME, self.raw_dir / EX_NAME)
        self.index_path = self.tmp / "index_files" / INDEX_NAME
        self.index_path.parent.mkdir()
        shutil.copy(TEST_DATA / INDEX_NAME, self.index_path)

    def tearDown(self):
        self._tmp.cleanup()

    def build(self, name: str, urls=(EX_URL,), index=REAL_INDEX, location=None) -> Path:
        """A real index database: the fidelis-ex slice loaded from its local copy."""
        db = self.parsed / name
        run_load(index, list(urls), db, self.raw_dir, "replace", self.backups,
                 download=False, index_location=str(location or self.index_path))
        return db


class DbNameTest(unittest.TestCase):
    def test_the_index_file_name_with_db(self):
        self.assertEqual(db_name_for("/x/index_files/2026-09-28_fidelis_index.json"), "2026-09-28_fidelis_index.db")
        self.assertEqual(db_name_for(Path("a.b.json")), "a.b.db")


class IndexInfoTest(TempDirs):
    def test_a_load_records_its_index_in_the_database(self):
        db = self.build("2026-09-28_fidelis_index.db")
        with sqlite3.connect(db) as conn:
            info = read_index_info(conn)
        self.assertEqual(info["location"], str(self.index_path))
        self.assertEqual(info["file_name"], INDEX_NAME)
        self.assertEqual(info["reporting_entity_name"], "Centene Management Company LLC")
        self.assertEqual(info["reporting_entity_type"], "Third Party Administrator")
        self.assertEqual(info["last_updated_on"], "2026-09-28")
        self.assertEqual(info["version"], "2.0.0")
        self.assertIs(info["valid"], True)
        self.assertEqual(info["problems"], [])
        self.assertRegex(info["loaded_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
        # Every file the index lists, loaded or not, with its plans.
        self.assertEqual([f["url"] for f in info["files"]], [EX_URL, ES_URL])
        self.assertEqual([f["file"] for f in info["files"]], [EX_NAME, ES_NAME])
        self.assertEqual([len(f["plans"]) for f in info["files"]], [88, 6])
        self.assertEqual(info["files"][1]["plans"][0]["plan_id"], "25303EP000")

    def test_writing_again_replaces_it(self):
        conn = sqlite3.connect(":memory:")
        write_index_info(conn, REAL_INDEX, "first.json")
        index = json.loads(json.dumps(REAL_INDEX))
        del index["reporting_structure"][1]
        write_index_info(conn, index, "/a/second.json")
        info = read_index_info(conn)
        self.assertEqual(info["file_name"], "second.json")
        self.assertEqual([f["url"] for f in info["files"]], [EX_URL])

    def test_an_invalid_index_is_recorded_as_such(self):
        index = json.loads(json.dumps(REAL_INDEX))
        del index["reporting_structure"][0]["reporting_plans"][0]["plan_id"]
        conn = sqlite3.connect(":memory:")
        write_index_info(conn, index, "bad.json")
        info = read_index_info(conn)
        self.assertIs(info["valid"], False)
        self.assertTrue(info["problems"])

    def test_a_database_without_index_info_reads_as_none(self):
        conn = sqlite3.connect(":memory:")
        self.assertIsNone(read_index_info(conn))  # no table at all (a database from before #37)
        conn.execute("CREATE TABLE index_info (id INTEGER)")
        self.assertIsNone(read_index_info(conn))

    def test_without_an_index_location_nothing_is_recorded(self):
        db = self.parsed / "plain.db"
        run_load(REAL_INDEX, [EX_URL], db, self.raw_dir, "replace", self.backups, download=False)
        with sqlite3.connect(db) as conn:
            self.assertIsNone(read_index_info(conn))


class StoreTest(TempDirs):
    def store(self):
        return IndexStore(self.parsed, self.backups)

    def test_empty_folder_has_no_databases_and_none_active(self):
        store = self.store()
        self.assertEqual(store.dbs(), [])
        self.assertIsNone(store.active())
        self.parsed.mkdir()
        self.assertEqual(store.dbs(), [])

    def test_lists_index_databases_newest_name_first(self):
        self.build("2026-08-31_fidelis_index.db")
        self.build("2026-09-28_fidelis_index.db")
        (self.parsed / "notes.txt").write_text("not a database")
        self.backups.mkdir(exist_ok=True)
        (self.backups / "old.db").write_text("")
        self.assertEqual([p.name for p in self.store().dbs()],
                         ["2026-09-28_fidelis_index.db", "2026-08-31_fidelis_index.db"])

    def test_without_a_setting_the_newest_database_is_active(self):
        older = self.build("b.db")
        newer = self.build("a.db")
        os.utime(older, (time.time() - 100, time.time() - 100))
        self.assertEqual(self.store().active(), newer)

    def test_set_active_is_saved_and_survives_a_new_store(self):
        first = self.build("a.db")
        self.build("b.db")
        self.store().set_active("a.db")
        self.assertEqual((self.parsed / ACTIVE_FILE).read_text().strip(), "a.db")
        self.assertEqual((self.parsed / ACTIVE_FILE).stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.store().active(), first)

    def test_set_active_refuses_an_unknown_or_unsafe_name(self):
        self.build("a.db")
        for name in ("nope.db", "../a.db", "backups", "", "a"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.store().set_active(name)
        self.assertFalse((self.parsed / ACTIVE_FILE).exists())

    def test_a_setting_naming_a_missing_database_falls_back_to_the_newest(self):
        db = self.build("a.db")
        (self.parsed / ACTIVE_FILE).write_text("gone.db\n")
        self.assertEqual(self.store().active(), db)

    def test_db_for_an_index_is_named_after_it(self):
        self.assertEqual(self.store().db_for(self.index_path), self.parsed / "2026-09-28_fidelis_index.db")

    def test_remove_backs_up_then_deletes(self):
        db = self.build("a.db")
        other = self.build("b.db")
        self.store().set_active("a.db")
        backup = self.store().remove("a.db")
        self.assertFalse(db.exists())
        self.assertEqual(backup.parent, self.backups)
        self.assertRegex(backup.name, r"^a-\d{8}-\d{6}\.db$")
        with sqlite3.connect(backup) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM rates").fetchone()[0], EX_RATES)
        # The removed one was active: the setting goes, and the newest remaining database is active.
        self.assertFalse((self.parsed / ACTIVE_FILE).exists())
        self.assertEqual(self.store().active(), other)

    def test_removing_the_only_database_leaves_none(self):
        self.build("a.db")
        self.store().remove("a.db")
        self.assertEqual(self.store().dbs(), [])
        self.assertIsNone(self.store().active())

    def test_remove_refuses_an_unknown_name(self):
        self.build("a.db")
        with self.assertRaises(ValueError):
            self.store().remove("../a.db")
        self.assertTrue((self.parsed / "a.db").exists())

    def test_a_fixed_database_is_the_only_one_and_always_active(self):
        fixed = self.tmp / "elsewhere" / "rates.db"
        store = IndexStore(self.parsed, self.backups, fixed=fixed)
        self.build("a.db")
        self.assertEqual(store.dbs(), [])  # not created yet
        self.assertEqual(store.active(), fixed)
        self.assertEqual(store.db_for(self.index_path), fixed)
        fixed.parent.mkdir()
        shutil.copy(self.parsed / "a.db", fixed)
        self.assertEqual(store.dbs(), [fixed])
        store.set_active("rates.db")
        self.assertFalse((self.parsed / ACTIVE_FILE).exists())
        store.remove("rates.db")
        self.assertFalse(fixed.exists())


class MigrateTest(TempDirs):
    """The old data/parsed/rates.db (no index_info) becomes <index>.db, with its index recorded, and active."""

    def setUp(self):
        super().setUp()
        self.old = self.parsed / "rates.db"
        run_load(REAL_INDEX, [EX_URL], self.old, self.raw_dir, "replace", self.backups, download=False)
        with sqlite3.connect(self.old) as conn:
            conn.execute("DROP TABLE index_info")
            conn.execute("DROP TABLE index_in_network_files")
            self.loaded_at = conn.execute("SELECT loaded_at FROM source_files").fetchone()[0]
        self.before = self.old.read_bytes()

    def test_backs_up_records_the_index_renames_and_activates(self):
        result = migrate_rates_db(self.parsed, self.index_path, self.backups)
        new = self.parsed / "2026-09-28_fidelis_index.db"
        self.assertEqual(result["db"], str(new))
        self.assertFalse(self.old.exists())
        self.assertEqual(Path(result["backup"]).parent, self.backups)
        with sqlite3.connect(result["backup"]) as conn:  # rates.db as it was: no index recorded yet
            self.assertIsNone(read_index_info(conn))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM rates").fetchone()[0], EX_RATES)
        with sqlite3.connect(new) as conn:
            info = read_index_info(conn)
            rates = conn.execute("SELECT COUNT(*) FROM rates").fetchone()[0]
        self.assertEqual(rates, EX_RATES)  # nothing re-parsed or lost
        self.assertEqual(info["file_name"], INDEX_NAME)
        self.assertEqual(info["loaded_at"], self.loaded_at)  # when its files were loaded, not now
        self.assertEqual(len(info["files"]), 2)
        self.assertEqual((self.parsed / ACTIVE_FILE).read_text().strip(), new.name)
        self.assertEqual(IndexStore(self.parsed, self.backups).active(), new)

    def test_refuses_to_overwrite_an_existing_index_database(self):
        (self.parsed / "2026-09-28_fidelis_index.db").write_text("already here")
        with self.assertRaises(FileExistsError):
            migrate_rates_db(self.parsed, self.index_path, self.backups)
        self.assertEqual(self.old.read_bytes(), self.before)

    def test_nothing_to_migrate(self):
        self.old.unlink()
        with self.assertRaises(FileNotFoundError):
            migrate_rates_db(self.parsed, self.index_path, self.backups)


if __name__ == "__main__":
    unittest.main()
