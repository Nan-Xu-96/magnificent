#!/usr/bin/env python
# coding=utf-8

import os
import time

import redis

# redis key
REDIS_KEY = "magnets"
REDIS_INVALID_KEY = "magnets:invalid"
REDIS_METADATA_VALID_KEY = "magnets:metadata_valid"
REDIS_RETRY_AFTER_KEY = "magnets:retry_after"
REDIS_LAST_ERROR_KEY = "magnets:last_error"
REDIS_LAST_SEEN_KEY = "magnets:last_seen"
REDIS_LAST_ANNOUNCE_KEY = "magnets:last_announce"
REDIS_SEEN_COUNT_KEY = "magnets:seen_count"
REDIS_ANNOUNCE_COUNT_KEY = "magnets:announce_count"
REDIS_ANNOUNCE_HEAT_PREFIX = "magnets:announce_hour"
REDIS_ANNOUNCED_PEER_PREFIX = "magnets:announce_peers"
REDIS_PROCESSED_KEY = "magnets:processed"
REDIS_FAILURE_COUNT_KEY = "magnets:failure_count"
REDIS_COLD_KEY = "magnets:cold"
ANNOUNCE_BUCKET_SECONDS = 3600
ANNOUNCE_BUCKET_TTL = 48 * 3600
ANNOUNCED_PEER_TTL = int(os.getenv("MAGNET_DHT_PEER_TTL", "1800"))
ANNOUNCED_PEERS_PER_HASH = int(os.getenv("MAGNET_DHT_PEERS_PER_HASH", "64"))
# redis 地址
REDIS_HOST = "localhost"
# redis 端口
REDIS_PORT = 6379
# redis 密码
REDIS_PASSWORD = None
# redis 连接池最大连接量
REDIS_MAX_CONNECTION = 20


