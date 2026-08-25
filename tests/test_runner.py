"""An end-to-end sequence run against fakes -- no credentials, no network."""

import json
import os
import pathlib
import tempfile
import unittest

from reviewbot.accounting import Ledger
from reviewbot.agents import BaselineAgent, MemoryAgent
from reviewbot.claude import ClaudeClient
from reviewbot.config import ConfoundError, ModelConfig
from reviewbot.dataset import Sequence, SequenceEntry
from reviewbot.github import FileChange, PRStore, PullRequest
from reviewbot.memory import AgentMemoryClient, NotVisible
from reviewbot.quality import GoldItem, GoldLabels
from reviewbot.repo import DictSourceProvider
from reviewbot.runner import (
    AGENT_ORDER,
    ConcurrentRun,
    SequenceRunner,
    render_report,
    save_report,
    single_process,
)
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


def harness_manifest(
    *, retrieval_limits=None, dedupe_writes=None, agents=("baseline", "memory")
):
    """A manifest from a built runner, without executing the sequence."""
    with tempfile.TemporaryDirectory() as tmp:
        runner, _, _, _ = build(tmp)
        if retrieval_limits is not None:
            runner.agents["memory"].retrieval_limits = dict(retrieval_limits)
        if dedupe_writes is not None:
            runner.agents["memory"].dedupe_writes = dedupe_writes
        runner.agents = {k: v for k, v in runner.agents.items() if k in agents}
        return runner.write_manifest()


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


class TestReplayPageContract(unittest.TestCase):
    """The page renders report.json verbatim, so its shape is a contract.

    web/src/data/contract.js reads `report.per_pr` and `report.rows`; neither
    existed in the report for a while, so a real run would have rendered a
    broken page while the synthetic fixture kept working. These are the exact
    fields each consumer touches.
    """

    # contract.js cumulativeSeries/crossover/netSaving, PerPrBreakdown, AccountingTable
    PER_PR_FIELDS = (
        "agent",
        "pr_ordinal",
        "context_volume",
        "billed_usd",
        "billed_usd_production",
        "output_tokens",
        "by_phase",
        "tiers",
    )
    # AccountingTable byNumber lookup + App.jsx PR count
    ROW_FIELDS = ("ordinal", "pr_number", "title", "n_files", "diff_size", "human_comments")
    # contract.js marginalFor -> the four KPI tiles and the caveat line
    MARGINAL_FIELDS = (
        "n_prs",
        "aggregate_pct",
        "median_pct",
        "mean_saving_usd",
        "context_aggregate_pct",
        "primer_usd",
        "primer_payback_prs",
        "worst_pr",
    )

    def test_report_carries_every_field_the_page_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = build(tmp)[0].run()
            payload = json.loads(json.dumps(report.to_json(), default=str))

        self.assertTrue(payload["per_pr"], "per_pr is empty; the charts would render nothing")
        for row in payload["per_pr"]:
            for field_name in self.PER_PR_FIELDS:
                self.assertIn(field_name, row)
            self.assertEqual(set(row["tiers"]), {"uncached", "cache_write", "cache_read"})
        for row in payload["rows"]:
            for field_name in self.ROW_FIELDS:
                self.assertIn(field_name, row)

        # The primer is ordinal 0 and belongs to the memory agent alone -- that
        # is what makes the cumulative curve start above the baseline's.
        primer = [r for r in payload["per_pr"] if r["pr_ordinal"] == 0]
        self.assertEqual([r["agent"] for r in primer], ["memory"])

        # contract.js marginalFor() -> the page's lead KPI. A missing field here
        # renders "--" where the headline should be, which looks like a working
        # page.
        marginal = payload["accounting"]["marginal"]
        self.assertEqual(set(marginal), {"as_measured", "production_equivalent"})
        for regime in marginal.values():
            for field_name in self.MARGINAL_FIELDS:
                self.assertIn(field_name, regime)
            self.assertIn("saving_pct", regime["worst_pr"])
        self.assertIn("prime", primer[0]["by_phase"])

    def test_per_pr_rows_carry_what_each_agent_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = build(tmp)[0].run()
        by_agent = {}
        for row in report.per_pr:
            if row["pr_ordinal"] == 1:
                by_agent[row["agent"]] = row
        # The baseline's cost is explained by files read; the memory agent's by
        # memories retrieved. The table shows one or the other per agent.
        self.assertIsNotNone(by_agent["baseline"]["files_read"])
        self.assertIsNotNone(by_agent["memory"]["retrieved"])


