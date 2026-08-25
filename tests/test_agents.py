"""Both agents, and the one thing that must stay identical between them."""

import tempfile
import unittest

from reviewbot.accounting import Ledger
from reviewbot.agents import BaselineAgent, MemoryAgent
from reviewbot.claude import ClaudeClient
from reviewbot.config import ModelConfig
from reviewbot.github import FileChange, PullRequest
from reviewbot.memory import (
    REPO_CONVENTION,
    REVIEW_FINDING,
    AgentMemoryClient,
    decode_ordinal_attr,
)
from reviewbot.repo import DictSourceProvider
from tests.fakes import FakeClaude, FakeMemoryService

NS = "redis-py-run-1"
CONVENTIONS = {"CONTRIBUTING.md": "Type hints everywhere. " * 100}
SOURCES = {
    "redis/connection.py": "class Connection:\n    pass\n" * 200,
    "redis/cluster.py": "class RedisCluster:\n    pass\n" * 200,
}

PR = PullRequest(
    repo="redis/redis-py",
    number=3411,
    title="Close the socket when the handshake fails",
    body="",
    base_sha="basesha",
    head_sha="headsha",
    merge_commit_sha=None,
    merged_at=None,
    files=[
        FileChange("redis/connection.py", "modified", 9, 2, 11, patch="@@ -1 +1 @@\n+x"),
        FileChange("docs/logo.png", "modified", 0, 0, 0, patch=None),
    ],
)

FINDINGS = [
    {
        "file": "redis/connection.py",
        "line": 12,
        "severity": "major",
        "category": "resource-leak",
        "message": "the socket is left open when the handshake raises",
        "confidence": "high",
    }
]
FACTS = [
    {"topic": "socket ownership", "kind": "invariant", "fact": "connection.py owns the socket and must close it on any setup failure"},
    {"topic": "retry policy", "kind": "convention", "fact": "retries are configured by the caller, never defaulted inside the connection"},
]
WRITE_RECORDS = [
    {
        "module": "redis/connection.py",
        "topic": "handshake cleanup",
        "pattern": "resource-leak",
        "text": "connection setup must close the socket if the handshake raises",
    }
]


def harness(fake, ledger, service=None, **memory_kwargs):
    client = ClaudeClient(
        ModelConfig(stream=False), ledger, api_key="t", transport=fake.transport
    )
    memory = None
    if service is not None:
        memory = AgentMemoryClient(
            "store-1",
            base_url="https://example.invalid",
            api_key="k",
            namespace=NS,
            ledger=ledger,
            transport=service.transport,
            **memory_kwargs,
        )
    return client, memory


class TestBaselineAgent(unittest.TestCase):
    def test_reads_touched_source_every_pr_and_never_touches_memory(self):
        fake = FakeClaude(findings=FINDINGS)
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(tmp, "run-b")
            client, _ = harness(fake, ledger)
            provider = DictSourceProvider(SOURCES)
            agent = BaselineAgent(client, provider, conventions=CONVENTIONS)
            first = agent.review_pr(PR, 1)
            second = agent.review_pr(PR, 2)
            records = list(ledger.records())

        self.assertEqual(len(first.findings), 1)
        # The binary file is skipped; the python file is read.
        self.assertEqual(first.files_read, 1)
        self.assertEqual(second.files_read, 1)
        self.assertEqual([r.phase for r in records], ["review", "review"])
        self.assertTrue(all(r.agent == "baseline" for r in records))
        # No retrieve/write rows at all -- that is the control condition.
        self.assertEqual([r for r in records if r.kind != "model_call"], [])
        # The source text is in the prompt both times: this is the re-derivation
        # the demo is measuring.
        for sent in fake.sent:
            blocks = sent["messages"][0]["content"]
            self.assertTrue(any("Source context:" in b["text"] for b in blocks))


