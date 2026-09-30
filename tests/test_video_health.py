import hashlib
import tempfile
import unittest
from pathlib import Path

from bencoder import bencode

from magnet_dht.video_health import (
    Aria2RpcClient,
    MIB,
    _effective_budget,
    discover_video_tasks,
    sample_video_task,
)


class FakeRpc:
    def __init__(self, task, downloaded):
        self.task = task
        self.downloaded = downloaded
        self.options = None
        self.removed = []
        self.forgotten = []
        self.source = None
        self.changed = []
        self.unpaused = []

    def add_torrent(self, torrent_path, options):
        self.options = options
        self.source = Path(options["dir"]) / self.task.file_path
        self.source.parent.mkdir(parents=True, exist_ok=True)
        self.source.write_bytes(b"verified partial pieces")
        return "sample-gid"

    def tell_status(self, gid):
        files = [{"path": ""} for _ in range(self.task.file_index)]
        files[self.task.file_index - 1] = {"path": str(self.source)}
        return {
            "status": "active",
            "completedLength": str(self.downloaded),
            "numSeeders": "3",
            "connections": "5",
            "files": files,
        }

    def pause(self, gid):
        return gid

    def unpause(self, gid):
        self.unpaused.append(gid)
        return gid

    def change_options(self, gid, options):
        self.options = options
        self.changed.append(gid)
        self.source = Path(options["dir"]) / self.task.file_path
        self.source.parent.mkdir(parents=True, exist_ok=True)
        self.source.write_bytes(b"verified partial pieces")
        return "OK"

    def remove(self, gid):
        self.removed.append(gid)

    def forget(self, gid):
        self.forgotten.append(gid)


