import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from etl.schema import create_schema, display_name, local_path_for

BASE = "https://www.centene.com/content/dam/centene/Centene%20Corporate/json/DOCUMENT/"


class LocalPathForTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.raw = Path(self._tmp.name) / "raw"
        self.raw.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_keeps_the_readable_url_filename_plus_a_hash_of_the_url(self):
        url = BASE + "2026-09-28_centene-management-company-llc_fidelis-ex_in-network.json"
        path = local_path_for(url, self.raw)
        digest = hashlib.sha256(url.encode()).hexdigest()[:12]
        self.assertEqual(
            path,
            self.raw / f"2026-09-28_centene-management-company-llc_fidelis-ex_in-network.{digest}.json",
        )

    def test_decodes_the_url_filename(self):
        path = local_path_for("https://h/a%20b.json", self.raw)
        self.assertRegex(path.name, r"^a b\.[0-9a-f]{12}\.json$")

    def test_same_basename_from_different_urls_maps_to_different_files(self):
        a = local_path_for("https://a/x/file.json?sig=1", self.raw)
        b = local_path_for("https://b/y/file.json", self.raw)
        self.assertNotEqual(a, b)
        for path in (a, b):
            self.assertEqual(path.parent, self.raw)
            self.assertRegex(path.name, r"^file\.[0-9a-f]{12}\.json$")

    def test_an_existing_plain_basename_copy_is_still_used(self):
        url = BASE + "2026-09-28_centene-management-company-llc_fidelis-ex_in-network.json"
        legacy = self.raw / "2026-09-28_centene-management-company-llc_fidelis-ex_in-network.json"
        legacy.write_text("{}")
        self.assertEqual(local_path_for(url, self.raw), legacy)

    def test_an_existing_decoded_plain_copy_is_still_used(self):
        legacy = self.raw / "a b.json"
        legacy.write_text("{}")
        self.assertEqual(local_path_for("https://h/a%20b.json", self.raw), legacy)

    def test_encoded_traversal_stays_inside_the_dir(self):
        victim = self.raw.parent / "main.py"
        victim.write_text("precious")
        for url in (
            "https://h/x/..%2F..%2Fapi%2Fmain.py",
            "https://h/x/..%2Fmain.py",
            "https://h/x/..%5Cmain.py",
            "https://h/x/%2E%2E",
            "https://h/x/..",
            "https://h/x/.",
            "https://h/",
            "https://h/x/a%00b.json",
            "https://h/x/%2Fetc%2Fpasswd",
        ):
            with self.subTest(url=url):
                path = local_path_for(url, self.raw)
                self.assertEqual(path.resolve().parent, self.raw.resolve())
                self.assertNotIn("/", path.name)
                self.assertNotIn("\\", path.name)
                self.assertNotIn("\x00", path.name)
                self.assertNotIn(path.name, ("", ".", ".."))

    def test_an_unsafe_basename_never_matches_an_outside_legacy_file(self):
        # "..%2Fmain.py" decodes to "../main.py": it must not pick up raw/../main.py as a copy.
        (self.raw.parent / "main.py").write_text("precious")
        path = local_path_for("https://h/x/..%2Fmain.py", self.raw)
        self.assertEqual(path.resolve().parent, self.raw.resolve())
        self.assertFalse(path.exists())

    def test_display_name_is_the_readable_basename(self):
        self.assertEqual(display_name("https://h/a%20b.json"), "a b.json")
        self.assertEqual(display_name("https://h/x/..%2F..%2Fapi%2Fmain.py"), ".._.._api_main.py")


class CreateSchemaTest(unittest.TestCase):
    def test_creates_the_tables(self):
        conn = sqlite3.connect(":memory:")
        create_schema(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        # The three data tables, plus the index the database was built from (#37.5).
        self.assertEqual(tables, {"source_files", "plans", "rates", "index_info", "index_in_network_files"})

    def test_rates_are_indexed_by_source_file(self):
        conn = sqlite3.connect(":memory:")
        create_schema(conn)
        indexed = {name for (name,) in conn.execute(
            "SELECT i.name FROM pragma_index_list('rates') l, pragma_index_info(l.name) i")}
        self.assertIn("source_file_id", indexed)

    def test_adds_the_warnings_column_to_an_older_database(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE source_files (id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, "
                     "local_path TEXT, rate_count INTEGER NOT NULL DEFAULT 0, error TEXT)")
        conn.execute("INSERT INTO source_files (url) VALUES ('u')")
        create_schema(conn)
        create_schema(conn)
        self.assertEqual(conn.execute("SELECT url, warnings FROM source_files").fetchall(), [("u", None)])

    def test_can_run_twice(self):
        conn = sqlite3.connect(":memory:")
        create_schema(conn)
        create_schema(conn)


if __name__ == "__main__":
    unittest.main()