class TestPrimer(unittest.TestCase):
    def test_one_call_per_module_and_deterministic_records(self):
        fake = FakeClaude(facts=FACTS)
        service = FakeMemoryService()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(tmp, "run-p")
            client, memory = harness(fake, ledger, service)
            agent = MemoryAgent(client, memory, conventions=CONVENTIONS)
            written = agent.prime(SOURCES, docs=CONVENTIONS)
            records = list(ledger.records())

        self.assertEqual(len(written), 4)  # 2 modules x 2 facts
        prime_calls = [r for r in records if r.phase == "prime" and r.kind == "model_call"]
        self.assertEqual(len(prime_calls), 2)
        self.assertEqual(sorted(c.notes["module"] for c in prime_calls), sorted(SOURCES))
        # Primer tokens are the memory agent's tokens, at ordinal 0.
        self.assertTrue(all(c.pr_ordinal == 0 for c in prime_calls))
        self.assertEqual(
            sorted(m.id for m in written)[:2],
            ["conv-redis-cluster-py-retry-policy", "conv-redis-cluster-py-socket-ownership"],
        )
        self.assertTrue(all(m.memory_type == REPO_CONVENTION for m in written))
        self.assertTrue(all(m.attributes["convention_version"] == 1 for m in written))
        self.assertEqual(len(service.records), 4)

    def test_re_priming_is_idempotent(self):
        fake = FakeClaude(facts=FACTS)
        service = FakeMemoryService()
        with tempfile.TemporaryDirectory() as tmp:
            client, memory = harness(fake, Ledger(tmp, "run-p"), service)
            agent = MemoryAgent(client, memory, conventions=CONVENTIONS)
            agent.prime(SOURCES)
            agent.prime(SOURCES)
        self.assertEqual(len(service.records), 4)


class TestMemoryAgentLoop(unittest.TestCase):
    def _run(self, **kwargs):
        fake = FakeClaude(
            facts=FACTS,
            findings=FINDINGS,
            memories_used=["conv-redis-connection-py-socket-ownership"],
            records=WRITE_RECORDS,
            count_tokens=777,
        )
        service = FakeMemoryService()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger = Ledger(tmp.name, "run-m")
        client, memory = harness(fake, ledger, service)
        agent = MemoryAgent(client, memory, conventions=CONVENTIONS, **kwargs)
        agent.prime(SOURCES)
        outcome = agent.review_pr(PR, 1)
        return fake, service, ledger, outcome

    def test_retrieve_review_write_are_all_tagged(self):
        _, _, ledger, outcome = self._run()
        records = list(ledger.records())
        phases = {(r.phase, r.kind) for r in records}
        self.assertIn(("prime", "model_call"), phases)
        self.assertIn(("retrieve", "memory_op"), phases)
        self.assertIn(("review", "model_call"), phases)
        self.assertIn(("write", "model_call"), phases)
        self.assertIn(("write", "memory_op"), phases)
        self.assertTrue(all(r.agent == "memory" for r in records))

    def test_retrieved_memories_replace_source_in_the_prompt(self):
        fake, _, _, outcome = self._run()
        review_call = [
            s for s in fake.sent
            if "findings" in set(s.get("output_config", {}).get("format", {}).get("schema", {}).get("required", []))
        ][0]
        blocks = review_call["messages"][0]["content"]
        texts = [b["text"] for b in blocks]
        self.assertTrue(any(t.startswith("Prior knowledge:") for t in texts))
        self.assertFalse(any("Source context:" in t for t in texts))
        self.assertGreater(len(outcome.retrieved), 0)

    def test_retrieval_cost_and_precision_are_measured(self):
        _, _, ledger, outcome = self._run()
        search = [r for r in ledger.records() if r.memory_op == "search"][0]
        measure = [r for r in ledger.records() if r.memory_op == "measure"][0]
        self.assertEqual(search.search_limit, 20)
        self.assertEqual(measure.injected_tokens, 777)
        self.assertEqual(outcome.injected_tokens, 777)
        # Retrieved several, used one.
        self.assertEqual(len(outcome.memories_used), 1)
        self.assertLess(outcome.retrieval_precision, 1.0)

    def test_write_phase_persists_a_reusable_finding(self):
        _, service, _, outcome = self._run()
        self.assertEqual(len(outcome.written), 1)
        rec = service.records[outcome.written[0]]
        self.assertEqual(rec["memoryType"], REVIEW_FINDING)
        self.assertEqual(rec["attributes"]["finding_class"], "resource-leak")
        # createdAt is server-assigned, so chronology rides in attributes -- as
        # a zero-padded string, because the store declares these fields as str
        # and a bare str(n) would sort "10" before "9".
        self.assertEqual(rec["attributes"]["pr_ordinal"], "001")
        self.assertEqual(rec["attributes"]["pr_number"], "3411")

    def test_undistilled_writes_cost_no_model_tokens(self):
        _, service, ledger, outcome = self._run(distill_writes=False)
        write_calls = [
            r for r in ledger.records() if r.phase == "write" and r.kind == "model_call"
        ]
        self.assertEqual(write_calls, [])
        self.assertEqual(len(outcome.written), 1)

    def test_the_query_carries_no_pr_identifier(self):
        fake = FakeClaude()
        service = FakeMemoryService()
        with tempfile.TemporaryDirectory() as tmp:
            client, memory = harness(fake, Ledger(tmp, "run-q"), service)
            agent = MemoryAgent(client, memory, conventions=CONVENTIONS)
            query = agent.query_for(PR)
        self.assertNotIn("3411", query)
        self.assertIn("redis/connection.py", query)


