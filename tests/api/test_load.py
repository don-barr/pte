import copy
import html
import http.server
import json
import shutil
import os
import re
import socket
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

import httpx
import uvicorn
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from jinja2.filters import do_filesizeformat as filesizeformat

from app import create_app
from etl.schema import local_path_for
from tests.api import EX_URL, ES_URL, build_db

ETL_TEST_DATA = Path(__file__).parents[1] / "etl" / "test_data"
INDEX_NAME = "2026-09-28_fidelis_index.json"
INDEX = str(ETL_TEST_DATA / INDEX_NAME)
REAL_INDEX = json.loads(Path(INDEX).read_text())
EX_FILE = ETL_TEST_DATA / "2026-09-28_centene-management-company-llc_fidelis-ex_in-network.json"
OWN = "http://testserver"  # TestClient's own origin


class FileServer:
    """A real local HTTP server for the fixture files. Paths in `slow` wait for `release` before answering;
    every request path is recorded in `requests`."""

    def __init__(self, files: dict[str, bytes], slow: set[str] = frozenset(), tls: tuple[str, str] | None = None):
        self.requests, self.release = [], threading.Event()
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                server.requests.append(self.path)
                if self.path in slow:
                    server.release.wait(20)
                body = files.get(self.path)
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        if tls:  # (cert file, key file): serve https
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*tls)
            self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.url = f"{'https' if tls else 'http'}://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()


def wait_for_job(client: TestClient, response, timeout: float = 30):
    """Follow a load's redirect to its status page and poll it until the load is done."""
    if response.status_code != 303:
        return response
    url, deadline = response.headers["location"], time.monotonic() + timeout
    while True:
        page = client.get(url)
        if 'data-job="running"' not in page.text or time.monotonic() > deadline:
            return page
        time.sleep(0.05)


