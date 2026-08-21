import tempfile
import unittest

from reviewbot.accounting import Ledger
from reviewbot.claude import Tags
from reviewbot.memory import (
    MAX_TEXT_LEN,
    REPO_CONVENTION,
    REVIEW_FINDING,
    AgentMemoryClient,
    Memory,
    NotVisible,
    PartialWrite,
    memory_id,
    scoped_filter,
    validate_namespace,
)
from tests.fakes import FakeMemoryService

NS = "redis-py-run-3"


def conv(mid, text, module, **attrs):
    return Memory(
        id=mid,
        text=text,
        memory_type=REPO_CONVENTION,
        namespace=NS,
        owner_id="memory-agent",
        topics=["convention"],
        attributes={"module": module, **attrs},
    )


def client(service, ledger=None, namespace=NS):
    return AgentMemoryClient(
        "store-1",
        base_url="https://example.invalid",
        api_key="k",
        namespace=namespace,
        ledger=ledger,
        transport=service.transport,
    )


class TestIdentifiers(unittest.TestCase):
    def test_ids_are_deterministic_and_pattern_legal(self):
        a = memory_id("conv", "redis/connection.py", "socket-ownership")
        self.assertEqual(a, memory_id("conv", "redis/connection.py", "socket-ownership"))
        self.assertEqual(a, "conv-redis-connection-py-socket-ownership")

    def test_long_ids_hash_instead_of_colliding(self):
        a = memory_id("conv", "redis/asyncio/cluster.py", "x" * 60)
        b = memory_id("conv", "redis/cluster.py", "x" * 60)
        self.assertNotEqual(a, b)
        self.assertLessEqual(len(a), 64)
        self.assertLessEqual(len(b), 64)

    def test_the_design_docs_namespace_example_is_illegal(self):
        # The spec writes `repo-x/run-3`; the service pattern forbids the slash.
        with self.assertRaises(ValueError):
            validate_namespace("repo-x/run-3")
        self.assertEqual(validate_namespace("repo-x-run-3"), "repo-x-run-3")

    def test_oversized_text_is_rejected_with_advice(self):
        with self.assertRaises(ValueError) as ctx:
            conv("c1", "x" * (MAX_TEXT_LEN + 1), "redis/connection.py")
        self.assertIn("Split the fact", str(ctx.exception))


class TestScopedFilter(unittest.TestCase):
    def test_namespace_uses_eq_not_ne(self):
        f = scoped_filter(NS, modules=["a.py"])
        self.assertEqual(f["namespace"], {"eq": NS})

    def test_modules_or_within_one_clause(self):
        f = scoped_filter(NS, memory_types=[REPO_CONVENTION, REVIEW_FINDING], modules=["a.py", "b.py"])
        self.assertEqual(f["attributes"]["module"], {"in": ["a.py", "b.py"]})
        self.assertEqual(f["memoryType"], {"in": [REPO_CONVENTION, REVIEW_FINDING]})


class TestCreateAndSearch(unittest.TestCase):
    def test_scoped_search_excludes_other_namespaces_and_modules(self):
        svc = FakeMemoryService()
        c = client(svc)
        c.create(
            [
                conv("c-conn", "connection.py owns the socket lifecycle", "redis/connection.py"),
                conv("c-clust", "cluster.py maps slots to nodes", "redis/cluster.py"),
            ]
        )
        other = client(svc, namespace="redis-py-run-4")
        other.create(
            [
                Memory(
                    id="c-other",
                    text="connection.py owns the socket lifecycle",
                    memory_type=REPO_CONVENTION,
                    namespace="redis-py-run-4",
                    owner_id="memory-agent",
                    attributes={"module": "redis/connection.py"},
                )
            ]
        )
        found, _ = c.search(
            "socket lifecycle ownership",
            filter=scoped_filter(NS, memory_types=[REPO_CONVENTION], modules=["redis/connection.py"]),
            limit=10,
        )
        # filterOp `all` keeps the other run out. `any` would have returned it.
        self.assertEqual([m.id for m in found], ["c-conn"])

    def test_partial_write_failure_raises(self):
        svc = FakeMemoryService(reject_ids={"c-bad"})
        c = client(svc)
        with self.assertRaises(PartialWrite) as ctx:
            c.create([conv("c-ok", "fine", "a.py"), conv("c-bad", "also fine", "b.py")])
        self.assertEqual(ctx.exception.created, ["c-ok"])
        self.assertEqual(len(ctx.exception.errors), 1)

    def test_create_batches_at_one_hundred(self):
        svc = FakeMemoryService()
        c = client(svc)
        c.create([conv(f"c-{i:03d}", f"fact {i}", "a.py") for i in range(150)])
        creates = [call for call in svc.calls if call[:2] == ("POST", "long-term-memory")]
        self.assertEqual([len(call[2]["memories"]) for call in creates], [100, 50])

    def test_search_limit_is_bounded(self):
        c = client(FakeMemoryService())
        with self.assertRaises(ValueError):
            c.search("x", limit=101)

    def test_idempotent_rewrite_keeps_one_record(self):
        svc = FakeMemoryService()
        c = client(svc)
        record = conv("c-conn", "connection.py owns the socket", "redis/connection.py")
        c.create([record])
        c.create([record])  # re-priming the same repo
        self.assertEqual(len(svc.records), 1)