class TestNoConfound(unittest.TestCase):
    def test_both_agents_share_client_config_and_system_prompt(self):
        baseline_fake = FakeClaude(findings=FINDINGS)
        memory_fake = FakeClaude(findings=FINDINGS, records=WRITE_RECORDS)
        service = FakeMemoryService()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(tmp, "run-c")
            b_client, _ = harness(baseline_fake, ledger)
            m_client, memory = harness(memory_fake, ledger, service)
            BaselineAgent(b_client, DictSourceProvider(SOURCES), conventions=CONVENTIONS).review_pr(PR, 1)
            MemoryAgent(m_client, memory, conventions=CONVENTIONS).review_pr(PR, 1)

        b_review = baseline_fake.sent[0]
        m_review = [
            s for s in memory_fake.sent
            if "findings" in set(s.get("output_config", {}).get("format", {}).get("schema", {}).get("required", []))
        ][0]
        for key in ("model", "max_tokens", "output_config", "thinking"):
            if key == "output_config":
                self.assertEqual(b_review[key], m_review[key])
            else:
                self.assertEqual(b_review.get(key), m_review.get(key))
        self.assertEqual(b_review["system"], m_review["system"])
        # Identical cacheable prefix: same conventions block, same breakpoint.
        self.assertEqual(
            b_review["messages"][0]["content"][0], m_review["messages"][0]["content"][0]
        )


def pr_touching(*paths):
    return PullRequest(
        repo="redis/redis-py",
        number=4444,
        title="a change",
        body="",
        base_sha="basesha",
        head_sha="headsha",
        merge_commit_sha=None,
        merged_at=None,
        files=[FileChange(p, "modified", 5, 1, 6, patch="@@ -1 +1 @@\n+x") for p in paths],
    )


