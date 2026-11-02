import copy
import functools
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from etl.schema import local_path_for
from etl.run import (
    HEADER_BYTES, IndexReadError, index_files, list_local_indexes, read_file_header, read_index, recover_interrupted,
    run_load, save_index_upload,
)

TEST_DATA = Path(__file__).parent / "test_data"
INDEX_NAME = "2026-09-28_fidelis_index.json"
EX_NAME = "2026-09-28_centene-management-company-llc_fidelis-ex_in-network.json"
ES_NAME = "2026-09-28_centene-management-company-llc_fidelis-es_in-network.json"
REAL_INDEX = json.loads((TEST_DATA / INDEX_NAME).read_text())
BASE = "https://www.centene.com/content/dam/centene/Centene%20Corporate/json/DOCUMENT/"
EX_URL = BASE + EX_NAME
ES_URL = BASE + ES_NAME
EX_RATES = 1828


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


class ServedFilesTest(unittest.TestCase):
    """Serves a temp folder over real HTTP on 127.0.0.1; each test gets temp dirs."""

    @classmethod
    def setUpClass(cls):
        cls._served = tempfile.TemporaryDirectory()
        cls.served = Path(cls._served.name)
        shutil.copy(TEST_DATA / INDEX_NAME, cls.served / INDEX_NAME)
        shutil.copy(TEST_DATA / EX_NAME, cls.served / EX_NAME)
        (cls.served / "not-json.json").write_text("{this is not json")
        (cls.served / "not-an-index.json").write_text(json.dumps({"hello": "world"}))
        handler = functools.partial(QuietHandler, directory=str(cls.served))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls._served.cleanup()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.index_dir = self.tmp / "index_files"
        self.raw_dir = self.tmp / "in_network_files"
        self.db_path = self.tmp / "parsed" / "rates.db"
        self.backup_dir = self.tmp / "parsed" / "backups"

    def tearDown(self):
        self._tmp.cleanup()


class ReadIndexTest(ServedFilesTest):
    def test_reads_a_local_path_in_place(self):
        index, path = read_index(str(TEST_DATA / INDEX_NAME), self.index_dir)
        self.assertEqual(index, REAL_INDEX)
        self.assertEqual(path, TEST_DATA / INDEX_NAME)
        self.assertFalse(self.index_dir.exists())

    def test_downloads_a_url_into_the_index_dir(self):
        index, path = read_index(self.base + INDEX_NAME, self.index_dir)
        self.assertEqual(index, REAL_INDEX)
        self.assertEqual(path, local_path_for(self.base + INDEX_NAME, self.index_dir))
        self.assertTrue(path.name.startswith("2026-09-28_fidelis_index."))
        self.assertEqual(path.read_bytes(), (TEST_DATA / INDEX_NAME).read_bytes())

    def test_url_already_downloaded_is_read_from_disk(self):
        self.index_dir.mkdir()
        local = self.index_dir / INDEX_NAME
        local.write_text(json.dumps({"reporting_structure": []}))
        index, path = read_index(self.base + INDEX_NAME, self.index_dir)
        self.assertEqual(index, {"reporting_structure": []})
        self.assertEqual(path, local)

    def test_missing_file(self):
        with self.assertRaises(IndexReadError) as ctx:
            read_index(str(self.tmp / "nope.json"), self.index_dir)
        self.assertIn("not found", str(ctx.exception))

    def test_http_error(self):
        with self.assertRaises(IndexReadError) as ctx:
            read_index(self.base + "missing_index.json", self.index_dir)
        self.assertIn("404", str(ctx.exception))
        self.assertEqual(list(self.index_dir.iterdir()), [])

    def test_invalid_json(self):
        with self.assertRaises(IndexReadError) as ctx:
            read_index(self.base + "not-json.json", self.index_dir)
        self.assertIn("not valid JSON", str(ctx.exception))
        # A bad download is not kept, so a retry fetches it again.
        self.assertEqual(list(self.index_dir.iterdir()), [])

    def test_json_that_is_not_an_index(self):
        path = self.tmp / "other.json"
        path.write_text(json.dumps({"hello": "world"}))
        with self.assertRaises(IndexReadError) as ctx:
            read_index(str(path), self.index_dir)
        self.assertIn("reporting_structure", str(ctx.exception))
        with self.assertRaises(IndexReadError):
            read_index(self.base + "not-an-index.json", self.index_dir)
        with self.assertRaises(IndexReadError):
            path.write_text("[1, 2]")
            read_index(str(path), self.index_dir)