class VideoHealthTests(unittest.TestCase):
    def _write_torrent(self, root):
        info = {
            b"name": b"collection",
            b"piece length": 256 * 1024,
            b"pieces": b"x" * 20,
            b"files": [
                {b"length": 100, b"path": [b"readme.txt"]},
                {b"length": 100 * MIB, b"path": [b"episode.mp4"]},
                {b"length": 200 * MIB, b"path": [b"feature.mkv"]},
            ],
        }
        info_hash = hashlib.sha1(bencode(info)).hexdigest()
        path = Path(root) / (info_hash + ".torrent")
        path.write_bytes(bencode({b"info": info}))
        return path, info_hash

    def test_discovers_only_largest_video_with_bounded_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            _, info_hash = self._write_torrent(directory)

            tasks, summary = discover_video_tasks(directory, sample_bytes=64 * MIB)

        self.assertEqual(1, len(tasks))
        self.assertEqual(info_hash, tasks[0].info_hash)
        self.assertEqual(3, tasks[0].file_index)
        self.assertEqual("collection/feature.mkv", tasks[0].file_path)
        self.assertEqual(64 * MIB, tasks[0].sample_budget)
        self.assertLess(tasks[0].sample_budget, tasks[0].file_size)
        self.assertEqual(1, summary["scanned"])

    def test_skips_when_two_pieces_exceed_requested_budget(self):
        self.assertEqual(0, _effective_budget(1024 * MIB, 64 * MIB, 64 * MIB))

    def test_stale_metadata_result_is_forgotten_before_retry(self):
        info_hash = "a" * 40

        class StaleResultRpc(Aria2RpcClient):
            def __init__(self):
                self.add_attempts = 0
                self.forgotten = []

            def call(self, method, params=None):
                if method == "aria2.addTorrent":
                    self.add_attempts += 1
                    if self.add_attempts == 1:
                        raise RuntimeError("duplicate torrent")
                    return "new-gid"
                if method == "aria2.tellStopped":
                    return [{"gid": "old-gid", "infoHash": info_hash, "status": "complete"}]
                if method == "aria2.removeDownloadResult":
                    self.forgotten.append(params[0])
                    return "OK"
                raise AssertionError(method)

        with tempfile.TemporaryDirectory() as directory:
            torrent = Path(directory) / (info_hash + ".torrent")
            torrent.write_bytes(b"metadata")
            rpc = StaleResultRpc()

            gid = rpc.add_torrent(torrent, {})

        self.assertEqual("new-gid", gid)
        self.assertEqual(2, rpc.add_attempts)
        self.assertEqual(["old-gid"], rpc.forgotten)

    def test_extracts_one_frame_then_stops_and_removes_partial_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            torrent_dir = root / "torrents"
            torrent_dir.mkdir()
            self._write_torrent(torrent_dir)
            task = discover_video_tasks(torrent_dir, sample_bytes=32 * MIB)[0][0]
            rpc = FakeRpc(task, downloaded=8 * MIB)
            sample_dir = root / "samples"
            work_dir = root / "work"

            def fake_extract(source, output):
                self.assertTrue(source.is_file())
                Path(output).write_bytes(b"jpg")

            result = sample_video_task(
                task,
                sample_dir,
                work_dir,
                rpc,
                frame_extractor=fake_extract,
                poll_interval=0.05,
                progress_interval=0,
            )

            self.assertEqual("healthy", result["health"])
            self.assertEqual(8 * MIB, result["downloaded_bytes"])
            self.assertLess(result["downloaded_bytes"], task.file_size)
            self.assertTrue((sample_dir / (task.info_hash + ".jpg")).is_file())
            self.assertEqual("3", rpc.options["select-file"])
            self.assertIn("head=", rpc.options["bt-prioritize-piece"])
            self.assertEqual(["sample-gid"], rpc.removed)
            self.assertEqual(["sample-gid"], rpc.forgotten)
            self.assertEqual([], list(work_dir.iterdir()))

    def test_reuses_paused_payload_created_by_metadata_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            torrent_dir = root / "torrents"
            torrent_dir.mkdir()
            self._write_torrent(torrent_dir)
            task = discover_video_tasks(torrent_dir, sample_bytes=32 * MIB)[0][0]
            rpc = FakeRpc(task, downloaded=8 * MIB)

            def fake_extract(source, output):
                Path(output).write_bytes(b"jpg")

            result = sample_video_task(
                task,
                root / "samples",
                root / "work",
                rpc,
                frame_extractor=fake_extract,
                existing_gid="payload-from-metadata",
                progress_interval=0,
            )

        self.assertEqual("healthy", result["health"])
        self.assertEqual(["payload-from-metadata"], rpc.changed)
        self.assertEqual(["payload-from-metadata"], rpc.unpaused)
        self.assertEqual(["payload-from-metadata"], rpc.removed)

    def test_zero_connections_and_zero_bytes_are_reported_as_no_peers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            torrent_dir = root / "torrents"
            torrent_dir.mkdir()
            self._write_torrent(torrent_dir)
            task = discover_video_tasks(torrent_dir, sample_bytes=16 * MIB)[0][0]
            rpc = FakeRpc(task, downloaded=0)

            def terminal_status(gid):
                return {
                    "status": "complete",
                    "completedLength": "0",
                    "totalLength": str(task.file_size),
                    "downloadSpeed": "0",
                    "numSeeders": "0",
                    "connections": "0",
                    "files": [],
                }

            rpc.tell_status = terminal_status
            result = sample_video_task(
                task,
                root / "samples",
                root / "work",
                rpc,
                progress_interval=0,
            )

        self.assertEqual("unavailable", result["health"])
        self.assertEqual("no_peers", result["outcome"])
        self.assertEqual(0, result["downloaded_bytes"])

    def test_rpc_connection_failure_is_not_misreported_as_no_peers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            torrent_dir = root / "torrents"
            torrent_dir.mkdir()
            self._write_torrent(torrent_dir)
            task = discover_video_tasks(torrent_dir, sample_bytes=16 * MIB)[0][0]
            rpc = FakeRpc(task, downloaded=0)

            def refused(torrent_path, options):
                raise ConnectionRefusedError(111, "Connection refused")

            rpc.add_torrent = refused
            result = sample_video_task(
                task,
                root / "samples",
                root / "work",
                rpc,
                progress_interval=0,
            )

        self.assertEqual("unavailable", result["health"])
        self.assertEqual("rpc_error", result["outcome"])
        self.assertIn("RPC", result["reason"])

    def test_budget_exhaustion_is_inconclusive_not_unhealthy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            torrent_dir = root / "torrents"
            torrent_dir.mkdir()
            self._write_torrent(torrent_dir)
            task = discover_video_tasks(torrent_dir, sample_bytes=16 * MIB)[0][0]
            rpc = FakeRpc(task, downloaded=task.sample_budget)

            def failed_extract(source, output):
                raise RuntimeError("not enough container data")

            result = sample_video_task(
                task,
                root / "samples",
                root / "work",
                rpc,
                frame_extractor=failed_extract,
                progress_interval=0,
            )

        self.assertEqual("inconclusive", result["health"])
        self.assertIn("分段预算", result["reason"])
        self.assertEqual(["sample-gid"], rpc.removed)


if __name__ == "__main__":
    unittest.main()