class TestWritePhaseRouting(unittest.TestCase):
    """A record whose module is not a touched file can never be retrieved."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _agent(self, records, finding_file="redis/cluster.py"):
        svc = FakeMemoryService()
        fake = FakeClaude(
            findings=[
                {
                    "file": finding_file,
                    "line": 10,
                    "severity": "major",
                    "category": "slots",
                    "message": "slot cache mutated without the lock",
                    "confidence": "high",
                }
            ],
            memories_used=[],
            records=records,
        )
        ledger = Ledger(self.tmp, "r")
        client = ClaudeClient(ModelConfig(stream=False), ledger, api_key="t", transport=fake.transport)
        agent = MemoryAgent(
            client,
            AgentMemoryClient(
                "store-1",
                base_url="https://example.invalid",
                api_key="k",
                namespace="redis-py-run-1",
                ledger=ledger,
                transport=svc.transport,
            ),
            conventions={},
        )
        return agent, svc, ledger

    def test_a_bare_basename_is_routed_to_the_touched_path(self):
        agent, svc, _ = self._agent(
            [{"module": "cluster.py", "topic": "slots", "pattern": "p", "text": "a durable rule"}]
        )
        outcome = agent.review_pr(pr_touching("redis/cluster.py"), 1)
        self.assertEqual(len(outcome.written), 1)
        self.assertIn("redis/cluster.py", svc.records[outcome.written[0]]["topics"])

    def test_free_text_module_is_dropped_and_logged_not_written_unreachable(self):
        agent, svc, ledger = self._agent(
            [{"module": "the cluster handling code", "topic": "nope", "pattern": "p", "text": "a rule"}],
            finding_file="redis/cluster.py",
        )
        outcome = agent.review_pr(pr_touching("redis/cluster.py"), 1)
        self.assertEqual(outcome.written, [])
        notes = [r.notes for r in ledger.records() if (r.notes or {}).get("unroutable_modules")]
        self.assertTrue(notes, "an unroutable module must be recorded, not swallowed")
        self.assertIn("the cluster handling code", notes[0]["unroutable_modules"])

    def test_a_directory_module_routes_to_all_touched_files(self):
        """The commonest real failure: the distiller names a directory."""
        agent, svc, _ = self._agent(
            [{"module": "redis/commands", "topic": "api", "pattern": "p", "text": "a rule"}],
            finding_file="redis/commands/core.py",
        )
        pr = pr_touching("redis/commands/core.py", "redis/commands/helpers.py", "redis/cluster.py")
        outcome = agent.review_pr(pr, 1)
        self.assertEqual(len(outcome.written), 1)
        topics = svc.records[outcome.written[0]]["topics"]
        self.assertIn("redis/commands/core.py", topics)
        self.assertIn("redis/commands/helpers.py", topics)
        self.assertNotIn("redis/cluster.py", topics)  # not under that directory

    def test_the_finding_file_rescues_a_module_the_distiller_mangled(self):
        """The distilled `module` is free text, but the finding it came from
        names a real file -- routing by `topic` recovers it."""
        agent, svc, _ = self._agent(
            [{"module": "unclear", "topic": "slots", "pattern": "p", "text": "a rule"}],
            finding_file="redis/cluster.py",
        )
        outcome = agent.review_pr(pr_touching("redis/cluster.py"), 1)
        self.assertEqual(len(outcome.written), 1)
        self.assertIn("redis/cluster.py", svc.records[outcome.written[0]]["topics"])


if __name__ == "__main__":
    unittest.main()


class TestRetrievalBudget(unittest.TestCase):
    """Pooled vs split retrieval, which is a real experimental variable.

    Run-1 used one pooled budget and it came back *full* on every PR from the
    first one onward. A saturated window is not measuring relevance, it is
    measuring the limit -- and since the store only ever grows (nothing is
    merged or superseded), conventions and an accumulating pile of findings
    compete for the same fixed slots.
    """

    def _run(self, **kwargs):
        fake = FakeClaude(
            facts=FACTS,
            findings=FINDINGS,
            memories_used=[],
            records=WRITE_RECORDS,
            count_tokens=777,
        )
        service = FakeMemoryService()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger = Ledger(tmp.name, "run-r")
        client, memory = harness(fake, ledger, service)
        agent = MemoryAgent(client, memory, conventions=CONVENTIONS, **kwargs)
        agent.prime(SOURCES)
        agent.review_pr(PR, 1)
        searches = [
            payload
            for method, path, payload in service.calls
            if path == "long-term-memory/search"
        ]
        return searches, ledger

    def test_the_default_is_one_pooled_search_as_run_1_did(self):
        # Run-1 must stay reproducible, so the default cannot change shape.
        searches, _ = self._run()
        self.assertEqual(len(searches), 1)
        self.assertEqual(
            searches[0]["filter"]["memoryType"],
            {"in": [REPO_CONVENTION, REVIEW_FINDING]},
        )
        self.assertEqual(searches[0]["limit"], 20)

    def test_a_split_issues_one_search_per_type_with_its_own_budget(self):
        searches, _ = self._run(
            retrieval_limits={REPO_CONVENTION: 6, REVIEW_FINDING: 4}
        )
        self.assertEqual(len(searches), 2)
        budgets = {s["filter"]["memoryType"]["eq"]: s["limit"] for s in searches}
        self.assertEqual(budgets, {REPO_CONVENTION: 6, REVIEW_FINDING: 4})

    def test_a_type_given_no_budget_is_not_searched_for(self):
        searches, _ = self._run(retrieval_limits={REPO_CONVENTION: 5})
        self.assertEqual(len(searches), 1)
        self.assertEqual(searches[0]["filter"]["memoryType"], {"eq": REPO_CONVENTION})

    def test_splitting_costs_round_trips_and_zero_model_tokens(self):
        """The reason this knob is safe to turn: it cannot move the cost series.

        Searches are billable=False, so a second one changes the retrieval mix
        without touching either agent's billed total.
        """
        _, ledger = self._run(retrieval_limits={REPO_CONVENTION: 6, REVIEW_FINDING: 4})
        searches = [r for r in ledger.records() if r.memory_op == "search"]
        self.assertEqual(len(searches), 2)
        self.assertFalse(any(r.billable for r in searches))
        self.assertEqual(sum(r.context_volume for r in searches), 0)

    def test_a_split_search_still_scopes_to_the_namespace_and_modules(self):
        # Splitting by type must not drop the two clauses that keep runs
        # isolated and retrieval per-module.
        searches, _ = self._run(retrieval_limits={REPO_CONVENTION: 6, REVIEW_FINDING: 4})
        for payload in searches:
            self.assertEqual(payload["filter"]["namespace"], {"eq": NS})
            self.assertIn("redis/connection.py", payload["filter"]["topics"]["in"])
            self.assertEqual(payload["filterOp"], "all")


class TestDedupeOnWrite(unittest.TestCase):
    """A repeat finding updates its record instead of appending a copy.

    The problem this fixes is not token volume -- retrieval saturates its window
    either way. It is that an append-only store turns a wrong belief into a
    *growing* one: run-1 restated one false convention 11 times across 9 PRs,
    and every copy competed for the same fixed retrieval slots.
    """

    def _agent(self, **kwargs):
        fake = FakeClaude(
            facts=FACTS,
            findings=FINDINGS,
            memories_used=[],
            records=WRITE_RECORDS,
            count_tokens=777,
        )
        service = FakeMemoryService()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger = Ledger(tmp.name, "run-d")
        client, memory = harness(fake, ledger, service)
        agent = MemoryAgent(client, memory, conventions=CONVENTIONS, **kwargs)
        return agent, service, ledger

    def _findings(self, service):
        return {
            mid: rec
            for mid, rec in service.records.items()
            if rec["memoryType"] == REVIEW_FINDING
        }

    def test_append_only_writes_a_second_copy_of_the_same_finding(self):
        """Run-1's behaviour, kept reproducible and pinned as the contrast."""
        agent, service, _ = self._agent()
        agent.review_pr(PR, 1)
        agent.review_pr(PR, 2)
        ids = sorted(self._findings(service))
        self.assertEqual(len(ids), 2)
        # The ordinal in the id is precisely what makes them distinct records.
        self.assertTrue(any(i.endswith("-1") for i in ids))
        self.assertTrue(any(i.endswith("-2") for i in ids))

    def test_dedupe_collapses_the_repeat_into_one_record(self):
        agent, service, _ = self._agent(dedupe_writes=True)
        agent.review_pr(PR, 1)
        agent.review_pr(PR, 2)
        self.assertEqual(len(self._findings(service)), 1)

    def test_recurrence_becomes_explicit_instead_of_a_copy_count(self):
        agent, service, _ = self._agent(dedupe_writes=True)
        agent.review_pr(PR, 1)
        agent.review_pr(PR, 4)
        record = next(iter(self._findings(service).values()))
        attrs = record["attributes"]
        # Attributes come back as strings, so decode rather than compare raw --
        # asserting on "001" would pin the encoding, not the behaviour.
        self.assertEqual(decode_ordinal_attr(attrs["occurrences"]), 2)
        # First-seen, not last-seen: pr_ordinal carries chronology, and
        # overwriting it would make "when did we learn this" unanswerable.
        self.assertEqual(decode_ordinal_attr(attrs["pr_ordinal"]), 1)
        self.assertEqual(decode_ordinal_attr(attrs["last_pr_ordinal"]), 4)

    def test_a_merge_keeps_the_earlier_modules_retrievable(self):
        """Topics are unioned, not replaced. A recurrence in a different file
        must not make the record unreachable from the first one."""
        agent, service, _ = self._agent(dedupe_writes=True)
        agent.review_pr(PR, 1)
        first = next(iter(self._findings(service).values()))
        original_topics = set(first["topics"])
        agent.review_pr(PR, 2)
        merged = next(iter(self._findings(service).values()))
        self.assertTrue(original_topics <= set(merged["topics"]))

    def test_the_merge_is_logged_as_a_measured_thing(self):
        _, _, ledger = (lambda t: t)(self._agent(dedupe_writes=True))
        agent, service, ledger = self._agent(dedupe_writes=True)
        agent.review_pr(PR, 1)
        agent.review_pr(PR, 2)
        notes = [
            r.notes
            for r in ledger.records()
            if r.memory_op == "measure" and "deduped_writes" in (r.notes or {})
        ]
        self.assertTrue(notes)
        self.assertGreaterEqual(notes[-1]["deduped_writes"], 1)

    def test_a_merge_uses_the_guarded_patch_not_a_blind_overwrite(self):
        agent, service, _ = self._agent(dedupe_writes=True)
        agent.review_pr(PR, 1)
        agent.review_pr(PR, 2)
        patches = [c for c in service.calls if c[0] == "PATCH"]
        self.assertTrue(patches)
        # The guard is the service's: it rejects unless both match the stored
        # record, which is what stops a write clobbering another run's memory.
        for _, _, payload in patches:
            self.assertEqual(payload["memoryType"], REVIEW_FINDING)
            self.assertEqual(payload["namespace"], NS)

    def test_dedupe_costs_no_model_tokens(self):
        """The reason this is safe to turn on: a GET and a PATCH are both
        billable=False, so the write phase's model cost is unchanged."""
        agent, _, ledger = self._agent(dedupe_writes=True)
        agent.review_pr(PR, 1)
        before = sum(r.billed_usd() for r in ledger.records() if r.billable)
        agent.review_pr(PR, 2)
        after = sum(r.billed_usd() for r in ledger.records() if r.billable)
        write_ops = [r for r in ledger.records() if r.memory_op in ("update", "measure")]
        self.assertTrue(any(r.memory_op == "update" for r in write_ops))
        self.assertFalse(any(r.billable for r in write_ops))
        # The second review still costs model tokens; the *merge* adds none.
        self.assertGreater(after, before)
