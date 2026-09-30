#!/usr/bin/env python
# coding=utf-8

import argparse
import os

from magnet_dht.crawler import start_server
from magnet_dht.magnet_to_torrent_aria2c import magnet2torrent
from magnet_dht.parse_torrent import export_torrents_csv, parse_torrent
from magnet_dht.catalog import CatalogStore
from magnet_dht.video_health import MIB, verify_video_torrents
from magnet_dht.web import run_server


def get_parser():
    """
    解析命令行参数
    """
    parser = argparse.ArgumentParser(description="start manage.py with flag.")
    parser.add_argument("-s", action="store_true", help="run start_server func.")
    parser.add_argument("-m", action="store_true", help="run magnet2torrent func")
    parser.add_argument("-p", action="store_true", help="run parse_torrent func")
    parser.add_argument(
        "--web",
        action="store_true",
        help="run the self-hosted metadata/review catalogue",
    )
    parser.add_argument("--host", default="127.0.0.1", help="web bind host")
    parser.add_argument("--port", default=8080, type=int, help="web bind port")
    parser.add_argument(
        "--import-catalog",
        action="store_true",
        help="import local torrent metadata into the catalogue and exit",
    )
    parser.add_argument(
        "--generate-covers",
        action="store_true",
        help="generate random-frame covers from an already local media directory",
    )
    parser.add_argument("--media-root", default=os.environ.get("MAGNET_DHT_MEDIA_ROOT", ""), help="local media directory for cover extraction")
    parser.add_argument("--cover-dir", default=os.environ.get("MAGNET_DHT_COVER_DIR", "catalog-covers"), help="directory for generated covers")
    parser.add_argument(
        "--sample-video-torrents",
        action="store_true",
        help="download bounded BitTorrent pieces and keep one decoded JPG per video torrent",
    )
    parser.add_argument(
        "--torrent-dir",
        default=os.environ.get("MAGNET_DHT_TORRENT_DIR", "torrents"),
        help="directory containing validated .torrent files",
    )
    parser.add_argument(
        "--sample-dir",
        default=os.environ.get("MAGNET_DHT_SAMPLE_DIR", "video-samples"),
        help="local directory for sampled JPG files",
    )
    parser.add_argument(
        "--sample-report",
        default=os.environ.get("MAGNET_DHT_HEALTH_REPORT", "video-health.jsonl"),
        help="append-only JSONL health report path",
    )
    parser.add_argument(
        "--sample-work-dir",
        default=os.environ.get("MAGNET_DHT_SAMPLE_WORK_DIR", ".video-sample-work"),
        help="temporary partial-piece directory (cleaned after every torrent)",
    )
    parser.add_argument(
        "--sample-limit",
        default=0,
        type=int,
        help="maximum video torrents to check; 0 scans all",
    )
    parser.add_argument(
        "--sample-mib",
        default=64,
        type=int,
        help="maximum verified piece data per torrent (default: 64 MiB)",
    )
    parser.add_argument(
        "--sample-rate-mib",
        default=2,
        type=int,
        help="aria2 per-task rate cap, limiting polling overshoot (default: 2 MiB/s)",
    )
    parser.add_argument(
        "--sample-timeout",
        default=300,
        type=int,
        help="maximum seconds for each torrent preview",
    )
    parser.add_argument(
        "--sample-idle-timeout",
        default=120,
        type=int,
        help="stop a torrent after this many seconds at zero speed",
    )
    parser.add_argument(
        "--sample-progress-interval",
        default=10,
        type=float,
        help="seconds between peer/progress diagnostics; 0 disables them",
    )
    parser.add_argument(
        "--sample-workers",
        default=2,
        type=int,
        help="parallel frame samplers used after new metadata (default: 2)",
    )
    parser.add_argument(
        "--no-sample-on-metadata",
        action="store_true",
        help="with -m, save metadata only and do not sample its video payload",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="with -m, keep polling current announce heat instead of exiting",
    )
    parser.add_argument(
        "--watch-interval",
        default=5,
        type=float,
        help="seconds between announce queue polls in --watch mode",
    )
    parser.add_argument(
        "--sample-dry-run",
        action="store_true",
        help="only inventory representative videos and preview budgets",
    )
    parser.add_argument(
        "--csv",
        nargs="?",
        const="torrent_metadata.csv",
        metavar="PATH",
        help="export validated torrent file information to CSV",
    )
    return parser


def command_line_runner():
    """
    执行命令行操作
    """
    parser = get_parser()
    args = vars(parser.parse_args())

    if args["web"]:
        run_server(args["host"], args["port"])
    elif args["import_catalog"]:
        directory = os.environ.get("MAGNET_DHT_TORRENT_DIR", "torrents")
        print(CatalogStore().import_torrents(directory))
    elif args["generate_covers"]:
        print(CatalogStore().generate_covers(media_root=args["media_root"], cover_dir=args["cover_dir"]))
    elif args["sample_video_torrents"]:
        print(
            verify_video_torrents(
                torrent_dir=args["torrent_dir"],
                sample_dir=args["sample_dir"],
                report_path=args["sample_report"],
                work_dir=args["sample_work_dir"],
                sample_bytes=max(args["sample_mib"], 1) * MIB,
                limit=max(args["sample_limit"], 0) or None,
                timeout=max(args["sample_timeout"], 1),
                idle_timeout=max(args["sample_idle_timeout"], 1),
                rate_bytes=max(args["sample_rate_mib"], 1) * MIB,
                progress_interval=max(args["sample_progress_interval"], 0),
                dry_run=args["sample_dry_run"],
            )
        )
    elif args["s"]:
        start_server()
    elif args["m"]:
        magnet2torrent(
            sample_on_metadata=False if args["no_sample_on_metadata"] else None,
            sample_dir=args["sample_dir"],
            sample_report=args["sample_report"],
            sample_work_dir=args["sample_work_dir"],
            sample_bytes=max(args["sample_mib"], 1) * MIB,
            sample_timeout=max(args["sample_timeout"], 1),
            sample_idle_timeout=max(args["sample_idle_timeout"], 1),
            sample_rate_bytes=max(args["sample_rate_mib"], 1) * MIB,
            sample_progress_interval=max(args["sample_progress_interval"], 0),
            sample_workers=max(args["sample_workers"], 1),
            watch=args["watch"],
            watch_interval=max(args["watch_interval"], 0.5),
        )
    elif args["p"]:
        parse_torrent()
    elif args["csv"] is not None:
        export_torrents_csv(args["csv"])


if __name__ == "__main__":
    command_line_runner()
