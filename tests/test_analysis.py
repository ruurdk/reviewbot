"""Aggregation and the spec 7d repricing rule."""

import unittest

from dataclasses import replace

from reviewbot.accounting import ABANDONED, MEMORY_OP, MODEL_CALL, CallRecord, Usage
from reviewbot.analysis import (
    abandoned_cost,
    breakeven_ordinal,
    cache_integrity,
    cumulative,
    per_pr,
    primer_amortization,
    marginal_per_pr,
    production_equivalent_usd,
    retrieval_mix,
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
        self.assertIn("varying between calls", problems[0])
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


class TestMemoriesReturnedCountsSearchesOnly(unittest.TestCase):
    def test_writes_and_visibility_waits_are_not_retrievals(self):
        """The primer row once read 216 memories returned for 108 written.

        `memories_returned` answers "how many did retrieval pull to use three"
        (spec 7b). A create reporting what it wrote, and a visibility wait
        reporting what became searchable, are not retrievals.
        """
        rows = [
            rec("memory", 0, "prime", kind=MEMORY_OP, memory_op="create", memories_returned=100),
            rec("memory", 0, "prime", kind=MEMORY_OP, memory_op="create", memories_returned=8),
            rec("memory", 0, "prime", kind=MEMORY_OP, memory_op="wait", memories_returned=108),
            rec("memory", 1, "retrieve", kind=MEMORY_OP, memory_op="search", memories_returned=20),
        ]
        table = per_pr(rows)
        self.assertEqual(table[("memory", 0)].memories_returned, 0)
        self.assertEqual(table[("memory", 1)].memories_returned, 20)
        # The ops themselves are still counted.
        self.assertEqual(table[("memory", 0)].memory_ops, 3)


if __name__ == "__main__":
    unittest.main()

class TestCacheExpiryIsNotAPrefixBug(unittest.TestCase):
    """A repeated prefix with no read means two different things.

    The prefix id is a hash of the prefix bytes, so an identical id across two
    calls already proves byte-stability. Only the elapsed time can tell an
    expired entry from a genuinely varying prefix -- and reporting expiry as
    instability sends a reader hunting a nondeterminism bug that is not there.
    Every such warning in run-1 was expiry: 13 to 23 minutes apart on a 5-minute
    TTL, because an xhigh review streams for 6-8 minutes.
    """

    def _two_calls(self, gap_seconds):
        a = rec("baseline", 1, inp=100, write=50, prefix="P")
        b = rec("baseline", 2, inp=100, read=0, prefix="P")
        return [replace(a, ts=1000.0), replace(b, ts=1000.0 + gap_seconds)]

    def test_a_gap_beyond_the_ttl_is_reported_as_expiry(self):
        problems = cache_integrity(self._two_calls(900))  # 15 min on a 5 min TTL
        joined = " ".join(problems)
        self.assertIn("had expired", joined)
        self.assertIn("not a prefix bug", joined)
        self.assertNotIn("varying between calls", joined)

    def test_a_gap_inside_the_ttl_is_still_reported_as_a_defect(self):
        problems = cache_integrity(self._two_calls(30))
        joined = " ".join(problems)
        self.assertIn("varying between calls", joined)
        self.assertNotIn("had expired", joined)

    def test_the_ttl_used_is_the_one_the_call_asked_for(self):
        """A 15-minute gap is expiry at 5m and a real defect at 1h."""
        calls = [replace(r, cache_ttl="1h") for r in self._two_calls(900)]
        self.assertIn("varying between calls", " ".join(cache_integrity(calls)))


class TestRetrievalMix(unittest.TestCase):
    """Which records filled the window, not just how many.

    This is the measurement the compaction question hinges on: the store only
    grows, the window is fixed, so are the accumulating episodic findings
    winning slots or are the primed conventions holding them? A count cannot
    say. The id prefix can.
    """

    def _search(self, ordinal, ids, limit=4):
        return rec(
            "memory",
            ordinal,
            phase="retrieve",
            kind=MEMORY_OP,
            memory_op="search",
            memories_returned=len(ids),
            search_limit=limit,
            retrieved_ids=ids,
        )

    def test_records_are_attributed_to_their_memory_type(self):
        mix = retrieval_mix(
            [self._search(1, ["conv-a-one", "conv-a-two", "find-a-x-1"])]
        )
        self.assertEqual(
            mix["per_pr"]["1"]["by_type"],
            {"repo_convention": 2, "review_finding": 1},
        )
        self.assertEqual(mix["totals"], {"repo_convention": 2, "review_finding": 1})

    def test_a_split_budget_sums_its_searches_into_one_pr_row(self):
        mix = retrieval_mix(
            [
                self._search(1, ["conv-a-one", "conv-a-two"], limit=2),
                self._search(1, ["find-a-x-1"], limit=2),
            ]
        )
        self.assertEqual(mix["per_pr"]["1"]["total"], 3)
        self.assertEqual(
            mix["per_pr"]["1"]["by_type"],
            {"repo_convention": 2, "review_finding": 1},
        )

    def test_a_full_window_is_flagged_as_saturated(self):
        """A window that comes back full is reporting the limit, not relevance:
        whatever ranked one past the limit is invisible, and the response
        carries no score to say whether the last slot was worth having."""
        mix = retrieval_mix(
            [
                self._search(1, ["conv-a-one", "conv-a-two"], limit=2),
                self._search(2, ["conv-a-one"], limit=2),
            ]
        )
        self.assertEqual(mix["saturated_prs"], [1])

    def test_a_foreign_id_is_counted_as_unknown_not_silently_typed(self):
        mix = retrieval_mix([self._search(1, ["conv-a-one", "somebody-elses"])])
        self.assertEqual(mix["per_pr"]["1"]["unknown"], 1)
        self.assertEqual(mix["per_pr"]["1"]["by_type"], {"repo_convention": 1})

    def test_a_run_that_predates_id_logging_reports_uninstrumented(self):
        """Not an empty mix -- that reads as 'retrieval returned nothing', which
        is the opposite of what run-1's saturated window actually did."""
        old = replace(
            self._search(1, ["conv-a-one"]), retrieved_ids=None, memories_returned=20
        )
        mix = retrieval_mix([old])
        self.assertFalse(mix["instrumented"])
        self.assertEqual(mix["per_pr"], {})

    def test_creates_and_waits_are_not_counted_as_retrieval(self):
        # Every memory op reports a count; only a search reports a window.
        written = rec(
            "memory",
            1,
            phase="write",
            kind=MEMORY_OP,
            memory_op="create",
            memories_returned=5,
            retrieved_ids=["find-a-x-1"],
        )
        mix = retrieval_mix([written, self._search(1, ["conv-a-one"])])
        self.assertEqual(mix["totals"], {"repo_convention": 1})


class TestMarginalPerPr(unittest.TestCase):
    """The headline figure, and why it is not the cumulative percentage.

    A cumulative saving is a function of sequence length, because the one-time
    primer is amortised over it -- "21% over 19 PRs" is a per-review saving, a
    setup cost and an arbitrary N glued together. Only the first is a property
    of the technique, so that is what gets reported.
    """

    def _run(self, prs, primer_usd=1.0):
        """prs: [(baseline_out, memory_out)] -- output tokens drive the cost."""
        recs = [rec("memory", 0, phase="prime", out=int(primer_usd / 25e-6))]
        for i, (b, m) in enumerate(prs, 1):
            recs.append(rec("baseline", i, out=b))
            recs.append(rec("memory", i, out=m))
        return recs

    def test_the_primer_is_excluded_from_the_per_review_figure(self):
        # Two PRs, memory half the cost of baseline on each. The per-review
        # saving is 50% regardless of how big the primer is.
        for primer in (0.5, 5.0, 50.0):
            m = marginal_per_pr(self._run([(1000, 500), (1000, 500)], primer))
            self.assertAlmostEqual(m["aggregate_pct"], 50.0, places=6)

    def test_the_primer_is_reported_in_reviews_not_dollars_alone(self):
        m = marginal_per_pr(self._run([(1000, 500)] * 4, primer_usd=1.0))
        # $1.00 primer against $0.0125 saved per PR (500 tok x $25/MTok).
        self.assertAlmostEqual(m["mean_saving_usd"], 500 * 25e-6, places=9)
        self.assertAlmostEqual(m["primer_payback_prs"], 1.0 / (500 * 25e-6), places=3)

    def test_aggregate_is_size_weighted_and_the_mean_is_not(self):
        """The distinction that decides which number is quotable.

        One huge PR where memory saves nothing, one tiny PR where it saves
        everything: the unweighted mean says +50%, the size-weighted aggregate
        says the truth about the bill.
        """
        m = marginal_per_pr(self._run([(10_000, 10_000), (100, 0)]))
        self.assertAlmostEqual(m["mean_pct"], 50.0, places=6)
        self.assertAlmostEqual(m["aggregate_pct"], 100 * 100 / 10_100, places=6)
        self.assertLess(m["aggregate_pct"], m["mean_pct"])

    def test_the_worst_pr_is_surfaced_not_averaged_away(self):
        # A PR where memory costs MORE is the case a skeptic needs to see.
        m = marginal_per_pr(self._run([(1000, 500), (1000, 2000)]))
        self.assertEqual(m["worst_pr"]["pr_ordinal"], 2)
        self.assertLess(m["worst_pr"]["saving_pct"], 0)
        self.assertEqual(m["best_pr"]["pr_ordinal"], 1)

    def test_a_half_measured_pr_is_skipped_not_counted_as_a_win(self):
        """One agent missing means the PR is not comparable. Counting it would
        credit memory with a baseline cost of zero."""
        recs = self._run([(1000, 500)])
        recs.append(rec("memory", 2, out=500))  # no baseline row for PR 2
        m = marginal_per_pr(recs)
        self.assertEqual(m["n_prs"], 1)
        self.assertEqual([r["pr_ordinal"] for r in m["per_pr"]], [1])

    def test_both_regimes_are_available(self):
        recs = self._run([(1000, 500)] * 3)
        a = marginal_per_pr(recs, regime="as_measured")
        b = marginal_per_pr(recs, regime="production_equivalent")
        self.assertEqual(a["regime"], "as_measured")
        self.assertEqual(b["regime"], "production_equivalent")

    def test_an_empty_run_does_not_divide_by_zero(self):
        m = marginal_per_pr([])
        self.assertEqual(m["n_prs"], 0)
        self.assertEqual(m["per_pr"], [])

    def test_it_appears_in_the_summary_under_both_regimes(self):
        out = summary(self._run([(1000, 500)] * 3))
        self.assertIn("marginal", out)
        self.assertEqual(set(out["marginal"]), {"as_measured", "production_equivalent"})
        self.assertGreater(out["marginal"]["as_measured"]["aggregate_pct"], 0)


class TestPrimerExcludesAbandoned(unittest.TestCase):
    """Two primer costs in one report is worse than either being wrong.

    Run-2's first primer attempt died after paying for its model calls (the
    store reported creating a record it never stored). `per_pr` excluded those
    rows, `primer_amortization` did not, and the report printed $4.67 for the
    primer next to $2.39 for the same primer.
    """

    def _with_abandoned(self):
        first = rec("memory", 0, phase="prime", out=40_000)   # the dead attempt
        second = rec("memory", 0, phase="prime", out=20_000)  # the real one
        marker = CallRecord(
            run_id="r", agent="harness", pr_id="-", pr_ordinal=0, phase="-",
            kind=ABANDONED, seq=999,
            notes={"superseded_seqs": [first.seq], "reason": "primer aborted"},
        )
        reviews = [rec("memory", 1, out=1000), rec("baseline", 1, out=1000)]
        return [first, second, marker, *reviews], first, second

    def test_a_superseded_primer_attempt_is_not_added_to_the_real_one(self):
        recs, first, second = self._with_abandoned()
        primer = primer_amortization(recs)
        self.assertEqual(primer["primer_calls"], 1)
        self.assertAlmostEqual(primer["primer_usd"], second.billed_usd(), places=9)

    def test_the_report_agrees_with_itself(self):
        """The invariant that actually matters: the primer figure in `primer`
        and the one in `marginal` must be the same number."""
        recs, _, _ = self._with_abandoned()
        out = summary(recs)
        self.assertAlmostEqual(
            out["primer"]["primer_usd"],
            out["marginal"]["as_measured"]["primer_usd"],
            places=9,
        )

    def test_the_abandoned_spend_is_still_reported_somewhere(self):
        # Excluded from the primer, not erased -- the money was really spent.
        recs, first, _ = self._with_abandoned()
        self.assertAlmostEqual(
            abandoned_cost(recs)["billed_usd"], first.billed_usd(), places=9
        )
