#!/usr/bin/env python3
# coding=utf-8

"""Sample a verified BitTorrent piece range and extract one local frame.

This module never intentionally completes a video download.  aria2 is asked
to prioritise a bounded head/tail preview, the transfer is rate limited and
stopped as soon as ffmpeg can decode a frame.  Partial payload files live in a
temporary directory and are removed after every attempt.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPConnection
from pathlib import Path

from .covers import LOCAL_VIDEO_EXTENSIONS
from .metadata import iter_files, validate_torrent


MIB = 1024 * 1024
DEFAULT_SAMPLE_BYTES = 64 * MIB
DEFAULT_MIN_ATTEMPT_BYTES = 8 * MIB
DEFAULT_RATE_BYTES = 2 * MIB
HASH_NAME = re.compile(r"^[0-9a-fA-F]{40}$")


@dataclass(frozen=True)
class VideoSampleTask:
    torrent_path: Path
    info_hash: str
    file_index: int
    file_path: str
    file_size: int
    piece_length: int
    sample_budget: int


class Aria2RpcClient:
    """Minimal aria2 JSON-RPC client used by the bounded preview worker."""

    def __init__(self, host=None, port=None, secret=None):
        self.host = host or os.getenv("ARIA2RPC_ADDR", "127.0.0.1")
        self.port = int(port or os.getenv("ARIA2RPC_PORT", "6800"))
        self.secret = secret if secret is not None else os.getenv("ARIA2RPC_SECRET")

    def call(self, method, params=None):
        rpc_params = list(params or [])
        if self.secret:
            rpc_params.insert(0, "token:" + self.secret)
        request = {
            "jsonrpc": "2.0",
            "id": "magnet-dht-video-sample",
            "method": method,
            "params": rpc_params,
        }
        connection = HTTPConnection(self.host, self.port, timeout=10)
        try:
            connection.request(
                "POST",
                "/jsonrpc",
                json.dumps(request),
                {"Content-Type": "application/json"},
            )
            response = json.loads(connection.getresponse().read())
        finally:
            connection.close()
        if "error" in response:
            error = response["error"]
            raise RuntimeError(
                "aria2 RPC {}: {}".format(error.get("code"), error.get("message"))
            )
        return response.get("result")

    def add_torrent(self, torrent_path, options):
        encoded = base64.b64encode(Path(torrent_path).read_bytes()).decode("ascii")
        params = [encoded, [], options]
        try:
            return self.call("aria2.addTorrent", params)
        except RuntimeError:
            # Metadata-only acquisition leaves completed results in aria2.
            # Forget a matching stopped result and retry without touching any
            # active user download or deleting its files.
            if not self._forget_stopped_info_hash(Path(torrent_path).stem):
                raise
            return self.call("aria2.addTorrent", params)

    def _forget_stopped_info_hash(self, info_hash):
        fields = ["gid", "infoHash", "status"]
        rows = self.call("aria2.tellStopped", [0, 1000, fields]) or []
        removed = 0
        for row in rows:
            if str(row.get("infoHash", "")).casefold() != str(info_hash).casefold():
                continue
            self.forget(row["gid"])
            removed += 1
        return removed

    def tell_status(self, gid):
        return self.call(
            "aria2.tellStatus",
            [
                gid,
                [
                    "status",
                    "totalLength",
                    "completedLength",
                    "downloadSpeed",
                    "numSeeders",
                    "connections",
                    "verifiedLength",
                    "errorCode",
                    "errorMessage",
                    "files",
                ],
            ],
        )

    def pause(self, gid):
        return self.call("aria2.forcePause", [gid])

    def unpause(self, gid):
        return self.call("aria2.unpause", [gid])

    def change_options(self, gid, options):
        return self.call("aria2.changeOption", [gid, options])

    def remove(self, gid):
        return self.call("aria2.forceRemove", [gid])

    def forget(self, gid):
        return self.call("aria2.removeDownloadResult", [gid])


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _as_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _effective_budget(file_size, piece_length, requested_bytes):
    """Keep the preview far enough below the complete file size.

    The half-file ceiling and four-piece reserve are deliberate safety margins
    for RPC polling/rate-limit lag.  Tiny videos that cannot satisfy the margin
    are skipped rather than accidentally downloaded in full.
    """
    piece_length = max(int(piece_length or 0), 16 * 1024)
    requested_bytes = max(int(requested_bytes), 0)
    if requested_bytes < piece_length * 2:
        return 0
    ceiling = min(file_size // 2, file_size - piece_length * 4)
    if ceiling < piece_length * 2:
        return 0
    return min(requested_bytes, ceiling)


def build_video_sample_task(torrent_path, sample_bytes=DEFAULT_SAMPLE_BYTES):
    """Build one representative-video task from a validated torrent.

    Returns ``(task, reason)``.  ``task`` is ``None`` for a torrent without a
    video or one whose piece geometry cannot respect the sampling budget.
    """
    torrent_path = Path(torrent_path).expanduser()
    expected = torrent_path.stem if HASH_NAME.fullmatch(torrent_path.stem) else None
    metadata, info_hash, _ = validate_torrent(torrent_path, expected)
    entries = [
        (index, path, size)
        for index, (path, size) in enumerate(iter_files(metadata), 1)
        if Path(path).suffix.casefold() in LOCAL_VIDEO_EXTENSIONS
    ]
    if not entries:
        return None, "no_video"
    index, file_path, file_size = max(entries, key=lambda item: item[2])
    piece_length = _as_int(metadata[b"info"].get(b"piece length")) or 256 * 1024
    budget = _effective_budget(file_size, piece_length, sample_bytes)
    if not budget:
        return None, "unsafe_sample_budget"
    return (
        VideoSampleTask(
            torrent_path=torrent_path.resolve(),
            info_hash=info_hash.lower(),
            file_index=index,
            file_path=file_path,
            file_size=file_size,
            piece_length=piece_length,
            sample_budget=budget,
        ),
        "",
    )


def discover_video_tasks(torrent_dir, sample_bytes=DEFAULT_SAMPLE_BYTES, limit=None):
    """Return representative-video tasks and a scan summary.

    File indices are one-based because that is what aria2's ``select-file``
    option expects.  Only the largest video in each torrent is sampled.
    """
    root = Path(torrent_dir).expanduser()
    tasks = []
    scanned = invalid = no_video = too_small = 0
    errors = []
    if not root.is_dir():
        return [], {
            "directory": str(root),
            "scanned": 0,
            "invalid": 0,
            "no_video": 0,
            "too_small": 0,
            "errors": ["torrent 目录不存在: {}".format(root)],
        }

    for torrent_path in sorted(root.glob("*.torrent")):
        scanned += 1
        try:
            task, reason = build_video_sample_task(torrent_path, sample_bytes)
        except Exception as exc:
            invalid += 1
            if len(errors) < 25:
                errors.append("{}: {}".format(torrent_path.name, exc))
            continue
        if reason == "no_video":
            no_video += 1
            continue
        if reason == "unsafe_sample_budget":
            too_small += 1
            continue
        tasks.append(task)
        if limit and len(tasks) >= int(limit):
            break

    return tasks, {
        "directory": str(root),
        "scanned": scanned,
        "invalid": invalid,
        "no_video": no_video,
        "too_small": too_small,
        "errors": errors,
    }


def _selected_path(status, file_index, work_root):
    files = status.get("files") or []
    if file_index < 1 or file_index > len(files):
        return None
    raw_path = files[file_index - 1].get("path")
    if not raw_path:
        return None
    candidate = Path(raw_path).resolve()
    try:
        candidate.relative_to(Path(work_root).resolve())
    except ValueError:
        raise ValueError("aria2 返回了工作目录之外的文件路径")
    return candidate


def extract_partial_frame(source, output, ffmpeg=None):
    """Decode the first available video frame from a sparse partial file."""
    executable = ffmpeg or shutil.which("ffmpeg")
    if not executable:
        raise FileNotFoundError("需要安装 ffmpeg 才能从视频片段抽帧")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp.jpg")
    try:
        subprocess.run(
            [
                executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-probesize",
                "16M",
                "-analyzeduration",
                "5000000",
                "-fflags",
                "+discardcorrupt",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-an",
                "-vf",
                "scale='min(720,iw)':-2",
                "-q:v",
                "3",
                str(temporary),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=45,
        )
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError("ffmpeg 未生成图片")
        temporary.replace(output)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _preview_options(task, work_root, rate_bytes, idle_timeout):
    tail_bytes = min(8 * MIB, max(task.piece_length, task.sample_budget // 4))
    head_bytes = max(task.piece_length, task.sample_budget - tail_bytes)
    rate_kib = max(int(rate_bytes) // 1024, 1)
    return {
        "dir": str(Path(work_root).resolve()),
        "select-file": str(task.file_index),
        "bt-metadata-only": "false",
        "bt-save-metadata": "false",
        "bt-prioritize-piece": "head={}K,tail={}K".format(
            max(head_bytes // 1024, 1), max(tail_bytes // 1024, 1)
        ),
        "bt-stop-timeout": str(max(int(idle_timeout), 1)),
        "file-allocation": "none",
        "max-download-limit": "{}K".format(rate_kib),
        "seed-time": "0",
        "allow-overwrite": "true",
        "auto-file-renaming": "false",
    }


def _stop_rpc_job(rpc, gid):
    if not gid:
        return
    try:
        rpc.remove(gid)
    except Exception:
        pass
    try:
        rpc.forget(gid)
    except Exception:
        pass


def _human_bytes(value):
    size = float(max(_as_int(value), 0))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return "{:.1f}{}".format(size, unit) if unit != "B" else "{}B".format(int(size))
        size /= 1024


def _classify_no_frame(result, cause, detail=""):
    downloaded = result["downloaded_bytes"]
    connections = result["max_connections"]
    seeders = result["max_seeders"]
    suffix = ": {}".format(detail) if detail else ""
    if downloaded == 0 and connections == 0 and seeders == 0:
        result["health"] = "unavailable"
        result["outcome"] = "no_peers"
        result["reason"] = "未发现任何 peer，连接数和下载量始终为 0" + suffix
    elif downloaded == 0:
        result["health"] = "unavailable"
        result["outcome"] = "peers_no_data"
        result["reason"] = "发现过 peer/连接，但没有收到可校验数据" + suffix
    elif cause == "budget":
        result["health"] = "inconclusive"
        result["outcome"] = "sample_budget_exhausted"
        result["reason"] = "已下载部分 piece，但分段预算内没有可解码帧" + suffix
    elif cause in ("timeout", "stopped"):
        result["health"] = "inconclusive"
        result["outcome"] = "partial_timeout"
        result["reason"] = "已下载部分 piece，但停止前尚不足以解码一帧" + suffix
    else:
        result["health"] = "inconclusive"
        result["outcome"] = "partial_error"
        result["reason"] = "已下载部分 piece，但任务异常结束" + suffix


def format_sample_result(result):
    """Return one concise diagnostic line for console and logs."""
    total = result.get("total_length_bytes") or result.get("video_size_bytes") or 0
    return (
        "[{outcome}] {info_hash} downloaded={downloaded}/{total} ({progress:.2f}%) "
        "peers={peers} seeders={seeders} max_speed={speed}/s frame={frame} reason={reason}"
    ).format(
        outcome=result.get("outcome") or result.get("health", "unknown"),
        info_hash=result.get("info_hash", ""),
        downloaded=_human_bytes(result.get("downloaded_bytes", 0)),
        total=_human_bytes(total),
        progress=float(result.get("progress_percent") or 0),
        peers=result.get("max_connections", 0),
        seeders=result.get("max_seeders", 0),
        speed=_human_bytes(result.get("max_download_speed_bytes", 0)),
        frame=result.get("frame_path") or "-",
        reason=result.get("reason") or "-",
    )


def sample_video_task(
    task,
    sample_dir,
    work_parent,
    rpc,
    frame_extractor=extract_partial_frame,
    timeout=300,
    idle_timeout=120,
    poll_interval=0.25,
    rate_bytes=DEFAULT_RATE_BYTES,
    progress_interval=10,
    existing_gid=None,
    trigger="standalone",
):
    """Fetch bounded verified pieces, extract one frame, then remove payload."""
    started_at = _utc_now()
    started = time.monotonic()
    gid = None
    result = {
        "time_utc": started_at,
        "info_hash": task.info_hash,
        "torrent_file": str(task.torrent_path),
        "video_path": task.file_path,
        "video_size_bytes": task.file_size,
        "sample_budget_bytes": task.sample_budget,
        "downloaded_bytes": 0,
        "verified_bytes": 0,
        "total_length_bytes": task.file_size,
        "progress_percent": 0.0,
        "max_seeders": 0,
        "max_connections": 0,
        "last_download_speed_bytes": 0,
        "max_download_speed_bytes": 0,
        "last_state": "",
        "last_error_code": "",
        "last_error_message": "",
        "health": "inconclusive",
        "outcome": "",
        "reason": "",
        "frame_path": "",
        "trigger": trigger,
    }
    Path(sample_dir).mkdir(parents=True, exist_ok=True)
    Path(work_parent).mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="{}-".format(task.info_hash), dir=str(work_parent)
    ) as work_root:
        try:
            options = _preview_options(task, work_root, rate_bytes, idle_timeout)
            if existing_gid:
                gid = existing_gid
                rpc.change_options(gid, options)
                rpc.unpause(gid)
            else:
                gid = rpc.add_torrent(task.torrent_path, options)
            next_attempt = min(
                task.sample_budget,
                max(DEFAULT_MIN_ATTEMPT_BYTES, task.piece_length * 4),
            )
            last_progress_print = float("-inf")
            while True:
                status = rpc.tell_status(gid)
                state = status.get("status", "")
                downloaded = _as_int(status.get("completedLength"))
                speed = _as_int(status.get("downloadSpeed"))
                total_length = _as_int(status.get("totalLength")) or task.file_size
                result["downloaded_bytes"] = max(result["downloaded_bytes"], downloaded)
                result["verified_bytes"] = max(
                    result["verified_bytes"], _as_int(status.get("verifiedLength"))
                )
                result["total_length_bytes"] = total_length
                result["last_download_speed_bytes"] = speed
                result["max_download_speed_bytes"] = max(
                    result["max_download_speed_bytes"], speed
                )
                result["max_seeders"] = max(
                    result["max_seeders"], _as_int(status.get("numSeeders"))
                )
                result["max_connections"] = max(
                    result["max_connections"], _as_int(status.get("connections"))
                )
                result["last_state"] = state
                result["last_error_code"] = str(status.get("errorCode") or "")
                result["last_error_message"] = status.get("errorMessage") or ""
                result["progress_percent"] = round(
                    100.0 * result["downloaded_bytes"] / max(total_length, 1), 4
                )

                elapsed = time.monotonic() - started
                if progress_interval and elapsed - last_progress_print >= float(progress_interval):
                    print(
                        "[sample-progress] {} state={} downloaded={}/{} peers={} "
                        "seeders={} speed={}/s elapsed={:.1f}s".format(
                            task.info_hash,
                            state,
                            _human_bytes(downloaded),
                            _human_bytes(total_length),
                            _as_int(status.get("connections")),
                            _as_int(status.get("numSeeders")),
                            _human_bytes(speed),
                            elapsed,
                        )
                    )
                    last_progress_print = elapsed

                terminal = state in ("complete", "error", "removed")
                if terminal and downloaded == 0:
                    detail = status.get("errorMessage") or "aria2 状态 {}，错误码 {}".format(
                        state, status.get("errorCode", "unknown")
                    )
                    _classify_no_frame(result, "stopped", detail)
                    break

                should_try = downloaded >= next_attempt or terminal
                if should_try:
                    paused = False
                    if not terminal and state in ("active", "waiting"):
                        try:
                            rpc.pause(gid)
                            paused = True
                        except Exception:
                            pass
                    source = _selected_path(status, task.file_index, work_root)
                    output = Path(sample_dir) / "{}.jpg".format(task.info_hash)
                    try:
                        if not source or not source.is_file():
                            raise FileNotFoundError("aria2 尚未创建视频片段")
                        frame_extractor(source, output)
                    except Exception as exc:
                        if terminal:
                            detail = status.get("errorMessage") or str(exc)
                            _classify_no_frame(result, "stopped", detail)
                            break
                        if downloaded >= task.sample_budget:
                            _classify_no_frame(result, "budget", str(exc))
                            break
                        next_attempt = min(
                            task.sample_budget,
                            max(next_attempt * 2, downloaded + task.piece_length),
                        )
                        if paused:
                            try:
                                rpc.unpause(gid)
                            except Exception as unpause_exc:
                                _classify_no_frame(
                                    result,
                                    "error",
                                    "无法继续 aria2 任务: {}".format(unpause_exc),
                                )
                                break
                    else:
                        result["health"] = "healthy"
                        result["outcome"] = "frame_decoded"
                        result["reason"] = "已下载并校验少量 piece，成功解码一帧"
                        result["frame_path"] = str(output.resolve())
                        break

                if elapsed >= float(timeout):
                    _classify_no_frame(
                        result,
                        "timeout",
                        "总等待时间达到 {} 秒".format(timeout),
                    )
                    break
                time.sleep(max(float(poll_interval), 0.05))
        except OSError as exc:
            result["health"] = (
                "inconclusive" if result["downloaded_bytes"] else "unavailable"
            )
            result["outcome"] = (
                "partial_rpc_error" if result["downloaded_bytes"] else "rpc_error"
            )
            result["reason"] = "aria2 RPC 连接中断: {}".format(exc)
        except Exception as exc:
            result["health"] = (
                "inconclusive" if result["downloaded_bytes"] else "unavailable"
            )
            result["outcome"] = (
                "partial_error" if result["downloaded_bytes"] else "task_error"
            )
            result["reason"] = str(exc)
        finally:
            _stop_rpc_job(rpc, gid)

    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    if not result["outcome"]:
        _classify_no_frame(result, "error", "任务未返回明确状态")
    return result


def append_sample_report(path, record):
    report_path = Path(path).expanduser()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("a", encoding="utf-8") as report_file:
        report_file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        report_file.flush()


def verify_video_torrents(
    torrent_dir="torrents",
    sample_dir="video-samples",
    report_path="video-health.jsonl",
    work_dir=".video-sample-work",
    sample_bytes=DEFAULT_SAMPLE_BYTES,
    limit=None,
    timeout=300,
    idle_timeout=120,
    rate_bytes=DEFAULT_RATE_BYTES,
    progress_interval=10,
    dry_run=False,
    rpc=None,
    frame_extractor=extract_partial_frame,
):
    """Sample local video torrents and return an aggregate health summary."""
    tasks, scan = discover_video_tasks(torrent_dir, sample_bytes, limit)
    summary = dict(scan)
    summary.update(
        {
            "eligible": len(tasks),
            "attempted": 0,
            "healthy": 0,
            "unavailable": 0,
            "inconclusive": 0,
            "sample_dir": str(Path(sample_dir).expanduser()),
            "report_path": str(Path(report_path).expanduser()),
            "dry_run": bool(dry_run),
            "planned_max_bytes": sum(task.sample_budget for task in tasks),
        }
    )
    if dry_run:
        summary["examples"] = [
            {
                "info_hash": task.info_hash,
                "video_path": task.file_path,
                "video_size_bytes": task.file_size,
                "sample_budget_bytes": task.sample_budget,
            }
            for task in tasks[:20]
        ]
        return summary
    if not shutil.which("ffmpeg") and frame_extractor is extract_partial_frame:
        summary["errors"].append("需要安装 ffmpeg 才能执行视频抽帧")
        return summary

    client = rpc or Aria2RpcClient()
    if rpc is None:
        try:
            client.call("aria2.getVersion")
        except Exception as exc:
            summary["errors"].append("aria2 RPC 不可用，未开始任何任务: {}".format(exc))
            return summary
    for task in tasks:
        record = sample_video_task(
            task,
            sample_dir,
            work_dir,
            client,
            frame_extractor=frame_extractor,
            timeout=timeout,
            idle_timeout=idle_timeout,
            rate_bytes=rate_bytes,
            progress_interval=progress_interval,
        )
        append_sample_report(report_path, record)
        summary["attempted"] += 1
        summary[record["health"]] += 1
        print(format_sample_result(record))
    return summary
