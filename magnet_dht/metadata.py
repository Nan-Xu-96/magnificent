#!/usr/bin/env python
# coding=utf-8

"""Torrent metadata and path validation."""

import base64
import binascii
import hashlib
import os
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlparse

from bencoder import bdecode, bencode


def extract_info_hash(magnet):
    """Return a normalized 40-character v1 BTIH hash, or ``None``."""
    try:
        query = parse_qs(urlparse(magnet).query)
        exact_topic = query.get("xt", [""])[0]
        prefix = "urn:btih:"
        if not exact_topic.lower().startswith(prefix):
            return None
        value = exact_topic[len(prefix) :].strip()
        if len(value) == 40:
            bytes.fromhex(value)
            return value.lower()
        if len(value) == 32:
            return base64.b32decode(value.upper()).hex()
    except (TypeError, ValueError, binascii.Error):
        return None
    return None


def _decode_name(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def load_torrent(path):
    max_bytes = int(os.getenv("MAGNET_DHT_MAX_METADATA_BYTES", str(16 * 1024 * 1024)))
    if os.path.getsize(path) > max_bytes:
        raise ValueError("torrent metadata exceeds {} bytes".format(max_bytes))
    with open(path, "rb") as torrent_file:
        metadata = bdecode(torrent_file.read())
    if not isinstance(metadata, dict) or not isinstance(metadata.get(b"info"), dict):
        raise ValueError("torrent metadata has no info dictionary")
    return metadata


def calculate_info_hash(metadata):
    """Calculate the BEP 3 SHA-1 hash of the bencoded ``info`` dictionary."""
    return hashlib.sha1(bencode(metadata[b"info"])).hexdigest()


def iter_files(metadata):
    """Yield ``(display_path, size_bytes)`` for v1 torrent files."""
    info = metadata[b"info"]
    root_name = _decode_name(info.get(b"name.utf-8", info.get(b"name", b"")))
    files = info.get(b"files")
    if not files:
        length = info.get(b"length")
        if not isinstance(length, int) or length < 0:
            raise ValueError("single-file torrent has an invalid length")
        yield root_name, length
        return
    for item in files:
        length = item.get(b"length")
        if not isinstance(length, int) or length < 0:
            raise ValueError("multi-file torrent has an invalid file length")
        components = item.get(b"path.utf-8", item.get(b"path", []))
        decoded = [_decode_name(component) for component in components]
        yield "/".join([root_name] + decoded), length


def iter_paths(metadata):
    """Yield display paths from single-file and multi-file metadata."""
    for path, _ in iter_files(metadata):
        yield path


def validate_paths(metadata):
    """Reject unsafe absolute, parent-traversal, empty and NUL-containing paths."""
    paths = list(iter_paths(metadata))
    if not paths:
        raise ValueError("torrent metadata contains no file paths")
    for value in paths:
        path = PurePosixPath(value)
        if (
            not value
            or "\x00" in value
            or path.is_absolute()
            or ".." in path.parts
            or any(not part for part in value.split("/"))
        ):
            raise ValueError("unsafe torrent path: {!r}".format(value))
    return paths


def validate_torrent(path, expected_info_hash=None):
    """Decode a torrent, validate paths, and optionally verify its info hash."""
    metadata = load_torrent(path)
    info_hash = calculate_info_hash(metadata)
    if expected_info_hash and info_hash.lower() != expected_info_hash.lower():
        raise ValueError(
            "metadata info hash mismatch: expected {}, got {}".format(
                expected_info_hash, info_hash
            )
        )
    paths = validate_paths(metadata)
    return metadata, info_hash, paths
