import copy
import functools
import json
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import gzip

from etl.fetch import FetchResult, download_url, fetch_all, fetch_bytes
from etl.schema import local_path_for

TEST_DATA = Path(__file__).parent / "test_data"
SERVED_NAME = "2026-09-28_fidelis_index.json"
REAL_INDEX = json.loads((TEST_DATA / SERVED_NAME).read_text())


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


def index_with_locations(*urls):
    """The real index, with its in-network file locations swapped for local URLs."""
    index = copy.deepcopy(REAL_INDEX)
    structure = index["reporting_structure"][0]
    template = structure["in_network_files"][0]
    structure["in_network_files"] = [dict(template, location=url) for url in urls]
    index["reporting_structure"] = [structure]
    return index


class FetchAllTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handler = functools.partial(QuietHandler, directory=str(TEST_DATA))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
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
        self.raw_dir = Path(self._tmp.name) / "in_network_files"
        self.good_url = self.base + SERVED_NAME
        self.missing_url = self.base + "no-such-file_in-network.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_already_present_file_is_not_downloaded(self):
        self.raw_dir.mkdir()
        existing = self.raw_dir / SERVED_NAME
        existing.write_text("already here")

        results = fetch_all(index_with_locations(self.good_url), self.raw_dir)

        self.assertEqual(
            results,
            [FetchResult(url=self.good_url, path=existing, downloaded=False, error=None)],
        )
        self.assertEqual(existing.read_text(), "already here")

    def test_missing_file_is_downloaded_under_the_urls_filename_plus_a_url_hash(self):
        results = fetch_all(index_with_locations(self.good_url), self.raw_dir)

        expected = local_path_for(self.good_url, self.raw_dir)
        self.assertRegex(expected.name, r"^2026-09-28_fidelis_index\.[0-9a-f]{12}\.json$")
        self.assertEqual(
            results,
            [FetchResult(url=self.good_url, path=expected, downloaded=True, error=None)],
        )
        self.assertEqual(expected.read_bytes(), (TEST_DATA / SERVED_NAME).read_bytes())
        self.assertEqual(list(self.raw_dir.iterdir()), [expected])

    def test_http_error_is_reported_and_leaves_no_file(self):
        results = fetch_all(index_with_locations(self.missing_url), self.raw_dir)

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.url, self.missing_url)
        self.assertIsNone(result.path)
        self.assertFalse(result.downloaded)
        self.assertIn("404", result.error)
        self.assertEqual(list(self.raw_dir.iterdir()), [])

    def test_missing_file_without_download_is_reported(self):
        results = fetch_all(index_with_locations(self.good_url), self.raw_dir, download=False)

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertIsNone(result.path)
        self.assertFalse(result.downloaded)
        self.assertIn("not downloaded", result.error)
        self.assertFalse((self.raw_dir / SERVED_NAME).exists())

    def test_one_failing_url_does_not_stop_the_next(self):
        results = fetch_all(
            index_with_locations(self.missing_url, self.good_url), self.raw_dir
        )

        self.assertEqual([r.url for r in results], [self.missing_url, self.good_url])
        self.assertIsNotNone(results[0].error)
        self.assertEqual(
            results[1],
            FetchResult(
                url=self.good_url, path=local_path_for(self.good_url, self.raw_dir),
                downloaded=True, error=None,
            ),
        )


    def test_same_basename_under_different_urls_downloads_to_different_files(self):
        # Same file name: this server, the same URL with a ?sig= query, and another host.
        handler = functools.partial(MisbehavingHandler, directory=str(TEST_DATA))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            other = f"http://127.0.0.1:{server.server_address[1]}/gzip/" + SERVED_NAME
            urls = [self.good_url, self.good_url + "?sig=1", other]
            results = fetch_all(index_with_locations(*urls), self.raw_dir)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual([r.error for r in results], [None, None, None])
        self.assertTrue(all(r.downloaded for r in results))
        self.assertEqual(len({r.path for r in results}), 3)
        self.assertEqual(sorted(self.raw_dir.iterdir()), sorted(r.path for r in results))

class MisbehavingHandler(QuietHandler):
    """Serves real fixture files, but under /short/ declares a Content-Length larger than
    the bytes it then sends and closes the connection; under /gzip/ it sends the file
    gzip-encoded with a correct Content-Length (the raw, encoded size)."""

    EXTRA = 1000

    def do_GET(self):
        kind, _, name = self.path.lstrip("/").partition("/")
        if kind not in ("short", "gzip"):
            return super().do_GET()
        body = (TEST_DATA / name).read_bytes()
        if kind == "gzip":
            body = gzip.compress(body)
            declared = len(body)
        else:
            declared = len(body) + self.EXTRA
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(declared))
        if kind == "gzip":
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True


