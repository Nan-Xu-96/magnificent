#!/usr/bin/env python3
# coding=utf-8

"""SQLite-backed catalogue for the local torrent metadata review site.

The catalogue deliberately stores descriptive metadata only.  The web layer
does not expose torrent files, magnet actions, streaming, or transfer APIs.
"""

import json
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .covers import generate_cover_for_media
from .metadata import iter_files, validate_torrent


CATEGORY_LABELS = {
    "video": "视频",
    "audio": "音频",
    "image": "图片",
    "document": "文档",
    "archive": "压缩包",
    "other": "其他",
}

VIDEO_EXTENSIONS = {
    ".3gp", ".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg",
    ".mpg", ".ogm", ".ogv", ".rm", ".rmvb", ".ts", ".vob", ".webm", ".wmv",
}
AUDIO_EXTENSIONS = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"}
IMAGE_EXTENSIONS = {".avif", ".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
DOCUMENT_EXTENSIONS = {".csv", ".doc", ".docx", ".epub", ".md", ".pdf", ".ppt", ".pptx", ".rtf", ".txt", ".xls", ".xlsx"}
ARCHIVE_EXTENSIONS = {".7z", ".bz2", ".gz", ".iso", ".rar", ".tar", ".xz", ".zip"}


def _text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return "" if value is None else str(value)


def _creation_time(metadata):
    value = metadata.get(b"creation date")
    if not isinstance(value, int) or value < 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _guess_year(title):
    """Extract a conservative four-digit release-year hint from a filename."""
    match = re.search(r"(?:^|[^0-9])(19[0-9]{2}|20[0-2][0-9])(?:[^0-9]|$)", title)
    return int(match.group(1)) if match else None


def classify_extensions(extensions):
    """Return the primary catalogue category for a torrent's file extensions."""
    extension_set = {str(value).casefold() for value in extensions}
    if extension_set & VIDEO_EXTENSIONS:
        return "video"
    if extension_set & AUDIO_EXTENSIONS:
        return "audio"
    if extension_set & IMAGE_EXTENSIONS:
        return "image"
    if extension_set & DOCUMENT_EXTENSIONS:
        return "document"
    if extension_set & ARCHIVE_EXTENSIONS:
        return "archive"
    return "other"


class CatalogStore:
    """Small, dependency-free persistence layer suitable for one instance."""

    def __init__(self, path=None):
        self.path = str(path or os.getenv("MAGNET_DHT_CATALOG_DB", "catalog.sqlite3"))
        self._uri = self.path == ":memory:"
        self._sqlite_path = (
            "file:magnet-dht-catalog-{}?mode=memory&cache=shared".format(id(self))
            if self._uri else self.path
        )
        self._anchor = None
        if self._uri:
            # Keep one connection alive so SQLite's shared in-memory database
            # survives the short-lived connections used by each operation.
            self._anchor = sqlite3.connect(self._sqlite_path, uri=True)
        else:
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self._sqlite_path, uri=self._uri)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS media (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    info_hash TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    year INTEGER,
                    summary TEXT NOT NULL DEFAULT '',
                    cover_url TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT '',
                    creation_time TEXT,
                    total_size INTEGER NOT NULL DEFAULT 0,
                    file_count INTEGER NOT NULL DEFAULT 0,
                    file_extensions TEXT NOT NULL DEFAULT '[]',
                    file_paths TEXT NOT NULL DEFAULT '[]',
                    category TEXT NOT NULL DEFAULT 'other',
                    cover_path TEXT NOT NULL DEFAULT '',
                    imported_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS media_title_idx ON media(title COLLATE NOCASE);
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
                    author TEXT NOT NULL,
                    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
                    body TEXT NOT NULL,
                    spoiler INTEGER NOT NULL DEFAULT 0 CHECK (spoiler IN (0, 1)),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS reviews_media_idx ON reviews(media_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    UNIQUE(media_id, name)
                );
                """
            )
            # Existing catalog databases predate categories/covers.  Keep
            # those databases usable without requiring a destructive reset.
            columns = {row[1] for row in connection.execute("PRAGMA table_info(media)")}
            if "category" not in columns:
                connection.execute("ALTER TABLE media ADD COLUMN category TEXT NOT NULL DEFAULT 'other'")
            if "cover_path" not in columns:
                connection.execute("ALTER TABLE media ADD COLUMN cover_path TEXT NOT NULL DEFAULT ''")
            # Backfill categories for databases imported before the category
            # field existed.
            for row in connection.execute("SELECT id, file_extensions, category FROM media"):
                category = classify_extensions(json.loads(row[1] or "[]"))
                if row[2] != category:
                    connection.execute("UPDATE media SET category = ? WHERE id = ?", (category, row[0]))

    @staticmethod
    def _row_media(row):
        if row is None:
            return None
        item = dict(row)
        item["file_extensions"] = json.loads(item.pop("file_extensions") or "[]")
        item["file_paths"] = json.loads(item.pop("file_paths") or "[]")
        item["review_count"] = int(item.get("review_count") or 0)
        item["rating_average"] = (
            round(float(item["rating_average"]), 1)
            if item.get("rating_average") is not None
            else None
        )
        return item

    def import_torrents(self, directory):
        """Import valid ``*.torrent`` metadata and return a scan summary.

        Invalid files are reported but never abort the rest of the import.
        The filename is used as the expected hash when it is a 40-character
        hexadecimal value, matching the existing acquisition worker.
        """
        root = Path(directory).expanduser()
        scanned = imported = invalid = 0
        errors = []
        if not root.exists() or not root.is_dir():
            return {"directory": str(root), "scanned": 0, "imported": 0,
                    "invalid": 0, "errors": ["目录不存在: {}".format(root)]}
        for torrent in sorted(root.glob("*.torrent")):
            scanned += 1
            expected = torrent.stem if len(torrent.stem) == 40 else None
            try:
                metadata, info_hash, _ = validate_torrent(
                    torrent, expected_info_hash=expected
                )
                files = list(iter_files(metadata))
            except Exception as exc:
                invalid += 1
                if len(errors) < 25:
                    errors.append("{}: {}".format(torrent.name, exc))
                continue
            info = metadata[b"info"]
            title = _text(info.get(b"name.utf-8", info.get(b"name", b""))).strip()
            if not title:
                title = info_hash
            paths = [path for path, _ in files]
            extensions = sorted({Path(path).suffix.casefold() for path in paths if Path(path).suffix})
            category = classify_extensions(extensions)
            now = datetime.now(timezone.utc).isoformat()
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO media (
                        info_hash, title, year, created_by, creation_time, total_size,
                        file_count, file_extensions, file_paths, category, imported_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(info_hash) DO UPDATE SET
                        title=excluded.title,
                        year=excluded.year,
                        created_by=excluded.created_by,
                        creation_time=excluded.creation_time,
                        total_size=excluded.total_size,
                        file_count=excluded.file_count,
                        file_extensions=excluded.file_extensions,
                        file_paths=excluded.file_paths,
                        category=excluded.category,
                        imported_at=excluded.imported_at
                    """,
                    (
                        info_hash.lower(), title, _guess_year(title), _text(metadata.get(b"created by")),
                        _creation_time(metadata), sum(size for _, size in files),
                        len(files), json.dumps(extensions, ensure_ascii=False),
                        json.dumps(paths, ensure_ascii=False), category, now,
                    ),
                )
            imported += 1
        return {"directory": str(root), "scanned": scanned, "imported": imported,
                "invalid": invalid, "errors": errors}

    def list_media(self, query="", sort="recent", page=1, page_size=24, category="video"):
        page = max(int(page or 1), 1)
        page_size = min(max(int(page_size or 24), 1), 100)
        conditions = []
        params = []
        if category in CATEGORY_LABELS:
            conditions.append("m.category = ?")
            params.append(category)
        if query:
            conditions.append("(m.title LIKE ? OR m.info_hash LIKE ? OR m.file_paths LIKE ?)")
            needle = "%{}%".format(query.strip())
            params.extend([needle, needle, needle])
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        order = {
            "rating": "(rating_average IS NULL) ASC, rating_average DESC, review_count DESC, m.title COLLATE NOCASE",
            "title": "m.title COLLATE NOCASE ASC",
            "size": "m.total_size DESC, m.title COLLATE NOCASE",
            "recent": "m.imported_at DESC, m.title COLLATE NOCASE",
        }.get(sort, "m.imported_at DESC, m.title COLLATE NOCASE")
        offset = (page - 1) * page_size
        with self._connect() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM media m {}".format(where), params
            ).fetchone()[0]
            rows = connection.execute(
                """
                SELECT m.*, AVG(r.rating) AS rating_average, COUNT(r.id) AS review_count
                FROM media m LEFT JOIN reviews r ON r.media_id = m.id
                {} GROUP BY m.id ORDER BY {} LIMIT ? OFFSET ?
                """.format(where, order), params + [page_size, offset]
            ).fetchall()
        return {
            "items": [self._row_media(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max((total + page_size - 1) // page_size, 1),
        }

    def generate_covers(self, media_root=None, cover_dir=None, limit=None):
        """Create random-frame covers from files already present on disk.

        This never resolves or downloads a magnet.  ``media_root`` must point
        to a local directory containing the user's already-acquired files.
        """
        root_value = media_root if media_root is not None else os.getenv("MAGNET_DHT_MEDIA_ROOT", "")
        if not str(root_value).strip():
            return {"generated": 0, "skipped": 0, "errors": ["请先配置本地媒体目录（MAGNET_DHT_MEDIA_ROOT）"]}
        root = Path(root_value).expanduser()
        destination = Path(cover_dir or os.getenv("MAGNET_DHT_COVER_DIR", "catalog-covers")).expanduser()
        if not root.is_dir():
            return {"generated": 0, "skipped": 0, "errors": ["本地媒体目录不存在: {}".format(root)]}
        with self._connect() as connection:
            query = "SELECT info_hash, file_paths FROM media WHERE category = 'video' AND (cover_path = '' OR cover_path IS NULL)"
            values = []
            if limit:
                query += " LIMIT ?"
                values.append(int(limit))
            rows = connection.execute(query, values).fetchall()
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            return {"generated": 0, "skipped": len(rows), "errors": ["需要安装 ffmpeg 和 ffprobe 才能生成视频封面"], "directory": str(root), "cover_dir": str(destination)}
        generated = skipped = 0
        errors = []
        for row in rows:
            try:
                cover_name = generate_cover_for_media(
                    row["info_hash"], json.loads(row["file_paths"] or "[]"), root, destination
                )
            except Exception as exc:
                skipped += 1
                if len(errors) < 25:
                    errors.append("{}: {}".format(row["info_hash"], exc))
                continue
            if not cover_name:
                skipped += 1
                continue
            with self._connect() as connection:
                connection.execute("UPDATE media SET cover_path = ? WHERE info_hash = ?", (cover_name, row["info_hash"]))
            generated += 1
        return {"generated": generated, "skipped": skipped, "errors": errors, "directory": str(root), "cover_dir": str(destination)}

    def get_media(self, info_hash):
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT m.*, AVG(r.rating) AS rating_average, COUNT(r.id) AS review_count
                FROM media m LEFT JOIN reviews r ON r.media_id = m.id
                WHERE m.info_hash = ? GROUP BY m.id
                """, (info_hash.lower(),)
            ).fetchone()
            item = self._row_media(row)
            if item is None:
                return None
            item["reviews"] = [dict(review) for review in connection.execute(
                """
                SELECT id, author, rating, body, spoiler, created_at FROM reviews
                WHERE media_id = ? ORDER BY created_at DESC
                """, (item["id"],)
            ).fetchall()]
            item["tags"] = [row[0] for row in connection.execute(
                "SELECT name FROM tags WHERE media_id = ? ORDER BY name", (item["id"],)
            ).fetchall()]
            return item

    def add_review(self, info_hash, author, rating, body, spoiler=False):
        author = (author or "匿名").strip()[:80] or "匿名"
        body = (body or "").strip()
        rating = int(rating)
        if not 1 <= rating <= 5:
            raise ValueError("评分必须是 1 到 5 星")
        if not 3 <= len(body) <= 5000:
            raise ValueError("影评长度必须在 3 到 5000 个字符之间")
        with self._connect() as connection:
            media = connection.execute(
                "SELECT id FROM media WHERE info_hash = ?", (info_hash.lower(),)
            ).fetchone()
            if media is None:
                raise KeyError(info_hash)
            connection.execute(
                """
                INSERT INTO reviews(media_id, author, rating, body, spoiler, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (media[0], author, rating, body, int(bool(spoiler)),
                 datetime.now(timezone.utc).isoformat()),
            )

    def add_tag(self, info_hash, name):
        name = (name or "").strip().replace("\n", " ")[:40]
        if not name:
            raise ValueError("标签不能为空")
        with self._connect() as connection:
            media = connection.execute(
                "SELECT id FROM media WHERE info_hash = ?", (info_hash.lower(),)
            ).fetchone()
            if media is None:
                raise KeyError(info_hash)
            connection.execute(
                "INSERT OR IGNORE INTO tags(media_id, name) VALUES (?, ?)",
                (media[0], name),
            )
