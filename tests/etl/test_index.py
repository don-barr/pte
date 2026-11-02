import json
import unittest
from pathlib import Path

from etl.index import in_network_urls

TEST_DATA = Path(__file__).parent / "test_data"
BASE = "https://www.centene.com/content/dam/centene/Centene%20Corporate/json/DOCUMENT/"


class InNetworkUrlsTest(unittest.TestCase):
    def test_returns_every_in_network_file_url_in_the_index(self):
        index = json.loads((TEST_DATA / "2026-09-28_fidelis_index.json").read_text())

        self.assertEqual(
            in_network_urls(index),
            [
                BASE + "2026-09-28_centene-management-company-llc_fidelis-ex_in-network.json",
                BASE + "2026-09-28_centene-management-company-llc_fidelis-es_in-network.json",
            ],
        )


if __name__ == "__main__":
    unittest.main()