class RedisClient:
    def __init__(self, host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD):
        conn_pool = redis.ConnectionPool(
            host=host,
            port=port,
            password=password,
            max_connections=REDIS_MAX_CONNECTION,
        )
        self.redis = redis.Redis(connection_pool=conn_pool)

    def add_magnet(self, magnet, source="get_peers"):
        """
        新增磁力链接，并记录观测时间和来源。

        announce_peer 比 get_peers 更能说明近期存在活跃 peer，
        因此候选磁链会优先从 announce_peer 中选取。
        """
        now = time.time()
        pipe = self.redis.pipeline(transaction=False)
        pipe.sadd(REDIS_KEY, magnet)
        pipe.zadd(REDIS_LAST_SEEN_KEY, {magnet: now})
        pipe.hincrby(REDIS_SEEN_COUNT_KEY, magnet, 1)
        if source == "announce_peer":
            pipe.zadd(REDIS_LAST_ANNOUNCE_KEY, {magnet: now})
            pipe.hincrby(REDIS_ANNOUNCE_COUNT_KEY, magnet, 1)
            # Update the current bucket immediately.  ``processed`` is not a
            # reason to discard new activity: successful/invalid hashes are
            # filtered at claim time, while failed hashes need fresh announce
            # evidence so they can be retried after their cooldown.
            heat_key = self._announce_heat_key(int(now // ANNOUNCE_BUCKET_SECONDS))
            pipe.zincrby(heat_key, 1, magnet)
            pipe.expire(heat_key, ANNOUNCE_BUCKET_TTL)
        pipe.execute()

    @staticmethod
    def _announce_heat_key(hour_bucket):
        return "{}:{}".format(REDIS_ANNOUNCE_HEAT_PREFIX, hour_bucket)

    @staticmethod
    def _announced_peer_key(info_hash):
        if isinstance(info_hash, bytes):
            info_hash = info_hash.hex()
        return "{}:{}".format(REDIS_ANNOUNCED_PEER_PREFIX, str(info_hash).lower())

    def add_announced_peer(
        self,
        info_hash,
        ip,
        port,
        now=None,
        ttl_seconds=ANNOUNCED_PEER_TTL,
        max_peers=ANNOUNCED_PEERS_PER_HASH,
    ):
        """Remember a token-validated DHT peer for a short, bounded period.

        The sorted-set score is the most recent ``announce_peer`` timestamp.
        The key TTL and per-hash cap prevent the crawler from accumulating a
        permanent peer-address database.
        """
        now = time.time() if now is None else float(now)
        ttl_seconds = max(int(ttl_seconds), 1)
        max_peers = max(int(max_peers), 1)
        key = self._announced_peer_key(info_hash)
        endpoint = "{}:{}".format(ip, int(port))

        pipe = self.redis.pipeline(transaction=False)
        pipe.zadd(key, {endpoint: now})
        pipe.zremrangebyscore(key, "-inf", now - ttl_seconds)
        pipe.expire(key, ttl_seconds)
        pipe.execute()

        # Keep only the most recently announced endpoints. The crawler runs
        # in multiple processes, so this is an intentionally approximate cap;
        # the Redis key TTL remains the hard memory bound.
        excess = self.redis.zcard(key) - max_peers
        if excess > 0:
            self.redis.zremrangebyrank(key, 0, excess - 1)
        return endpoint

    def get_announced_peers(
        self, info_hash, now=None, ttl_seconds=ANNOUNCED_PEER_TTL, limit=None
    ):
        """Return fresh peers newest-first for a future metadata downloader."""
        now = time.time() if now is None else float(now)
        ttl_seconds = max(int(ttl_seconds), 1)
        key = self._announced_peer_key(info_hash)
        self.redis.zremrangebyscore(key, "-inf", now - ttl_seconds)
        end = -1 if limit is None else max(int(limit), 0) - 1
        if end == -1 and limit is not None:
            return []
        peers = []
        for raw_endpoint, last_seen in self.redis.zrevrange(
            key, 0, end, withscores=True
        ):
            endpoint = self._decode(raw_endpoint)
            ip, raw_port = endpoint.rsplit(":", 1)
            peers.append(
                {
                    "ip": ip,
                    "port": int(raw_port),
                    "last_seen": float(last_seen),
                }
            )
        return peers

    @staticmethod
    def _decode(value):
        if isinstance(value, bytes):
            return value.decode()
        return value

    def get_hot_announce_candidates(
        self,
        count=8,
        window_hours=6,
        half_life_hours=2.0,
        once_only=True,
        now=None,
    ):
        """
        Claim candidates ranked only by locally observed ``announce_peer`` heat.

        Counts are stored in hourly buckets. Older buckets are exponentially
        discounted, so recent repeated announces outrank old activity. In
        once-only mode a claimed hash is permanently excluded from later runs.
        """
        if count <= 0 or window_hours <= 0 or half_life_hours <= 0:
            return []

        now = time.time() if now is None else now
        current_bucket = int(now // ANNOUNCE_BUCKET_SECONDS)
        heat_keys = [
            self._announce_heat_key(current_bucket - age)
            for age in range(window_hours)
        ]
        scores = {}
        scan_count = max(count * 64, 512)
        for age, heat_key in enumerate(heat_keys):
            weight = 0.5 ** (age / half_life_hours)
            for raw_magnet, bucket_count in self.redis.zrevrange(
                heat_key, 0, scan_count - 1, withscores=True
            ):
                magnet = self._decode(raw_magnet)
                scores[magnet] = scores.get(magnet, 0.0) + bucket_count * weight

        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        if not ranked:
            return []

        pipe = self.redis.pipeline(transaction=False)
        for magnet, _ in ranked:
            pipe.sismember(REDIS_METADATA_VALID_KEY, magnet)
            pipe.sismember(REDIS_INVALID_KEY, magnet)
            pipe.zscore(REDIS_RETRY_AFTER_KEY, magnet)
            pipe.sismember(REDIS_PROCESSED_KEY, magnet)
            pipe.hget(REDIS_LAST_ERROR_KEY, magnet)
        states = pipe.execute()

        selected = []
        cleanup = []
        clear_legacy_tracker_miss = []
        for index, (magnet, heat) in enumerate(ranked):
            valid, invalid, retry_after, processed, last_error = states[
                index * 5 : index * 5 + 5
            ]
            last_error = self._decode(last_error)
            if last_error == "tracker_miss_deferred":
                # Older releases wrote public-tracker misses into retry state.
                # They must not influence the announce-only selection model.
                retry_after = None
                clear_legacy_tracker_miss.append(magnet)
            if valid or invalid:
                cleanup.append(magnet)
                continue
            if once_only and (processed or retry_after is not None):
                cleanup.append(magnet)
                continue
            if not once_only and retry_after is not None and retry_after > now:
                continue
            if once_only and not self.redis.sadd(REDIS_PROCESSED_KEY, magnet):
                cleanup.append(magnet)
                continue
            selected.append((magnet, heat))
            if once_only:
                cleanup.append(magnet)
            if len(selected) >= count:
                break

        if cleanup:
            pipe = self.redis.pipeline(transaction=False)
            for magnet in cleanup:
                for heat_key in heat_keys:
                    pipe.zrem(heat_key, magnet)
            pipe.execute()
        if clear_legacy_tracker_miss:
            pipe = self.redis.pipeline(transaction=False)
            for magnet in clear_legacy_tracker_miss:
                pipe.zrem(REDIS_RETRY_AFTER_KEY, magnet)
                pipe.hdel(REDIS_LAST_ERROR_KEY, magnet)
            pipe.execute()
        return selected

    def mark_invalid(self, magnet, reason):
        pipe = self.redis.pipeline(transaction=False)
        pipe.sadd(REDIS_PROCESSED_KEY, magnet)
        pipe.sadd(REDIS_INVALID_KEY, magnet)
        pipe.hset(REDIS_LAST_ERROR_KEY, magnet, reason)
        pipe.zrem(REDIS_RETRY_AFTER_KEY, magnet)
        pipe.execute()

    def mark_retry(self, magnet, reason, delay_seconds):
        retry_after = time.time() + delay_seconds
        pipe = self.redis.pipeline(transaction=False)
        pipe.sadd(REDIS_PROCESSED_KEY, magnet)
        pipe.zadd(REDIS_RETRY_AFTER_KEY, {magnet: retry_after})
        pipe.hset(REDIS_LAST_ERROR_KEY, magnet, reason)
        pipe.execute()

    def mark_failure(
        self,
        magnet,
        reason,
        base_delay_seconds=21600,
        max_delay_seconds=2592000,
    ):
        """Record a real metadata failure and apply capped exponential backoff."""
        failures = self.redis.hincrby(REDIS_FAILURE_COUNT_KEY, magnet, 1)
        exponent = min(max(failures - 1, 0), 16)
        delay = min(base_delay_seconds * (2**exponent), max_delay_seconds)
        retry_after = time.time() + delay
        pipe = self.redis.pipeline(transaction=False)
        pipe.sadd(REDIS_PROCESSED_KEY, magnet)
        pipe.zadd(REDIS_RETRY_AFTER_KEY, {magnet: retry_after})
        pipe.hset(REDIS_LAST_ERROR_KEY, magnet, reason)
        if failures >= 3:
            pipe.sadd(REDIS_COLD_KEY, magnet)
        pipe.execute()
        return failures, delay

    def mark_metadata_valid(self, magnet):
        pipe = self.redis.pipeline(transaction=False)
        pipe.sadd(REDIS_PROCESSED_KEY, magnet)
        pipe.sadd(REDIS_METADATA_VALID_KEY, magnet)
        pipe.zrem(REDIS_RETRY_AFTER_KEY, magnet)
        pipe.hdel(REDIS_LAST_ERROR_KEY, magnet)
        pipe.hdel(REDIS_FAILURE_COUNT_KEY, magnet)
        pipe.srem(REDIS_COLD_KEY, magnet)
        pipe.execute()