class TestResume(unittest.TestCase):
    """A full sequence is a multi-hour, real-money run; a crash must not re-pay."""

    def test_a_resumed_run_reviews_only_the_missing_prs(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, ledger, _, _ = build(tmp)
            runner.run()
            first_pass = len([r for r in ledger.records() if r.kind == "model_call"])

            # Same run directory, same checkpoint: nothing left to do.
            runner2, ledger2, _, (base_fake, mem_fake) = build(tmp)
            report = runner2.run()
            second_pass = [r for r in ledger2.records() if r.kind == "model_call"]

        self.assertEqual(len(report.results), 3)
        # No new model call at all -- not the primer, not a review.
        self.assertEqual(len(second_pass), first_pass)
        self.assertEqual(base_fake.calls, 0)
        self.assertEqual(mem_fake.calls, 0)
        # And the report says so rather than presenting it as a fresh measurement.
        self.assertTrue(any("restored from a checkpoint" in w for w in report.warnings))
        # Quality is still scored over the whole sequence.
        self.assertEqual(set(report.quality_proxy), {"baseline", "memory"})

    def test_a_half_reviewed_pr_is_redone_not_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, ledger, _, _ = build(tmp)
            runner.run()
            # Simulate a crash between the memory and the baseline review of PR 3
            # by dropping the baseline outcome from its checkpoint row.
            path = pathlib.Path(f"{tmp}/run/checkpoint.jsonl")
            rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
            rows[-1]["outcomes"].pop("baseline")
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))

            runner2, _, _, (base_fake, mem_fake) = build(tmp)
            report = runner2.run()

        self.assertEqual(len(report.results), 3)
        # PR 3 was reviewed again by both agents; PRs 1-2 and the primer were not.
        self.assertEqual(base_fake.calls, 1)
        self.assertEqual(mem_fake.calls, 2)  # review + write
        self.assertEqual(runner2.resumed, [1, 2])


class TestSingleProcess(unittest.TestCase):
    """Two processes on one run silently invert the measurement (spec 7a)."""

    def test_a_second_process_is_refused_while_the_first_holds_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp) / "run"
            run_dir.mkdir()
            with single_process(run_dir):
                with self.assertRaises(ConcurrentRun) as ctx:
                    with single_process(run_dir):
                        pass
            self.assertIn("cannot share a prompt cache entry", str(ctx.exception))
            # Released on exit, so the next run is not blocked forever.
            self.assertFalse((run_dir / "run.lock").exists())

    def test_a_lock_left_by_a_dead_process_is_taken_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp) / "run"
            run_dir.mkdir()
            # A pid that cannot be running: 0 is never a real process here.
            (run_dir / "run.lock").write_text(json.dumps({"pid": 0, "started": "then"}))
            with single_process(run_dir):
                held = json.loads((run_dir / "run.lock").read_text())
            self.assertEqual(held["pid"], os.getpid())

    def test_a_run_holds_the_lock_for_its_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _, _, _ = build(tmp)
            seen = {}
            original = runner.review_pr

            def spy(pr, ordinal):
                seen["locked"] = (pathlib.Path(tmp) / "run" / "run.lock").exists()
                return original(pr, ordinal)

            runner.review_pr = spy
            runner.run()
            self.assertTrue(seen["locked"])
            self.assertFalse((pathlib.Path(tmp) / "run" / "run.lock").exists())


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


class TestAbandonedAttempts(unittest.TestCase):
    """A crash between the review and the write phase bills without finishing.

    That happened for real: the write phase 400'd on PR 2 after its review was
    already paid for. Counting both attempts would report that PR's cost twice.
    """

    def test_a_partially_billed_pr_is_not_counted_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            # First pass: complete PRs 1-3.
            runner, ledger, _, _ = build(tmp)
            runner.run()
            # Drop PR 3 from the checkpoint but leave its ledger rows: exactly
            # the shape of a crash after the review, before the PR completed.
            path = pathlib.Path(f"{tmp}/run/checkpoint.jsonl")
            rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
            path.write_text("".join(json.dumps(r) + "\n" for r in rows[:-1]))
            billed_before = sum(r.billed_usd() for r in ledger.records() if r.billable)

            runner2, ledger2, _, _ = build(tmp)
            report = runner2.run()
            records = list(ledger2.records())

        # The re-run really did spend more money, and the ledger says so.
        billed_after = sum(r.billed_usd() for r in records if r.billable)
        self.assertGreater(billed_after, billed_before)

        # But PR 3 is reported once, not twice: its per-PR total matches PR 2's,
        # which was reviewed exactly once.
        per_pr = {(r["agent"], r["pr_ordinal"]): r for r in report.per_pr}
        self.assertEqual(
            per_pr[("baseline", 3)]["calls"], per_pr[("baseline", 2)]["calls"]
        )

        # The superseded spend is disclosed rather than deleted.
        abandoned = report.accounting["abandoned"]
        self.assertGreater(abandoned["calls"], 0)
        self.assertGreater(abandoned["billed_usd"], 0)
        self.assertTrue(
            any("abandoned by a resume" in w for w in report.warnings),
            report.warnings,
        )
        # The rows themselves are still there -- append-only.
        self.assertTrue(any(r.seq in abandoned["seqs"] for r in records))

    def test_nothing_is_marked_when_a_run_starts_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = build(tmp)[0].run()
        self.assertEqual(report.accounting["abandoned"]["calls"], 0)
        self.assertFalse(any("abandoned" in w for w in report.warnings))


if __name__ == "__main__":
    unittest.main()


