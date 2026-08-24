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
    MemoryError_,
    memory_id,
    module_topics,
    resolve_module,
    resolve_modules,
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
        # Mirrors the agents: the module path is a topic, because topics carry
        # the only membership filter the service offers.
        topics=["convention", module],
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

    def test_modules_or_within_one_topics_clause(self):
        f = scoped_filter(NS, memory_types=[REPO_CONVENTION, REVIEW_FINDING], modules=["b.py", "a.py"])
        # Modules route through `topics`, not `attributes`: an attribute clause
        # is a typed union with no membership operator at all (verified live).
        self.assertEqual(f["topics"], {"in": ["a.py", "b.py"]})
        self.assertNotIn("attributes", f)
        self.assertEqual(f["memoryType"], {"in": [REPO_CONVENTION, REVIEW_FINDING]})

    def test_attribute_extras_get_the_typed_clause(self):
        f = scoped_filter(NS, attributes={"finding_class": "resource-leak", "pr_ordinal": 3})
        self.assertEqual(f["attributes"]["finding_class"], {"string": "resource-leak"})
        self.assertEqual(f["attributes"]["pr_ordinal"], {"number": 3})

    def test_service_rejects_a_membership_operator_on_an_attribute(self):
        """The regression guard for the bug this shape caused.

        An earlier draft filtered modules with `attributes.module: {in: [...]}`.
        The live service answers that with a 400, and every retrieval in the run
        failed. The fake now models the same rejection, so the suite catches it.
        """
        svc = FakeMemoryService()
        c = client(svc)
        c.create([conv("c-conn", "connection.py owns the socket lifecycle", "redis/connection.py")])
        with self.assertRaises(MemoryError_) as ctx:
            c.search(
                "socket lifecycle",
                filter={"namespace": {"eq": NS}, "attributes": {"module": {"in": ["redis/connection.py"]}}},
                limit=10,
            )
        self.assertIn("unknown filter clause member", str(ctx.exception))

    def test_a_two_element_attribute_list_is_equality_not_membership(self):
        """`list` compares the whole value, so it silently matches nothing.

        This returns 200 with an empty result set, which is indistinguishable
        from "no memories written yet" -- the reason module routing does not use
        it.
        """
        svc = FakeMemoryService()
        c = client(svc)
        c.create(
            [
                conv("c-conn", "connection.py owns the socket lifecycle", "redis/connection.py"),
                conv("c-clust", "cluster.py maps slots to nodes", "redis/cluster.py"),
            ]
        )
        found, _ = c.search(
            "socket lifecycle",
            filter={
                "namespace": {"eq": NS},
                "attributes": {"module": {"list": ["redis/connection.py", "redis/cluster.py"]}},
            },
            limit=10,
        )
        self.assertEqual(found, [])

    def test_search_results_carry_no_attributes(self):
        """Only GET returns `attributes`; a searched record has none.

        Anything computed from retrieved memories has to read `topics` or the
        id, never `attributes`.
        """
        svc = FakeMemoryService()
        c = client(svc)
        c.create([conv("c-conn", "connection.py owns the socket lifecycle", "redis/connection.py")])
        found, _ = c.search(
            "socket lifecycle", filter=scoped_filter(NS, modules=["redis/connection.py"]), limit=10
        )
        self.assertEqual([m.id for m in found], ["c-conn"])
        self.assertEqual(found[0].attributes, {})
        self.assertIn("redis/connection.py", found[0].topics)
        self.assertEqual(c.get("c-conn").attributes["module"], "redis/connection.py")


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


class TestTopicConstraints(unittest.TestCase):
    """Topics carry module routing, so the service's limits on them bite hard."""

    def test_an_overlong_topic_is_refused_client_side(self):
        """The service answers with `Topics: (1: the length must be between 1
        and 100.)` -- an index, not a field name. This failed a real run's write
        phase, so the check names what actually went wrong."""
        sentence = "the connection module should close the socket when " * 3
        with self.assertRaises(ValueError) as ctx:
            Memory(
                id="c-long",
                text="fine",
                memory_type=REPO_CONVENTION,
                namespace=NS,
                owner_id="memory-agent",
                topics=["finding", sentence],
            )
        message = str(ctx.exception)
        self.assertIn("caps topics at 100", message)
        self.assertIn("module path was expected", message)

    def test_an_empty_topic_is_refused(self):
        with self.assertRaises(ValueError):
            Memory(
                id="c-empty",
                text="fine",
                memory_type=REPO_CONVENTION,
                namespace=NS,
                owner_id="memory-agent",
                topics=["finding", ""],
            )

    def test_module_topics_drops_rather_than_truncates(self):
        """A truncated path matches no filter, so keeping it hides the loss."""
        long_path = "redis/" + "x" * 120 + ".py"
        self.assertEqual(module_topics([long_path, "redis/connection.py"]), ["redis/connection.py"])


