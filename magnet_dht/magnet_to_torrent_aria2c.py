#!/usr/bin/python
# encoding=utf-8

import json
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path

from .acquisition import record_acquisition
from .database import RedisClient
from .metadata import extract_info_hash, validate_torrent
from .video_health import (
    DEFAULT_RATE_BYTES,
    DEFAULT_SAMPLE_BYTES,
    Aria2RpcClient,
    append_sample_report,
    build_video_sample_task,
    format_sample_result,
    sample_video_task,
)


SAVE_PATH = os.getenv("MAGNET_DHT_TORRENT_DIR", "torrents")
STOP_TIMEOUT = int(os.getenv("MAGNET_DHT_STOP_TIMEOUT", "90"))
MAX_CONCURRENT = int(os.getenv("MAGNET_DHT_MAX_CONCURRENT", "8"))
FAILURE_COOLDOWN = int(os.getenv("MAGNET_DHT_FAILURE_COOLDOWN", "900"))
RPC_POLL_INTERVAL = float(os.getenv("MAGNET_DHT_RPC_POLL_INTERVAL", "2"))
ANNOUNCE_WINDOW_HOURS = int(os.getenv("MAGNET_DHT_ANNOUNCE_WINDOW_HOURS", "6"))
ANNOUNCE_HALF_LIFE_HOURS = float(
    os.getenv("MAGNET_DHT_ANNOUNCE_HALF_LIFE_HOURS", "2")
)
ONCE_ONLY = os.getenv("MAGNET_DHT_ONCE_ONLY", "false").strip().lower() not in (
    "0",
    "false",
    "no",
)
SAMPLE_ON_METADATA = os.getenv("MAGNET_DHT_SAMPLE_ON_METADATA", "true").strip().lower() not in (
    "0",
    "false",
    "no",
)
SAMPLE_WORKERS = int(os.getenv("MAGNET_DHT_SAMPLE_WORKERS", "2"))
METADATA_PROGRESS_INTERVAL = float(os.getenv("MAGNET_DHT_METADATA_PROGRESS_INTERVAL", "10"))

ARIA2RPC_ADDR = os.getenv("ARIA2RPC_ADDR", "127.0.0.1")
ARIA2RPC_PORT = int(os.getenv("ARIA2RPC_PORT", "6800"))
ARIA2RPC_SECRET = os.getenv("ARIA2RPC_SECRET")

rd = RedisClient()


def _rpc_call(method, params=None):
    rpc_params = list(params or [])
    if ARIA2RPC_SECRET:
        rpc_params.insert(0, "token:" + ARIA2RPC_SECRET)
    request = {
        "jsonrpc": "2.0",
        "id": "magnet-dht",
        "method": method,
        "params": rpc_params,
    }
    connection = HTTPConnection(ARIA2RPC_ADDR, ARIA2RPC_PORT, timeout=10)
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
        raise RuntimeError("aria2 RPC {}: {}".format(error.get("code"), error.get("message")))
    return response.get("result")


def get_magnets():
    """Claim candidates ranked only by locally observed announce heat."""
    yield from rd.get_hot_announce_candidates(
        count=MAX_CONCURRENT,
        window_hours=ANNOUNCE_WINDOW_HOURS,
        half_life_hours=ANNOUNCE_HALF_LIFE_HOURS,
        once_only=ONCE_ONLY,
    )


def exec_rpc(magnet, sample_on_metadata=False):
    """Submit a magnet job and optionally pause its generated payload job."""
    options = {
        "bt-stop-timeout": str(STOP_TIMEOUT),
        "bt-metadata-only": "false" if sample_on_metadata else "true",
        "bt-save-metadata": "true",
        "pause-metadata": "true" if sample_on_metadata else "false",
        "dir": str(Path(SAVE_PATH).resolve()),
    }
    return _rpc_call("aria2.addUri", [[magnet], options])


def _torrent_path(info_hash):
    return Path(SAVE_PATH).resolve() / "{}.torrent".format(info_hash.lower())


