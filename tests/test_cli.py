import json
import tempfile
import unittest
from pathlib import Path

from reviewbot.accounting import CallRecord, Ledger, Usage
from reviewbot.cli import main


def synthetic_run(tmp):
    """A run where memory pays a primer up front and wins by PR 3."""
    ledger = Ledger(tmp, "run-x", manifest={"config_fingerprint": "abc123"})
    ledger.record(
        CallRecord(
            run_id="run-x",
            agent="memory",
            pr_id="pr-0",
            pr_ordinal=0,
            phase="prime",
            model="claude-opus-5",
            effort="xhigh",
            usage=Usage(input_tokens=200_000, output_tokens=6_000),
            prefix_id="P",
        )
    )
    for ordinal in (1, 2, 3, 4):
        ledger.record(
            CallRecord(
                run_id="run-x",
                agent="baseline",
                pr_id=f"pr-{ordinal}",
                pr_ordinal=ordinal,
                phase="review",
                model="claude-opus-5",
                effort="xhigh",
                usage=Usage(
                    input_tokens=2_000,
                    cache_creation_input_tokens=80_000 if ordinal == 1 else 0,
                    cache_read_input_tokens=0 if ordinal == 1 else 80_000,
                    output_tokens=1_200,
                ),
                prefix_id="B",
            )
        )
        ledger.record(
            CallRecord(
                run_id="run-x",
                agent="memory",
                pr_id=f"pr-{ordinal}",
                pr_ordinal=ordinal,
                phase="retrieve",
                kind="memory_op",
                memory_op="search",
                memories_returned=7,
                injected_tokens=1_800,
                latency_ms=140,
            )
        )
        ledger.record(
            CallRecord(
                run_id="run-x",
                agent="memory",
                pr_id=f"pr-{ordinal}",
                pr_ordinal=ordinal,
                phase="review",
                model="claude-opus-5",
                effort="xhigh",
                usage=Usage(
                    input_tokens=3_000,
                    cache_creation_input_tokens=6_000 if ordinal == 1 else 0,
                    cache_read_input_tokens=0 if ordinal == 1 else 6_000,
                    output_tokens=1_200,
                ),
                prefix_id="M",
            )
        )
        ledger.record(
            CallRecord(
                run_id="run-x",
                agent="memory",
                pr_id=f"pr-{ordinal}",
                pr_ordinal=ordinal,
                phase="write",
                model="claude-opus-5",
                effort="xhigh",
                usage=Usage(input_tokens=1_500, output_tokens=400),
                prefix_id=None,
            )
        )
    return ledger


class TestReportCommand(unittest.TestCase):
    def test_report_writes_summary_and_finds_breakeven(self):
        with tempfile.TemporaryDirectory() as tmp:
            synthetic_run(tmp)
            self.assertEqual(main(["report", tmp]), 0)
            out = json.loads(Path(tmp, "summary.json").read_text())

        self.assertEqual(set(out["agents"]), {"baseline", "memory"})
        # The baseline reads an 80k prefix across PRs, which is exactly the
        # compressed-replay artefact spec 7d reprices -- so its production
        # number must exceed its as-measured number.
        base = out["agents"]["baseline"]
        self.assertGreater(base["billed_usd_production"], base["billed_usd"])
        self.assertEqual(base["cross_pr_read_tokens"], 240_000)
        # Memory's overhead (primer + writes) is reported, not hidden.
        self.assertGreater(out["agents"]["memory"]["memory_overhead_usd"], 0)
        self.assertEqual(out["primer"]["prs"], 4)
        self.assertIsNotNone(out["primer"]["primer_usd_per_pr"])
        # Coherent cache behaviour on both agents, so no integrity warnings.
        self.assertEqual(out["cache_integrity"], [])

    def test_report_on_a_missing_ledger_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(main(["report", tmp]), 1)


class TestDatasetCommand(unittest.TestCase):
    def test_validate_reports_problems_with_a_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            seq = {
                "repo": "redis/redis-py",
                "entries": [{"ordinal": 1, "pr_number": 1, "beats": [], "gold_labeled": False, "note": ""}],
                "spine": [],
                "style_guide_paths": [],
                "frozen_at_sha": None,
                "selection_rule": "",
            }
            path = Path(tmp, "sequence.json")
            path.write_text(json.dumps(seq))
            code = main(["dataset", "validate", "--sequence", str(path), "--store", tmp])
        self.assertEqual(code, 1)


class TestDoctorCommand(unittest.TestCase):
    def test_doctor_runs(self):
        # Exit code depends on the environment's tokens; it must not raise.
        self.assertIn(main(["doctor"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
