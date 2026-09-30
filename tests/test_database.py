import unittest

from magnet_dht import database


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.results = []

    def zadd(self, key, values):
        self.redis.zsets.setdefault(key, {}).update(values)
        return self

    def zincrby(self, key, amount, value):
        values = self.redis.zsets.setdefault(key, {})
        values[value] = values.get(value, 0) + amount
        return self

    def zrem(self, key, value):
        self.redis.zsets.setdefault(key, {}).pop(value, None)
        return self

    def zremrangebyscore(self, key, minimum, maximum):
        self.redis.zremrangebyscore(key, minimum, maximum)
        return self

    def zscore(self, key, value):
        self.results.append(self.redis.zsets.get(key, {}).get(value))
        return self

    def hset(self, key, field, value):
        self.redis.hashes.setdefault(key, {})[field] = value
        return self

    def hget(self, key, field):
        self.results.append(self.redis.hashes.get(key, {}).get(field))
        return self

    def hdel(self, key, field):
        self.redis.hashes.setdefault(key, {}).pop(field, None)
        return self

    def hincrby(self, key, field, amount):
        self.redis.hincrby(key, field, amount)
        return self

    def sadd(self, key, value):
        self.redis.sadd(key, value)
        return self

    def srem(self, key, value):
        self.redis.sets.setdefault(key, set()).discard(value)
        return self

    def sismember(self, key, value):
        self.results.append(self.redis.sismember(key, value))
        return self

    def expire(self, key, seconds):
        self.redis.expirations[key] = seconds
        return self

    def execute(self):
        return self.results


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.zsets = {}
        self.sets = {}
        self.expirations = {}

    def hincrby(self, key, field, amount):
        values = self.hashes.setdefault(key, {})
        values[field] = values.get(field, 0) + amount
        return values[field]

    def sadd(self, key, value):
        values = self.sets.setdefault(key, set())
        added = value not in values
        values.add(value)
        return int(added)

    def sismember(self, key, value):
        return value in self.sets.get(key, set())

    def zrevrange(self, key, start, end, withscores=False):
        values = sorted(
            self.zsets.get(key, {}).items(),
            key=lambda item: (-item[1], item[0]),
        )
        selected = values[start:] if end == -1 else values[start : end + 1]
        return selected if withscores else [value for value, _ in selected]

    def zremrangebyscore(self, key, minimum, maximum):
        minimum = float("-inf") if minimum == "-inf" else float(minimum)
        maximum = float("inf") if maximum == "+inf" else float(maximum)
        values = self.zsets.setdefault(key, {})
        removed = [
            value for value, score in values.items() if minimum <= score <= maximum
        ]
        for value in removed:
            values.pop(value)
        return len(removed)

    def zcard(self, key):
        return len(self.zsets.get(key, {}))

    def zremrangebyrank(self, key, start, end):
        values = sorted(self.zsets.get(key, {}).items(), key=lambda item: item[1])
        selected = values[start : end + 1]
        for value, _ in selected:
            self.zsets[key].pop(value)
        return len(selected)

    def pipeline(self, transaction=False):
        return FakePipeline(self)


class DatabaseTests(unittest.TestCase):
    def make_client(self):
        client = object.__new__(database.RedisClient)
        client.redis = FakeRedis()
        return client

    def test_metadata_failures_back_off_and_become_cold(self):
        client = self.make_client()

        self.assertEqual((1, 10), client.mark_failure("m", "timeout", 10, 1000))
        self.assertEqual((2, 20), client.mark_failure("m", "timeout", 10, 1000))
        self.assertEqual((3, 40), client.mark_failure("m", "timeout", 10, 1000))
        self.assertIn("m", client.redis.sets[database.REDIS_COLD_KEY])

    def test_recent_announce_heat_is_ranked_and_claimed_once(self):
        client = self.make_client()
        now = 10 * database.ANNOUNCE_BUCKET_SECONDS
        current = client._announce_heat_key(10)
        previous = client._announce_heat_key(9)
        client.redis.zsets[current] = {"m1": 2, "m2": 3}
        client.redis.zsets[previous] = {"m1": 4}

        selected = client.get_hot_announce_candidates(
            count=2,
            window_hours=2,
            half_life_hours=1,
            once_only=True,
            now=now,
        )

        self.assertEqual(["m1", "m2"], [magnet for magnet, _ in selected])
        self.assertAlmostEqual(4.0, selected[0][1])
        self.assertEqual(
            {"m1", "m2"}, client.redis.sets[database.REDIS_PROCESSED_KEY]
        )
        self.assertEqual(
            [],
            client.get_hot_announce_candidates(
                count=2,
                window_hours=2,
                half_life_hours=1,
                once_only=True,
                now=now,
            ),
        )

    def test_get_peers_does_not_increase_announce_heat(self):
        client = self.make_client()
        client.add_magnet("m1", source="get_peers")
        self.assertFalse(
            any(key.startswith(database.REDIS_ANNOUNCE_HEAT_PREFIX) for key in client.redis.zsets)
        )

    def test_new_announce_refreshes_heat_even_after_previous_attempt(self):
        client = self.make_client()
        client.redis.sets[database.REDIS_PROCESSED_KEY] = {"m1"}

        client.add_magnet("m1", source="announce_peer")

        heat_values = [
            values["m1"]
            for key, values in client.redis.zsets.items()
            if key.startswith(database.REDIS_ANNOUNCE_HEAT_PREFIX)
        ]
        self.assertEqual([1], heat_values)

    def test_announced_peers_have_timestamp_ttl_and_per_hash_cap(self):
        client = self.make_client()
        info_hash = "a" * 40

        client.add_announced_peer(
            info_hash, "192.0.2.1", 6001, now=100, ttl_seconds=30, max_peers=2
        )
        client.add_announced_peer(
            info_hash, "192.0.2.2", 6002, now=101, ttl_seconds=30, max_peers=2
        )
        client.add_announced_peer(
            info_hash, "192.0.2.3", 6003, now=102, ttl_seconds=30, max_peers=2
        )

        key = client._announced_peer_key(info_hash)
        self.assertEqual(30, client.redis.expirations[key])
        self.assertEqual(
            [
                {"ip": "192.0.2.3", "port": 6003, "last_seen": 102.0},
                {"ip": "192.0.2.2", "port": 6002, "last_seen": 101.0},
            ],
            client.get_announced_peers(info_hash, now=102, ttl_seconds=30),
        )

        self.assertEqual(
            [], client.get_announced_peers(info_hash, now=132, ttl_seconds=30)
        )

    def test_legacy_tracker_miss_does_not_block_new_announce(self):
        client = self.make_client()
        now = 20 * database.ANNOUNCE_BUCKET_SECONDS
        heat_key = client._announce_heat_key(20)
        client.redis.zsets[heat_key] = {"m1": 5}
        client.redis.zsets[database.REDIS_RETRY_AFTER_KEY] = {"m1": now + 9999}
        client.redis.hashes[database.REDIS_LAST_ERROR_KEY] = {
            "m1": "tracker_miss_deferred"
        }

        selected = client.get_hot_announce_candidates(count=1, now=now)

        self.assertEqual(["m1"], [magnet for magnet, _ in selected])
        self.assertNotIn(
            "m1", client.redis.zsets[database.REDIS_RETRY_AFTER_KEY]
        )


if __name__ == "__main__":
    unittest.main()
