"""The accounting math is the demo's credibility, so it gets exact assertions."""

import json
import tempfile
import unittest
from pathlib import Path

from reviewbot.accounting import CallRecord, Ledger, Usage, total_billed_usd
from reviewbot.config import ModelConfig, pricing_for

OPUS = pricing_for("claude-opus-5")


class TestUsage(unittest.TestCase):
    def test_context_volume_is_the_sum_not_input_tokens(self):
        u = Usage.from_response(
            {
                "input_tokens": 1_000,
                "cache_creation_input_tokens": 2_000,
                "cache_read_input_tokens": 40_000,
                "output_tokens": 500,
            }
        )
        # The trap this whole module exists to prevent: input_tokens alone
        # would report 1k for a 43k prompt.
        self.assertEqual(u.input_tokens, 1_000)
        self.assertEqual(u.context_volume, 43_000)

    def test_billed_usd_weights_each_tier(self):
        u = Usage(
            input_tokens=1_000,
            cache_creation_input_tokens=2_000,
            cache_read_input_tokens=40_000,
            output_tokens=500,
        )
        expected = (
            1_000 * 5e-6
            + 2_000 * 5e-6 * 1.25
            + 40_000 * 5e-6 * 0.1
            + 500 * 25e-6
        )
        self.assertAlmostEqual(u.billed_usd(OPUS), expected, places=12)

    def test_1h_ttl_costs_twice_the_write(self):
        u = Usage(cache_creation_input_tokens=10_000)
        self.assertAlmostEqual(u.billed_usd(OPUS, "1h"), 10_000 * 5e-6 * 2.0, places=12)
        self.assertAlmostEqual(u.billed_usd(OPUS, "5m"), 10_000 * 5e-6 * 1.25, places=12)

    def test_per_ttl_breakdown_beats_the_configured_ttl(self):
        u = Usage.from_response(
            {
                "cache_creation_input_tokens": 3_000,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 1_000,
                    "ephemeral_1h_input_tokens": 2_000,
                },
            }
        )
        # Priced per bucket, so the configured ttl argument is ignored.
        expected = 1_000 * 5e-6 * 1.25 + 2_000 * 5e-6 * 2.0
        self.assertAlmostEqual(u.billed_usd(OPUS, "5m"), expected, places=12)

    def test_merge_keeps_input_side_and_takes_latest_output(self):
        start = Usage(input_tokens=900, cache_read_input_tokens=40_000, output_tokens=1)
        delta = Usage(output_tokens=812)
        merged = start.merge(delta)
        self.assertEqual(merged.input_tokens, 900)
        self.assertEqual(merged.cache_read_input_tokens, 40_000)
        self.assertEqual(merged.output_tokens, 812)


class TestCallRecord(unittest.TestCase):
    def test_memory_ops_are_not_billed_and_do_not_double_count(self):
        rec = CallRecord(
            run_id="r",
            agent="memory",
            pr_id="pr-1",
            pr_ordinal=1,
            phase="retrieve",
            kind="memory_op",
            memory_op="search",
            memories_returned=6,
            injected_tokens=1_450,
        )
        # Injected memory tokens land inside the review call's context volume;
        # counting them here too would inflate memory's cost.
        self.assertFalse(rec.billable)
        self.assertEqual(rec.context_volume, 0)
        self.assertEqual(rec.billed_usd(), 0.0)

    def test_round_trip_through_json(self):
        rec = CallRecord(
            run_id="r",
            agent="baseline",
            pr_id="pr-9",
            pr_ordinal=9,
            phase="review",
            model="claude-opus-5",
            effort="xhigh",
            usage=Usage(input_tokens=10, output_tokens=20),
            prefix_id="abc",
        )
        back = CallRecord.from_json(json.loads(json.dumps(rec.to_json())))
        self.assertEqual(back.usage, rec.usage)
        self.assertEqual(back.prefix_id, "abc")
        self.assertEqual(back.billed_usd(), rec.billed_usd())


class TestLedger(unittest.TestCase):
    def test_append_only_and_seq_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = Ledger(tmp, "run-1", manifest={"config_fingerprint": "x"})
            for i in range(3):
                led.record(
                    CallRecord(
                        run_id="run-1",
                        agent="baseline",
                        pr_id=f"pr-{i}",
                        pr_ordinal=i,
                        phase="review",
                        model="claude-opus-5",
                        usage=Usage(input_tokens=100),
                    )
                )
            self.assertEqual([r.seq for r in led.records()], [1, 2, 3])

            # A resumed run continues the sequence instead of overwriting.
            again = Ledger(tmp, "run-1")
            again.record(
                CallRecord(
                    run_id="run-1",
                    agent="memory",
                    pr_id="pr-3",
                    pr_ordinal=3,
                    phase="review",
                    model="claude-opus-5",
                    usage=Usage(input_tokens=100),
                )
            )
            self.assertEqual([r.seq for r in again.records()], [1, 2, 3, 4])
            self.assertEqual(again.read_manifest()["config_fingerprint"], "x")
            self.assertAlmostEqual(total_billed_usd(again.records()), 4 * 100 * 5e-6, 12)
            self.assertEqual(
                len(Path(tmp, "calls.jsonl").read_text().strip().splitlines()), 4
            )


class TestConfoundGuard(unittest.TestCase):
    def test_identical_configs_compare(self):
        from reviewbot.config import assert_comparable

        assert_comparable(ModelConfig(), ModelConfig())

    def test_differing_effort_is_a_confound(self):
        from reviewbot.config import ConfoundError, assert_comparable

        with self.assertRaises(ConfoundError):
            assert_comparable(ModelConfig(effort="xhigh"), ModelConfig(effort="low"))

    def test_budget_tokens_rejected(self):
        with self.assertRaises(ValueError):
            ModelConfig(thinking={"type": "enabled", "budget_tokens": 4096})

    def test_thinking_disabled_at_xhigh_rejected(self):
        with self.assertRaises(ValueError):
            ModelConfig(thinking={"type": "disabled"}, effort="xhigh")

    def test_request_params_omit_per_pr_content(self):
        params = ModelConfig().request_params()
        self.assertEqual(params["output_config"], {"effort": "xhigh"})
        self.assertNotIn("system", params)
        self.assertNotIn("messages", params)


if __name__ == "__main__":
    unittest.main()