class AnyPathHandler(QuietHandler):
    """Answers every GET with 200 and the real index, whatever the path."""

    def do_GET(self):
        body = (TEST_DATA / INDEX_NAME).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ReadIndexTraversalTest(unittest.TestCase):
    """An index URL whose basename decodes to a path must be saved inside index_dir."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), AnyPathHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.index_dir = self.root / "data" / "raw" / "index_files"
        (self.root / "api").mkdir()
        self.victim = self.root / "api" / "main.py"
        self.victim.write_text("precious")

    def tearDown(self):
        self._tmp.cleanup()

    def test_encoded_traversal_is_saved_inside_the_index_dir(self):
        index, path = read_index(self.base + "x/..%2F..%2F..%2Fapi%2Fmain.py", self.index_dir)
        self.assertEqual(self.victim.read_text(), "precious")
        self.assertEqual(path.resolve().parent, self.index_dir.resolve())
        self.assertEqual(index, REAL_INDEX)
        outside = {p for p in self.root.rglob("*") if p.is_file() and self.index_dir not in p.parents}
        self.assertEqual(outside, {self.victim})


class IndexFilesTest(unittest.TestCase):
    def test_one_entry_per_url_with_its_plans(self):
        files = index_files(REAL_INDEX)
        self.assertEqual([f["url"] for f in files], [EX_URL, ES_URL])
        self.assertEqual([f["file"] for f in files], [EX_NAME, ES_NAME])
        self.assertEqual([len(f["plans"]) for f in files], [88, 6])
        bronze = next(
            p for p in files[0]["plans"] if p["plan_name"] == "Ambetter from Fidelis Care Bronze"
        )
        self.assertEqual(
            bronze,
            {
                "plan_name": "Ambetter from Fidelis Care Bronze",
                "plan_id": "25303NY005",
                "plan_id_type": "hios",
                "plan_market_type": "individual",
                "issuer_name": "Fidelis Care",
            },
        )

    def test_url_under_several_structures_appears_once_with_all_plans(self):
        index = copy.deepcopy(REAL_INDEX)
        es_structure = index["reporting_structure"][1]
        es_structure["in_network_files"].append(dict(es_structure["in_network_files"][0], location=EX_URL))
        files = index_files(index)
        self.assertEqual([f["url"] for f in files], [EX_URL, ES_URL])
        self.assertEqual([len(f["plans"]) for f in files], [94, 6])

    def test_file_name_is_decoded(self):
        index = copy.deepcopy(REAL_INDEX)
        index["reporting_structure"][0]["in_network_files"][0]["location"] = BASE + "a%20b.json"
        self.assertEqual(index_files(index)[0]["file"], "a b.json")


class RunLoadTest(ServedFilesTest):
    def served_index(self):
        """The real index with locations pointing at the local server (ES is not served)."""
        index = copy.deepcopy(REAL_INDEX)
        index["reporting_structure"][0]["in_network_files"][0]["location"] = self.base + EX_NAME
        index["reporting_structure"][1]["in_network_files"][0]["location"] = self.base + ES_NAME
        return index

    def db_counts(self, path=None):
        conn = sqlite3.connect(path or self.db_path)
        try:
            return tuple(
                conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("source_files", "plans", "rates")
            )
        finally:
            conn.close()

    def test_add_into_an_empty_db_downloads_and_loads_only_the_chosen_file(self):
        index = self.served_index()
        url = self.base + EX_NAME
        result = run_load(index, [url], self.db_path, self.raw_dir, "add", self.backup_dir)

        self.assertEqual(result["mode"], "add")
        self.assertIsNone(result["backup_path"])
        self.assertFalse(result["replaced"])
        self.assertGreaterEqual(result["seconds"], 0)
        self.assertEqual(
            result["files"],
            [{"url": url, "file": EX_NAME, "source": "download", "downloaded": True,
              "rate_count": EX_RATES, "plan_count": 88, "error": None}],
        )
        self.assertEqual(self.db_counts(), (1, 88, EX_RATES))
        self.assertEqual(list(self.raw_dir.iterdir()), [local_path_for(url, self.raw_dir)])

    def test_add_twice_reloads_in_place_without_duplicates(self):
        index = self.served_index()
        url = self.base + EX_NAME
        run_load(index, [url], self.db_path, self.raw_dir, "add", self.backup_dir)
        result = run_load(index, [url], self.db_path, self.raw_dir, "add", self.backup_dir)

        self.assertFalse(result["files"][0]["downloaded"])
        self.assertEqual(result["files"][0]["rate_count"], EX_RATES)
        self.assertEqual(self.db_counts(), (1, 88, EX_RATES))
        conn = sqlite3.connect(self.db_path)
        (source_id, rate_count, error) = conn.execute(
            "SELECT id, rate_count, error FROM source_files"
        ).fetchone()
        conn.close()
        self.assertEqual((rate_count, error), (EX_RATES, None))
        self.assertEqual(source_id, 1)

    def test_add_keeps_other_files_already_loaded(self):
        # Seed the DB with the real Centene URL, then add the served copy too.
        shutil.copy(TEST_DATA / EX_NAME, self.tmp / EX_NAME)
        run_load(REAL_INDEX, [EX_URL], self.db_path, self.tmp, "add", self.backup_dir,
                 download=False)
        index = self.served_index()
        run_load(index, [self.base + EX_NAME], self.db_path, self.raw_dir, "add", self.backup_dir)
        self.assertEqual(self.db_counts(), (2, 176, 2 * EX_RATES))

    def test_fetch_failure_is_reported_and_others_still_load(self):
        index = self.served_index()
        result = run_load(
            index, [self.base + ES_NAME, self.base + EX_NAME], self.db_path, self.raw_dir,
            "add", self.backup_dir,
        )
        es, ex = result["files"]
        self.assertIn("404", es["error"])
        self.assertEqual((es["rate_count"], es["plan_count"], es["downloaded"]), (0, 0, False))
        self.assertIsNone(ex["error"])
        self.assertEqual(self.db_counts(), (2, 94, EX_RATES))

    def test_unparseable_file_is_reported(self):
        self.raw_dir.mkdir()
        (self.raw_dir / EX_NAME).write_text("{broken")
        index = self.served_index()
        result = run_load(index, [self.base + EX_NAME], self.db_path, self.raw_dir, "add",
                          self.backup_dir)
        self.assertIn("JSONDecodeError", result["files"][0]["error"])
        self.assertEqual(result["files"][0]["rate_count"], 0)

    def test_replace_backs_up_the_old_db_and_swaps_in_the_new_one(self):
        self.db_path.parent.mkdir(parents=True)
        old = sqlite3.connect(self.db_path)
        old.execute("CREATE TABLE marker (x)")
        old.execute("INSERT INTO marker VALUES ('old')")
        old.commit()
        old.close()

        index = self.served_index()
        result = run_load(index, [self.base + EX_NAME], self.db_path, self.raw_dir, "replace",
                          self.backup_dir)

        self.assertTrue(result["replaced"])
        backup = Path(result["backup_path"])
        self.assertEqual(backup.parent, self.backup_dir)
        self.assertRegex(backup.name, r"^rates-\d{8}-\d{6}\.db$")
        conn = sqlite3.connect(backup)
        self.assertEqual(conn.execute("SELECT x FROM marker").fetchall(), [("old",)])
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        conn.close()
        self.assertEqual(self.db_counts(), (1, 88, EX_RATES))
        conn = sqlite3.connect(self.db_path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertNotIn("marker", tables)
        self.assertEqual(sorted(p.name for p in self.db_path.parent.iterdir()),
                         ["backups", "rates.db"])

    def test_replace_backs_up_a_file_sqlite_cannot_read_byte_for_byte(self):
        self.db_path.parent.mkdir(parents=True)
        self.db_path.write_bytes(b"not a database at all")
        result = run_load(self.served_index(), [self.base + EX_NAME], self.db_path, self.raw_dir,
                          "replace", self.backup_dir)
        self.assertTrue(result["replaced"])
        self.assertEqual(Path(result["backup_path"]).read_bytes(), b"not a database at all")
        self.assertEqual(self.db_counts(), (1, 88, EX_RATES))

    def test_replace_without_an_existing_db_makes_no_backup(self):
        result = run_load(self.served_index(), [self.base + EX_NAME], self.db_path, self.raw_dir,
                          "replace", self.backup_dir)
        self.assertTrue(result["replaced"])
        self.assertIsNone(result["backup_path"])
        self.assertEqual(self.db_counts(), (1, 88, EX_RATES))

    def test_replace_where_every_file_fails_leaves_the_old_db(self):
        index = self.served_index()
        run_load(index, [self.base + EX_NAME], self.db_path, self.raw_dir, "add", self.backup_dir)
        before = self.db_path.read_bytes()

        result = run_load(index, [self.base + ES_NAME], self.db_path, self.raw_dir, "replace",
                          self.backup_dir)

        self.assertFalse(result["replaced"])
        self.assertIsNone(result["backup_path"])
        self.assertIn("404", result["files"][0]["error"])
        self.assertEqual(self.db_path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.db_path.parent.iterdir()), ["rates.db"])

    def test_bad_mode_raises(self):
        with self.assertRaises(ValueError):
            run_load(REAL_INDEX, [EX_URL], self.db_path, self.raw_dir, "merge", self.backup_dir)
        self.assertFalse(self.db_path.exists())

    def test_download_disabled_with_a_missing_file_is_reported(self):
        index = self.served_index()
        result = run_load(index, [self.base + EX_NAME], self.db_path, self.raw_dir, "add",
                          self.backup_dir, download=False)
        self.assertIn("not downloaded", result["files"][0]["error"])
        self.assertFalse(result["files"][0]["downloaded"])

    def test_local_source_uses_the_file_on_disk_without_the_network(self):
        self.raw_dir.mkdir()
        shutil.copy(TEST_DATA / EX_NAME, self.raw_dir / EX_NAME)
        # The real Centene URL: "local" must never touch the network.
        result = run_load(REAL_INDEX, [EX_URL], self.db_path, self.raw_dir, "add", self.backup_dir,
                          sources={EX_URL: "local"})
        (entry,) = result["files"]
        self.assertEqual((entry["source"], entry["downloaded"], entry["error"]), ("local", False, None))
        self.assertEqual(entry["rate_count"], EX_RATES)

    def test_local_source_without_a_local_copy_is_reported(self):
        result = run_load(self.served_index(), [self.base + EX_NAME], self.db_path, self.raw_dir,
                          "add", self.backup_dir, sources={self.base + EX_NAME: "local"})
        (entry,) = result["files"]
        self.assertEqual((entry["source"], entry["error"]), ("local", "no local copy"))
        self.assertFalse((self.raw_dir / EX_NAME).exists())

    def test_download_source_replaces_a_stale_local_copy(self):
        self.raw_dir.mkdir()
        local = self.raw_dir / EX_NAME
        local.write_text("{stale copy}")
        url = self.base + EX_NAME
        result = run_load(self.served_index(), [url], self.db_path, self.raw_dir, "add",
                          self.backup_dir, sources={url: "download"})
        (entry,) = result["files"]
        self.assertEqual((entry["source"], entry["downloaded"], entry["error"]), ("download", True, None))
        self.assertEqual(entry["rate_count"], EX_RATES)
        self.assertEqual(local.read_bytes(), (TEST_DATA / EX_NAME).read_bytes())
        self.assertEqual([p.name for p in self.raw_dir.iterdir()], [EX_NAME])  # no temp file left

    def test_web_source_loads_from_memory_and_writes_nothing(self):
        self.raw_dir.mkdir()
        (self.raw_dir / "other.json").write_text("{}")
        url = self.base + EX_NAME
        result = run_load(self.served_index(), [url], self.db_path, self.raw_dir, "add",
                          self.backup_dir, sources={url: "web"})
        (entry,) = result["files"]
        self.assertEqual((entry["source"], entry["downloaded"], entry["error"]), ("web", False, None))
        self.assertEqual(entry["rate_count"], EX_RATES)
        self.assertEqual([p.name for p in self.raw_dir.iterdir()], ["other.json"])
        self.assertEqual(self.db_counts(), (1, 88, EX_RATES))
        conn = sqlite3.connect(self.db_path)
        (local_path,) = conn.execute("SELECT local_path FROM source_files").fetchone()
        conn.close()
        self.assertIsNone(local_path)

    def test_web_source_http_error_is_reported(self):
        url = self.base + ES_NAME
        result = run_load(self.served_index(), [url], self.db_path, self.raw_dir, "add",
                          self.backup_dir, sources={url: "web"})
        self.assertIn("404", result["files"][0]["error"])
        self.assertFalse(self.raw_dir.exists())

    def test_download_and_web_sources_are_refused_when_downloads_are_disabled(self):
        self.raw_dir.mkdir()
        shutil.copy(TEST_DATA / EX_NAME, self.raw_dir / EX_NAME)
        for source in ("download", "web"):
            result = run_load(REAL_INDEX, [EX_URL], self.db_path, self.raw_dir, "add",
                              self.backup_dir, download=False, sources={EX_URL: source})
            (entry,) = result["files"]
            self.assertEqual(entry["source"], source)
            self.assertIn("downloads disabled", entry["error"])
        self.assertFalse(self.db_path.exists() and self.db_counts()[2])

    def test_unknown_source_raises(self):
        with self.assertRaises(ValueError):
            run_load(REAL_INDEX, [EX_URL], self.db_path, self.raw_dir, "add", self.backup_dir,
                     download=False, sources={EX_URL: "ftp"})
        self.assertFalse(self.db_path.exists())

    def test_an_index_given_as_an_in_network_file_is_an_error(self):
        index = self.served_index()
        url = self.base + INDEX_NAME
        index["reporting_structure"][0]["in_network_files"][0]["location"] = url
        result = run_load(index, [url], self.db_path, self.raw_dir, "replace", self.backup_dir)
        (entry,) = result["files"]
        self.assertEqual(entry["error"], "not an in-network file: no in_network list")
        self.assertEqual((entry["rate_count"], entry["plan_count"]), (0, 0))
        self.assertFalse(result["replaced"])
        self.assertFalse(self.db_path.exists())

    def test_url_not_in_the_index_is_reported(self):
        result = run_load(self.served_index(), [self.base + "other.json"], self.db_path,
                          self.raw_dir, "add", self.backup_dir)
        self.assertIn("not in the index", result["files"][0]["error"])


class LoadProgressTest(ServedFilesTest):
    """#42.2: run_load reports each file's stages as it goes: Downloading (bytes of total) -> Reading -> Saving rates
    (rows so far, database size) -> loaded; then, when the new database is swapped in, activating."""

    served_index = RunLoadTest.served_index

    def run_with_progress(self, urls, mode="replace", **kwargs):
        events = []
        result = run_load(self.served_index(), urls, self.db_path, self.raw_dir, mode, self.backup_dir,
                          progress=events.append, **kwargs)
        return result, events

    def test_a_downloaded_file_goes_through_every_stage_then_activating(self):
        url = self.base + EX_NAME
        size = (TEST_DATA / EX_NAME).stat().st_size
        result, events = self.run_with_progress([url])
        self.assertTrue(result["replaced"])
        stages = [e["stage"] for e in events]
        self.assertEqual([s for i, s in enumerate(stages) if i == 0 or stages[i - 1] != s],
                         ["downloading", "reading", "saving", "loaded", "activating"])
        downloads = [e for e in events if e["stage"] == "downloading"]
        self.assertEqual((downloads[0]["bytes"], downloads[-1]["bytes"], downloads[-1]["total"]), (0, size, size))
        saving = [e for e in events if e["stage"] == "saving"]
        self.assertEqual((saving[0]["rows"], saving[-1]["rows"]), (0, EX_RATES))
        self.assertGreater(saving[-1]["db_bytes"], 0)
        (loaded,) = [e for e in events if e["stage"] == "loaded"]
        self.assertEqual((loaded["url"], loaded["rate_count"], loaded["error"]), (url, EX_RATES, None))
        self.assertTrue(all(e["url"] == url for e in events if e["stage"] != "activating"))

    def test_a_web_file_reports_its_download_too(self):
        url = self.base + EX_NAME
        _, events = self.run_with_progress([url], sources={url: "web"})
        downloads = [e for e in events if e["stage"] == "downloading"]
        self.assertEqual(downloads[-1]["bytes"], (TEST_DATA / EX_NAME).stat().st_size)

    def test_a_local_file_starts_at_reading(self):
        self.raw_dir.mkdir()
        url = self.base + EX_NAME
        shutil.copy(TEST_DATA / EX_NAME, local_path_for(url, self.raw_dir))
        _, events = self.run_with_progress([url], sources={url: "local"})
        self.assertEqual(events[0]["stage"], "reading")

    def test_each_file_is_fetched_and_loaded_before_the_next_one_starts(self):
        ex, es = self.base + EX_NAME, self.base + ES_NAME  # es isn't served: its download fails
        result, events = self.run_with_progress([ex, es])
        first_es = next(i for i, e in enumerate(events) if e.get("url") == es)
        ex_loaded = next(i for i, e in enumerate(events) if e["stage"] == "loaded" and e["url"] == ex)
        self.assertLess(ex_loaded, first_es)
        (es_loaded,) = [e for e in events if e["stage"] == "loaded" and e["url"] == es]
        self.assertIn("404", es_loaded["error"])
        self.assertEqual(events[-1]["stage"], "activating")

    def test_nothing_loaded_means_nothing_is_activated(self):
        _, events = self.run_with_progress([self.base + ES_NAME])
        self.assertNotIn("activating", [e["stage"] for e in events])

    def test_add_mode_reports_files_but_never_activates(self):
        _, events = self.run_with_progress([self.base + EX_NAME], mode="add")
        self.assertIn("loaded", [e["stage"] for e in events])
        self.assertNotIn("activating", [e["stage"] for e in events])


class ShortHandler(QuietHandler):
    """Serves the folder, but under /short/<name> declares a Content-Length 1000 bytes longer
    than the file it sends, then closes the connection: a truncated download."""

    def do_GET(self):
        kind, _, name = self.path.lstrip("/").partition("/")
        if kind != "short":
            return super().do_GET()
        body = (Path(self.directory) / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body) + 1000))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True


class FetchFailureRecordedTest(ServedFilesTest):
    """A chosen file that can't be fetched gets a source_files row carrying the error, so
    /api/health and the dashboard (which read source_files) can show it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        handler = functools.partial(ShortHandler, directory=str(cls.served))
        cls.short_server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.short_thread = threading.Thread(target=cls.short_server.serve_forever, daemon=True)
        cls.short_thread.start()
        cls.short_base = f"http://127.0.0.1:{cls.short_server.server_address[1]}/"

    @classmethod
    def tearDownClass(cls):
        cls.short_server.shutdown()
        cls.short_server.server_close()
        cls.short_thread.join()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.ex_url = self.base + EX_NAME
        self.es_url = self.base + ES_NAME  # not served: 404
        self.index = copy.deepcopy(REAL_INDEX)
        self.index["reporting_structure"][0]["in_network_files"][0]["location"] = self.ex_url
        self.index["reporting_structure"][1]["in_network_files"][0]["location"] = self.es_url

    def rows(self, path=None):
        """url -> (local_path, rate_count, error, loaded_at, plan count, rate rows)."""
        conn = sqlite3.connect(path or self.db_path)
        try:
            return {
                url: (local_path, rate_count, error, loaded_at,
                      conn.execute("SELECT COUNT(*) FROM plans WHERE source_file_id = ?",
                                   (sid,)).fetchone()[0],
                      conn.execute("SELECT COUNT(*) FROM rates WHERE source_file_id = ?",
                                   (sid,)).fetchone()[0])
                for sid, url, local_path, rate_count, error, loaded_at in conn.execute(
                    "SELECT id, url, local_path, rate_count, error, loaded_at FROM source_files")
            }
        finally:
            conn.close()

    def assert_failure_row(self, url, error_part, local_path=None):
        local, rate_count, error, loaded_at, plans, rates = self.rows()[url]
        self.assertIn(error_part, error)
        self.assertEqual((local, rate_count, loaded_at, rates), (local_path, 0, None, 0))
        self.assertEqual(plans, len(self.index_plans(url)))

    def index_plans(self, url):
        return [p for s in self.index["reporting_structure"]
                for f in s["in_network_files"] if f["location"] == url
                for p in s["reporting_plans"]]

    def test_http_error_on_download_is_recorded(self):
        result = run_load(self.index, [self.es_url], self.db_path, self.raw_dir, "add",
                          self.backup_dir)
        self.assertIn("404", result["files"][0]["error"])
        self.assert_failure_row(self.es_url, "404")
        self.assertEqual(self.rows()[self.es_url][4], 6)

    def test_http_error_on_web_fetch_is_recorded(self):
        run_load(self.index, [self.es_url], self.db_path, self.raw_dir, "add",
                 self.backup_dir, sources={self.es_url: "web"})
        self.assert_failure_row(self.es_url, "404")

    def test_downloads_disabled_is_recorded(self):
        for source in (None, "download", "web"):
            sources = {self.ex_url: source} if source else None
            result = run_load(self.index, [self.ex_url], self.db_path, self.raw_dir, "add",
                              self.backup_dir, download=False, sources=sources)
            self.assertIn("downloads disabled", result["files"][0]["error"])
            self.assert_failure_row(self.ex_url, "downloads disabled")
            self.assertEqual(len(self.rows()), 1)

    def test_no_local_copy_is_recorded(self):
        run_load(self.index, [self.ex_url], self.db_path, self.raw_dir, "add",
                 self.backup_dir, sources={self.ex_url: "local"})
        self.assert_failure_row(self.ex_url, "no local copy")

    def test_truncated_download_is_recorded(self):
        url = self.short_base + "short/" + EX_NAME
        self.index["reporting_structure"][0]["in_network_files"][0]["location"] = url
        result = run_load(self.index, [url], self.db_path, self.raw_dir, "add", self.backup_dir)
        self.assertIn("incomplete", result["files"][0]["error"])
        self.assert_failure_row(url, "download incomplete")

    def test_a_url_not_in_the_index_gets_no_row(self):
        run_load(self.index, [self.base + "other.json", self.ex_url], self.db_path,
                 self.raw_dir, "add", self.backup_dir)
        self.assertEqual(list(self.rows()), [self.ex_url])

    def test_add_reuses_a_failed_row_and_a_later_success_clears_it(self):
        run_load(self.index, [self.ex_url], self.db_path, self.raw_dir, "add",
                 self.backup_dir, download=False)
        run_load(self.index, [self.ex_url], self.db_path, self.raw_dir, "add",
                 self.backup_dir, sources={self.ex_url: "local"})
        self.assert_failure_row(self.ex_url, "no local copy")
        self.assertEqual(len(self.rows()), 1)

        run_load(self.index, [self.ex_url], self.db_path, self.raw_dir, "add", self.backup_dir)

        (row,) = self.rows().values()
        local, rate_count, error, loaded_at, plans, rates = row
        self.assertEqual((rate_count, error, plans, rates), (EX_RATES, None, 88, EX_RATES))

    def test_add_never_wipes_a_good_earlier_load_when_a_fetch_fails(self):
        # The ES URL loaded earlier from a local copy (the real EX content), then a
        # re-download of it fails with 404: its rates and row stay exactly as they were.
        self.raw_dir.mkdir()
        local = local_path_for(self.es_url, self.raw_dir)
        shutil.copy(TEST_DATA / EX_NAME, local)
        run_load(self.index, [self.es_url], self.db_path, self.raw_dir, "add",
                 self.backup_dir, sources={self.es_url: "local"})
        before = self.rows()
        self.assertEqual(before[self.es_url][1:3], (EX_RATES, None))
        local.unlink()

        for sources, download in (({self.es_url: "download"}, True),
                                  ({self.es_url: "web"}, True),
                                  ({self.es_url: "local"}, True),
                                  (None, False)):
            result = run_load(self.index, [self.es_url], self.db_path, self.raw_dir, "add",
                              self.backup_dir, download=download, sources=sources)
            (entry,) = result["files"]
            self.assertIsNotNone(entry["error"])
            self.assertEqual(entry["rate_count"], 0)
            self.assertEqual(self.rows(), before)

    def test_replace_puts_the_failed_files_row_in_the_new_db(self):
        result = run_load(self.index, [self.es_url, self.ex_url], self.db_path, self.raw_dir,
                          "replace", self.backup_dir)
        self.assertTrue(result["replaced"])
        rows = self.rows()
        self.assertEqual(rows[self.ex_url][1:3], (EX_RATES, None))
        self.assert_failure_row(self.es_url, "404")

    def test_replace_where_every_file_fails_keeps_the_old_db(self):
        run_load(self.index, [self.ex_url], self.db_path, self.raw_dir, "add", self.backup_dir)
        before = self.db_path.read_bytes()
        result = run_load(self.index, [self.es_url], self.db_path, self.raw_dir, "replace",
                          self.backup_dir)
        self.assertFalse(result["replaced"])
        self.assertEqual(self.db_path.read_bytes(), before)


class ListLocalIndexesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.index_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_folder_lists_nothing(self):
        self.assertEqual(list_local_indexes(self.index_dir / "nope"), [])

    def test_lists_each_json_newest_name_first_with_its_counts_and_validity(self):
        shutil.copy(TEST_DATA / INDEX_NAME, self.index_dir / INDEX_NAME)
        invalid = copy.deepcopy(REAL_INDEX)
        del invalid["reporting_structure"][0]["reporting_plans"][0]["plan_id"]
        (self.index_dir / "2026-10-01_invalid_index.json").write_text(json.dumps(invalid))
        (self.index_dir / "2025-01-01_broken_index.json").write_text("{broken")
        (self.index_dir / "notes.txt").write_text("not an index")

        listed = list_local_indexes(self.index_dir)

        self.assertEqual([i["name"] for i in listed], [
            "2026-10-01_invalid_index.json", INDEX_NAME, "2025-01-01_broken_index.json"])
        invalid_entry, real, broken = listed
        self.assertEqual(real["path"], str(self.index_dir / INDEX_NAME))
        self.assertEqual(real["size_bytes"], (TEST_DATA / INDEX_NAME).stat().st_size)
        self.assertRegex(real["modified"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        self.assertEqual((real["file_count"], real["plan_count"], real["valid"], real["error"]),
                         (2, 94, True, None))
        self.assertEqual((invalid_entry["file_count"], invalid_entry["plan_count"]), (2, 94))
        self.assertFalse(invalid_entry["valid"])
        self.assertIsNone(invalid_entry["error"])
        self.assertFalse(broken["valid"])
        self.assertIn("not valid JSON", broken["error"])
        self.assertEqual((broken["file_count"], broken["plan_count"]), (0, 0))


class SaveIndexUploadTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.index_dir = Path(self._tmp.name) / "index_files"
        self.content = (TEST_DATA / INDEX_NAME).read_bytes()

    def tearDown(self):
        self._tmp.cleanup()

    def test_saved_under_its_basename(self):
        path = save_index_upload("../../somewhere/" + INDEX_NAME, self.content, self.index_dir)
        self.assertEqual(path, self.index_dir / INDEX_NAME)
        self.assertEqual(path.read_bytes(), self.content)
        windows = save_index_upload("C:\\Users\\me\\other.json", b"{}", self.index_dir)
        self.assertEqual(windows, self.index_dir / "other.json")

    def test_same_file_again_reuses_it_and_a_different_one_gets_a_number(self):
        first = save_index_upload(INDEX_NAME, self.content, self.index_dir)
        again = save_index_upload(INDEX_NAME, self.content, self.index_dir)
        second = save_index_upload(INDEX_NAME, b"{}", self.index_dir)
        third = save_index_upload(INDEX_NAME, b"[]", self.index_dir)
        self.assertEqual(again, first)
        self.assertEqual(second.name, "2026-09-28_fidelis_index-1.json")
        self.assertEqual(third.name, "2026-09-28_fidelis_index-2.json")
        self.assertEqual((first.read_bytes(), second.read_bytes(), third.read_bytes()),
                         (self.content, b"{}", b"[]"))

    def test_must_be_a_json_file(self):
        for name in ("index.txt", "", "folder/", "json"):
            with self.assertRaises(ValueError):
                save_index_upload(name, self.content, self.index_dir)
        self.assertFalse(self.index_dir.exists())


# Run in a child process: an Add reload of the EX file into argv[1] that stops mid-insert,
# inside its write transaction, with pages already spilled into the database file (a tiny
# page cache forces that). The parent kills it there, leaving a hot rollback journal.
INTERRUPTED_ADD = """
import json, sqlite3, sys, time
from pathlib import Path
from etl.load import _reset_source, load_in_network
db, fixture, url = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
data = json.loads(fixture.read_text())
conn = sqlite3.connect(db, isolation_level=None)
conn.execute("PRAGMA cache_size = 2")
conn.execute("BEGIN")
(existing_id,) = conn.execute("SELECT id FROM source_files WHERE url = ?", (url,)).fetchone()
source_id = _reset_source(conn, url, fixture, [], existing_id)
for _ in range(5):
    load_in_network(conn, source_id, data)
print("mid-insert", flush=True)
time.sleep(60)
"""


class InterruptedAddThenReplaceTest(unittest.TestCase):
    """A killed Add load leaves rates.db with a hot journal; Replace must still back up the
    last committed state and swap in a new database that SQLite never "rolls back"."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.raw_dir = self.tmp / "raw"
        self.raw_dir.mkdir()
        shutil.copy(TEST_DATA / EX_NAME, self.raw_dir / EX_NAME)
        self.db_path = self.tmp / "parsed" / "rates.db"
        self.backup_dir = self.tmp / "parsed" / "backups"

    def tearDown(self):
        self._tmp.cleanup()

    def check(self, path):
        """(integrity_check result, table names, (source_files, plans, rates) counts)."""
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            counts = tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                           for t in ("source_files", "plans", "rates"))
            return integrity, tables, counts
        finally:
            conn.close()

    def kill_an_add_load_mid_insert(self):
        child = subprocess.Popen(
            [sys.executable, "-c", INTERRUPTED_ADD, str(self.db_path),
             str(self.raw_dir / EX_NAME), EX_URL],
            cwd=Path(__file__).resolve().parents[2], stdout=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "mid-insert")
            os.kill(child.pid, signal.SIGKILL)
        finally:
            child.wait()
            child.stdout.close()
        journal = Path(str(self.db_path) + "-journal")
        self.assertTrue(journal.exists() and journal.stat().st_size > 0, "no hot journal left")

    def test_replace_after_a_killed_add_load_keeps_backup_and_new_db_intact(self):
        sources = {EX_URL: "local"}
        result = run_load(REAL_INDEX, [EX_URL], self.db_path, self.raw_dir, "add",
                          self.backup_dir, sources=sources)
        self.assertIsNone(result["files"][0]["error"])
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE marker (x)")
        conn.execute("INSERT INTO marker VALUES ('old')")
        conn.commit()
        conn.close()

        self.kill_an_add_load_mid_insert()

        result = run_load(REAL_INDEX, [EX_URL], self.db_path, self.raw_dir, "replace",
                          self.backup_dir, sources=sources)

        self.assertTrue(result["replaced"])
        integrity, tables, counts = self.check(result["backup_path"])
        self.assertEqual(integrity, "ok")
        self.assertIn("marker", tables)  # the last committed state, not the half-written one
        self.assertEqual(counts, (1, 88, EX_RATES))
        for suffix in ("-journal", "-wal", "-shm"):
            self.assertFalse(Path(str(self.db_path) + suffix).exists(), suffix)
        integrity, tables, counts = self.check(self.db_path)
        self.assertEqual(integrity, "ok")
        self.assertNotIn("marker", tables)
        self.assertEqual(counts, (1, 88, EX_RATES))
        # A read-write open (what the next Add load does) must not "recover" anything into it.
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM rates").fetchone()[0], EX_RATES)
        conn.close()


# Run in a child process: a Replace load of the EX file (argv: index path, db path, raw dir, backup dir) that stops
# while saving rates, inside its write transaction; the parent kills it there, as a dev-server restart would.
INTERRUPTED_REPLACE = """
import json, sys, time
from pathlib import Path
from etl.run import run_load
index_path, db, raw_dir, backup_dir = sys.argv[1:5]
index = json.loads(Path(index_path).read_text())
url = index["reporting_structure"][0]["in_network_files"][0]["location"]

def progress(event):
    if event["stage"] == "saving":
        print("saving", flush=True)
        time.sleep(60)

run_load(index, [url], Path(db), Path(raw_dir), "replace", Path(backup_dir), sources={url: "local"},
         index_location=index_path, progress=progress)
"""


def kill_a_replace_load(index_path: Path, db_path: Path, raw_dir: Path, backup_dir: Path) -> None:
    """Really kill (SIGKILL) a Replace load of index_path's first file part-way through saving its rates."""
    child = subprocess.Popen(
        [sys.executable, "-c", INTERRUPTED_REPLACE, str(index_path), str(db_path), str(raw_dir), str(backup_dir)],
        cwd=Path(__file__).resolve().parents[2], stdout=subprocess.PIPE, text=True)
    try:
        line = child.stdout.readline().strip()
        os.kill(child.pid, signal.SIGKILL)
    finally:
        child.wait()
        child.stdout.close()
    assert line == "saving", f"the child load didn't reach saving: {line!r}"


class InterruptedReplaceTest(unittest.TestCase):
    """#42.3: a Replace load killed part-way leaves a half-built <db>.*.new (with its journal) and a marker; the next
    start finds them, deletes them and says which index's load was interrupted. The database is never touched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.raw_dir = self.tmp / "raw"
        self.raw_dir.mkdir()
        shutil.copy(TEST_DATA / EX_NAME, local_path_for(EX_URL, self.raw_dir))
        self.index_path = self.tmp / INDEX_NAME
        shutil.copy(TEST_DATA / INDEX_NAME, self.index_path)
        self.parsed = self.tmp / "parsed"
        self.db_path = self.parsed / "2026-09-28_fidelis_index.db"
        self.backup_dir = self.parsed / "backups"

    def tearDown(self):
        self._tmp.cleanup()

    def leftovers(self):
        return sorted(p.name for p in self.parsed.iterdir() if p.name != "backups" and p != self.db_path)

    def test_a_killed_load_is_cleaned_up_and_reported_and_the_database_is_unchanged(self):
        run_load(REAL_INDEX, [EX_URL], self.db_path, self.raw_dir, "replace", self.backup_dir,
                 sources={EX_URL: "local"}, index_location=str(self.index_path))
        before = self.db_path.read_bytes()

        kill_a_replace_load(self.index_path, self.db_path, self.raw_dir, self.backup_dir)
        left = self.leftovers()
        self.assertTrue(any(n.endswith(".new") for n in left), left)
        self.assertTrue(any(n.endswith(".new-journal") for n in left), left)
        self.assertTrue(any(n.endswith(".loading") for n in left), left)

        (found,) = recover_interrupted(self.parsed)
        self.assertEqual(found["index"], INDEX_NAME)
        self.assertEqual(found["index_location"], str(self.index_path))
        self.assertEqual(found["db"], self.db_path.name)
        self.assertRegex(found["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.db_path.read_bytes(), before)
        self.assertEqual(recover_interrupted(self.parsed), [])  # said once

    def test_a_load_still_running_in_a_live_process_is_left_alone(self):
        self.parsed.mkdir()
        started = threading.Event()
        release = threading.Event()

        def progress(event):
            if event["stage"] == "saving" and not started.is_set():
                started.set()
                release.wait(10)

        thread = threading.Thread(target=run_load, args=(REAL_INDEX, [EX_URL], self.db_path, self.raw_dir,
                                                         "replace", self.backup_dir),
                                  kwargs={"sources": {EX_URL: "local"}, "progress": progress})
        thread.start()
        try:
            self.assertTrue(started.wait(10))
            left = self.leftovers()
            self.assertEqual(recover_interrupted(self.parsed), [])
            self.assertEqual(self.leftovers(), left)
        finally:
            release.set()
            thread.join(10)
        self.assertEqual(self.leftovers(), [])
        self.assertTrue(self.db_path.exists())

    def test_half_built_files_without_a_marker_are_cleaned_up_too(self):
        self.parsed.mkdir()
        (self.parsed / "2026-01-01_x_index.db.abc123.new").write_bytes(b"half")
        (self.parsed / "2026-01-01_x_index.db.abc123.new-journal").write_bytes(b"journal")
        (self.parsed / "keep.db-journal").write_bytes(b"a real database's journal is never touched")
        (found,) = recover_interrupted(self.parsed)
        self.assertEqual((found["index"], found["db"]), ("2026-01-01_x_index", "2026-01-01_x_index.db"))
        self.assertIsNone(found["index_location"])
        self.assertEqual(sorted(p.name for p in self.parsed.iterdir()), ["keep.db-journal"])

    def test_no_parsed_dir_finds_nothing(self):
        self.assertEqual(recover_interrupted(self.tmp / "missing"), [])


class ReadFileHeaderTest(unittest.TestCase):
    """#35: an in-network file's top-level fields, read from its first HEADER_BYTES without parsing the rest."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, data: bytes) -> Path:
        path = Path(self.tmp.name) / "file.json"
        path.write_bytes(data)
        return path

    def test_reads_the_real_files_header(self):
        self.assertEqual(read_file_header(TEST_DATA / EX_NAME), {
            "reporting_entity_name": "Centene Management Company LLC",
            "reporting_entity_type": "Third Party Administrator",
            "version": "2.0.0", "last_updated_on": "2026-10-06"})

    def test_the_rest_of_the_file_is_not_parsed(self):
        # The first half of the real file is not valid JSON, and its header still reads.
        data = (TEST_DATA / EX_NAME).read_bytes()
        header = read_file_header(self.write(data[:len(data) // 2]))
        self.assertEqual(header["reporting_entity_name"], "Centene Management Company LLC")

    def test_only_the_first_chunk_is_read(self):
        # Fields past HEADER_BYTES are not seen, even though the file is valid JSON.
        padding = " " * HEADER_BYTES
        path = self.write(f'{{"version": "2.0.0",{padding}"last_updated_on": "2026-10-06"}}'.encode())
        self.assertEqual(read_file_header(path), {"version": "2.0.0"})

    def test_stops_at_the_first_array_or_object(self):
        path = self.write(json.dumps({"version": "1.0.0", "provider_references": [{"provider_group_id": 1}],
                                      "reporting_entity_name": "After the array"}).encode())
        self.assertEqual(read_file_header(path), {"version": "1.0.0"})

    def test_strings_are_json_decoded(self):
        path = self.write(b'\xef\xbb\xbf { "reporting_entity_name" : "A \\"quoted\\" Caf\\u00e9, Inc.", "n": 3,'
                          b' "in_network": [')
        self.assertEqual(read_file_header(path), {"reporting_entity_name": 'A "quoted" Caf\u00e9, Inc.', "n": 3})

    def test_a_value_cut_off_by_the_chunk_is_left_out(self):
        path = self.write(b'{"version": "2.0.0", "last_updated_on": "2026-' + b" " * HEADER_BYTES)
        self.assertEqual(read_file_header(path), {"version": "2.0.0"})
        number = self.write(b'{"version": "2.0.0", "n": ' + b" " * (HEADER_BYTES - 29) + b"12345}")  # cut after "123"
        self.assertEqual(read_file_header(number), {"version": "2.0.0"})

    def test_not_json_or_not_an_object_is_empty(self):
        self.assertEqual(read_file_header(self.write(b"{this is not json")), {})
        self.assertEqual(read_file_header(self.write(b"[1, 2]")), {})
        self.assertEqual(read_file_header(self.write(b"")), {})


if __name__ == "__main__":
    unittest.main()
