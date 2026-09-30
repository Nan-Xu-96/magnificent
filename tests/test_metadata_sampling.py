import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from magnet_dht import magnet_to_torrent_aria2c as worker


class MetadataSamplingTests(unittest.TestCase):
    def test_metadata_job_pauses_generated_payload_for_sampling(self):
        with patch.object(worker, "_rpc_call", return_value="metadata-gid") as rpc:
            gid = worker.exec_rpc(
                "magnet:?xt=urn:btih:" + "a" * 40,
                sample_on_metadata=True,
            )

        self.assertEqual("metadata-gid", gid)
        options = rpc.call_args.args[1][1]
        self.assertEqual("false", options["bt-metadata-only"])
        self.assertEqual("true", options["bt-save-metadata"])
        self.assertEqual("true", options["pause-metadata"])

    def test_metadata_failure_distinguishes_no_peers(self):
        stats = {
            "downloaded_bytes": 0,
            "max_connections": 0,
            "max_seeders": 0,
            "max_download_speed": 0,
        }

        code, reason = worker._metadata_failure(
            stats,
            {"status": "complete", "errorMessage": "stopped by timeout"},
            "timeout",
        )

        self.assertEqual("no_peers", code)
        self.assertIn("连接数、做种数和下载量始终为 0", reason)

    def test_monitor_hands_paused_payload_to_sample_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            torrent_path = Path(directory) / ("b" * 40 + ".torrent")
            torrent_path.write_bytes(b"saved metadata")
            status = {
                "status": "complete",
                "completedLength": "1234",
                "totalLength": "1234",
                "downloadSpeed": "0",
                "connections": "1",
                "numSeeders": "1",
                "followedBy": ["payload-gid"],
            }
            config = {
                "workers": 1,
                "sample_bytes": 64 * 1024 * 1024,
                "sample_dir": str(Path(directory) / "samples"),
                "report_path": str(Path(directory) / "report.jsonl"),
                "work_dir": str(Path(directory) / "work"),
                "timeout": 10,
                "idle_timeout": 10,
                "rate_bytes": 1024,
                "progress_interval": 0,
            }
            jobs = [("metadata-gid", ("magnet", "b" * 40, 1.0))]

            with (
                patch.object(worker, "_torrent_path", return_value=torrent_path),
                patch.object(worker, "_accept_metadata", return_value=True),
                patch.object(worker, "_rpc_call", return_value=status),
                patch.object(
                    worker,
                    "_sample_metadata_followup",
                    return_value={"health": "healthy", "outcome": "frame_decoded"},
                ) as sample,
            ):
                succeeded, failed, counts = worker._monitor_jobs(jobs, config)

        self.assertEqual((1, 0), (succeeded, failed))
        self.assertEqual(1, counts["healthy"])
        sample.assert_called_once_with(torrent_path, "payload-gid", config)

    def test_watch_mode_keeps_polling_current_bucket(self):
        idle = {
            "rpc_ready": True,
            "aria2_version": "1.37.0",
            "candidates": 0,
            "submitted": 0,
        }
        with (
            patch.object(worker, "_run_magnet_batch", side_effect=[idle, idle]) as batch,
            patch.object(worker.time, "sleep", side_effect=[None, KeyboardInterrupt]),
        ):
            result = worker.magnet2torrent(
                sample_on_metadata=False,
                watch=True,
                watch_interval=0.5,
            )

        self.assertEqual(idle, result)
        self.assertEqual(2, batch.call_count)


if __name__ == "__main__":
    unittest.main()
