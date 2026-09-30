#!/usr/bin/env python
# coding=utf-8

import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from .acquisition import load_acquisitions
from .metadata import iter_files, validate_torrent

TORRENT_SAVE_PATH = "torrents"
CSV_FIELDS = (
    "status",
    "error",
    "info_hash",
    "magnet_uri",
    "torrent_file",
    "torrent_saved_time_utc",
    "acquisition_method",
    "acquisition_time_utc",
    "announce_heat",
    "resource_name",
    "total_size_bytes",
    "total_size_mib",
    "file_count",
    "file_extensions",
    "created_by",
    "creation_time_utc",
    "file_paths_json",
)


def _decode_text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return "" if value is None else str(value)


def _spreadsheet_safe(value):
    """Prevent untrusted torrent names from becoming spreadsheet formulas."""
    text = _decode_text(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _creation_time(metadata):
    value = metadata.get(b"creation date")
    if not isinstance(value, int) or value < 0:
        return ""
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _torrent_saved_time(torrent):
    try:
        return datetime.fromtimestamp(
            torrent.stat().st_mtime, tz=timezone.utc
        ).isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


def parse_torrent():
    """Validate saved torrent files and print their hash and paths for review."""
    root = Path(os.getenv("MAGNET_DHT_TORRENT_DIR", TORRENT_SAVE_PATH))
    for torrent in sorted(root.glob("*.torrent")):
        expected = torrent.stem if len(torrent.stem) == 40 else None
        try:
            _, info_hash, paths = validate_torrent(torrent, expected_info_hash=expected)
        except Exception as exc:
            print("[invalid] {}: {}".format(torrent, exc))
            continue
        print("[valid] {} hash={}".format(torrent, info_hash))
        for path in paths:
            print("  - {}".format(path))


def export_torrents_csv(output_path="torrent_metadata.csv"):
    """Validate torrents and export one UTF-8 CSV row per unique info hash."""
    root = Path(os.getenv("MAGNET_DHT_TORRENT_DIR", TORRENT_SAVE_PATH))
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    scanned_count = 0
    resource_count = 0
    contained_file_count = 0
    invalid_count = 0
    seen_info_hashes = set()
    acquisitions = load_acquisitions(root)

    with output.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for torrent in sorted(root.glob("*.torrent")):
            scanned_count += 1
            expected = torrent.stem if len(torrent.stem) == 40 else None
            try:
                metadata, info_hash, _ = validate_torrent(
                    torrent, expected_info_hash=expected
                )
                files = list(iter_files(metadata))
            except Exception as exc:
                invalid_count += 1
                writer.writerow(
                    {
                        "status": "invalid",
                        "error": _spreadsheet_safe(exc),
                        "info_hash": expected or "",
                        "torrent_file": str(torrent),
                        "torrent_saved_time_utc": _torrent_saved_time(torrent),
                    }
                )
                continue

            if info_hash in seen_info_hashes:
                continue
            seen_info_hashes.add(info_hash)
            resource_count += 1

            info = metadata[b"info"]
            resource_name = info.get(b"name.utf-8", info.get(b"name", b""))
            total_size = sum(size for _, size in files)
            contained_file_count += len(files)
            file_paths = [path for path, _ in files]
            extensions = sorted(
                {
                    Path(path).suffix.casefold()
                    for path in file_paths
                    if Path(path).suffix
                }
            )
            created_by = _spreadsheet_safe(metadata.get(b"created by"))
            creation_time = _creation_time(metadata)
            acquisition = acquisitions.get(info_hash, {})
            writer.writerow(
                {
                    "status": "valid",
                    "error": "",
                    "info_hash": info_hash,
                    "magnet_uri": "magnet:?xt=urn:btih:{}".format(info_hash),
                    "torrent_file": str(torrent),
                    "torrent_saved_time_utc": _torrent_saved_time(torrent),
                    "acquisition_method": acquisition.get("acquisition_method", ""),
                    "acquisition_time_utc": acquisition.get(
                        "acquisition_time_utc", ""
                    ),
                    "announce_heat": acquisition.get("announce_heat", ""),
                    "resource_name": _spreadsheet_safe(resource_name),
                    "total_size_bytes": total_size,
                    "total_size_mib": "{:.3f}".format(total_size / (1024 * 1024)),
                    "file_count": len(files),
                    "file_extensions": json.dumps(extensions, ensure_ascii=False),
                    "created_by": created_by,
                    "creation_time_utc": creation_time,
                    "file_paths_json": json.dumps(
                        file_paths, ensure_ascii=False, separators=(",", ":")
                    ),
                }
            )

    print(
        "CSV exported: {} resources={} files={} scanned={} invalid={}".format(
            output.resolve(),
            resource_count,
            contained_file_count,
            scanned_count,
            invalid_count,
        )
    )
    return output