class ContentLengthTest(unittest.TestCase):
    """A server that sends fewer bytes than its Content-Length is a failed download."""

    @classmethod
    def setUpClass(cls):
        handler = functools.partial(MisbehavingHandler, directory=str(TEST_DATA))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/"
        cls.size = (TEST_DATA / SERVED_NAME).stat().st_size

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.raw_dir = Path(self._tmp.name) / "in_network_files"

    def tearDown(self):
        self._tmp.cleanup()

    def assert_failed_and_nothing_kept(self, result, url):
        self.assertEqual(result.url, url)
        self.assertIsNone(result.path)
        self.assertFalse(result.downloaded)
        self.assertEqual(list(self.raw_dir.iterdir()), [])  # no .part, nothing renamed

    def test_truncated_download_is_an_error_and_leaves_no_file(self):
        url = self.base + "short/" + SERVED_NAME
        (result,) = fetch_all(index_with_locations(url), self.raw_dir)
        self.assert_failed_and_nothing_kept(result, url)
        expected = self.size + MisbehavingHandler.EXTRA
        self.assertEqual(
            result.error, f"download incomplete: got {self.size:,} of {expected:,} bytes"
        )

    def test_truncated_download_does_not_replace_an_existing_copy(self):
        self.raw_dir.mkdir()
        existing = self.raw_dir / SERVED_NAME
        existing.write_text("previous copy")
        result = download_url(self.base + "short/" + SERVED_NAME, self.raw_dir)
        self.assertIn("download incomplete", result.error)
        self.assertIsNone(result.path)
        self.assertEqual(list(self.raw_dir.iterdir()), [existing])
        self.assertEqual(existing.read_text(), "previous copy")

    def test_gzip_encoded_download_is_complete(self):
        url = self.base + "gzip/" + SERVED_NAME
        (result,) = fetch_all(index_with_locations(url), self.raw_dir)
        self.assertIsNone(result.error)
        self.assertEqual(result.path.read_bytes(), (TEST_DATA / SERVED_NAME).read_bytes())

    def test_fetch_bytes_rejects_a_truncated_response(self):
        content, error = fetch_bytes(self.base + "short/" + SERVED_NAME)
        self.assertIsNone(content)
        expected = self.size + MisbehavingHandler.EXTRA
        self.assertEqual(error, f"download incomplete: got {self.size:,} of {expected:,} bytes")

    def test_fetch_bytes_reads_a_complete_gzip_response(self):
        content, error = fetch_bytes(self.base + "gzip/" + SERVED_NAME)
        self.assertIsNone(error)
        self.assertEqual(content, (TEST_DATA / SERVED_NAME).read_bytes())



class AnyPathHandler(QuietHandler):
    """Answers every GET with 200 and the real index fixture, whatever the path."""

    def do_GET(self):
        body = (TEST_DATA / SERVED_NAME).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def all_files(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file()}


class PathTraversalTest(unittest.TestCase):
    """A URL whose basename decodes to a path (..%2F) must never write outside raw_dir."""

    URLS = (
        "x/..%2F..%2Fapi%2Fmain.py",
        "x/..%2Fvictim.json",
        "x/..%5Cvictim.json",
        "x/%2E%2E",
    )

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
        # root/app.py and root/data/victim.json are what the URLs aim at.
        self.raw_dir = self.root / "data" / "raw"
        self.raw_dir.mkdir(parents=True)
        self.targets = [self.root / "app.py", self.root / "data" / "victim.json"]
        for target in self.targets:
            target.write_text("precious")

    def tearDown(self):
        self._tmp.cleanup()

    def assert_only_inside_raw_dir(self, results):
        for target in self.targets:
            self.assertEqual(target.read_text(), "precious")
        outside = all_files(self.root) - all_files(self.raw_dir)
        self.assertEqual(outside, set(self.targets))
        for result in results:
            self.assertIsNone(result.error)
            self.assertEqual(result.path.resolve().parent, self.raw_dir.resolve())
            self.assertEqual(result.path.read_bytes(), (TEST_DATA / SERVED_NAME).read_bytes())

    def test_download_url_stays_inside_raw_dir(self):
        results = [download_url(self.base + url, self.raw_dir) for url in self.URLS]
        self.assert_only_inside_raw_dir(results)

    def test_fetch_all_stays_inside_raw_dir(self):
        results = fetch_all(index_with_locations(*(self.base + u for u in self.URLS)), self.raw_dir)
        self.assert_only_inside_raw_dir(results)
        self.assertEqual(len(all_files(self.raw_dir)), len(self.URLS))


if __name__ == "__main__":
    unittest.main()
