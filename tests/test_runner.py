"""An end-to-end sequence run against fakes -- no credentials, no network."""

import tempfile
import unittest

from reviewbot.accounting import Ledger
from reviewbot.agents import BaselineAgent, MemoryAgent
from reviewbot.claude import ClaudeClient
from reviewbot.config import ConfoundError, ModelConfig
from reviewbot.dataset import Sequence, SequenceEntry
from reviewbot.github import FileChange, PRStore, PullRequest
from reviewbot.memory import AgentMemoryClient
from reviewbot.quality import GoldItem, GoldLabels
from reviewbot.repo import DictSourceProvider
from reviewbot.runner import AGENT_ORDER, SequenceRunner, render_report, save_report
from tests.fakes import FakeClaude, FakeMemoryService

REPO = "redis/redis-py"
SHA = "frozensha"
SPINE = ["redis/connection.py", "redis/cluster.py"]
SOURCES = {
    "redis/connection.py": "class Connection: pass\n" * 300,
    "redis/cluster.py": "class RedisCluster: pass\n" * 300,
    "CONTRIBUTING.md": "Type hints everywhere.\n" * 80,
}
FACTS = [{"topic": "socket ownership", "kind": "invariant", "fact": "close the socket on setup failure"}]
FINDINGS = [
    {
        "file": "redis/connection.py",
        "line": 12,
        "severity": "major",
        "category": "resource-leak",
        "message": "socket left open",
        "confidence": "high",
    }
]
WRITE_RECORDS = [
    {"module": "redis/connection.py", "topic": "handshake", "pattern": "resource-leak", "text": "close on raise"}
]


def make_pr(store, number, ordinal):
    pr = PullRequest(
        repo=REPO,
        number=number,
        title=f"change {ordinal}",
        body="",
        base_sha=SHA,
        head_sha="head",
        merge_commit_sha=None,
        merged_at=None,
        files=[FileChange("redis/connection.py", "modified", 5, 1, 6, patch="@@ -1 +1 @@\n+x")],
        comments=[],
    )
    store.save(pr)
    return pr


def build(tmp, *, with_gold=True, n=3):
    store = PRStore(f"{tmp}/prs")
    entries = []
    for i in range(1, n + 1):
        make_pr(store, 3400 + i, i)
        entries.append(SequenceEntry(ordinal=i, pr_number=3400 + i, gold_labeled=i == 1))
    sequence = Sequence(
        repo=REPO,
        entries=entries,
        spine=SPINE,
        style_guide_paths=["CONTRIBUTING.md"],
        frozen_at_sha=SHA,
        selection_rule="upper half of the diff-size distribution",
    )
    ledger = Ledger(f"{tmp}/run", "run-e2e")
    config = ModelConfig(stream=False)
    provider = DictSourceProvider(SOURCES)

    baseline_fake = FakeClaude(findings=FINDINGS)
    memory_fake = FakeClaude(facts=FACTS, findings=FINDINGS, memories_used=[], records=WRITE_RECORDS, count_tokens=500)
    service = FakeMemoryService()

    baseline = BaselineAgent(
        ClaudeClient(config, ledger, api_key="t", transport=baseline_fake.transport),
        provider,
        conventions={"CONTRIBUTING.md": SOURCES["CONTRIBUTING.md"]},
    )
    memory = MemoryAgent(
        ClaudeClient(config, ledger, api_key="t", transport=memory_fake.transport),
        AgentMemoryClient(
            "store-1",
            base_url="https://example.invalid",
            api_key="k",
            namespace="redis-py-run-1",
            ledger=ledger,
            transport=service.transport,
        ),
        conventions={"CONTRIBUTING.md": SOURCES["CONTRIBUTING.md"]},
    )
    gold = (
        {3401: GoldLabels(3401, "ruurd", [GoldItem(id="d1", file="redis/connection.py", line=12)])}
        if with_gold
        else {}
    )
    runner = SequenceRunner(
        sequence=sequence,
        store=store,
        ledger=ledger,
        provider=provider,
        config=config,
        baseline=baseline,
        memory=memory,
        gold=gold,
    )
    return runner, ledger, service, (baseline_fake, memory_fake)


