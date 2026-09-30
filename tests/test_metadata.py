import hashlib
import tempfile
import unittest
from pathlib import Path

from bencoder import bencode

from magnet_dht.metadata import validate_torrent


class MetadataTests(unittest.TestCase):
    def _write_torrent(self, metadata):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "sample.torrent"
        path.write_bytes(bencode(metadata))
        self.addCleanup(directory.cleanup)
        return path

    def test_validates_info_hash_and_paths(self):
        info = {
            b"length": 123,
            b"name": b"example.txt",
            b"piece length": 16384,
            b"pieces": b"x" * 20,
        }
        path = self._write_torrent({b"announce": b"udp://tracker", b"info": info})
        expected = hashlib.sha1(bencode(info)).hexdigest()

        _, info_hash, paths = validate_torrent(path, expected)

        self.assertEqual(expected, info_hash)
        self.assertEqual(["example.txt"], paths)

    def test_rejects_info_hash_mismatch(self):
        info = {b"length": 1, b"name": b"example.txt"}
        path = self._write_torrent({b"info": info})

        with self.assertRaisesRegex(ValueError, "mismatch"):
            validate_torrent(path, "0" * 40)

    def test_rejects_parent_path(self):
        info = {
            b"name": b"root",
            b"files": [{b"length": 1, b"path": [b"..", b"secret"]}],
        }
        path = self._write_torrent({b"info": info})

        with self.assertRaisesRegex(ValueError, "unsafe"):
            validate_torrent(path)

if __name__ == "__main__":
    unittest.main()
