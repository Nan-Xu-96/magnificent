#!/usr/bin/env python3
# coding=utf-8

"""Optional local-only video cover extraction.

The helper intentionally accepts filesystem paths only.  It does not parse,
resolve, fetch, or download magnet links.
"""

import random
import shutil
import subprocess
from pathlib import Path

LOCAL_VIDEO_EXTENSIONS = {
    ".3gp", ".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg",
    ".mpg", ".ogm", ".ogv", ".rm", ".rmvb", ".ts", ".vob", ".webm", ".wmv",
}


def _safe_candidate(root, relative):
    root = Path(root).expanduser().resolve()
    candidate = (root / Path(relative)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _find_local_videos(file_paths, media_root):
    root = Path(media_root).expanduser().resolve()
    candidates = []
    for value in file_paths:
        path = Path(str(value))
        if path.suffix.casefold() not in LOCAL_VIDEO_EXTENSIONS:
            continue
        possibilities = [path, Path(path.name)]
        if len(path.parts) > 1:
            possibilities.append(Path(*path.parts[1:]))
        for relative in possibilities:
            candidate = _safe_candidate(root, relative)
            if candidate and candidate.is_file() and candidate not in candidates:
                candidates.append(candidate)
    return candidates


def _duration_seconds(path, ffprobe):
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True, timeout=20,
    )
    duration = float(result.stdout.strip())
    if duration <= 0:
        raise ValueError("视频时长无效")
    return duration


def _extract_frame(path, output, ffmpeg, ffprobe):
    duration = _duration_seconds(path, ffprobe)
    margin = min(2.0, duration / 10.0)
    low = margin
    high = max(margin, duration - margin)
    timestamp = random.SystemRandom().uniform(low, high) if high > low else 0.0
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", "{:.3f}".format(timestamp),
         "-i", str(path), "-map", "0:v:0", "-frames:v", "1", "-an",
         "-vf", "scale='min(720,iw)':-2", "-q:v", "3", str(output)],
        check=True, capture_output=True, text=True, timeout=90,
    )


def generate_cover_for_media(info_hash, file_paths, media_root, cover_dir):
    """Pick a local video and save one random frame as ``<hash>.jpg``.

    Returns the cover filename, or an empty string when no local video exists.
    Raises ``FileNotFoundError`` when ffmpeg/ffprobe are not installed.
    """
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise FileNotFoundError("需要安装 ffmpeg 和 ffprobe 才能生成视频封面")
    videos = _find_local_videos(file_paths, media_root)
    if not videos:
        return ""
    output_dir = Path(cover_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "{}.jpg".format(info_hash.lower())
    _extract_frame(random.SystemRandom().choice(videos), output, ffmpeg, ffprobe)
    return output.name