def _accept_metadata(
    magnet,
    info_hash,
    path,
    acquisition_method=None,
    announce_heat=None,
):
    try:
        _, _, paths = validate_torrent(path, expected_info_hash=info_hash)
    except Exception as exc:
        failures, delay = rd.mark_failure(
            magnet,
            "metadata_invalid: {}".format(exc),
            FAILURE_COOLDOWN,
        )
        print("[invalid] {}: {}".format(info_hash, exc))
        print("[cooldown] failures={} retry_in={}s".format(failures, delay))
        return False
    rd.mark_metadata_valid(magnet)
    if acquisition_method:
        try:
            record_acquisition(
                info_hash,
                path,
                method=acquisition_method,
                announce_heat=announce_heat,
            )
        except Exception as exc:
            print("[acquisition-log-error] {}: {}".format(info_hash, exc))
    print(
        "[success] {} files={} path={}".format(info_hash, len(paths), path)
    )
    return True


def _prepare_candidates(magnets):
    prepared = []
    for magnet, heat in magnets:
        info_hash = extract_info_hash(magnet)
        if not info_hash:
            rd.mark_invalid(magnet, "invalid magnet/info hash")
            print("[invalid] {}".format(magnet))
            continue
        path = _torrent_path(info_hash)
        if path.exists() and _accept_metadata(magnet, info_hash, path):
            continue
        prepared.append((magnet, info_hash, heat))
    return prepared


def _status_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _metadata_failure(stats, status, cause):
    downloaded = stats["downloaded_bytes"]
    connections = stats["max_connections"]
    seeders = stats["max_seeders"]
    detail = status.get("errorMessage") or cause
    if downloaded == 0 and connections == 0 and seeders == 0:
        code = "no_peers"
        text = "未发现 peer；连接数、做种数和下载量始终为 0"
    elif downloaded == 0:
        code = "peers_no_metadata"
        text = "发现过 peer/连接，但没有收到 metadata"
    else:
        code = "metadata_incomplete"
        text = "收到部分数据，但 metadata 尚未完成"
    return code, (
        "{}: {}; state={} downloaded={} peers={} seeders={} max_speed={} detail={}".format(
            code,
            text,
            status.get("status", "unknown"),
            downloaded,
            connections,
            seeders,
            stats["max_download_speed"],
            detail,
        )
    )


def _remove_rpc_job(gid, state=""):
    if not gid:
        return
    if state not in ("complete", "error", "removed"):
        try:
            _rpc_call("aria2.forceRemove", [gid])
        except Exception:
            pass
    try:
        _rpc_call("aria2.removeDownloadResult", [gid])
    except Exception:
        pass


def _cancel_sample_child(client, gid):
    if not gid:
        return
    try:
        client.remove(gid)
    except Exception:
        pass
    try:
        client.forget(gid)
    except Exception:
        pass


def _sample_metadata_followup(torrent_path, child_gid, config):
    client = Aria2RpcClient()
    try:
        task, skip_reason = build_video_sample_task(
            torrent_path, config["sample_bytes"]
        )
    except Exception as exc:
        _cancel_sample_child(client, child_gid)
        print("[sample-invalid] {}: {}".format(torrent_path, exc))
        return {"health": "skipped", "outcome": "invalid_metadata"}
    if task is None:
        _cancel_sample_child(client, child_gid)
        print("[sample-skip] {} reason={}".format(torrent_path, skip_reason))
        return {"health": "skipped", "outcome": skip_reason}

    record = sample_video_task(
        task,
        config["sample_dir"],
        config["work_dir"],
        client,
        timeout=config["timeout"],
        idle_timeout=config["idle_timeout"],
        rate_bytes=config["rate_bytes"],
        progress_interval=config["progress_interval"],
        existing_gid=child_gid,
        trigger="metadata",
    )
    append_sample_report(config["report_path"], record)
    print(format_sample_result(record))
    return record