class TestRetrievalManifest(unittest.TestCase):
    """The retrieval shape is an experimental variable with no other trace.

    ModelConfig.fingerprint() does not cover it, and the ledger's search rows
    record the limit that was used -- which for a pooled run and a split run
    sharing a number look alike. Without the manifest, two runs differing only
    in retrieval shape are indistinguishable from their artifacts.
    """

    def test_a_pooled_run_says_so_and_names_its_budget(self):
        manifest = harness_manifest()
        self.assertEqual(manifest["retrieval"]["shape"], "pooled")
        self.assertEqual(manifest["retrieval"]["total_budget"], 20)

    def test_a_split_run_records_the_per_type_budgets(self):
        manifest = harness_manifest(
            retrieval_limits={"repo_convention": 10, "review_finding": 10}
        )
        self.assertEqual(manifest["retrieval"]["shape"], "split")
        self.assertEqual(
            manifest["retrieval"]["limits"],
            {"repo_convention": 10, "review_finding": 10},
        )
        self.assertEqual(manifest["retrieval"]["total_budget"], 20)

    def test_the_two_shapes_are_distinguishable_at_equal_total_budget(self):
        # The whole point: same 20 records retrieved, different shape.
        pooled = harness_manifest()
        split = harness_manifest(
            retrieval_limits={"repo_convention": 10, "review_finding": 10}
        )
        self.assertEqual(pooled["config_fingerprint"], split["config_fingerprint"])
        self.assertEqual(
            pooled["retrieval"]["total_budget"], split["retrieval"]["total_budget"]
        )
        self.assertNotEqual(pooled["retrieval"], split["retrieval"])

    def test_a_baseline_only_run_has_no_retrieval_shape(self):
        manifest = harness_manifest(agents=("baseline",))
        self.assertEqual(manifest["retrieval"]["shape"], "none")


class TestWritePolicyManifest(unittest.TestCase):
    """Dedup changes the finding *id scheme*, so two runs on either side of it
    produce stores that cannot be compared record-for-record. Like the
    retrieval shape, it is invisible to ModelConfig.fingerprint()."""

    def test_append_only_is_recorded_with_its_id_scheme(self):
        writes = harness_manifest(dedupe_writes=False)["writes"]
        self.assertEqual(writes["shape"], "append-only")
        self.assertFalse(writes["dedupe"])
        self.assertIn("{ordinal}", writes["id_scheme"])

    def test_dedupe_is_recorded_with_its_id_scheme(self):
        writes = harness_manifest(dedupe_writes=True)["writes"]
        self.assertEqual(writes["shape"], "dedupe")
        self.assertTrue(writes["dedupe"])
        self.assertNotIn("{ordinal}", writes["id_scheme"])

    def test_the_two_are_distinguishable_at_an_identical_fingerprint(self):
        append = harness_manifest(dedupe_writes=False)
        dedupe = harness_manifest(dedupe_writes=True)
        self.assertEqual(append["config_fingerprint"], dedupe["config_fingerprint"])
        self.assertNotEqual(append["writes"], dedupe["writes"])


class TestPrimerCheckpoint(unittest.TestCase):
    """The primer is the most expensive thing the memory agent does, so a
    failure *after* the store was written must not cost it twice."""

    def test_the_marker_carries_the_records_not_just_their_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _, _, _ = build(tmp)
            runner.prime()
            marker = json.loads((pathlib.Path(tmp) / "run/primed.json").read_text())
            self.assertTrue(marker["records"])
            self.assertEqual(len(marker["records"]), len(marker["ids"]))
            # Enough to re-create without a model call: that is the point.
            for rec in marker["records"]:
                self.assertTrue(rec["id"] and rec["text"] and rec["memory_type"])

    def test_the_marker_is_written_before_the_visibility_wait(self):
        """A dropped write raises from the wait. If the marker were written
        after, the primer's model calls would be lost with it."""
        with tempfile.TemporaryDirectory() as tmp:
            runner, _, service, _ = build(tmp)
            memory = runner.agents["memory"]
            # Force every write to be dropped so the wait cannot succeed.
            original = service._create

            def drop_everything(payload):
                body = original(payload)
                for rec in payload["memories"]:
                    service.records.pop(rec["id"], None)
                return body

            service._create = drop_everything
            memory.memory.wait_for_visibility = lambda *a, **k: (_ for _ in ()).throw(
                NotVisible("simulated drop")
            )
            with self.assertRaises(NotVisible):
                runner.prime()
            marker = pathlib.Path(tmp) / "run/primed.json"
            self.assertTrue(marker.exists(), "primer spend was not checkpointed")

    def test_a_resume_repairs_a_dropped_record_without_a_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _, service, (_, memory_fake) = build(tmp)
            runner.prime()
            calls_after_prime = memory_fake.calls
            # Simulate the live failure: one primed record vanished from the
            # store while the marker still claims it.
            victim = sorted(service.records)[0]
            service.records.pop(victim)
            runner2, _, service2, (_, fake2) = build(tmp)
            # Same run dir, so the marker is found; point it at the same store.
            runner2.agents["memory"].memory = runner.agents["memory"].memory
            self.assertEqual(runner2.prime(), 0)  # no re-priming
            self.assertIn(victim, service.records)  # repaired
            self.assertEqual(memory_fake.calls, calls_after_prime)  # no model call
