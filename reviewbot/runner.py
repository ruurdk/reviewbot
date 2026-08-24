"""Executes the frozen sequence for both agents.

Two ordering rules, both load-bearing:

1. **Sequential, never concurrent.** Concurrent requests cannot share a prompt
   cache entry, so a parallel run would produce cache misses that look like
   context volume.
2. **The memory agent goes first on every PR.** Both agents send a
   byte-identical cacheable prefix, so they share one cache entry and the second
   agent to run reads a prefix the first one paid to write. Running memory first
   hands that free ride to the baseline -- the bias then runs *against* the
   thesis, which is the only direction a skeptical audience will accept.
   `analysis.shared_prefix_freeriding()` measures it.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

from . import analysis
from .accounting import Ledger
from .agents import BaselineAgent, MemoryAgent, ReviewOutcome
from .config import ModelConfig, assert_comparable
from .dataset import Sequence as PRSequence
from .dataset import summary as dataset_summary
from .github import PRStore, PullRequest
from .quality import (
    GoldLabels,
    aggregate_gold,
    aggregate_proxy,
    gold_score,
    proxy_score,
    quality_table,
)
from .repo import SourceProvider, read_docs, touched_sources
from .review import Finding

# The memory agent first: see the module docstring.
AGENT_ORDER = ("memory", "baseline")


class ConcurrentRun(RuntimeError):
    """Another process is already executing this run."""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def single_process(run_dir: Path) -> Iterator[Path]:
    """Refuse to execute a run that another process is already executing.

    Rule 1 in the module docstring is not advice. Two processes on one run
    corrupt it three ways at once, and none of them announce themselves: they
    interleave rows in the append-only ledger under duplicate `seq` values, they
    re-pay for the primer while writing the same namespace, and -- worst,
    because it silently inverts the measurement -- their requests cannot share a
    prompt cache entry, so cache misses get recorded as context volume.

    This was not hypothetical. Four processes ran one sequence concurrently on
    2026-08-24, primed four times over, and burned real money before the
    duplicate `seq` numbers in the ledger gave it away.
    """
    lock = run_dir / "run.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            held = json.loads(lock.read_text())
        except (OSError, ValueError):
            held = {}
        pid = int(held.get("pid") or 0)
        if pid and _alive(pid):
            raise ConcurrentRun(
                f"run {run_dir.name} is already being executed by pid {pid} "
                f"(started {held.get('started', 'unknown')}). Two processes on one "
                "run cannot share a prompt cache entry, so the second would record "
                f"cache misses as context volume. Wait for it, or remove {lock} if "
                "you are certain it is stale."
            ) from None
        # The holder is gone: a crashed run, not a live one. Take it over.
        lock.unlink(missing_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(
            fd,
            json.dumps(
                {"pid": os.getpid(), "started": _now(), "run_dir": str(run_dir)}
            ).encode()
            + b"\n",
        )
        os.close(fd)
        yield lock
    finally:
        lock.unlink(missing_ok=True)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class PRResult:
    ordinal: int
    pr_id: str
    pr_number: int
    outcomes: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class RestoredOutcome:
    """A checkpointed outcome, carrying only what `report()` reads back.

    A resumed run must score quality over PRs it did not review in this process,
    so the fields the report needs are reconstituted from disk. Deliberately not
    a `ReviewOutcome`: the model result and the retrieved `Memory` objects are
    gone, and a type that pretends otherwise would invite code to read them.
    """

    agent: str
    pr_id: str
    findings: list[Finding]
    files_dropped: int = 0


@dataclass
class RunReport:
    run_id: str
    config_fingerprint: str
    results: list[PRResult] = field(default_factory=list)
    # `per_pr` and `rows` are lifted to the top level for the replay page: it
    # renders these verbatim and does no accounting of its own (web/src/data/
    # contract.js). `per_pr` is the accounting table joined with each agent's
    # outcome detail; `rows` is the dataset disclosure table.
    per_pr: list[dict[str, Any]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    accounting: dict[str, Any] = field(default_factory=dict)
    quality_gold: dict[str, Any] = field(default_factory=dict)
    quality_proxy: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class SequenceRunner:
    def __init__(
        self,
        *,
        sequence: PRSequence,
        store: PRStore,
        ledger: Ledger,
        provider: SourceProvider,
        config: ModelConfig,
        baseline: BaselineAgent | None = None,
        memory: MemoryAgent | None = None,
        gold: dict[int, GoldLabels] | None = None,
    ):
        self.sequence = sequence
        self.store = store
        self.ledger = ledger
        self.provider = provider
        self.config = config
        self.agents: dict[str, Any] = {}
        if memory is not None:
            self.agents["memory"] = memory
        if baseline is not None:
            self.agents["baseline"] = baseline
        self.gold = gold or {}
        self.outcomes: list[ReviewOutcome | RestoredOutcome] = []
        # A full sequence is a multi-hour, real-money run. Checkpointing each
        # PR as it completes means a transient failure costs the PR in flight,
        # not the whole run -- and the primer, which is the single most
        # expensive call group, is never re-paid.
        self.checkpoint_path = self.ledger.run_dir / "checkpoint.jsonl"
        self.resumed: list[int] = []
        self.superseded: list[int] = []
        # Both agents were handed the same ModelConfig object by construction;
        # assert it anyway, because a future refactor is exactly how that breaks.
        assert_comparable(*[a.client.config for a in self.agents.values()])

    def write_manifest(self) -> dict[str, Any]:
        manifest = {
            "run_id": self.ledger.run_id,
            "config_fingerprint": self.config.fingerprint(),
            "config": json.loads(self.config.canonical()),
            "agent_order": list(AGENT_ORDER),
            "agents": sorted(self.agents),
            "execution": "sequential (concurrent requests cannot share a prompt cache entry)",
            "dataset": dataset_summary(self.sequence, self.store),
            "gold_labelled_prs": sorted(self.gold),
            "extraction_mode": "explicit client-side writes; automatic extraction off",
        }
        self.ledger.write_manifest(manifest)
        return manifest

    def prime(self) -> int:
        """Run the primer once, before the sequence. Returns records written.

        Idempotent across processes: the record ids are a pure function of the
        content, but the *model calls* that produce them are not free, so a
        completed primer is recorded on disk and skipped on resume.
        """
        memory = self.agents.get("memory")
        if memory is None:
            return 0
        marker = self.ledger.run_dir / "primed.json"
        if marker.exists():
            primed = json.loads(marker.read_text())
            if primed.get("namespace") == memory.memory.namespace:
                memory.primed_ids.extend(primed.get("ids") or [])
                return 0
        sha = self.sequence.frozen_at_sha
        if not sha:
            raise ValueError("sequence.frozen_at_sha is required to prime")
        sources = {
            path: text
            for path, text in (
                (p, self.provider.read(p, sha)) for p in self.sequence.spine
            )
            if text is not None
        }
        if not sources:
            raise ValueError(
                f"none of the spine modules were readable at {sha}: {self.sequence.spine}"
            )
        docs = read_docs(self.provider, self.sequence.style_guide_paths, sha)
        records = memory.prime(sources, docs=docs)
        marker.write_text(
            json.dumps(
                {
                    "namespace": memory.memory.namespace,
                    "frozen_at_sha": sha,
                    "modules": sorted(sources),
                    "ids": [r.id for r in records],
                },
                indent=2,
            )
            + "\n"
        )
        return len(records)

    def review_pr(self, pr: PullRequest, ordinal: int) -> PRResult:
        result = PRResult(ordinal=ordinal, pr_id=pr.pr_id, pr_number=pr.number)
        for name in AGENT_ORDER:
            agent = self.agents.get(name)
            if agent is None:
                continue
            outcome = agent.review_pr(pr, ordinal)
            self.outcomes.append(outcome)
            result.outcomes[name] = {
                "findings": [asdict(f) for f in outcome.findings],
                "files_read": outcome.files_read,
                "files_dropped_over_budget": outcome.files_dropped,
                "retrieved": len(outcome.retrieved),
                "memories_used": outcome.memories_used,
                "injected_tokens": outcome.injected_tokens,
                "retrieval_precision": outcome.retrieval_precision,
                "written": outcome.written,
                "truncated": outcome.result.truncated,
            }
        return result

    def load_checkpoint(self) -> dict[int, PRResult]:
        """Completed PRs from a previous process, keyed by ordinal.

        A PR counts as complete only if every agent this run is executing has an
        outcome for it -- a crash between the memory and baseline review of the
        same PR re-runs that PR rather than reporting a half-measured one.
        """
        done: dict[int, PRResult] = {}
        if not self.checkpoint_path.exists():
            return done
        with self.checkpoint_path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if not set(self.agents) <= set(row.get("outcomes") or {}):
                    continue
                done[row["ordinal"]] = PRResult(**row)
        return done

    def supersede_partial(self, ordinal: int) -> int:
        """Mark any already-billed rows for `ordinal` as superseded.

        The ledger is append-only, so the spend stays on the record; the marker
        keeps it out of the per-PR series. `analysis.abandoned_cost()` reports
        the total separately, because money spent on an abandoned attempt is
        real and hiding it would be the opposite of the point.
        """
        stale = [
            rec.seq
            for rec in self.ledger.records()
            if rec.pr_ordinal == ordinal and rec.billable
        ]
        if stale:
            self.ledger.mark_abandoned(
                stale,
                f"resumed run redoing PR ordinal {ordinal}; an earlier attempt "
                "billed these calls but did not complete the PR",
            )
            self.superseded.extend(stale)
        return len(stale)

    def restore(self, result: PRResult) -> None:
        for name, payload in result.outcomes.items():
            self.outcomes.append(
                RestoredOutcome(
                    agent=name,
                    pr_id=result.pr_id,
                    findings=[Finding.from_json(f) for f in payload.get("findings") or []],
                    files_dropped=payload.get("files_dropped_over_budget") or 0,
                )
            )

    def run(self) -> RunReport:
        with single_process(self.ledger.run_dir):
            return self._run()

    def _run(self) -> RunReport:
        self.write_manifest()
        done = self.load_checkpoint()
        self.prime()
        results = []
        for entry in self.sequence:
            completed = done.get(entry.ordinal)
            if completed is not None:
                self.resumed.append(entry.ordinal)
                self.restore(completed)
                results.append(completed)
                continue
            # This PR is about to be redone. If an earlier process already
            # billed calls for it -- a crash between the review and the write
            # phase does exactly that -- those rows must not also count, or the
            # PR's cost is reported twice.
            self.supersede_partial(entry.ordinal)
            pr = self.store.load(self.sequence.repo, entry.pr_number)
            result = self.review_pr(pr, entry.ordinal)
            with self.checkpoint_path.open("a") as fh:
                fh.write(json.dumps(asdict(result), default=str) + "\n")
                fh.flush()
            results.append(result)
        return self.report(results)

    @staticmethod
    def _join_outcomes(
        per_pr: Sequence[dict[str, Any]], results: Sequence[PRResult]
    ) -> list[dict[str, Any]]:
        """Attach each agent's review detail to its accounting row.

        The two halves are measured separately -- tokens come from the ledger,
        `files_read` and the retrieval counts from the review outcome -- and the
        page needs them on one row to answer "what did this cost, and what did
        it read to get there".
        """
        detail = {
            (agent, result.ordinal): payload
            for result in results
            for agent, payload in result.outcomes.items()
        }
        joined = []
        for row in per_pr:
            extra = detail.get((row["agent"], row["pr_ordinal"])) or {}
            joined.append(
                {
                    **row,
                    "files_read": extra.get("files_read"),
                    "files_dropped_over_budget": extra.get("files_dropped_over_budget"),
                    "retrieved": extra.get("retrieved"),
                    "memories_used": len(extra.get("memories_used") or [])
                    if extra.get("memories_used") is not None
                    else None,
                    "retrieval_precision": extra.get("retrieval_precision"),
                    "findings": len(extra.get("findings") or [])
                    if extra.get("findings") is not None
                    else None,
                }
            )
        return joined

    def report(self, results: Sequence[PRResult]) -> RunReport:
        records = list(self.ledger.records())
        accounting = analysis.summary(records)
        warnings = list(accounting["cache_integrity"])
        freeriding = accounting.get("shared_prefix_freeriding") or {}
        for agent, tokens in sorted(freeriding.items()):
            warnings.append(
                f"{agent} read {tokens:,} cache tokens on a shared prefix it did "
                "not write; the production-equivalent series prices this out"
            )
        dropped = sum(o.files_dropped for o in self.outcomes)
        if dropped:
            warnings.append(
                f"{dropped} touched file(s) exceeded the per-review source budget "
                "and were not read; the budget lowers the baseline's cost, so the "
                "measured gap stays conservative"
            )
        abandoned = accounting.get("abandoned") or {}
        if abandoned.get("calls"):
            warnings.append(
                f"{abandoned['calls']} call(s) costing ${abandoned['billed_usd']:.2f} "
                f"({abandoned['context_volume']:,} context tokens) were abandoned by a "
                "resume and are excluded from the per-PR series; the spend was real "
                "and stays in the ledger"
            )
        if self.resumed:
            warnings.append(
                f"{len(self.resumed)} PR(s) were restored from a checkpoint rather "
                f"than reviewed in this process (ordinals "
                f"{', '.join(str(o) for o in self.resumed)}); their token rows come "
                "from the same ledger, but the cache state they saw was a different "
                "process's"
            )
        truncated = sum(a["truncated_calls"] for a in accounting["agents"].values())
        if truncated:
            warnings.append(
                f"{truncated} call(s) hit max_tokens -- those reviews were cut off "
                "mid-finding and their findings are incomplete"
            )

        gold_by_agent: dict[str, Any] = {}
        proxy_by_agent: dict[str, Any] = {}
        for name in sorted(self.agents):
            outcomes = [o for o in self.outcomes if o.agent == name]
            proxies, golds = [], []
            for outcome in outcomes:
                pr = self.store.load(
                    self.sequence.repo, int(outcome.pr_id.rsplit("#", 1)[-1])
                )
                proxies.append(proxy_score(pr, name, outcome.findings))
                labels = self.gold.get(pr.number)
                if labels is not None:
                    golds.append(gold_score(labels, name, outcome.findings))
            if proxies:
                proxy_by_agent[name] = aggregate_proxy(proxies)
            if golds:
                gold_by_agent[name] = aggregate_gold(golds)

        if not gold_by_agent:
            warnings.append(
                "no gold labels were loaded -- quality is proxy-only, which spec "
                "7c rules out as a sole standard"
            )

        dataset = dataset_summary(self.sequence, self.store)
        return RunReport(
            run_id=self.ledger.run_id,
            config_fingerprint=self.config.fingerprint(),
            results=list(results),
            per_pr=self._join_outcomes(accounting.get("per_pr") or [], results),
            rows=dataset["rows"],
            accounting=accounting,
            quality_gold=gold_by_agent,
            quality_proxy=proxy_by_agent,
            warnings=warnings,
        )


def save_report(report: RunReport, run_dir: str | Path) -> Path:
    path = Path(run_dir) / "report.json"
    path.write_text(json.dumps(report.to_json(), indent=2, default=str) + "\n")
    return path


def render_report(report: RunReport) -> str:
    a = report.accounting["agents"]
    lines = [
        f"run {report.run_id}  config {report.config_fingerprint}",
        "",
        f"{'agent':<10}{'context vol':>14}{'billed $':>12}{'prod-equiv $':>14}{'overhead $':>12}",
        "-" * 62,
    ]
    for agent in sorted(a):
        row = a[agent]
        lines.append(
            f"{agent:<10}{row['context_volume']:>14,}{row['billed_usd']:>12.4f}"
            f"{row['billed_usd_production']:>14.4f}{row['memory_overhead_usd']:>12.4f}"
        )
    primer = report.accounting["primer"]
    if primer.get("primer_usd_per_pr"):
        lines += [
            "",
            f"primer ${primer['primer_usd']:.4f} over {primer['prs']} PRs "
            f"= ${primer['primer_usd_per_pr']:.4f}/PR",
        ]
    lines += [
        "",
        f"break-even PR (as measured):        {report.accounting['breakeven']['as_measured']}",
        f"break-even PR (production cadence): {report.accounting['breakeven']['production_equivalent']}",
    ]
    if report.quality_gold:
        lines += ["", quality_table(report.quality_gold, kind="gold")]
    if report.quality_proxy:
        lines += ["", quality_table(report.quality_proxy, kind="proxy")]
    if report.warnings:
        lines += [""] + [f"WARNING: {w}" for w in report.warnings]
    return "\n".join(lines)