class TestGuardedUpdate(unittest.TestCase):
    def test_field_update_applies_within_the_namespace(self):
        svc = FakeMemoryService()
        c = client(svc)
        c.create([conv("c-conn", "old convention", "redis/connection.py", convention_version=1)])
        c.patch_fields(
            "c-conn",
            memory_type=REPO_CONVENTION,
            text="new convention",
            attributes={"module": "redis/connection.py", "convention_version": 2},
        )
        self.assertEqual(svc.records["c-conn"]["text"], "new convention")
        self.assertEqual(svc.records["c-conn"]["attributes"]["convention_version"], 2)

    def test_guard_blocks_a_cross_namespace_write(self):
        svc = FakeMemoryService()
        client(svc).create([conv("c-conn", "text", "a.py")])
        intruder = client(svc, namespace="redis-py-run-9")
        with self.assertRaises(Exception) as ctx:
            intruder.patch_fields("c-conn", memory_type=REPO_CONVENTION, text="hijacked")
        self.assertIn("409", str(ctx.exception))
        self.assertEqual(svc.records["c-conn"]["text"], "text")


class TestWriteVisibility(unittest.TestCase):
    def test_waits_until_records_are_readable(self):
        svc = FakeMemoryService(visibility_lag=2)
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(tmp, "run-m")
            c = client(svc, ledger)
            tags = Tags("memory", "pr-1", 1, "write")
            ids = c.create([conv("c-a", "a", "a.py")], tags)
            waited = c.wait_for_visibility(ids, timeout=5, interval=0, tags=tags)
            records = list(ledger.records())
        self.assertGreaterEqual(waited, 0.0)
        wait_rec = [r for r in records if r.memory_op == "wait"][0]
        # The wait is logged and explicitly excluded from the latency metric.
        self.assertTrue(wait_rec.notes["excluded_from_latency"])
        self.assertFalse(wait_rec.billable)

    def test_timeout_raises_rather_than_reviewing_a_stale_store(self):
        svc = FakeMemoryService(visibility_lag=10_000)
        c = client(svc)
        ids = c.create([conv("c-a", "a", "a.py")])
        with self.assertRaises(NotVisible):
            c.wait_for_visibility(ids, timeout=0.05, interval=0)


class TestLedgerIntegration(unittest.TestCase):
    def test_memory_ops_are_logged_as_unbilled_rows(self):
        svc = FakeMemoryService()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(tmp, "run-m")
            c = client(svc, ledger)
            tags = Tags("memory", "pr-2", 2, "retrieve")
            c.create([conv("c-a", "socket ownership", "a.py")], tags)
            c.search("socket", filter=scoped_filter(NS), limit=5, similarity_threshold=0.7, tags=tags)
            records = list(ledger.records())
        ops = {r.memory_op for r in records}
        self.assertEqual(ops, {"create", "search"})
        search = [r for r in records if r.memory_op == "search"][0]
        self.assertEqual((search.search_limit, search.similarity_threshold), (5, 0.7))
        self.assertEqual(search.memories_returned, 1)
        self.assertTrue(all(not r.billable for r in records))


class TestMissingCredentials(unittest.TestCase):
    def test_absent_url_is_a_clear_error(self):
        c = AgentMemoryClient("store-1", base_url="", api_key="k", namespace=NS)
        with self.assertRaises(Exception) as ctx:
            c.search("x")
        self.assertIn("REDIS_AGENT_MEMORY_URL", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