class TestSequenceRun(unittest.TestCase):
    def test_full_run_produces_a_complete_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, ledger, service, _ = build(tmp)
            report = runner.run()
            report_written = save_report(report, f"{tmp}/run").exists()
            text = render_report(report)
            manifest = ledger.read_manifest()
            records = list(ledger.records())

        self.assertEqual(len(report.results), 3)
        self.assertEqual(set(report.accounting["agents"]), {"baseline", "memory"})
        # Primer ran once, before the sequence, at ordinal 0.
        prime = [r for r in records if r.phase == "prime" and r.kind == "model_call"]
        self.assertEqual(len(prime), len(SPINE))
        self.assertTrue(all(r.pr_ordinal == 0 for r in prime))
        # Every model call is attributable.
        self.assertTrue(all(r.agent and r.pr_id and r.phase for r in records))
        self.assertTrue(report_written)
        self.assertIn("break-even PR", text)
        self.assertIn("precision", text)  # the gold table is rendered
        self.assertEqual(manifest["agent_order"], list(AGENT_ORDER))
        self.assertEqual(
            manifest["extraction_mode"],
            "explicit client-side writes; automatic extraction off",
        )

    def test_memory_runs_first_so_the_baseline_gets_any_free_ride(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, ledger, _, _ = build(tmp, n=2)
            runner.run()
            records = [r for r in ledger.records() if r.phase == "review"]
        first_per_pr = {}
        for rec in sorted(records, key=lambda r: r.seq):
            first_per_pr.setdefault(rec.pr_ordinal, rec.agent)
        self.assertEqual(set(first_per_pr.values()), {"memory"})

    def test_baseline_reads_source_every_pr_memory_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _, _, (baseline_fake, memory_fake) = build(tmp, n=3)
            report = runner.run()
        for result in report.results:
            self.assertEqual(result.outcomes["baseline"]["files_read"], 1)
            self.assertEqual(result.outcomes["memory"]["files_read"], 0)
            self.assertGreaterEqual(result.outcomes["memory"]["retrieved"], 1)
        # Every baseline review carries the file contents; no memory review does.
        review_prompts = [
            s for s in baseline_fake.sent if s.get("messages")
        ]
        self.assertTrue(
            all(
                any("Source context:" in b["text"] for b in s["messages"][0]["content"])
                for s in review_prompts
            )
        )

    def test_missing_gold_labels_are_called_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _, _, _ = build(tmp, with_gold=False, n=2)
            report = runner.run()
        self.assertTrue(any("proxy-only" in w for w in report.warnings))
        self.assertEqual(report.quality_gold, {})
        # The proxy still runs, and flags that these PRs had no human comments.
        self.assertEqual(report.quality_proxy["baseline"]["prs_blind"], 2)

    def test_priming_without_a_pinned_sha_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _, _, _ = build(tmp, n=1)
            runner.sequence.frozen_at_sha = None
            with self.assertRaises(ValueError):
                runner.prime()

    def test_unreadable_spine_is_refused_rather_than_primed_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _, _, _ = build(tmp, n=1)
            runner.sequence.spine = ["redis/does_not_exist.py"]
            with self.assertRaises(ValueError) as ctx:
                runner.prime()
            self.assertIn("spine modules were readable", str(ctx.exception))


class TestConfoundGuardAtRunTime(unittest.TestCase):
    def test_agents_on_different_configs_cannot_be_run_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, ledger, _, _ = build(tmp, n=1)
            drifted = ModelConfig(stream=False, effort="low")
            runner.agents["baseline"].client.config = drifted
            with self.assertRaises(ConfoundError):
                SequenceRunner(
                    sequence=runner.sequence,
                    store=runner.store,
                    ledger=ledger,
                    provider=runner.provider,
                    config=runner.config,
                    baseline=runner.agents["baseline"],
                    memory=runner.agents["memory"],
                )


if __name__ == "__main__":
    unittest.main()