def _monitor_jobs(
    jobs,
    sample_config=None,
    sample_executor=None,
    deferred_sample_futures=None,
):
    pending = dict(jobs)
    submitted_at = time.monotonic()
    activity = {
        gid: {
            "submitted_at": submitted_at,
            "last_print": float("-inf"),
            "downloaded_bytes": 0,
            "max_connections": 0,
            "max_seeders": 0,
            "max_download_speed": 0,
            "metadata_accepted": False,
            "ready_since": None,
            "last_status": {"status": "waiting"},
        }
        for gid in pending
    }
    succeeded = 0
    failed = 0
    overall_deadline = time.monotonic() + STOP_TIMEOUT * 2 + 30
    owns_sample_executor = False
    sample_futures = []
    sample_counts = {"healthy": 0, "unavailable": 0, "inconclusive": 0, "skipped": 0}
    if sample_config and sample_executor is None:
        sample_executor = ThreadPoolExecutor(
            max_workers=max(int(sample_config.get("workers", SAMPLE_WORKERS)), 1),
            thread_name_prefix="torrent-frame",
        )
        owns_sample_executor = True

    while pending and time.monotonic() < overall_deadline:
        now = time.monotonic()
        for gid, (magnet, info_hash, heat) in list(pending.items()):
            path = _torrent_path(info_hash)
            try:
                status = _rpc_call(
                    "aria2.tellStatus",
                    [
                        gid,
                        [
                            "status",
                            "errorCode",
                            "errorMessage",
                            "completedLength",
                            "totalLength",
                            "downloadSpeed",
                            "connections",
                            "numSeeders",
                            "followedBy",
                        ],
                    ],
                )
            except Exception as exc:
                print("[rpc-error] gid={} {}".format(gid, exc))
                continue

            stats = activity[gid]
            stats["last_status"] = status
            stats["downloaded_bytes"] = max(
                stats["downloaded_bytes"], _status_int(status.get("completedLength"))
            )
            stats["max_connections"] = max(
                stats["max_connections"], _status_int(status.get("connections"))
            )
            stats["max_seeders"] = max(
                stats["max_seeders"], _status_int(status.get("numSeeders"))
            )
            stats["max_download_speed"] = max(
                stats["max_download_speed"], _status_int(status.get("downloadSpeed"))
            )
            elapsed = now - stats["submitted_at"]
            if (
                METADATA_PROGRESS_INTERVAL
                and elapsed - stats["last_print"] >= METADATA_PROGRESS_INTERVAL
            ):
                print(
                    "[metadata-progress] {} state={} downloaded={} peers={} seeders={} "
                    "speed={}B/s elapsed={:.1f}s".format(
                        info_hash,
                        status.get("status", "unknown"),
                        _status_int(status.get("completedLength")),
                        _status_int(status.get("connections")),
                        _status_int(status.get("numSeeders")),
                        _status_int(status.get("downloadSpeed")),
                        elapsed,
                    )
                )
                stats["last_print"] = elapsed

            state = status.get("status", "")
            if path.exists():
                if not stats["metadata_accepted"]:
                    accepted = _accept_metadata(
                        magnet,
                        info_hash,
                        path,
                        acquisition_method="announce_heat",
                        announce_heat=heat,
                    )
                    if not accepted:
                        _remove_rpc_job(gid, state)
                        pending.pop(gid)
                        failed += 1
                        continue
                    stats["metadata_accepted"] = True
                    stats["ready_since"] = now

                if not sample_config:
                    _remove_rpc_job(gid, state)
                    pending.pop(gid)
                    succeeded += 1
                    continue

                followed_by = status.get("followedBy") or []
                child_gid = followed_by[-1] if followed_by else None
                child_wait = now - stats["ready_since"]
                if child_gid or state in ("complete", "error", "removed") or child_wait >= 5:
                    if child_gid:
                        # The metadata parent is complete.  Forgetting its
                        # result does not touch the paused payload child.
                        _remove_rpc_job(gid, state)
                        print(
                            "[sample-handoff] {} metadata_gid={} payload_gid={}".format(
                                info_hash, gid, child_gid
                            )
                        )
                    else:
                        # Older/misconfigured aria2 instances may not create a
                        # paused child.  Re-add the freshly saved .torrent as a
                        # bounded sample without waiting for a later batch.
                        _remove_rpc_job(gid, state)
                        print(
                            "[sample-fallback] {} no followedBy payload; re-adding saved metadata".format(
                                info_hash
                            )
                        )
                    sample_futures.append(
                        sample_executor.submit(
                            _sample_metadata_followup,
                            path,
                            child_gid,
                            sample_config,
                        )
                    )
                    pending.pop(gid)
                    succeeded += 1
                continue

            terminal = state in ("complete", "error", "removed")
            timed_out = elapsed > STOP_TIMEOUT + 15
            if terminal or timed_out:
                cause = (
                    "aria2 completed without saved metadata"
                    if state == "complete"
                    else "metadata timeout after {} seconds".format(STOP_TIMEOUT)
                    if timed_out
                    else "aria2 status {} code={}".format(
                        state, status.get("errorCode", "unknown")
                    )
                )
                code, reason = _metadata_failure(stats, status, cause)
                failures, delay = rd.mark_failure(magnet, reason, FAILURE_COOLDOWN)
                print(
                    "[metadata-{}] {}: {}; failures={} retry_in={}s".format(
                        code, info_hash, reason, failures, delay
                    )
                )
                _remove_rpc_job(gid, state)
                pending.pop(gid)
                failed += 1
        if pending:
            time.sleep(RPC_POLL_INTERVAL)

    for gid, (magnet, info_hash, _) in pending.items():
        stats = activity[gid]
        status = stats["last_status"]
        code, reason = _metadata_failure(stats, status, "metadata worker overall timeout")
        _remove_rpc_job(gid, status.get("status", ""))
        failures, delay = rd.mark_failure(magnet, reason, FAILURE_COOLDOWN)
        print(
            "[metadata-{}] {}: {}; failures={} retry_in={}s".format(
                code, info_hash, reason, failures, delay
            )
        )
        failed += 1

    if deferred_sample_futures is not None:
        deferred_sample_futures.extend(sample_futures)
    elif sample_executor:
        if owns_sample_executor:
            sample_executor.shutdown(wait=True)
        for future in sample_futures:
            try:
                result = future.result()
            except Exception as exc:
                print("[sample-worker-error] {}".format(exc))
                sample_counts["unavailable"] += 1
                continue
            health = result.get("health", "unavailable")
            sample_counts[health if health in sample_counts else "unavailable"] += 1
    return succeeded, failed, sample_counts