class LoadTestCase(unittest.TestCase):
    download = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.parsed = root / "parsed"
        self.db_path = self.parsed / "2026-09-28_fidelis_index.db"  # loading INDEX builds this (#37.5)
        self.backup_dir = self.parsed / "backups"
        self.raw_dir = root / "raw"
        self.index_dir = root / "index_files"
        shutil.copytree(ETL_TEST_DATA, self.raw_dir)  # the fidelis-ex slice is "already downloaded"
        # download=False: tests never fetch the real Centene files.
        self.app = create_app(parsed_dir=self.parsed, raw_dir=self.raw_dir, index_dir=self.index_dir,
                              backup_dir=self.backup_dir, download=self.download)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.tmp.cleanup()

    def open_index(self, location: str, **kwargs):
        return self.client.post("/load/open", data={"location": location}, **kwargs)

    def load(self, data: dict, **kwargs):
        return wait_for_job(self.client, self.client.post("/load", data=data, follow_redirects=False, **kwargs))

    def rate_count(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM rates").fetchone()[0]


class LoadPageTest(LoadTestCase):
    """The Load page reads a real index fixture and loads the real fidelis-ex slice (1,828 rates)."""

    def test_page_asks_for_an_index_file(self):
        text = self.client.get("/load").text
        self.assertIn("Choose An Index File", text)
        self.assertIn("Open Another", text)
        self.assertIn('name="location"', text)
        self.assertIn("https:// URL or a local path", text)

    def test_reading_an_index_lists_its_files_with_their_plans(self):
        text = self.open_index(INDEX).text
        self.assertEqual(text.count('type="checkbox" name="url"'), 2)
        self.assertIn("fidelis-ex_in-network.json", text)
        self.assertIn("88 plans", text)
        self.assertIn("6 plans", text)
        self.assertIn("Ambetter from Fidelis Care Bronze", text)
        self.assertIn("on disk (", text)
        self.assertIn("size unknown until download", text)
        self.assertNotIn('name="mode"', text)  # no Add or Replace: loading just loads (#37.3)
        self.assertIn("2026-09-28_fidelis_index.db", text)  # what loading builds

    def test_unreadable_index_shows_why(self):
        response = self.open_index("/nope/index.json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Index file not found", response.text)
        self.assertNotIn('name="url"', response.text)
        self.assertIn('value="/nope/index.json"', response.text)  # kept in the box to fix

    def test_loads_the_chosen_file(self):
        response = self.load({"index": INDEX, "url": [EX_URL]})
        self.assertEqual(response.status_code, 200)
        self.assertIn("1,828", response.text)
        self.assertIn("88", response.text)
        self.assertEqual(self.rate_count(), 1828)
        self.assertIn("fidelis-ex_in-network.json", self.client.get("/").text)

    def test_loading_the_same_index_again_backs_up_its_database_first(self):
        self.db_path.parent.mkdir(parents=True)
        with sqlite3.connect(self.db_path) as conn:
            build_db(conn)
        response = self.load({"index": INDEX, "url": [EX_URL]})
        self.assertIn("Backed up", response.text)
        backups = list(self.backup_dir.glob("2026-09-28_fidelis_index-*.db"))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM rates").fetchone()[0], 3)
        self.assertEqual(self.rate_count(), 1828)

    def test_a_file_that_fails_is_reported_not_a_crash(self):
        response = self.load({"index": INDEX, "url": [ES_URL]})
        self.assertEqual(response.status_code, 200)
        self.assertIn("fidelis-es_in-network.json", response.text)
        self.assertIn("error", response.text.lower())

    def test_must_pick_a_file(self):
        none_picked = self.load({"index": INDEX})
        self.assertIn("Pick at least one file", none_picked.text)
        self.assertFalse(self.db_path.exists())

    def invalid_index(self, *plan_numbers) -> str:
        """The real index with plan_id removed from the given plans of its first structure."""
        index = copy.deepcopy(REAL_INDEX)
        for n in plan_numbers:
            del index["reporting_structure"][0]["reporting_plans"][n]["plan_id"]
        path = Path(self.tmp.name) / "invalid_index.json"
        path.write_text(json.dumps(index))
        return str(path)

    # Step 1: schema validation

    def test_a_valid_index_says_so(self):
        self.assertIn("✓ Valid CMS index", self.open_index(INDEX).text)

    def test_an_invalid_index_lists_its_problems_and_hides_step_2(self):
        text = self.open_index(self.invalid_index(3)).text
        self.assertNotIn("✓ Valid CMS index", text)
        self.assertIn("reporting_structure[0] → reporting_plans[3]: &#39;plan_id&#39; is a required property", text)
        self.assertNotIn('name="url"', text)
        self.assertNotIn('name="mode"', text)

    def test_many_problems_are_capped(self):
        # 30 missing plan_ids, plus plans that are now duplicates of each other: 31 problems.
        text = self.open_index(self.invalid_index(*range(30))).text
        self.assertIn("(31 problems)", text)
        self.assertEqual(text.count("<li><code>"), 20)
        self.assertIn("and 11 more", text)

    def test_posting_an_invalid_index_loads_nothing(self):
        response = self.load({"index": self.invalid_index(3), "url": [EX_URL]})
        self.assertEqual(response.status_code, 200)
        self.assertIn("doesn’t match the CMS schema", response.text)
        self.assertFalse(self.db_path.exists())

    # Step 1 (#31): one verb, "Open"

    def test_lists_index_files_on_disk_each_row_opens_it(self):
        self.index_dir.mkdir()
        shutil.copy(INDEX, self.index_dir / INDEX_NAME)
        (self.index_dir / "2025-01-01_broken_index.json").write_text("{broken")
        text = self.client.get("/load").text
        href = f'href="/?index={quote(str(self.index_dir / INDEX_NAME), safe="")}"'
        self.assertIn(f'<a class="index-row" {href}', text)  # the whole row is the link
        self.assertIn("2 in-network files", text)
        self.assertIn("94 plans", text)
        self.assertIn("✓ valid", text)
        self.assertIn("2025-01-01_broken_index.json", text)
        self.assertIn("not valid JSON", text)
        self.assertNotIn(">Use<", text)
        self.assertNotIn("Read index", text)

    def test_opening_a_listed_file_marks_it_and_shows_its_files(self):
        self.index_dir.mkdir()
        shutil.copy(INDEX, self.index_dir / INDEX_NAME)
        text = self.client.get("/load", params={"index": str(self.index_dir / INDEX_NAME)}).text
        self.assertIn('aria-current="true"', text)
        self.assertIn("✓ Valid CMS index", text)
        self.assertEqual(text.count('type="checkbox" name="url"'), 2)

    def test_opening_a_local_path_adds_it_to_the_list(self):
        response = self.open_index(INDEX, follow_redirects=False)
        saved = self.index_dir / INDEX_NAME
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/?index={quote(str(saved), safe='')}")
        self.assertEqual(saved.read_bytes(), Path(INDEX).read_bytes())
        # Opening it again (from where it was, or from the list) adds nothing new.
        self.open_index(INDEX)
        self.open_index(str(saved))
        self.assertEqual([p.name for p in self.index_dir.iterdir()], [INDEX_NAME])

    def test_a_path_that_is_not_an_index_is_not_added(self):
        broken = Path(self.tmp.name) / "broken.json"
        broken.write_text("{broken")
        response = self.open_index(str(broken))
        self.assertEqual(response.status_code, 400)
        self.assertIn("not valid JSON", response.text)
        self.assertFalse(self.index_dir.exists() and any(self.index_dir.iterdir()))

    def test_get_only_reads_files_from_the_list(self):
        text = self.client.get("/load", params={"index": INDEX}).text
        self.assertIn("isn’t in your index files", text)
        self.assertNotIn('name="url"', text)
        self.assertFalse(self.index_dir.exists())

    def test_an_index_file_deleted_from_the_list_is_said_once_not_on_every_reload(self):
        self.index_dir.mkdir()
        listed = self.index_dir / INDEX_NAME
        shutil.copy(INDEX, listed)
        self.assertIn(INDEX_NAME, self.client.get("/").text)
        listed.unlink()  # deleted in Finder
        url = f"/?index={quote(str(listed), safe='')}"
        response = self.client.get(url, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")
        text = self.client.get("/").text
        self.assertIn(f"That index file no longer exists ({INDEX_NAME}); it was removed from your index files.",
                      text)
        self.assertNotIn("isn’t in your index files", text)
        self.assertNotIn("no longer exists", self.client.get("/").text)  # once

    def test_choose_a_file_opens_on_pick(self):
        text = self.client.get("/load").text
        self.assertIn("Choose a file from your computer…", text)
        self.assertIn('action="/load/upload"', text)
        self.assertIn("this.form.submit()", text)  # opens as soon as a file is picked
        self.assertIn("<noscript>", text)  # without JS, a button to open it
        self.assertNotIn(">Upload<", text)

    def test_upload_saves_into_the_index_folder_and_opens_it(self):
        content = Path(INDEX).read_bytes()
        response = self.client.post("/load/upload", files={"file": (INDEX_NAME, content, "application/json")},
                                    follow_redirects=False)
        saved = self.index_dir / INDEX_NAME
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/?index={quote(str(saved), safe='')}")
        self.assertEqual(saved.read_bytes(), content)
        self.assertIn("✓ Valid CMS index", self.client.get(response.headers["location"]).text)

    def test_upload_must_be_a_json_file(self):
        response = self.client.post("/load/upload", files={"file": ("notes.txt", b"hello", "text/plain")},
                                    follow_redirects=False)
        self.assertEqual(response.status_code, 400)
        self.assertIn("is not a .json file", response.text)
        self.assertFalse(self.index_dir.exists())

    def test_uploading_a_file_that_is_not_an_index_shows_why_and_saves_nothing(self):
        # A real in-network file chosen by mistake: it must not join the index list as a failed entry.
        response = self.client.post("/load/upload", files={"file": (EX_FILE.name, EX_FILE.read_bytes(),
                                                                    "application/json")}, follow_redirects=False)
        self.assertEqual(response.status_code, 400)
        self.assertIn("is not an index file", response.text)
        self.assertFalse((self.index_dir / EX_FILE.name).exists())
        self.assertNotIn(EX_FILE.name, self.client.get("/").text.split("1. Choose An Index File", 1)[1])

    def test_uploading_a_file_that_is_not_json_shows_why_and_saves_nothing(self):
        response = self.client.post("/load/upload", files={"file": ("broken_index.json", b"{not json",
                                                                    "application/json")}, follow_redirects=False)
        self.assertEqual(response.status_code, 400)
        self.assertIn("is not valid JSON", response.text)
        self.assertFalse((self.index_dir / "broken_index.json").exists())

    # Step 2: each file's source

    def test_each_file_offers_its_sources(self):
        text = self.open_index(INDEX).text
        # fidelis-ex is on disk: local copy is the default.
        self.assertIn(f'name="source:{EX_URL}" value="local" checked>', text)
        self.assertIn(f'name="source:{EX_URL}" value="download">', text)
        self.assertIn(f'name="source:{EX_URL}" value="web">', text)
        # fidelis-es is not: no local copy to pick, download and keep is the default.
        self.assertIn(f'name="source:{ES_URL}" value="local" disabled>', text)
        self.assertIn(f'name="source:{ES_URL}" value="download" checked>', text)
        self.assertIn(f'name="source:{ES_URL}" value="web">', text)

    def test_post_uses_the_chosen_source(self):
        local = self.load({"index": INDEX, "url": [EX_URL], f"source:{EX_URL}": "local"})
        self.assertIn("1,828", local.text)
        self.assertIn("local copy", local.text)
        # This app has downloads disabled, so "web" is refused rather than fetched.
        web = self.load({"index": INDEX, "url": [EX_URL], f"source:{EX_URL}": "web"})
        self.assertIn("downloads disabled", web.text)
        self.assertIn("from the web", web.text)
        self.assertEqual(self.rate_count(), 1828)

    def test_unknown_source_is_an_error_not_a_crash(self):
        response = self.load({"index": INDEX, "url": [EX_URL], f"source:{EX_URL}": "ftp"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Choose where each file comes from", response.text)
        self.assertFalse(self.db_path.exists())


class FilePreviewTest(LoadTestCase):
    """#35: before loading, each in-network file's plans (from the index) and header (from its local copy's first
    bytes) can be previewed: inline in step 2, or at /load/preview and /api/load/preview for an index in the list."""

    def setUp(self):
        super().setUp()
        self.index_dir.mkdir()
        self.listed = str(self.index_dir / INDEX_NAME)
        shutil.copy(INDEX, self.listed)

    def preview(self, url: str, index: str | None = None, api: bool = True):
        return self.client.get("/api/load/preview" if api else "/load/preview",
                               params={"index": self.listed if index is None else index, "url": url})

    def test_plans_come_from_the_index_sorted_by_name(self):
        body = self.preview(EX_URL).json()
        self.assertEqual(body["file"], "2026-09-28_centene-management-company-llc_fidelis-ex_in-network.json")
        self.assertEqual(body["url"], EX_URL)
        self.assertEqual((body["plan_count"], body["name_count"], body["plan_id_count"]), (88, 37, 23))
        self.assertEqual(len(body["plans"]), 88)
        keys = [(p["plan_name"], p["plan_id"]) for p in body["plans"]]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(body["plans"][0], {"plan_name": "Ambetter from Fidelis Care Bronze", "plan_id": "25303NY001",
                                            "plan_id_type": "hios", "plan_market_type": "individual",
                                            "issuer_name": "Fidelis Care"})

    def test_names_under_several_plan_ids_are_noted(self):
        body = self.preview(EX_URL).json()
        several = {n["plan_name"]: n["plan_ids"] for n in body["names"] if len(n["plan_ids"]) > 1}
        self.assertEqual(several["Ambetter from Fidelis Care Bronze"], ["25303NY001", "25303NY005", "25303NY011"])
        self.assertEqual(len(several), 36)
        text = self.preview(EX_URL, api=False).text
        self.assertIn("36 of the 37 plan names appear under more than one plan ID", text)
        self.assertIn("3 plan IDs", text)

    def test_other_files_sharing_plans(self):
        self.assertEqual(self.preview(EX_URL).json()["shared_with"], [])
        self.assertIn("No other file in this index covers any of these plans", self.preview(EX_URL, api=False).text)
        # An index where fidelis-es also lists one of fidelis-ex's plans.
        index = copy.deepcopy(REAL_INDEX)
        index["reporting_structure"][1]["reporting_plans"].append(index["reporting_structure"][0]["reporting_plans"][0])
        shared = self.index_dir / "2026-09-28_shared_index.json"
        shared.write_text(json.dumps(index))
        body = self.preview(EX_URL, index=str(shared)).json()
        self.assertEqual(body["shared_with"], [
            {"url": ES_URL, "file": "2026-09-28_centene-management-company-llc_fidelis-es_in-network.json",
             "plan_ids": ["25303NY005"]}])
        self.assertIn("fidelis-es_in-network.json", self.preview(EX_URL, index=str(shared), api=False).text)

    def test_a_local_copy_shows_its_size_date_and_header(self):
        body = self.preview(EX_URL).json()
        local = body["local"]
        self.assertEqual(local["size"], EX_FILE.stat().st_size)
        self.assertRegex(local["modified"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
        self.assertEqual(local["header"], {"reporting_entity_name": "Centene Management Company LLC",
                                           "reporting_entity_type": "Third Party Administrator",
                                           "version": "2.0.0", "last_updated_on": "2026-10-06"})
        text = self.preview(EX_URL, api=False).text
        self.assertIn("Centene Management Company LLC", text)
        self.assertIn("Third Party Administrator", text)
        self.assertIn("2026-10-06", text)
        self.assertIn("145.3 kB", text)

    def test_no_local_copy_says_not_downloaded_and_fetches_nothing(self):
        body = self.preview(ES_URL).json()
        self.assertIsNone(body["local"])
        self.assertEqual(body["plan_count"], 6)
        text = self.preview(ES_URL, api=False).text
        self.assertIn("Not downloaded yet", text)
        self.assertIn(ES_URL, text)
        self.assertFalse(local_path_for(ES_URL, self.raw_dir).exists())

    def test_header_fields_missing_from_the_first_bytes_say_so(self):
        # fidelis-es's "local copy": the real file's arrays first, its header fields after them.
        data = json.loads(EX_FILE.read_bytes())
        header = {k: data.pop(k) for k in ("reporting_entity_name", "reporting_entity_type", "version")}
        local_path_for(ES_URL, self.raw_dir).write_text(json.dumps(dict(data, **header)))
        body = self.preview(ES_URL).json()
        self.assertEqual(body["local"]["header"], {"reporting_entity_name": None, "reporting_entity_type": None,
                                                   "version": None, "last_updated_on": "2026-10-06"})
        self.assertEqual(self.preview(ES_URL, api=False).text.count("not in the file’s header"), 3)

    def test_says_whether_the_file_is_loaded_and_its_rates(self):
        self.assertIsNone(self.preview(EX_URL).json()["loaded"])
        self.assertIn("Not loaded", self.preview(EX_URL, api=False).text)
        self.load({"index": INDEX, "url": [EX_URL]})
        loaded = self.preview(EX_URL).json()["loaded"]
        self.assertEqual(loaded["rate_count"], 1828)
        self.assertIsNone(loaded["error"])
        self.assertTrue(loaded["loaded_at"])
        self.assertIn("1,828 rates", self.preview(EX_URL, api=False).text)

    def test_the_fragment_is_not_a_whole_page(self):
        response = self.preview(EX_URL, api=False)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<html", response.text)
        self.assertIn("<table", response.text)
        self.assertIn("25303NY011", response.text)
        self.assertIn("HIOS", response.text)

    def test_step_2_has_a_preview_toggle_per_file(self):
        text = self.client.get("/load", params={"index": self.listed}).text
        self.assertEqual(text.count('<details class="preview">'), 2)
        self.assertEqual(text.count("<summary>Preview</summary>"), 2)
        self.assertIn("Centene Management Company LLC", text)  # fidelis-ex's header, read from its local copy
        self.assertIn("Not downloaded yet", text)  # fidelis-es
        self.assertIn("25303EP000", text)

    def test_only_indexes_in_the_list_and_files_in_the_index(self):
        server = FileServer({"/" + INDEX_NAME: Path(INDEX).read_bytes()})
        self.addCleanup(server.close)
        for index in (INDEX, f"{server.url}/{INDEX_NAME}", "", str(self.index_dir / "nope.json")):
            for api in (True, False):
                with self.subTest(index=index, api=api):
                    response = self.preview(EX_URL, index=index, api=api)
                    self.assertEqual(response.status_code, 404)
                    self.assertIn("isn’t in your index files", response.text)
        self.assertEqual(server.requests, [])
        for api in (True, False):
            response = self.preview("https://example.com/other_in-network.json", api=api)
            self.assertEqual(response.status_code, 404)
            self.assertIn("isn’t one of this index’s in-network files", response.text)


class OriginCheckTest(LoadTestCase):
    """#33: a page on another site can't make the browser load data, open URLs or save files here."""

    def test_cross_origin_posts_are_refused_and_change_nothing(self):
        for headers in ({"Origin": "http://evil.example"}, {"Origin": "null"},
                        {"Referer": "http://evil.example/page"}, {"Origin": "http://testserver.evil.example"}):
            with self.subTest(headers=headers):
                load = self.client.post("/load", data={"index": INDEX, "url": [EX_URL]},
                                        headers=headers, follow_redirects=False)
                self.assertEqual(load.status_code, 403)
                self.assertIn("another site", load.text)
                opened = self.open_index(INDEX, headers=headers, follow_redirects=False)
                self.assertEqual(opened.status_code, 403)
                upload = self.client.post("/load/upload", headers=headers, follow_redirects=False,
                                          files={"file": (INDEX_NAME, Path(INDEX).read_bytes(), "application/json")})
                self.assertEqual(upload.status_code, 403)
        self.assertFalse(self.db_path.exists())
        self.assertFalse(self.index_dir.exists())

    def test_same_origin_posts_work(self):
        page = self.load({"index": INDEX, "url": [EX_URL]}, headers={"Origin": OWN})
        self.assertIn("1,828", page.text)
        referred = self.open_index(INDEX, headers={"Referer": OWN + "/load"}, follow_redirects=False)
        self.assertEqual(referred.status_code, 303)
        self.assertEqual(self.rate_count(), 1828)


class OpenUrlTest(LoadTestCase):
    """Opening a URL downloads it (a POST); a GET link naming a URL fetches nothing (#33)."""

    def setUp(self):
        super().setUp()
        self.server = FileServer({"/" + INDEX_NAME: Path(INDEX).read_bytes()})
        self.url = f"{self.server.url}/{INDEX_NAME}"

    def tearDown(self):
        self.server.close()
        super().tearDown()

    def test_get_with_a_url_does_not_fetch_it(self):
        response = self.client.get("/load", params={"index": self.url})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.server.requests, [])
        self.assertIn("isn’t in your index files", response.text)
        self.assertNotIn('name="url"', response.text)

    def test_opening_a_url_downloads_it_into_the_list(self):
        response = self.open_index(self.url, headers={"Origin": OWN})
        self.assertEqual(self.server.requests, ["/" + INDEX_NAME])
        self.assertIn("✓ Valid CMS index", response.text)
        self.assertEqual(response.text.count('type="checkbox" name="url"'), 2)
        (saved,) = self.index_dir.iterdir()
        self.assertIn(f'href="/?index={quote(str(saved), safe="")}"', self.client.get("/load").text)


def serve(app) -> tuple[uvicorn.Server, threading.Thread, str]:
    """Run app in a real uvicorn server on a free port (one event loop, as in production)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    return server, thread, f"http://127.0.0.1:{sock.getsockname()[1]}"


@unittest.skipUnless(shutil.which("openssl"), "needs openssl to make a certificate for the local https server")
class BackgroundLoadTest(LoadTestCase):
    """#33: a load runs in the background; the app keeps answering and the page shows progress. The fidelis-ex
    file comes from a local https server (the index schema requires https) that holds it until `release`."""
    download = True  # only ever fetches from the local FileServer below

    @classmethod
    def setUpClass(cls):
        cls.certs = tempfile.TemporaryDirectory()
        cls.cert, cls.key = (str(Path(cls.certs.name) / name) for name in ("cert.pem", "key.pem"))
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj",
                        "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", cls.key,
                        "-out", cls.cert], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.certs.cleanup()

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"SSL_CERT_FILE": self.cert, "NO_PROXY": "127.0.0.1"})
        env.start()
        self.addCleanup(env.stop)
        self.server = FileServer({"/ex.json": EX_FILE.read_bytes()}, slow={"/ex.json"}, tls=(self.cert, self.key))
        self.file_url = f"{self.server.url}/ex.json"
        # The real index, with fidelis-ex served by the local (slow) server.
        index = json.loads(json.dumps(REAL_INDEX).replace(json.dumps(EX_URL)[1:-1], self.file_url))
        self.index = Path(self.tmp.name) / "local_index.json"
        self.index.write_text(json.dumps(index))
        self.db_path = self.parsed / "local_index.db"
        self.form = {"index": str(self.index), "url": [self.file_url],
                     f"source:{self.file_url}": "web"}

    def tearDown(self):
        self.server.close()
        super().tearDown()

    def wait_for_download(self):
        deadline = time.monotonic() + 5
        while self.server.requests == [] and time.monotonic() < deadline:
            time.sleep(0.01)  # the load is now waiting on the slow file

    def test_load_shows_progress_then_the_result(self):
        started = self.client.post("/load", data=self.form, follow_redirects=False)
        self.assertEqual(started.status_code, 303)
        self.assertTrue(started.headers["location"].startswith("/load/status/"))
        running = self.client.get(started.headers["location"])
        self.assertIn('data-job="running"', running.text)
        self.assertIn('http-equiv="refresh"', running.text)
        self.assertIn("ex.json", running.text)
        # #42.6: right where Load was pressed, the form is replaced by the load's progress.
        self.assertTrue(started.headers["location"].endswith("#load-progress"))
        self.assertNotIn('id="load-form"', running.text)
        self.assertNotIn("Tick the files to load", running.text)
        load_section = running.text[running.text.index('id="load"'):]
        self.assertIn('id="load-progress"', load_section)
        self.assertEqual(running.text.count('id="load-progress"'), 1)
        # A second load while this one runs is refused.
        second = self.client.post("/load", data=self.form, follow_redirects=False)
        self.assertEqual(second.status_code, 409)
        self.assertIn("already running", second.text)
        self.assertIn(started.headers["location"], second.text)
        self.server.release.set()
        done = wait_for_job(self.client, started)
        self.assertIn('data-job="done"', done.text)
        self.assertNotIn('http-equiv="refresh"', done.text)
        self.assertIn("1,828", done.text)
        self.assertIn("loaded now", done.text)  # steps 2-3 are back, with the new marks
        self.assertEqual(self.rate_count(), 1828)

    def test_unknown_job_is_404(self):
        self.assertEqual(self.client.get("/load/status/nope").status_code, 404)

    def test_the_load_page_shows_a_running_load_at_the_top_from_any_reload(self):
        """#42.1: index, files done of total, the current file and its stage, rates so far, elapsed."""
        started = self.client.post("/load", data=self.form, follow_redirects=False)
        self.wait_for_download()
        try:
            for url in ("/", f"/?index={quote(INDEX, safe='')}"):
                text = self.client.get(url).text
                panel = text[text.index('id="load-progress"'):text.index('id="indexes"')]
                self.assertIn('data-job="running"', panel)
                self.assertIn("local_index.json", panel)
                self.assertIn("0 of 1 file done", panel)
                self.assertIn("0 rates so far", panel)
                self.assertRegex(panel, r"\d+ s elapsed")
                self.assertRegex(panel, r"ex\.json.*Downloading", )
                self.assertIn("Make it the active index", panel)
                self.assertIn('http-equiv="refresh"', text)  # without JavaScript, the page reloads itself
                self.assertIn(f'data-progress-url="/load/progress/{started.headers["location"].split("/")[3].split("#")[0]}"', panel)
        finally:
            self.server.release.set()
        wait_for_job(self.client, started)
        self.assertNotIn('id="load-progress"', self.client.get("/").text)

    def test_progress_as_json_and_as_a_fragment(self):
        started = self.client.post("/load", data=self.form, follow_redirects=False)
        job_id = started.headers["location"].split("/")[3].split("#")[0]
        self.wait_for_download()
        try:
            body = self.client.get("/api/load/progress").json()
            self.assertTrue(body["running"])
            self.assertEqual(body["job"]["id"], job_id)
            self.assertEqual(body["job"]["index"], "local_index.json")
            self.assertEqual((body["job"]["files_done"], body["job"]["files_total"]), (0, 1))
            self.assertEqual(body["job"]["files"][0]["stage"], "downloading")
            self.assertEqual(body["job"]["stage"], "loading")
            fragment = self.client.get(f"/load/progress/{job_id}").text
            self.assertIn('data-job="running"', fragment)
            self.assertNotIn("<html", fragment)
        finally:
            self.server.release.set()
        wait_for_job(self.client, started)
        self.assertFalse(self.client.get("/api/load/progress").json()["running"])
        self.assertIn('data-job="done"', self.client.get(f"/load/progress/{job_id}").text)
        self.assertEqual(self.client.get("/load/progress/nope").status_code, 404)

    def test_health_answers_while_a_load_runs(self):
        server, thread, base = serve(self.app)
        try:
            with httpx.Client(base_url=base, timeout=10) as http:
                started = time.monotonic()
                post = http.post("/load", data=self.form, follow_redirects=False)
                self.assertLess(time.monotonic() - started, 2)
                self.assertEqual(post.status_code, 303)
                deadline = time.monotonic() + 5
                while self.server.requests == [] and time.monotonic() < deadline:
                    time.sleep(0.01)  # the load is now waiting on the slow file
                started = time.monotonic()
                health = http.get("/api/health")
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(health.status_code, 200)
                self.assertIn('data-job="running"', http.get(post.headers["location"]).text)
                self.server.release.set()
        finally:
            self.server.release.set()
            server.should_exit = True
            thread.join(10)


class InterruptedLoadTest(LoadTestCase):
    """#42.3: a load really killed part-way (as by the dev server restarting) is cleaned up when the app starts, or
    when the Load Data page shows with no load running, and said once."""

    def setUp(self):
        super().setUp()
        self.index_dir.mkdir()
        self.listed = self.index_dir / INDEX_NAME
        shutil.copy(INDEX, self.listed)
        self.load({"index": str(self.listed), "url": [EX_URL], f"source:{EX_URL}": "local"})
        self.before = self.db_path.read_bytes()

    def kill(self):
        from tests.etl.test_run import kill_a_replace_load
        kill_a_replace_load(self.listed, self.db_path, self.raw_dir, self.backup_dir)
        self.assertTrue(any(p.name.endswith(".new") for p in self.parsed.iterdir()))

    def assert_said_once(self, client):
        text = client.get("/").text
        self.assertRegex(text, r"A load of 2026-09-28_fidelis_index\.json was interrupted at "
                               r"\d{4}-\d\d-\d\d \d\d:\d\d UTC; nothing was changed\. "
                               r'<a href="/\?index=[^"]+#load">Load again\?</a>')
        self.assertIn(f'href="/?index={quote(str(self.listed), safe="")}#load"', text)
        self.assertEqual([p.name for p in self.parsed.iterdir() if ".new" in p.name or p.suffix == ".loading"], [])
        self.assertEqual(self.db_path.read_bytes(), self.before)
        self.assertNotIn("was interrupted", client.get("/").text)

    def test_on_startup(self):
        self.kill()
        app = create_app(parsed_dir=self.parsed, raw_dir=self.raw_dir, index_dir=self.index_dir,
                         backup_dir=self.backup_dir, download=False)
        self.assertEqual([p.name for p in self.parsed.iterdir() if ".new" in p.name], [])  # before any request
        self.assert_said_once(TestClient(app))

    def test_when_the_page_shows_with_no_load_running(self):
        self.kill()
        self.assert_said_once(self.client)


class ProgressPanelTest(unittest.TestCase):
    """#42.2: the panel names each file's stage, and "load" and "make it active" are separate steps."""

    def setUp(self):
        from ui.load_page import Jobs
        self.jobs, self.release = Jobs(), threading.Event()
        self.templates = Jinja2Templates(directory=Path(__file__).parents[2] / "ui" / "templates")

    def start(self, events):
        def work(report):
            for event in events:
                report(event)
            self.release.wait(10)
            return {"replaced": True}
        files = [{"url": u, "file": u + ".json", "source": "download"} for u in ("a", "b", "c")]
        job, _ = self.jobs.start("/x/2026_index.json", files, work)
        self.addCleanup(self.release.set)
        deadline = time.monotonic() + 5
        while self.jobs.snapshot(job["id"])["events"] < len(events) and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.jobs.snapshot(job["id"])

    def render(self, job) -> str:
        return " ".join(self.templates.get_template("_load_progress.html").render(job=job).split())

    def test_each_file_shows_its_stage(self):
        job = self.start([
            {"stage": "downloading", "url": "a", "bytes": 0, "total": 2_000_000},
            {"stage": "reading", "url": "a"},
            {"stage": "saving", "url": "a", "rows": 0, "db_bytes": 4096},
            {"stage": "saving", "url": "a", "rows": 12345, "db_bytes": 1_200_000},
            {"stage": "loaded", "url": "a", "rate_count": 12345, "error": None},
            {"stage": "downloading", "url": "b", "bytes": 1_000_000, "total": 2_000_000},
        ])
        self.assertEqual((job["files_done"], job["files_total"], job["rates"]), (1, 3, 12345))
        self.assertEqual(job["current"]["file"], "b.json")
        text = self.render(job)
        self.assertIn("1 of 3 files done", text)
        self.assertIn("12,345 rates so far", text)
        self.assertIn("✓ 12,345 rates", text)
        self.assertIn("Downloading 1.0 MB of 2.0 MB", text)
        self.assertIn("Waiting", text)
        self.assertRegex(text, r"Make it the active index.*not yet")

    def test_saving_shows_rows_so_far_and_the_database_size(self):
        job = self.start([{"stage": "reading", "url": "a"},
                          {"stage": "saving", "url": "a", "rows": 50000, "db_bytes": 1_200_000_000}])
        self.assertEqual(job["rates"], 50000)
        text = self.render(job)
        self.assertIn("Saving rates: 50,000 so far · database 1.2 GB", text)

    def test_reading_and_unknown_download_size(self):
        job = self.start([{"stage": "downloading", "url": "a", "bytes": 3_000_000, "total": None},
                          {"stage": "reading", "url": "b"}])
        text = self.render(job)
        self.assertIn("Downloading 3.0 MB", text)
        self.assertNotIn("3.0 MB of", text)
        self.assertIn("Reading", text)

    def test_before_the_server_answers(self):
        job = self.start([{"stage": "downloading", "url": "a", "bytes": 0, "total": None}])
        text = self.render(job)
        self.assertIn("Downloading: waiting for the server", text)
        self.assertNotIn("0 Bytes", text)

    def test_a_failed_file_says_why(self):
        job = self.start([{"stage": "loaded", "url": "a", "rate_count": 0, "error": "HTTP 404 Not Found"}])
        self.assertIn("✗ HTTP 404 Not Found", self.render(job))
        self.assertEqual(job["files_done"], 1)

    def test_making_it_active_is_its_own_step(self):
        job = self.start([{"stage": "loaded", "url": u, "rate_count": 1, "error": None} for u in "abc"]
                         + [{"stage": "activating"}])
        self.assertEqual(job["stage"], "activating")
        text = self.render(job)
        self.assertIn("3 of 3 files done", text)
        self.assertRegex(text, r"Make it the active index.*Making it active")


class LoadErrorsVisibleTest(LoadTestCase):
    """#33, #37.6: a file that failed to load is shown with why, in health and on the Load Data page."""

    def setUp(self):
        super().setUp()
        # fidelis-es's "local copy" is the first half of the real fidelis-ex file, so loading it fails and is recorded.
        data = EX_FILE.read_bytes()
        local_path_for(ES_URL, self.raw_dir).write_bytes(data[:len(data) // 2])
        loaded = self.load({"index": INDEX, "url": [EX_URL, ES_URL], f"source:{ES_URL}": "local"})
        with sqlite3.connect(self.db_path) as conn:
            ((self.error,),) = conn.execute("SELECT error FROM source_files WHERE url = ?", (ES_URL,)).fetchall()
        self.assertTrue(self.error)
        self.assertIn(html.escape(self.error, quote=True), loaded.text)

    def test_health_lists_the_failed_file(self):
        body = self.client.get("/api/health").json()
        self.assertTrue(body["db_present"])
        self.assertEqual(body["rate_count"], 1828)
        entry = [f for f in body["source_files"] if f["url"] == ES_URL][0]
        self.assertEqual(entry["error"], self.error)
        self.assertIsNone(entry["warnings"])

    def test_the_page_shows_the_failed_file_and_why(self):
        cards = re.findall(r'<article class="file.*?</article>', self.client.get("/").text, re.S)
        es = [c for c in cards if "fidelis-es" in c][0]
        self.assertIn("✗", es)
        self.assertIn(html.escape(self.error, quote=True), es)
        self.assertIn("Contains 1 of the index’s 2 in-network files", self.client.get("/").text)


class IndexDatabasesTest(LoadTestCase):
    """#37.5/#37.2/#37.6: one database per index in parsed/, one of them active; "Loaded indexes" lists them, each
    saying what it contains, with Show this one and Remove."""

    def index_copy(self, name: str) -> str:
        """The real index under another name (another index file, so another database)."""
        self.index_dir.mkdir(exist_ok=True)
        path = self.index_dir / name
        shutil.copy(INDEX, path)
        return str(path)

    def active(self) -> str:
        return (self.parsed / "active_index").read_text().strip()

    def entries(self, text: str | None = None) -> dict[str, str]:
        """Each loaded index's entry on the page, by database name."""
        text = self.client.get("/").text if text is None else text
        return dict(re.findall(r'<section class="index-db[^"]*" data-db="([^"]+)"[^>]*>(.*?)</section>', text, re.S))

    def post(self, path: str, data: dict, **kwargs):
        return self.client.post(path, data=data, follow_redirects=False, **kwargs)

    def test_loading_builds_the_index_database_and_makes_it_active(self):
        done = self.load({"index": INDEX, "url": [EX_URL]})
        self.assertIn("2026-09-28_fidelis_index.db", done.text)
        self.assertIn("now the active index", done.text)
        self.assertEqual(self.active(), "2026-09-28_fidelis_index.db")
        self.assertEqual([p.name for p in self.parsed.glob("*.db")], ["2026-09-28_fidelis_index.db"])

    def test_the_entry_says_what_the_database_contains(self):
        self.load({"index": INDEX, "url": [EX_URL]})
        entry = self.entries()["2026-09-28_fidelis_index.db"]
        self.assertIn(INDEX_NAME, entry)
        self.assertIn("Centene Management Company LLC", entry)
        self.assertIn('class="tag ok">valid', entry)
        self.assertIn("2026-09-28", entry)
        self.assertIn("Active", entry)
        self.assertIn("Contains 1 of the index’s 2 in-network files", entry)
        cards = re.findall(r'<article class="file.*?</article>', entry, re.S)
        self.assertEqual(len(cards), 2)
        ex, es = cards
        self.assertIn("✓", ex)
        self.assertIn("fidelis-ex", ex)
        self.assertIn("88 plans", ex)
        self.assertIn("1,828 rates", ex)
        self.assertRegex(ex, r"loaded \d{4}-\d\d-\d\d \d\d:\d\d")
        self.assertIn("✗", es)
        self.assertIn("not in this database", es)

    def test_each_loaded_file_has_the_step_2_preview(self):
        self.load({"index": INDEX, "url": [EX_URL]})
        entry = self.entries()["2026-09-28_fidelis_index.db"]
        ex = re.findall(r'<article class="file.*?</article>', entry, re.S)[0]
        self.assertEqual(ex.count('<details class="preview">'), 1)
        self.assertIn('class="file-preview"', ex)
        self.assertIn("25303NY011", ex)  # its plans, from the index recorded in the database
        self.assertIn("Third Party Administrator", ex)  # its header, from the local copy
        self.assertIn('<span class="tag ok">loaded</span> 1,828 rates', ex)  # what's loaded from it

    def test_each_index_gets_its_own_database_and_the_last_loaded_is_active(self):
        self.load({"index": INDEX, "url": [EX_URL]})
        august = self.index_copy("2026-08-31_fidelis_index.json")
        self.load({"index": august, "url": [EX_URL]})
        self.assertEqual(sorted(p.name for p in self.parsed.glob("*.db")),
                         ["2026-08-31_fidelis_index.db", "2026-09-28_fidelis_index.db"])
        self.assertEqual(self.active(), "2026-08-31_fidelis_index.db")
        entries = self.entries()
        self.assertEqual(list(entries), ["2026-08-31_fidelis_index.db", "2026-09-28_fidelis_index.db"])  # active first
        self.assertIn("Active", entries["2026-08-31_fidelis_index.db"])
        self.assertNotIn("Show this one", entries["2026-08-31_fidelis_index.db"])
        self.assertIn("Show this one", entries["2026-09-28_fidelis_index.db"])

    def test_every_page_reads_the_active_database(self):
        self.load({"index": INDEX, "url": [EX_URL]})
        with sqlite3.connect(self.parsed / "other.db") as conn:  # 3 rates, no index recorded
            build_db(conn)
        self.post("/indexes/show", {"name": "2026-09-28_fidelis_index.db"})
        health = self.client.get("/api/health").json()
        self.assertEqual((health["active_index"], health["rate_count"]), ("2026-09-28_fidelis_index.db", 1828))
        sept_codes = self.client.get("/api/codes").json()["codes"]
        response = self.post("/indexes/show", {"name": "other.db"})
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/#indexes"))
        self.assertEqual(self.active(), "other.db")
        health = self.client.get("/api/health").json()
        self.assertEqual((health["active_index"], health["rate_count"]), ("other.db", 3))
        self.assertEqual(len(self.client.get("/api/codes").json()["codes"]), 2)
        self.assertNotEqual(len(sept_codes), 2)
        self.assertEqual(self.client.get("/api/search", params={"billing_code": "10006"}).json()["total"], 2)
        self.assertIn("All 1 provider (EIN)", self.client.get("/providers").text)  # build_db has one EIN
        # A database without a recorded index still lists its files.
        entry = self.entries()["other.db"]
        self.assertIn("fidelis-ex_in-network.json", entry)
        self.assertIn("index wasn’t recorded", entry)

    def test_show_an_unknown_database_changes_nothing(self):
        self.load({"index": INDEX, "url": [EX_URL]})
        for name in ("nope.db", "../2026-09-28_fidelis_index.db", ""):
            with self.subTest(name=name):
                response = self.post("/indexes/show", {"name": name})
                self.assertEqual(response.status_code, 404)
                self.assertIn("no index database called", response.text)
        self.assertEqual(self.active(), "2026-09-28_fidelis_index.db")

    def test_remove_asks_first(self):
        self.load({"index": INDEX, "url": [EX_URL]})
        entry = self.entries()["2026-09-28_fidelis_index.db"]
        self.assertIn('href="/?remove=2026-09-28_fidelis_index.db#indexes"', entry)
        confirm = self.client.get("/", params={"remove": "2026-09-28_fidelis_index.db"}).text
        entry = self.entries(confirm)["2026-09-28_fidelis_index.db"]
        self.assertIn('action="/indexes/remove"', entry)
        self.assertIn('name="confirm" value="yes"', entry)
        self.assertIn("backed up", entry)
        # A POST without the confirmation only asks.
        asked = self.post("/indexes/remove", {"name": "2026-09-28_fidelis_index.db"})
        self.assertEqual(asked.status_code, 200)
        self.assertIn('name="confirm" value="yes"', asked.text)
        self.assertTrue(self.db_path.exists())

    def test_remove_backs_up_then_deletes(self):
        self.load({"index": INDEX, "url": [EX_URL]})
        august = self.index_copy("2026-08-31_fidelis_index.json")
        self.load({"index": august, "url": [EX_URL]})
        response = self.post("/indexes/remove", {"name": "2026-08-31_fidelis_index.db", "confirm": "yes"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Removed 2026-08-31_fidelis_index.db", response.text)
        (backup,) = self.backup_dir.glob("2026-08-31_fidelis_index-*.db")
        self.assertIn(str(backup), response.text)
        with sqlite3.connect(backup) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM rates").fetchone()[0], 1828)
        self.assertFalse((self.parsed / "2026-08-31_fidelis_index.db").exists())
        self.assertEqual(list(self.entries()), ["2026-09-28_fidelis_index.db"])  # now the active one
        self.assertEqual(self.client.get("/api/health").json()["active_index"], "2026-09-28_fidelis_index.db")

    def test_removing_the_only_database_leaves_the_empty_state(self):
        self.load({"index": INDEX, "url": [EX_URL]})
        self.post("/indexes/remove", {"name": "2026-09-28_fidelis_index.db", "confirm": "yes"})
        self.assertIn("Nothing is loaded yet", self.client.get("/").text)
        self.assertFalse(self.client.get("/api/health").json()["db_present"])
        self.assertIn("No data loaded yet", self.client.get("/codes").text)

    def test_show_and_remove_from_another_site_are_refused(self):
        self.load({"index": INDEX, "url": [EX_URL]})
        with sqlite3.connect(self.parsed / "other.db") as conn:
            build_db(conn)
        self.post("/indexes/show", {"name": "2026-09-28_fidelis_index.db"})
        for path, data in (("/indexes/show", {"name": "other.db"}),
                           ("/indexes/remove", {"name": "other.db", "confirm": "yes"})):
            with self.subTest(path=path):
                response = self.post(path, data, headers={"Origin": "http://evil.example"})
                self.assertEqual(response.status_code, 403)
                self.assertIn("another site", response.text)
        self.assertEqual(self.active(), "2026-09-28_fidelis_index.db")
        self.assertTrue((self.parsed / "other.db").exists())

    def test_a_load_where_every_file_fails_builds_nothing(self):
        done = self.load({"index": INDEX, "url": [ES_URL]})  # no local copy, downloads disabled
        self.assertIn("downloads disabled", done.text)
        self.assertIn("No file loaded", done.text)
        self.assertFalse(self.db_path.exists())
        self.assertIn("Nothing is loaded yet", self.client.get("/").text)


class LoadPageLayoutTest(LoadTestCase):
    """#41.1-#41.3: tight index rows, loaded indexes and index files linked both ways, the active index first."""

    index_copy, entries = IndexDatabasesTest.index_copy, IndexDatabasesTest.entries

    def rows(self, text: str) -> dict[str, str]:
        """Each step-1 row (the row link plus anything beside it), by index file name."""
        return {re.search(r'<span class="name">([^<]+)</span>', row).group(1): row
                for row in re.findall(r'<div class="index-item" role="listitem">(.*?)</div>', text, re.S)}

    def href(self, path: str) -> str:
        return f'href="/?index={quote(str(path), safe="")}"'

    # 41.1

    def test_an_index_row_is_one_tight_line(self):
        sept = self.index_copy(INDEX_NAME)
        (self.index_dir / "2025-01-01_broken_index.json").write_text("{broken")
        text = self.client.get("/").text
        row = self.rows(text)[INDEX_NAME]
        self.assertRegex(row, r'<span class="name">2026-09-28_fidelis_index.json</span>\s*<span class="ok">✓ valid</span>'
                              r'\s*<span class="facts">2 in-network files · 94 plans · saved \d{4}-\d\d-\d\d \d\d:\d\d UTC</span>')
        self.assertIn(self.href(sept), row)
        broken = self.rows(text)["2025-01-01_broken_index.json"]
        self.assertRegex(broken, r'<span class="bad">✗ [^<]*not valid JSON')
        self.assertNotIn("in-network file", broken)
        self.assertNotIn("grid-template-columns: minmax(0, 1fr) 5.5rem", text)  # no columns across the width

    # 41.2

    def test_a_loaded_index_is_marked_in_step_1_and_links_to_its_entry(self):
        sept = self.index_copy(INDEX_NAME)
        august = self.index_copy("2026-08-31_fidelis_index.json")
        self.index_copy("2026-07-31_fidelis_index.json")  # never loaded
        self.load({"index": sept, "url": [EX_URL]})
        rows = self.rows(self.client.get("/").text)
        self.assertIn('<a class="tag loaded" href="#db-2026-09-28_fidelis_index.db">Loaded · active</a>',
                      rows[INDEX_NAME])
        self.assertNotIn("Loaded", rows["2026-08-31_fidelis_index.json"])
        self.load({"index": august, "url": [EX_URL]})
        text = self.client.get("/").text
        rows = self.rows(text)
        self.assertIn('<a class="tag loaded" href="#db-2026-09-28_fidelis_index.db">Loaded</a>', rows[INDEX_NAME])
        self.assertIn('<a class="tag loaded" href="#db-2026-08-31_fidelis_index.db">Loaded · active</a>',
                      rows["2026-08-31_fidelis_index.json"])
        self.assertNotIn("Loaded", rows["2026-07-31_fidelis_index.json"])
        for name in ("2026-09-28_fidelis_index.db", "2026-08-31_fidelis_index.db"):
            self.assertIn(f'<section class="index-db', text)
            self.assertRegex(text, rf'<section class="index-db[^"]*" data-db="{re.escape(name)}" id="db-{re.escape(name)}">')

    def test_each_entry_links_its_index_file_back_to_step_1(self):
        sept = self.index_copy(INDEX_NAME)
        self.load({"index": sept, "url": [EX_URL]})
        entry = self.entries()["2026-09-28_fidelis_index.db"]
        self.assertIn(f'<a {self.href(sept)} title="Open it in step 1">{INDEX_NAME}</a>', entry)
        self.assertNotIn("not in your list", entry)

    def test_an_entry_whose_index_file_is_not_in_the_list_says_so(self):
        self.load({"index": INDEX, "url": [EX_URL]})  # opened from the test data folder, not the list
        entry = self.entries()["2026-09-28_fidelis_index.db"]
        self.assertIn(INDEX_NAME, entry)
        self.assertIn("index file not in your list", entry)
        self.assertNotIn('href="/?index=', entry)

    def test_opening_a_loaded_index_says_so_at_step_2(self):
        sept = self.index_copy(INDEX_NAME)
        august = self.index_copy("2026-08-31_fidelis_index.json")
        self.load({"index": sept, "url": [EX_URL]})
        text = self.client.get("/", params={"index": sept}).text
        self.assertIn("This index is loaded (1 of 2 in-network files). Loading it again rebuilds its database; "
                      "the current one is backed up first.", text)
        self.assertNotIn("This index is loaded", self.client.get("/", params={"index": august}).text)

    # 41.3

    def test_the_page_leads_with_the_active_index(self):
        sept = self.index_copy(INDEX_NAME)
        august = self.index_copy("2026-08-31_fidelis_index.json")
        self.load({"index": sept, "url": [EX_URL]})
        self.load({"index": august, "url": [EX_URL]})
        text = self.client.get("/").text
        self.assertNotIn("Each index you load gets a database of its own", text)
        self.assertIn("The two price pages (Prices By Procedure, Prices By Provider) show the active index", text)
        heading = text.index("Active Index: Centene Management Company LLC, 2026-09-28")
        others, load = text.index("Other Loaded Indexes"), text.index(">Load An Index<")
        self.assertLess(heading, others)
        self.assertLess(others, load)
        self.assertLess(text.index('data-db="2026-08-31_fidelis_index.db"'), others)  # the active one, first
        self.assertLess(others, text.index('data-db="2026-09-28_fidelis_index.db"'))
        self.assertLess(text.index('data-db="2026-09-28_fidelis_index.db"'), load)
        self.assertLess(load, text.index("1. Choose An Index File"))

    def test_with_one_index_there_are_no_other_loaded_indexes(self):
        self.load({"index": INDEX, "url": [EX_URL]})
        text = self.client.get("/").text
        self.assertIn("Active Index: Centene Management Company LLC, 2026-09-28", text)
        self.assertNotIn("Other Loaded Indexes", text)

    def test_nothing_loaded_says_so_where_the_active_index_would_be(self):
        text = self.client.get("/").text
        self.assertNotIn("Active Index:", text)
        self.assertIn("Nothing is loaded yet", text)
        self.assertLess(text.index("Nothing is loaded yet"), text.index(">Load An Index<"))


class DownloadPlanTest(LoadTestCase):
    """#41.5: before Load, step 2 says per file whether it is on disk or will be downloaded (and how much, when a
    local copy tells), and a total line that the page keeps up to date as files and sources are ticked."""

    def fetch_lines(self, text: str) -> dict[str, str]:
        """Each file's "what Load fetches" line, by URL."""
        return {html.unescape(url): body for url, body in
                re.findall(r'<p class="fetch" data-url="([^"]+)"[^>]*>(.*?)</p>', text, re.S)}

    def spans(self, line: str) -> dict[str, tuple[bool, str]]:
        """source -> (shown, text) for one file's line."""
        return {source: (not hidden, re.sub(r"\s+", " ", body).strip())
                for source, hidden, body in re.findall(r'<span data-source="(\w+)"( hidden)?>(.*?)</span>', line, re.S)}

    def test_each_file_says_what_load_will_fetch_for_each_source(self):
        text = self.open_index(INDEX).text
        lines = self.fetch_lines(text)
        size = EX_FILE.stat().st_size
        shown = filesizeformat(size)
        self.assertIn(f'data-size="{size}"', re.search(r'<p class="fetch" data-url="[^"]*fidelis-ex[^>]*>', text).group(0))
        self.assertEqual(self.spans(lines[EX_URL]), {
            "local": (True, f"on disk ({shown})"),
            "download": (False, f"will download {shown} from www.centene.com"),
            "web": (False, f"will download {shown} from www.centene.com, not saved"),
        })
        self.assertEqual(self.spans(lines[ES_URL]), {  # not on disk: no size to go by
            "download": (True, "will download from www.centene.com (size unknown until download)"),
            "web": (False, "will download from www.centene.com, not saved (size unknown until download)"),
        })

    def test_a_total_line_kept_up_to_date_as_files_are_ticked(self):
        text = self.open_index(INDEX).text
        self.assertIn('<p class="download-total" data-download-total aria-live="polite">Tick the files to load.</p>', text)
        self.assertIn("Load will download", text)  # the script's wording
        self.assertIn("Nothing to download — all on disk", text)
        self.assertIn("form.addEventListener(\"change\"", text)
        self.assertIn("Opening an index fetched only the index file; nothing else downloads until Load.", text)

    def test_says_when_downloads_are_off(self):
        text = self.open_index(INDEX).text  # this app has downloads disabled
        self.assertIn('data-downloads-off="1"', text)
        self.assertIn("Downloads are turned off in this app", text)

    def test_the_old_on_disk_tags_are_gone(self):
        text = self.open_index(INDEX).text
        self.assertNotIn("already downloaded", text)
        self.assertNotIn("needs downloading", text)


if __name__ == "__main__":
    unittest.main()


GB = 1000 ** 3


def disk(free: int):
    """A stand-in for shutil.disk_usage with this much free."""
    return mock.patch("ui.load_page.shutil.disk_usage", return_value=shutil._ntuple_diskusage(4000 * GB, 0, free))


class BigLoadTest(LoadTestCase):
    """#42.3/#42.5 (light): before Load, a heads-up for big selections and the free disk space; Load refuses to start
    when less than 2 GB is free, and says why."""

    def form(self):
        return {"index": INDEX, "url": [EX_URL], f"source:{EX_URL}": "local"}

    def test_refuses_to_start_when_free_space_is_low(self):
        with disk(int(1.5 * GB)):
            response = self.client.post("/load", data=self.form(), follow_redirects=False)
        self.assertEqual(response.status_code, 507)
        self.assertIn("Only 1.5 GB is free on the disk that holds the index databases; a load needs at least 2.0 GB "
                      "free (a database can grow to several GB), so it didn’t start.", response.text)
        self.assertFalse(self.client.get("/api/load/progress").json()["running"])
        self.assertFalse(self.db_path.exists())
        self.assertIn('id="load-form"', response.text)  # the choices stay, to try again

    def test_enough_free_space_loads(self):
        with disk(3 * GB):
            loaded = self.load(self.form())
        self.assertIn('data-job="done"', loaded.text)
        self.assertEqual(self.rate_count(), 1828)

    def test_step_2_says_how_much_is_free(self):
        with disk(16 * GB):
            text = self.open_index(INDEX).text
        self.assertIn("16.0 GB free on the disk that holds the index databases.", text)
        self.assertNotIn("Load won’t start", text)
        with disk(1 * GB):
            text = self.open_index(INDEX).text
        self.assertIn("Only 1.0 GB free on the disk that holds the index databases: Load won’t start until at "
                      "least 2.0 GB is free.", text)

    def test_a_heads_up_as_big_selections_are_ticked(self):
        text = self.open_index(INDEX).text
        self.assertIn('data-big-files="5"', text)
        self.assertIn(f'data-big-bytes="{200 * 1000 ** 2}"', text)
        self.assertIn("; this may take several minutes and produce a large database.", text)  # the script's heads-up
        self.assertIn("data-heads-up", text)
        self.assertNotIn("This index lists", text)  # 2 files: no heads-up for the index itself

    def test_an_index_with_many_files_says_so_up_front(self):
        index = copy.deepcopy(REAL_INDEX)
        structure = index["reporting_structure"][0]
        structure["in_network_files"] += [dict(structure["in_network_files"][0],
                                               location=EX_URL.replace("fidelis-ex", f"extra-{n}")) for n in range(5)]
        path = Path(self.tmp.name) / "2026-10-01_big_index.json"
        path.write_text(json.dumps(index))
        text = self.open_index(str(path)).text
        self.assertIn("This index lists 7 in-network files. Loading many of them may take several minutes and "
                      "produce a large database (each price is stored once per provider, so it can be many times "
                      "the size of the files).", text)
