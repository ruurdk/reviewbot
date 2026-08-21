"""Both agents, and the one thing that must stay identical between them."""

import tempfile
import unittest

from reviewbot.accounting import Ledger
from reviewbot.agents import BaselineAgent, MemoryAgent
from reviewbot.claude import ClaudeClient
from reviewbot.config import ModelConfig
from reviewbot.github import FileChange, PullRequest
from reviewbot.memory import REPO_CONVENTION, REVIEW_FINDING, AgentMemoryClient
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


if __name__ == "__main__":
    unittest.main()