def _run_magnet_batch(
    sample_enabled,
    sample_config,
    sample_executor=None,
    deferred_sample_futures=None,
):
    """Run one immediate claim/download batch and return its counters."""
    try:
        version = _rpc_call("aria2.getVersion") or {}
    except Exception as exc:
        return {
            "rpc_ready": False,
            "rpc_error": str(exc),
            "candidates": 0,
            "submitted": 0,
        }

    candidates = _prepare_candidates(list(get_magnets()))
    if not candidates:
        return {
            "rpc_ready": True,
            "aria2_version": version.get("version", "unknown"),
            "candidates": 0,
            "submitted": 0,
        }

    jobs = []
    for magnet, info_hash, heat in candidates:
        try:
            gid = exec_rpc(magnet, sample_on_metadata=sample_enabled)
        except Exception as exc:
            rd.mark_retry(magnet, "aria2 submit failed: {}".format(exc), 300)
            print("[submit-error] {}: {}".format(info_hash, exc))
            continue
        jobs.append((gid, (magnet, info_hash, heat)))
        print(
            "[submitted] {} announce_heat={:.3f} gid={}".format(
                info_hash, heat, gid
            )
        )

    empty_samples = {
        "healthy": 0,
        "unavailable": 0,
        "inconclusive": 0,
        "skipped": 0,
    }
    succeeded, failed, sample_counts = (
        _monitor_jobs(
            jobs,
            sample_config=sample_config,
            sample_executor=sample_executor,
            deferred_sample_futures=deferred_sample_futures,
        )
        if jobs
        else (0, 0, empty_samples)
    )
    result = {
        "rpc_ready": True,
        "aria2_version": version.get("version", "unknown"),
        "candidates": len(candidates),
        "submitted": len(jobs),
        "metadata_success": succeeded,
        "metadata_failed": failed,
        "sample_counts": sample_counts,
        "sample_pending": len(deferred_sample_futures or []),
    }
    print(
        "Finished: candidates={} submitted={} metadata_success={} metadata_failed={} "
        "sample_healthy={} sample_unavailable={} sample_inconclusive={} sample_skipped={} "
        "sample_pending={}".format(
            result["candidates"],
            result["submitted"],
            succeeded,
            failed,
            sample_counts["healthy"],
            sample_counts["unavailable"],
            sample_counts["inconclusive"],
            sample_counts["skipped"],
            result["sample_pending"],
        )
    )
    return result


def _harvest_deferred_samples(futures):
    counts = {"healthy": 0, "unavailable": 0, "inconclusive": 0, "skipped": 0}
    remaining = []
    for future in futures:
        if not future.done():
            remaining.append(future)
            continue
        try:
            result = future.result()
        except Exception as exc:
            print("[sample-worker-error] {}".format(exc))
            counts["unavailable"] += 1
            continue
        health = result.get("health", "unavailable")
        counts[health if health in counts else "unavailable"] += 1
    futures[:] = remaining
    return counts


