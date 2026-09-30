import base64
import unittest

from magnet_dht.metadata import extract_info_hash


class InfoHashTests(unittest.TestCase):
    def test_extracts_hex_hash(self):
        value = "0123456789abcdef0123456789abcdef01234567"
        magnet = "magnet:?dn=test&xt=urn:btih:{}".format(value.upper())
        self.assertEqual(value, extract_info_hash(magnet))

    def test_extracts_base32_hash(self):
        raw = bytes(range(20))
        value = base64.b32encode(raw).decode()
        self.assertEqual(
            raw.hex(), extract_info_hash("magnet:?xt=urn:btih:" + value)
        )

    def test_rejects_invalid_hash(self):
        self.assertIsNone(extract_info_hash("magnet:?xt=urn:btih:not-a-hash"))
        self.assertIsNone(extract_info_hash("https://example.invalid"))


if __name__ == "__main__":
    unittest.main()
