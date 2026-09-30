import csv
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bencoder import bencode

from magnet_dht.acquisition import record_acquisition
from magnet_dht.parse_torrent import export_torrents_csv


class CsvExportTests(unittest.TestCase):
    def test_exports_one_safe_row_per_unique_info_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            info = {
                b"name": b"=sample",
                b"files": [
                    {b"length": 1024, b"path": [b"one.txt"]},
                    {b"length": 2048, b"path": [b"=formula.txt"]},
                ],
            }
            info_hash = hashlib.sha1(bencode(info)).hexdigest()
            torrent_bytes = bencode(
                {
                    b"creation date": 1,
                    b"created by": b"test-client",
                    b"info": info,
                }
            )
            torrent_path = root / (info_hash + ".torrent")
            torrent_path.write_bytes(torrent_bytes)
            (root / "duplicate.torrent").write_bytes(torrent_bytes)
            record_acquisition(
                info_hash,
                torrent_path,
                method="announce_heat",
                announce_heat=4.25,
            )
            output = root / "result.csv"

            with patch.dict(os.environ, {"MAGNET_DHT_TORRENT_DIR": directory}):
                export_torrents_csv(output)

            with output.open(encoding="utf-8-sig", newline="") as csv_file:
                rows = list(csv.DictReader(csv_file))

        self.assertEqual(1, len(rows))
        self.assertEqual(info_hash, rows[0]["info_hash"])
        self.assertEqual("magnet:?xt=urn:btih:" + info_hash, rows[0]["magnet_uri"])
        self.assertTrue(rows[0]["torrent_saved_time_utc"])
        self.assertEqual("announce_heat", rows[0]["acquisition_method"])
        self.assertTrue(rows[0]["acquisition_time_utc"])
        self.assertEqual("4.25", rows[0]["announce_heat"])
        self.assertEqual("'=sample", rows[0]["resource_name"])
        self.assertEqual("3072", rows[0]["total_size_bytes"])
        self.assertEqual("2", rows[0]["file_count"])
        self.assertEqual([".txt"], json.loads(rows[0]["file_extensions"]))
        self.assertEqual(
            ["=sample/one.txt", "=sample/=formula.txt"],
            json.loads(rows[0]["file_paths_json"]),
        )


if __name__ == "__main__":
    unittest.main()
