"""Scaling units: next_id layout, image shards, cache fallback, pooler/replicas."""
import os
import unittest

from tests.bootstrap import ensure_test_env

ensure_test_env()


class NextIdTests(unittest.TestCase):
    def test_SC_01_layout_roundtrip(self):
        from app.ids import (MAX_SEQUENCE, MAX_SHARD, NextIdGenerator,
                             decode, encode)
        stamp = 1735689600123  # 2025-01-01T00:00:00.123Z
        value = encode(stamp, 7, 9)
        parts = decode(value)
        self.assertEqual(parts["timestamp_ms"], stamp)
        self.assertEqual(parts["shard_id"], 7)
        self.assertEqual(parts["sequence"], 9)
        # Bit widths: 41-13-10.
        self.assertLessEqual(value >> 23, (1 << 41) - 1)
        self.assertEqual((value >> 10) & MAX_SHARD, 7)
        self.assertEqual(value & MAX_SEQUENCE, 9)

    def test_SC_02_bounds_rejected(self):
        from app.ids import MAX_SEQUENCE, MAX_SHARD, encode
        with self.assertRaises(ValueError):
            encode(1735689600123, MAX_SHARD + 1, 0)
        with self.assertRaises(ValueError):
            encode(1735689600123, 0, MAX_SEQUENCE + 1)
        with self.assertRaises(ValueError):
            decode_out_of_range = __import__("app.ids", fromlist=["decode"]).decode
            decode_out_of_range(1 << 64)

    def test_SC_03_sorted_and_unique(self):
        from app.ids import NextIdGenerator, is_sorted
        gen = NextIdGenerator(shard_id=3)
        ids = gen.next_ids(2000)
        self.assertEqual(len(set(ids)), 2000)
        self.assertTrue(is_sorted(ids))
        # Shard tag survives the round trip.
        from app.ids import decode
        self.assertTrue(all(decode(v)["shard_id"] == 3 for v in ids))

    def test_SC_04_sequence_overflow_waits(self):
        import itertools
        from app.ids import MAX_SEQUENCE, NextIdGenerator, decode
        ticks = itertools.chain([1735689600.0] * (MAX_SEQUENCE + 5),
                                itertools.count(1735689600.001, 0.001))
        gen = NextIdGenerator(shard_id=1, clock=lambda: next(ticks))
        ids = gen.next_ids(MAX_SEQUENCE + 5)
        self.assertEqual(len(set(ids)), MAX_SEQUENCE + 5)
        stamps = [decode(v)["timestamp_ms"] for v in ids]
        self.assertGreater(max(stamps), min(stamps))


class ImageShardTests(unittest.TestCase):
    def test_SC_10_deterministic_shard(self):
        from app.image_shards import shard_for
        self.assertEqual(shard_for("run:1:step:3"), shard_for("run:1:step:3"))
        self.assertLess(shard_for("anything", total=256), 256)

    def test_SC_11_machine_map_routing(self):
        from app.image_shards import MachineMap
        mapping = {"0-127": "img-01:/data/a", "128-255": "img-02:/data/b"}
        machine_map = MachineMap.from_mapping(mapping, 256)
        self.assertEqual(machine_map.machine_for(0).name, "img-01")
        self.assertEqual(machine_map.machine_for(255).name, "img-02")
        self.assertEqual(len(machine_map.describe()), 2)

    def test_SC_12_map_gaps_and_overlaps_rejected(self):
        from app.image_shards import MachineMap
        with self.assertRaises(ValueError):
            MachineMap.from_mapping({"0-100": "a"}, 256)  # gap
        with self.assertRaises(ValueError):
            MachineMap.from_mapping({"0-200": "a", "100-255": "b"}, 256)  # overlap

    def test_SC_13_store_roundtrip(self):
        import tempfile
        from app.image_shards import ImageShardStore, MachineMap
        with tempfile.TemporaryDirectory() as root:
            store = ImageShardStore(
                machine_map=MachineMap.from_mapping({"0-255": "local"}, 256),
                root=root)
            record = store.save("run:9:step:1", "step-1.jpg", b"\xff\xd8data")
            self.assertEqual(record["shard"], store.shard_of("run:9:step:1"))
            self.assertTrue(store.exists("run:9:step:1", "step-1.jpg"))
            self.assertEqual(store.load("run:9:step:1", "step-1.jpg"), b"\xff\xd8data")
            self.assertTrue(store.delete("run:9:step:1", "step-1.jpg"))
            self.assertIsNone(store.load("run:9:step:1", "step-1.jpg"))


class CacheTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("TF_MEMCACHED_SERVERS", None)
        os.environ["TF_CACHE_ENABLED"] = "1"
        from app import cache
        cache.clear_memory()

    def test_SC_20_memory_fallback_roundtrip(self):
        from app import cache
        self.assertIsNone(cache.get("nope"))
        cache.set("k", {"a": 1}, ttl=60)
        self.assertEqual(cache.get("k"), {"a": 1})
        cache.delete("k")
        self.assertIsNone(cache.get("k"))

    def test_SC_21_ttl_expiry(self):
        import time
        from app import cache
        cache.set("short", "v", ttl=1)
        self.assertEqual(cache.get("short"), "v")
        time.sleep(1.1)
        self.assertIsNone(cache.get("short"))

    def test_SC_22_get_or_load_and_decorator(self):
        from app import cache
        calls = []
        first = cache.get_or_load("heavy", lambda: calls.append(1) or "done")
        second = cache.get_or_load("heavy", lambda: calls.append(1) or "done")
        self.assertEqual((first, second), ("done", "done"))
        self.assertEqual(len(calls), 1)

        @cache.cached(ttl=60, namespace="t22")
        def add(a, b):
            calls.append(1)
            return a + b
        self.assertEqual(add(1, 2), 3)
        self.assertEqual(add(1, 2), 3)
        self.assertEqual(len(calls), 2)

    def test_SC_23_delete_prefix(self):
        from app import cache
        cache.set(cache.make_key("ns", "a"), 1)
        cache.set(cache.make_key("ns", "b"), 2)
        cache.set(cache.make_key("other", "c"), 3)
        dropped = cache.delete_prefix(cache.prefix() + "ns:")
        self.assertEqual(dropped, 2)
        self.assertIsNotNone(cache.get(cache.make_key("other", "c")))


class PoolAndReplicaTests(unittest.TestCase):
    def test_SC_30_pgbouncer_detection(self):
        from app.db import is_pgbouncer_url
        saved = dict(os.environ)
        try:
            os.environ.pop("TF_PGBOUNCER", None)
            os.environ.pop("TF_PGBOUNCER_ENABLED", None)
            self.assertTrue(is_pgbouncer_url(
                "postgresql://u:p@host:6439/db"))
            self.assertFalse(is_pgbouncer_url(
                "postgresql://u:p@host:5432/db"))
            os.environ["TF_PGBOUNCER"] = "1"
            self.assertTrue(is_pgbouncer_url(
                "postgresql://u:p@host:5432/db"))
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_SC_31_pgbouncer_engine_uses_nullpool(self):
        from sqlalchemy.pool import NullPool, QueuePool
        from app.db import create_postgres_engine
        saved = dict(os.environ)
        try:
            os.environ.pop("TF_PGBOUNCER", None)
            os.environ.pop("TF_PGBOUNCER_ENABLED", None)
            direct = create_postgres_engine("postgresql://u:p@h:5432/db")
            pooled = create_postgres_engine("postgresql://u:p@h:6439/db")
            self.assertIsInstance(direct.pool, QueuePool)
            self.assertIsInstance(pooled.pool, NullPool)
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_SC_32_replica_parsing_no_replicas(self):
        from app import db_replicas
        saved = dict(os.environ)
        try:
            os.environ.pop("TF_READ_REPLICAS", None)
            db_replicas.reset()
            self.assertEqual(db_replicas.replica_urls(), [])
            from app import db as db_module
            self.assertIs(db_replicas.get_read_engine(), db_module.engine)
        finally:
            os.environ.clear()
            os.environ.update(saved)
            db_replicas.reset()

    def test_SC_33_scaling_endpoint(self):
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        response = client.get("/api/scaling")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        for section in ("pool", "replicas", "cache", "images", "ids"):
            self.assertIn(section, body)


if __name__ == "__main__":
    unittest.main()