def magnet2torrent(
    sample_on_metadata=None,
    sample_dir=None,
    sample_report=None,
    sample_work_dir=None,
    sample_bytes=DEFAULT_SAMPLE_BYTES,
    sample_timeout=300,
    sample_idle_timeout=120,
    sample_rate_bytes=DEFAULT_RATE_BYTES,
    sample_progress_interval=10,
    sample_workers=SAMPLE_WORKERS,
    watch=False,
    watch_interval=5,
):
    """Acquire hot metadata, optionally polling continuously as announces arrive."""
    Path(SAVE_PATH).mkdir(parents=True, exist_ok=True)
    sample_enabled = (
        SAMPLE_ON_METADATA if sample_on_metadata is None else bool(sample_on_metadata)
    )
    if sample_enabled and not shutil.which("ffmpeg"):
        print("[sample-disabled] ffmpeg 未安装，本批只下载 metadata")
        sample_enabled = False
    sample_config = None
    if sample_enabled:
        sample_config = {
            "sample_dir": sample_dir or os.getenv("MAGNET_DHT_SAMPLE_DIR", "video-samples"),
            "report_path": sample_report
            or os.getenv("MAGNET_DHT_HEALTH_REPORT", "video-health.jsonl"),
            "work_dir": sample_work_dir
            or os.getenv("MAGNET_DHT_SAMPLE_WORK_DIR", ".video-sample-work"),
            "sample_bytes": int(sample_bytes),
            "timeout": int(sample_timeout),
            "idle_timeout": int(sample_idle_timeout),
            "rate_bytes": int(sample_rate_bytes),
            "progress_interval": float(sample_progress_interval),
            "workers": max(int(sample_workers), 1),
        }

    interval = max(float(watch_interval), 0.5)
    last_idle_log = float("-inf")
    rpc_version = None
    deferred_samples = []
    watch_sample_executor = None
    if watch and sample_config:
        watch_sample_executor = ThreadPoolExecutor(
            max_workers=max(int(sample_config["workers"]), 1),
            thread_name_prefix="torrent-frame-watch",
        )
    while True:
        completed_samples = _harvest_deferred_samples(deferred_samples)
        if any(completed_samples.values()):
            print(
                "[sample-summary] healthy={} unavailable={} inconclusive={} skipped={} "
                "pending={}".format(
                    completed_samples["healthy"],
                    completed_samples["unavailable"],
                    completed_samples["inconclusive"],
                    completed_samples["skipped"],
                    len(deferred_samples),
                )
            )
        if watch_sample_executor and len(deferred_samples) >= max(
            int(sample_config["workers"]) * 2, 1
        ):
            now = time.monotonic()
            if now - last_idle_log >= 30:
                print(
                    "[worker-backpressure] pending_samples={}；暂停领取新候选，"
                    "等待抽帧队列释放容量".format(len(deferred_samples))
                )
                last_idle_log = now
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                print("[worker-stopped] 已停止持续热度轮询")
                watch_sample_executor.shutdown(wait=False, cancel_futures=True)
                return {"rpc_ready": True, "candidates": 0, "submitted": 0}
            continue
        result = _run_magnet_batch(
            sample_enabled,
            sample_config,
            sample_executor=watch_sample_executor,
            deferred_sample_futures=deferred_samples if watch else None,
        )
        now = time.monotonic()
        if result["rpc_ready"] and result.get("aria2_version") != rpc_version:
            rpc_version = result.get("aria2_version")
            print("[rpc-ready] aria2 version={}".format(rpc_version))

        if not result["rpc_ready"]:
            if not watch or now - last_idle_log >= 30:
                print(
                    "[rpc-unavailable] aria2 RPC 不可用，未领取候选: {}".format(
                        result["rpc_error"]
                    )
                )
                last_idle_log = now
        elif result["candidates"] == 0:
            if not watch or now - last_idle_log >= 30:
                message = "当前没有可领取的 announce-hot 候选"
                if watch:
                    message += "；worker 会继续轮询，不需要等待整点"
                print("[worker-idle] {}".format(message))
                last_idle_log = now

        if not watch:
            return result
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("[worker-stopped] 已停止持续热度轮询")
            if watch_sample_executor:
                watch_sample_executor.shutdown(wait=False, cancel_futures=True)
            return result
