"""Aggregation and the spec 7d repricing rule."""

import unittest

from reviewbot.accounting import MEMORY_OP, MODEL_CALL, CallRecord, Usage
from reviewbot.analysis import (
    breakeven_ordinal,
    cache_integrity,
    cumulative,
    per_pr,
    primer_amortization,
    production_equivalent_usd,
    summary,
)

SEQ = [0]


def rec(
    agent,
    ordinal,
    phase="review",
    *,
    inp=0,
    write=0,
    read=0,
    out=0,
    prefix="P",
    kind=MODEL_CALL,
    **kw,
):
    SEQ[0] += 1
    return CallRecord(
        run_id="r",
        agent=agent,
        pr_id=f"pr-{ordinal}",
        pr_ordinal=ordinal,
        phase=phase,
        kind=kind,
        seq=SEQ[0],
        model="claude-opus-5" if kind == MODEL_CALL else None,
        effort="xhigh" if kind == MODEL_CALL else None,
        usage=Usage(
            input_tokens=inp,
            cache_creation_input_tokens=write,
            cache_read_input_tokens=read,
            output_tokens=out,
        ),
        prefix_id=prefix if kind == MODEL_CALL else None,
        **kw,
    )


class TestProductionEquivalent(unittest.TestCase):
    def test_cross_pr_reads_are_repriced_to_full_rate(self):
        records = [
            rec("baseline", 1, inp=500, write=40_000, out=800),
            rec("baseline", 2, inp=500, read=40_000, out=800),
        ]
        prod = production_equivalent_usd(records)
        as_measured = {r.seq: r.billed_usd() for r in records}

        # PR 1 wrote the cache: nothing to reprice.
        self.assertAlmostEqual(prod[records[0].seq], as_measured[records[0].seq], 12)
        # PR 2 read a prefix written under PR 1 -- in production that cache is
        # cold, so those tokens go from 0.1x to 1.0x.
        delta = 40_000 * 5e-6 * (1.0 - 0.1)
        self.assertAlmostEqual(
            prod[records[1].seq], as_measured[records[1].seq] + delta, 12
        )

    def test_within_pr_reads_stay_cheap(self):
        records = [
            rec("baseline", 1, inp=500, write=40_000),
            rec("baseline", 1, inp=100, read=40_000),  # same PR, back to back
        ]
        prod = production_equivalent_usd(records)
        self.assertAlmostEqual(prod[records[1].seq], records[1].billed_usd(), 12)

    def test_rule_applies_to_the_memory_agent_too(self):
        # The primed prefix goes cold in production as well; repricing only the
        # baseline would inflate the gap.
        records = [
            rec("memory", 0, phase="prime", inp=60_000, write=20_000, out=4_000),
            rec("memory", 1, inp=2_000, read=20_000, out=800),
        ]
        prod = production_equivalent_usd(records)
        self.assertGreater(prod[records[1].seq], records[1].billed_usd())

    def test_read_with_no_recorded_write_is_treated_as_cross_pr(self):
        records = [rec("baseline", 3, inp=100, read=10_000)]
        prod = production_equivalent_usd(records)
        self.assertGreater(prod[records[0].seq], records[0].billed_usd())


class TestPerPR(unittest.TestCase):
    def test_phase_split_and_memory_overhead(self):
        records = [
            rec("memory", 4, phase="retrieve", kind=MEMORY_OP, memory_op="search",
                memories_returned=6, injected_tokens=1_450),
            rec("memory", 4, phase="review", inp=3_000, read=20_000, out=900),
            rec("memory", 4, phase="write", inp=1_200, out=300),
        ]
        totals = per_pr(records)[("memory", 4)]
        self.assertEqual(set(totals.by_phase), {"review", "write"})
        self.assertEqual(totals.memory_ops, 1)
        self.assertEqual(totals.injected_memory_tokens, 1_450)
        self.assertEqual(totals.memories_returned, 6)
        # Overhead is the write phase only here; the retrieve op is not billable.
        self.assertAlmostEqual(
            totals.memory_overhead_usd, totals.by_phase["write"].billed_usd, 12
        )
        # Injected memory tokens are inside the review call's context volume,
        # counted once -- not added again by the retrieve op.
        self.assertEqual(totals.by_phase["review"].context_volume, 23_000)
        self.assertEqual(totals.total.context_volume, 23_000 + 1_200)

    def test_truncated_calls_surface_per_pr(self):
        records = [rec("baseline", 2, inp=10, out=32_000, truncated=True)]
        self.assertEqual(per_pr(records)[("baseline", 2)].truncated_calls, 1)


