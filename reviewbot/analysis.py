"""Aggregation, the two headline series, and the spec 7d repricing.

Nothing here touches the network. It reads a ledger and produces the numbers
the narrative page renders, so every chart is reproducible from calls.jsonl.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .accounting import ABANDONED, CallRecord
from .config import PHASES, pricing_for

# --- spec 7d --------------------------------------------------------------
#
# The replay runs 15-25 PRs in minutes; real PRs arrive hours or days apart.
# With caching on, compression hands *both* agents cache hits they would not
# get in production -- most visibly the baseline, whose style-guide-and-spine
# prefix stays warm across consecutive PRs.
#
# Repricing rule, stated so it can be checked: a cache read whose prefix was
# last written under a *different* PR ordinal is charged at the full input rate
# (1.0x) instead of the cache-read rate (0.1x). Within-PR cache reads keep the
# 0.1x rate, because a real reviewer would also make those calls back to back.
# A read of a prefix with no recorded write is treated as cross-PR, which is
# the conservative choice in both directions.
#
# The rule is applied to BOTH agents. Applying it only to the baseline would
# inflate the gap: the memory agent's primed prefix goes cold in production too.
#
# We charge 1.0x rather than the 1.25x cache-write premium because 1.0x is the
# smaller number, and the smaller number is the one that cannot be accused of
# padding the baseline.
CROSS_PR_READ_MULTIPLIER = 1.0


@dataclass
class Bucket:
    context_volume: int = 0
    billed_usd: float = 0.0
    billed_usd_production: float = 0.0
    output_tokens: int = 0
    calls: int = 0


@dataclass
class PRTotals:
    agent: str
    pr_ordinal: int
    pr_id: str
    total: Bucket = field(default_factory=Bucket)
    by_phase: dict[str, Bucket] = field(default_factory=dict)
    # caching-tier split of the input side, for the stacked bar
    uncached_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cross_pr_read_tokens: int = 0
    memory_ops: int = 0
    memory_wait_ms: int = 0
    injected_memory_tokens: int = 0
    memories_returned: int = 0
    truncated_calls: int = 0

    @property
    def memory_overhead_usd(self) -> float:
        """Prime + retrieve + write, billed. Savings are always reported net
        of this (spec 7); a gross saving is not a result."""
        return sum(
            self.by_phase[p].billed_usd
            for p in ("prime", "retrieve", "write")
            if p in self.by_phase
        )


def production_equivalent_usd(records: Sequence[CallRecord]) -> dict[int, float]:
    """Reprice each billable record under the production-cadence rule.

    Returns {seq: usd}. Walks per agent in ledger order so cache provenance is
    tracked the way it actually happened.
    """
    out: dict[int, float] = {}
    last_write: dict[tuple[str, str], int] = {}
    for rec in sorted(records, key=lambda r: (r.agent, r.seq)):
        if not rec.billable:
            continue
        u = rec.usage
        pricing = pricing_for(rec.model or "")
        key = (rec.agent, rec.prefix_id or "")
        cross = 0
        if u.cache_read_input_tokens and rec.prefix_id:
            written_at = last_write.get(key)
            if written_at is None or written_at != rec.pr_ordinal:
                cross = u.cache_read_input_tokens
        if u.cache_creation_input_tokens and rec.prefix_id:
            last_write[key] = rec.pr_ordinal

        warm = u.cache_read_input_tokens - cross
        write_5m, write_1h = u.cache_creation_5m, u.cache_creation_1h
        if not (write_5m or write_1h) and u.cache_creation_input_tokens:
            if rec.cache_ttl == "1h":
                write_1h = u.cache_creation_input_tokens
            else:
                write_5m = u.cache_creation_input_tokens
        usd = (
            u.input_tokens * pricing.input_per_token
            + write_5m * pricing.input_per_token * pricing.cache_write_5m_multiplier
            + write_1h * pricing.input_per_token * pricing.cache_write_1h_multiplier
            + warm * pricing.input_per_token * pricing.cache_read_multiplier
            + cross * pricing.input_per_token * CROSS_PR_READ_MULTIPLIER
            + u.output_tokens * pricing.output_per_token
        )
        out[rec.seq] = usd
    return out


def cross_pr_read_tokens(records: Sequence[CallRecord]) -> dict[int, int]:
    out: dict[int, int] = {}
    last_write: dict[tuple[str, str], int] = {}
    for rec in sorted(records, key=lambda r: (r.agent, r.seq)):
        if not rec.billable:
            continue
        key = (rec.agent, rec.prefix_id or "")
        if rec.usage.cache_read_input_tokens and rec.prefix_id:
            written_at = last_write.get(key)
            if written_at is None or written_at != rec.pr_ordinal:
                out[rec.seq] = rec.usage.cache_read_input_tokens
        if rec.usage.cache_creation_input_tokens and rec.prefix_id:
            last_write[key] = rec.pr_ordinal
    return out


def abandoned_seqs(records: Iterable[CallRecord]) -> set[int]:
    """Seq numbers superseded by a resume (see accounting.ABANDONED)."""
    out: set[int] = set()
    for rec in records:
        if rec.kind == ABANDONED:
            out.update((rec.notes or {}).get("superseded_seqs") or [])
    return out


def abandoned_cost(records: Iterable[CallRecord]) -> dict[str, Any]:
    """What the superseded attempts cost. Spent, but not attributable to a PR."""
    recs = list(records)
    dropped = abandoned_seqs(recs)
    rows = [r for r in recs if r.seq in dropped and r.billable]
    return {
        "calls": len(rows),
        "billed_usd": sum(r.billed_usd() for r in rows),
        "context_volume": sum(r.context_volume for r in rows),
        "seqs": sorted(dropped),
    }


def per_pr(records: Iterable[CallRecord]) -> dict[tuple[str, int], PRTotals]:
    recs = list(records)
    # Superseded attempts are excluded here, not filtered by the caller: every
    # aggregate in this module goes through per_pr(), so excluding once is what
    # keeps the two series consistent with each other.
    dropped = abandoned_seqs(recs)
    recs = [r for r in recs if r.seq not in dropped and r.kind != ABANDONED]
    prod = production_equivalent_usd(recs)
    cross = cross_pr_read_tokens(recs)
    table: dict[tuple[str, int], PRTotals] = {}

    for rec in sorted(recs, key=lambda r: (r.agent, r.pr_ordinal, r.seq)):
        key = (rec.agent, rec.pr_ordinal)
        totals = table.setdefault(
            key, PRTotals(agent=rec.agent, pr_ordinal=rec.pr_ordinal, pr_id=rec.pr_id)
        )
        if rec.kind != "model_call":
            totals.memory_ops += 1
            if rec.memory_op == "wait":
                totals.memory_wait_ms += rec.latency_ms
            totals.injected_memory_tokens += rec.injected_tokens or 0
            # Searches only. Every memory op reports a count -- a create reports
            # what it wrote, a visibility wait reports what became searchable --
            # and summing them made the primer row read 216 for the 108 records
            # it actually wrote. This number exists to answer "how many memories
            # did retrieval pull to use three", so a write is not one of them.
            if rec.memory_op == "search":
                totals.memories_returned += rec.memories_returned or 0
            continue

        usd = rec.billed_usd()
        usd_prod = prod.get(rec.seq, usd)
        for bucket in (totals.total, totals.by_phase.setdefault(rec.phase, Bucket())):
            bucket.context_volume += rec.context_volume
            bucket.billed_usd += usd
            bucket.billed_usd_production += usd_prod
            bucket.output_tokens += rec.usage.output_tokens
            bucket.calls += 1
        totals.uncached_tokens += rec.usage.input_tokens
        totals.cache_write_tokens += rec.usage.cache_creation_input_tokens
        totals.cache_read_tokens += rec.usage.cache_read_input_tokens
        totals.cross_pr_read_tokens += cross.get(rec.seq, 0)
        if rec.truncated:
            totals.truncated_calls += 1
    return table


def cumulative(
    records: Iterable[CallRecord], *, regime: str = "as_measured"
) -> dict[str, list[tuple[int, float]]]:
    """Cumulative billed cost per agent, as [(pr_ordinal, usd), ...].

    The primer sits at ordinal 0, so the memory agent's curve starts above the
    baseline's -- that upfront spike is the honest shape and it is what makes
    the crossover mean something.
    """
    table = per_pr(records)
    series: dict[str, list[tuple[int, float]]] = {}
    for (agent, ordinal), totals in sorted(
        table.items(), key=lambda kv: (kv[0][0], kv[0][1])
    ):
        running = series.setdefault(agent, [])
        prev = running[-1][1] if running else 0.0
        usd = (
            totals.total.billed_usd
            if regime == "as_measured"
            else totals.total.billed_usd_production
        )
        running.append((ordinal, prev + usd))
    return series


def breakeven_ordinal(
    records: Iterable[CallRecord],
    *,
    regime: str = "as_measured",
    baseline: str = "baseline",
    treatment: str = "memory",
) -> int | None:
    """First PR ordinal where cumulative treatment cost drops below baseline
    *and never crosses back*. A transient crossing is not a break-even."""
    series = cumulative(records, regime=regime)
    base = dict(series.get(baseline, []))
    mem = dict(series.get(treatment, []))
    ordinals = sorted(set(base) | set(mem))
    if not ordinals:
        return None

    def cum(d: dict[int, float], upto: int) -> float:
        seen = [v for o, v in sorted(d.items()) if o <= upto]
        return seen[-1] if seen else 0.0

    answer: int | None = None
    for ordinal in ordinals:
        if cum(mem, ordinal) < cum(base, ordinal):
            if answer is None:
                answer = ordinal
        else:
            answer = None  # crossed back; the earlier crossing did not hold
    return answer


def primer_amortization(
    records: Iterable[CallRecord], *, treatment: str = "memory"
) -> dict[str, Any]:
    """Primer cost and its per-PR amortisation.

    Superseded rows are excluded, exactly as `per_pr()` excludes them. Without
    that this reported the *sum* of every primer attempt: run-2's first primer
    died when the store dropped a write, and the report then showed $4.67 here
    against $2.39 in `marginal` -- two different primer costs in one document,
    which is worse than either being wrong on its own. The abandoned money is
    real and stays in the ledger; `abandoned_cost()` is where it belongs.
    """
    dropped = abandoned_seqs(list(records))
    recs = [
        r
        for r in records
        if r.agent == treatment and r.seq not in dropped and r.kind != ABANDONED
    ]
    prime = [r for r in recs if r.phase == "prime" and r.billable]
    reviewed = sorted({r.pr_ordinal for r in recs if r.phase == "review"})
    n = len(reviewed)
    usd = sum(r.billed_usd() for r in prime)
    return {
        "primer_calls": len(prime),
        "primer_context_volume": sum(r.context_volume for r in prime),
        "primer_usd": usd,
        "prs": n,
        "primer_usd_per_pr": (usd / n) if n else None,
    }


# Cache TTLs in seconds, for telling an expired entry from an unstable prefix.
CACHE_TTL_SECONDS = {"5m": 300, "1h": 3600}


def cache_integrity(records: Iterable[CallRecord]) -> list[str]:
    """Spec 5: verify caching actually happened before trusting a cost number.

    A repeated prefix that produced no cache read has two very different causes,
    and calling them both "not byte-stable" sends a reader hunting a
    nondeterminism bug that may not exist:

    * **Within the TTL** -- something in the prefix really is varying (a
      timestamp, a PR id, a re-serialized tool list). A genuine defect.
    * **Past the TTL** -- the entry simply expired. Expected at this cadence: an
      xhigh review of a 200k-token prompt streams for 6-8 minutes, so two calls
      sharing a prefix routinely land further apart than the 5-minute default.
      Not a defect, and worth reporting as evidence rather than as a warning
      (spec 7e).

    The prefix id is a hash of the prefix bytes, so an identical id across two
    calls is itself proof of byte-stability -- which is why the elapsed time is
    the only thing that can distinguish these.
    """
    problems: list[str] = []
    seen: dict[tuple[str, str], CallRecord] = {}
    reads: dict[str, int] = {}
    writes: dict[str, int] = {}
    expired: dict[str, int] = {}
    for rec in sorted(
        (r for r in records if r.billable), key=lambda r: (r.agent, r.seq)
    ):
        reads[rec.agent] = reads.get(rec.agent, 0) + rec.usage.cache_read_input_tokens
        writes[rec.agent] = (
            writes.get(rec.agent, 0) + rec.usage.cache_creation_input_tokens
        )
        if not rec.prefix_id:
            continue
        key = (rec.agent, rec.prefix_id)
        prev = seen.get(key)
        if prev is not None and rec.usage.cache_read_input_tokens == 0:
            ttl = CACHE_TTL_SECONDS.get(rec.cache_ttl, 300)
            gap = rec.ts - prev.ts if rec.ts and prev.ts else 0.0
            if gap > ttl:
                expired[rec.agent] = expired.get(rec.agent, 0) + 1
            else:
                problems.append(
                    f"{rec.agent}: prefix {rec.prefix_id} seen at seq {prev.seq} and "
                    f"again at seq {rec.seq} only {gap:.0f}s apart (TTL {ttl}s), but "
                    "cache_read_input_tokens is 0 -- something in the prefix is "
                    "varying between calls"
                )
        seen[key] = rec
    for agent, count in sorted(expired.items()):
        problems.append(
            f"{agent}: {count} repeat(s) of a byte-stable prefix missed the cache "
            "because the entry had expired -- the calls are further apart than the "
            "TTL. Expected at this review latency; it is why caching is inert here "
            "(spec 7e), not a prefix bug."
        )
    for agent, written in writes.items():
        if written and not reads.get(agent):
            problems.append(
                f"{agent}: wrote {written} cache tokens and read none -- "
                "no request ever reused a cache entry"
            )
    return problems


def shared_prefix_freeriding(records: Iterable[CallRecord]) -> dict[str, int]:
    """Cache-read tokens an agent got on a prefix it never wrote itself.

    Both agents send a byte-identical cacheable prefix (same system prompt, same
    conventions block -- that identity is what makes the comparison clean), so
    they share one cache entry. Whichever agent runs second on a given PR reads
    a prefix the *other* agent paid to write. As-measured, that is a real
    discount the second agent did not earn.

    The runner therefore runs the memory agent first, so the free ride falls to
    the baseline and the bias runs against the thesis rather than for it. This
    function quantifies it either way, and the production-equivalent regime
    prices it out entirely (its provenance is keyed per agent).
    """
    out: dict[str, int] = {}
    written: dict[tuple[str, str], bool] = {}
    for rec in sorted((r for r in records if r.billable), key=lambda r: r.seq):
        if not rec.prefix_id:
            continue
        key = (rec.agent, rec.prefix_id)
        if rec.usage.cache_creation_input_tokens:
            written[key] = True
        elif rec.usage.cache_read_input_tokens and not written.get(key):
            out[rec.agent] = out.get(rec.agent, 0) + rec.usage.cache_read_input_tokens
    return out


def retrieval_mix(records: Iterable[CallRecord]) -> dict[str, Any]:
    """What the retrieval window actually contained, per PR, by memory type.

    The question this exists to answer: the store grows monotonically (nothing
    is ever merged or superseded) while the retrieval budget is fixed, so does
    the growing pile of episodic findings win slots, or do the primed
    conventions hold them? A `memories_returned` count cannot tell them apart --
    only the ids can, via their `ID_PREFIX` tag.

    `saturated` is the other half of the picture. A window that comes back full
    on every PR is not measuring relevance, it is measuring the limit: whatever
    ranked 21st is invisible, and there is no relevance score in the response to
    tell us whether slot 20 was worth having. Run-1 saturated on all 19 PRs.
    """
    from .memory import memory_type_of

    by_ordinal: dict[int, dict[str, Any]] = {}
    for rec in sorted(records, key=lambda r: r.seq):
        if rec.memory_op != "search" or rec.retrieved_ids is None:
            continue
        row = by_ordinal.setdefault(
            rec.pr_ordinal, {"by_type": {}, "unknown": 0, "total": 0, "saturated": False}
        )
        for memory_id in rec.retrieved_ids:
            memory_type = memory_type_of(memory_id)
            if memory_type is None:
                row["unknown"] += 1
            else:
                row["by_type"][memory_type] = row["by_type"].get(memory_type, 0) + 1
            row["total"] += 1
        if rec.search_limit and len(rec.retrieved_ids) >= rec.search_limit:
            row["saturated"] = True
    ordinals = sorted(by_ordinal)
    totals: dict[str, int] = {}
    for row in by_ordinal.values():
        for memory_type, count in row["by_type"].items():
            totals[memory_type] = totals.get(memory_type, 0) + count
    return {
        "per_pr": {str(o): by_ordinal[o] for o in ordinals},
        "totals": totals,
        "unknown": sum(r["unknown"] for r in by_ordinal.values()),
        "saturated_prs": [o for o in ordinals if by_ordinal[o]["saturated"]],
        # Absent for a run whose search rows predate id logging, which is the
        # honest answer -- not an empty mix that reads like "nothing retrieved".
        "instrumented": bool(by_ordinal),
    }


def marginal_per_pr(
    records: Iterable[CallRecord], *, regime: str = "as_measured"
) -> dict[str, Any]:
    """The steady-state per-PR saving, primer excluded.

    Why this and not the cumulative percentage: a cumulative saving is a
    function of how many PRs you happened to run, because the one-time primer is
    amortised over them. "21% cheaper over 19 PRs" is really three numbers glued
    together -- a per-review saving, a setup cost, and an arbitrary N -- and only
    the first is a property of the technique.

    So the headline figure is the marginal one: what a review costs with memory
    versus without, once the repo has been primed. The primer is then reported
    for what it is, a setup cost denominated in reviews (`primer_payback_prs`).

    Three summary statistics, and they are not interchangeable:

    * `aggregate_pct` -- sum of savings over sum of baseline cost. **Quote this
      one.** It is size-weighted, so it predicts a bill.
    * `mean_pct` -- unweighted mean of per-PR percentages. Gives a 300-line PR
      the same vote as a 12,000-line one, and since memory helps proportionally
      more on small PRs, it runs several points higher. Flattering, not wrong.
    * `median_pct` -- the typical PR, robust to the largest diffs.

    `worst_pr` is reported alongside them because a mean hides the case a
    skeptic most needs to see: in run-1 the false-positive-trap PR cost the
    memory agent *more* than the baseline.

    Note that even the marginal figure is not fully sequence-independent -- it
    drifts with PR-size mix, because on a large diff the diff itself dominates
    the prompt and memory cannot help. Run-1 measures 33.0% over its first 11
    PRs and 27.7% over all 19, the back half holding the biggest diffs.
    """
    key = "billed_usd" if regime == "as_measured" else "billed_usd_production"
    table = per_pr(records)
    primer = sum(
        getattr(t.total, key) for (_, ordinal), t in table.items() if ordinal == 0
    )
    rows: list[dict[str, Any]] = []
    for ordinal in sorted({o for _, o in table if o > 0}):
        base = table.get(("baseline", ordinal))
        mem = table.get(("memory", ordinal))
        if base is None or mem is None:
            continue  # a half-measured PR is not comparable
        b_usd, m_usd = getattr(base.total, key), getattr(mem.total, key)
        b_ctx, m_ctx = base.total.context_volume, mem.total.context_volume
        rows.append(
            {
                "pr_ordinal": ordinal,
                "pr_id": base.pr_id,
                "baseline_usd": b_usd,
                "memory_usd": m_usd,
                "saving_usd": b_usd - m_usd,
                "saving_pct": (100.0 * (b_usd - m_usd) / b_usd) if b_usd else 0.0,
                "baseline_context": b_ctx,
                "memory_context": m_ctx,
                "context_pct": (100.0 * (b_ctx - m_ctx) / b_ctx) if b_ctx else 0.0,
            }
        )
    if not rows:
        return {"n_prs": 0, "primer_usd": primer, "per_pr": []}

    tb = sum(r["baseline_usd"] for r in rows)
    tm = sum(r["memory_usd"] for r in rows)
    cb = sum(r["baseline_context"] for r in rows)
    cm = sum(r["memory_context"] for r in rows)
    pcts = sorted(r["saving_pct"] for r in rows)
    mid = len(pcts) // 2
    median = pcts[mid] if len(pcts) % 2 else (pcts[mid - 1] + pcts[mid]) / 2
    mean_saving = (tb - tm) / len(rows)
    worst = min(rows, key=lambda r: r["saving_pct"])
    best = max(rows, key=lambda r: r["saving_pct"])
    return {
        "regime": regime,
        "n_prs": len(rows),
        # The headline: size-weighted, primer excluded.
        "aggregate_pct": 100.0 * (tb - tm) / tb if tb else 0.0,
        "mean_pct": sum(pcts) / len(pcts),
        "median_pct": median,
        "mean_saving_usd": mean_saving,
        "baseline_mean_usd": tb / len(rows),
        "memory_mean_usd": tm / len(rows),
        "context_aggregate_pct": 100.0 * (cb - cm) / cb if cb else 0.0,
        # The setup cost, denominated in reviews -- which is the unit that makes
        # it comparable to the saving it has to pay back.
        "primer_usd": primer,
        "primer_payback_prs": (primer / mean_saving) if mean_saving > 0 else None,
        "worst_pr": {k: worst[k] for k in ("pr_ordinal", "pr_id", "saving_pct", "saving_usd")},
        "best_pr": {k: best[k] for k in ("pr_ordinal", "pr_id", "saving_pct", "saving_usd")},
        "per_pr": rows,
    }


def summary(records: Iterable[CallRecord]) -> dict[str, Any]:
    recs = list(records)
    table = per_pr(recs)
    agents = sorted({a for a, _ in table})
    out: dict[str, Any] = {
        "agents": {},
        "breakeven": {
            "as_measured": breakeven_ordinal(recs, regime="as_measured"),
            "production_equivalent": breakeven_ordinal(
                recs, regime="production_equivalent"
            ),
        },
        "primer": primer_amortization(recs),
        "cache_integrity": cache_integrity(recs),
        "abandoned": abandoned_cost(recs),
        "shared_prefix_freeriding": shared_prefix_freeriding(recs),
        "retrieval_mix": retrieval_mix(recs),
        # The N-independent headline. Both regimes, because the page toggles.
        "marginal": {
            "as_measured": marginal_per_pr(recs, regime="as_measured"),
            "production_equivalent": marginal_per_pr(
                recs, regime="production_equivalent"
            ),
        },
        "phases": list(PHASES),
        # One row per (agent, PR), serialized rather than left for a consumer to
        # recompute: the replay page must not do its own accounting, or the
        # number on screen stops being traceable to a ledger row.
        "per_pr": [
            {
                "agent": t.agent,
                "pr_ordinal": t.pr_ordinal,
                "pr_id": t.pr_id,
                "context_volume": t.total.context_volume,
                "billed_usd": t.total.billed_usd,
                "billed_usd_production": t.total.billed_usd_production,
                "output_tokens": t.total.output_tokens,
                "calls": t.total.calls,
                "memory_overhead_usd": t.memory_overhead_usd,
                # Context volume per phase: the ordered stack on the per-PR bars.
                "by_phase": {
                    phase: bucket.context_volume
                    for phase, bucket in sorted(t.by_phase.items())
                },
                # Caching-tier split of the input side, for the other facet.
                "tiers": {
                    "uncached": t.uncached_tokens,
                    "cache_write": t.cache_write_tokens,
                    "cache_read": t.cache_read_tokens,
                },
                "cross_pr_read_tokens": t.cross_pr_read_tokens,
                "injected_tokens": t.injected_memory_tokens,
                "memories_returned": t.memories_returned,
                "truncated_calls": t.truncated_calls,
            }
            for _, t in sorted(table.items())
        ],
    }
    for agent in agents:
        rows = [t for (a, _), t in sorted(table.items()) if a == agent]
        out["agents"][agent] = {
            "context_volume": sum(t.total.context_volume for t in rows),
            "billed_usd": sum(t.total.billed_usd for t in rows),
            "billed_usd_production": sum(t.total.billed_usd_production for t in rows),
            "output_tokens": sum(t.total.output_tokens for t in rows),
            "calls": sum(t.total.calls for t in rows),
            "memory_overhead_usd": sum(t.memory_overhead_usd for t in rows),
            "truncated_calls": sum(t.truncated_calls for t in rows),
            "cross_pr_read_tokens": sum(t.cross_pr_read_tokens for t in rows),
        }
    return out
