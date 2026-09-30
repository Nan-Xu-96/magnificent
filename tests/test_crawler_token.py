import os
import time
import unittest

from magnet_dht.crawler import DHTServer


class CrawlerTokenTests(unittest.TestCase):
    def make_server(self):
        server = object.__new__(DHTServer)
        server.nid = os.urandom(20)
        server.token_secret = os.urandom(20)
        server.previous_token_secret = None
        server.token_secret_changed = time.monotonic()
        return server

    def test_token_is_bound_to_source_ip(self):
        server = self.make_server()
        token = server.make_token("192.0.2.10")
        self.assertTrue(server.validate_token(token, "192.0.2.10"))
        self.assertFalse(server.validate_token(token, "192.0.2.11"))
        self.assertFalse(server.validate_token(b"invalid", "192.0.2.10"))

    def test_only_valid_token_announce_is_saved(self):
        server = self.make_server()
        saved = []
        sent = []
        server.save_magnet = lambda info_hash, source, announced_peer=None: saved.append(
            (info_hash, source, announced_peer)
        )
        server.send_krpc = lambda message, address: sent.append((message, address))
        info_hash = os.urandom(20)
        address = ("192.0.2.10", 51413)

        server.on_get_peers_request(
            {b"t": b"aa", b"a": {b"info_hash": info_hash}}, address
        )
        token = sent[-1][0]["r"]["token"]
        server.on_announce_peer_request(
            {
                b"t": b"bb",
                b"a": {b"info_hash": info_hash, b"token": token, b"port": 51413},
            },
            address,
        )
        server.on_announce_peer_request(
            {
                b"t": b"cc",
                b"a": {b"info_hash": info_hash, b"token": b"wrong"},
            },
            address,
        )

        self.assertEqual(
            [
                (info_hash, "get_peers", None),
                (info_hash, "announce_peer", ("192.0.2.10", 51413)),
            ],
            saved,
        )

    def test_announce_uses_source_port_when_implied_port_is_set(self):
        server = self.make_server()
        saved = []
        sent = []
        server.save_magnet = lambda info_hash, source, announced_peer=None: saved.append(
            (info_hash, source, announced_peer)
        )
        server.send_krpc = lambda message, address: sent.append((message, address))
        info_hash = os.urandom(20)
        address = ("192.0.2.20", 49001)
        token = server.make_token(address[0])

        server.on_announce_peer_request(
            {
                b"t": b"aa",
                b"a": {
                    b"info_hash": info_hash,
                    b"token": token,
                    b"port": 6881,
                    b"implied_port": 1,
                },
            },
            address,
        )

        self.assertEqual(
            [(info_hash, "announce_peer", ("192.0.2.20", 49001))], saved
        )

    def test_invalid_announced_port_is_rejected(self):
        server = self.make_server()
        saved = []
        sent = []
        server.save_magnet = lambda info_hash, source, announced_peer=None: saved.append(
            (info_hash, source, announced_peer)
        )
        server.send_krpc = lambda message, address: sent.append((message, address))
        info_hash = os.urandom(20)
        address = ("192.0.2.30", 49002)

        server.on_announce_peer_request(
            {
                b"t": b"aa",
                b"a": {
                    b"info_hash": info_hash,
                    b"token": server.make_token(address[0]),
                    b"port": 0,
                },
            },
            address,
        )

        self.assertEqual([], saved)
        self.assertEqual("e", sent[-1][0]["y"])


if __name__ == "__main__":
    unittest.main()