class TestBreakeven(unittest.TestCase):
    def _series(self, per_ordinal):
        records = []
        for agent, costs in per_ordinal.items():
            for ordinal, tokens in costs.items():
                records.append(
                    rec(agent, ordinal, phase="prime" if ordinal == 0 else "review", inp=tokens)
                )
        return records

    def test_memory_loses_early_and_wins_later(self):
        records = self._series(
            {
                "baseline": {1: 100_000, 2: 100_000, 3: 100_000, 4: 100_000, 5: 100_000},
                "memory": {0: 250_000, 1: 20_000, 2: 20_000, 3: 20_000, 4: 20_000, 5: 20_000},
            }
        )
        cum = cumulative(records)
        self.assertGreater(cum["memory"][0][1], 0.0)
        # cumulative: baseline 100/200/300/400/500k; memory 250/270/290/310/330/350k
        self.assertEqual(breakeven_ordinal(records), 4)

    def test_a_crossing_that_reverses_is_not_a_breakeven(self):
        records = self._series(
            {
                "baseline": {1: 100_000, 2: 10_000, 3: 100_000, 4: 100_000},
                "memory": {0: 50_000, 1: 20_000, 2: 60_000, 3: 20_000, 4: 20_000},
            }
        )
        # cum baseline 100/110/210/310k ; memory 50/70/130/150/170k
        # memory is below at 1, above at 2, below from 3 on -> answer is 3.
        self.assertEqual(breakeven_ordinal(records), 3)

    def test_no_crossing_returns_none(self):
        records = self._series({"baseline": {1: 10_000}, "memory": {0: 500_000, 1: 10_000}})
        self.assertIsNone(breakeven_ordinal(records))


class TestPrimerAmortization(unittest.TestCase):
    def test_primer_cost_divided_by_pr_count(self):
        records = [
            rec("memory", 0, phase="prime", inp=200_000, out=8_000),
            rec("memory", 1, inp=3_000),
            rec("memory", 2, inp=3_000),
            rec("memory", 3, inp=3_000),
            rec("baseline", 1, inp=100_000),
        ]
        info = primer_amortization(records)
        self.assertEqual(info["prs"], 3)
        self.assertEqual(info["primer_context_volume"], 200_000)
        self.assertAlmostEqual(info["primer_usd"], 200_000 * 5e-6 + 8_000 * 25e-6, 12)
        self.assertAlmostEqual(info["primer_usd_per_pr"], info["primer_usd"] / 3, 12)


class TestCacheIntegrity(unittest.TestCase):
    def test_repeated_prefix_with_no_read_is_reported(self):
        records = [
            rec("baseline", 1, inp=100, write=5_000),
            rec("baseline", 2, inp=5_100),
        ]
        problems = cache_integrity(records)
        self.assertEqual(len(problems), 2)
        self.assertIn("not byte-stable", problems[0])
        self.assertIn("read none", problems[1])

    def test_healthy_caching_reports_nothing(self):
        records = [
            rec("baseline", 1, inp=100, write=5_000),
            rec("baseline", 2, inp=100, read=5_000),
        ]
        self.assertEqual(cache_integrity(records), [])


class TestSummary(unittest.TestCase):
    def test_summary_shape(self):
        records = [
            rec("memory", 0, phase="prime", inp=200_000, out=5_000),
            rec("memory", 1, phase="review", inp=3_000, write=20_000, out=900),
            rec("memory", 2, phase="review", inp=3_000, read=20_000, out=900),
            rec("baseline", 1, phase="review", inp=100_000, out=900),
            rec("baseline", 2, phase="review", inp=100_000, out=900),
        ]
        out = summary(records)
        self.assertEqual(set(out["agents"]), {"baseline", "memory"})
        self.assertIn("as_measured", out["breakeven"])
        self.assertGreater(
            out["agents"]["memory"]["billed_usd_production"],
            out["agents"]["memory"]["billed_usd"],
        )
        self.assertEqual(out["agents"]["memory"]["cross_pr_read_tokens"], 20_000)


if __name__ == "__main__":
    unittest.main()