class TestResolveModule(unittest.TestCase):
    """A module that is not a touched file is unretrievable forever."""

    TOUCHED = ["redis/connection.py", "redis/asyncio/connection.py", "redis/cluster.py"]

    def test_exact_path_wins(self):
        self.assertEqual(resolve_module("redis/cluster.py", self.TOUCHED), "redis/cluster.py")

    def test_a_unique_basename_resolves(self):
        self.assertEqual(resolve_module("cluster.py", self.TOUCHED), "redis/cluster.py")

    def test_an_ambiguous_basename_does_not_guess(self):
        """connection.py matches two touched files; guessing would route the
        memory to the wrong module, which is worse than not routing it."""
        self.assertIsNone(resolve_module("connection.py", self.TOUCHED))

    def test_free_text_and_empty_do_not_resolve(self):
        self.assertIsNone(resolve_module("the connection handling code", self.TOUCHED))
        self.assertIsNone(resolve_module("", self.TOUCHED))


class TestResolveModules(unittest.TestCase):
    """Measured on the first real run: 5 of 6 written findings named a directory.

    Dropping those loses 83% of a PR's episodic memory. A directory genuinely
    covers the touched files beneath it, so it routes to all of them.
    """

    TOUCHED = [
        "redis/connection.py",
        "redis/asyncio/connection.py",
        "redis/cluster.py",
        "redis/commands/search/commands.py",
        "redis/commands/search/query.py",
        "tests/test_cluster.py",
    ]

    def test_a_directory_routes_to_every_touched_file_beneath_it(self):
        self.assertEqual(
            resolve_modules("redis/commands/search", self.TOUCHED),
            ["redis/commands/search/commands.py", "redis/commands/search/query.py"],
        )

    def test_a_trailing_slash_is_equivalent(self):
        self.assertEqual(
            resolve_modules("redis/commands/search/", self.TOUCHED),
            resolve_modules("redis/commands/search", self.TOUCHED),
        )

    def test_an_exact_path_beats_the_directory_rule(self):
        self.assertEqual(resolve_modules("redis/cluster.py", self.TOUCHED), ["redis/cluster.py"])

    def test_an_ambiguous_basename_falls_through_to_nothing(self):
        """connection.py matches two touched files and is not a directory, so
        guessing one would route the memory to the wrong module."""
        self.assertEqual(resolve_modules("connection.py", self.TOUCHED), [])

    def test_free_text_resolves_to_nothing(self):
        self.assertEqual(resolve_modules("the connection handling code", self.TOUCHED), [])

    def test_a_directory_matching_nothing_touched_resolves_to_nothing(self):
        self.assertEqual(resolve_modules("docs", self.TOUCHED), [])

    def test_the_module_topic_count_is_capped(self):
        touched = [f"redis/commands/f{i}.py" for i in range(40)]
        resolved = resolve_modules("redis/commands", touched)
        self.assertEqual(len(resolved), 20)  # MAX_MODULE_TOPICS, under MAX_TOPICS


if __name__ == "__main__":
    unittest.main()


class TestAttributeEncoding(unittest.TestCase):
    """The store declares pr_ordinal / convention_version as str (verified
    against the live service). An int fails the whole create with
    `400 attribute "x" has the wrong type ... (expected str)`."""

    def test_ints_become_strings(self):
        from reviewbot.memory import encode_attributes

        self.assertEqual(encode_attributes({"pr_number": 4052})["pr_number"], "4052")

    def test_ordinals_are_zero_padded_so_string_order_is_correct(self):
        from reviewbot.memory import encode_attributes, ordinal_attr

        self.assertEqual(ordinal_attr(9), "009")
        self.assertEqual(ordinal_attr(10), "010")
        self.assertLess(ordinal_attr(9), ordinal_attr(10))  # "9" < "10" would fail
        self.assertEqual(encode_attributes({"pr_ordinal": 3})["pr_ordinal"], "003")

    def test_lists_and_bools_are_encoded_too(self):
        from reviewbot.memory import encode_attributes

        out = encode_attributes({"evidence": [1, "two"], "active": True})
        self.assertEqual(out["evidence"], ["1", "two"])
        self.assertEqual(out["active"], "true")

    def test_encoding_happens_on_the_wire_not_in_the_record(self):
        record = conv("c-x", "text", "a.py", pr_ordinal=7)
        self.assertEqual(record.attributes["pr_ordinal"], 7)  # readable in Python
        self.assertEqual(record.to_create()["attributes"]["pr_ordinal"], "007")
