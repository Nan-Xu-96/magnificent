#!/usr/bin/env python
# coding=utf-8

"""Durable provenance log for successfully acquired torrent metadata."""

from datetime import datetime, timezone
import json
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - the project currently targets Linux
    fcntl = None


LOG_FILENAME = ".acquisitions.jsonl"


def _log_path(torrent_dir):
    return Path(torrent_dir) / LOG_FILENAME


def record_acquisition(info_hash, torrent_path, method, announce_heat=None):
    """Append one successful acquisition record and return it."""
    path = Path(torrent_path)
    record = {
        "info_hash": info_hash.lower(),
        "acquisition_method": method,
        "acquisition_time_utc": datetime.now(timezone.utc).isoformat(),
        "announce_heat": announce_heat,
        "torrent_file": str(path),
    }
    log_path = _log_path(path.parent)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        if fcntl is not None:
            fcntl.flock(log_file.fileno(), fcntl.LOCK_EX)
        log_file.write(json.dumps(record, ensure_ascii=False) + "\n")
        log_file.flush()
        if fcntl is not None:
            fcntl.flock(log_file.fileno(), fcntl.LOCK_UN)
    return record


def load_acquisitions(torrent_dir):
    """Return the newest provenance record for each info hash."""
    log_path = _log_path(torrent_dir)
    records = {}
    if not log_path.exists():
        return records
    with log_path.open(encoding="utf-8") as log_file:
        for line in log_file:
            try:
                record = json.loads(line)
                info_hash = record["info_hash"].lower()
                acquired_at = record["acquisition_time_utc"]
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            previous = records.get(info_hash)
            if previous is None or acquired_at >= previous["acquisition_time_utc"]:
                records[info_hash] = record
    return records
