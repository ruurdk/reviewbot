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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

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

# The memory agent first: see the module docstring.
AGENT_ORDER = ("memory", "baseline")


@dataclass
class PRResult:
    ordinal: int
    pr_id: str
    pr_number: int
    outcomes: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class RunReport:
    run_id: str
    config_fingerprint: str
    results: list[PRResult] = field(default_factory=list)
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
        self.outcomes: list[ReviewOutcome] = []
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
        """Run the primer once, before the sequence. Returns records written."""
        memory = self.agents.get("memory")
        if memory is None:
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
        return len(memory.prime(sources, docs=docs))

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
                "retrieved": len(outcome.retrieved),
                "memories_used": outcome.memories_used,
                "injected_tokens": outcome.injected_tokens,
                "retrieval_precision": outcome.retrieval_precision,
                "written": outcome.written,
                "truncated": outcome.result.truncated,
            }
        return result

    def run(self) -> RunReport:
        self.write_manifest()
        self.prime()
        results = []
        for entry in self.sequence:
            pr = self.store.load(self.sequence.repo, entry.pr_number)
            results.append(self.review_pr(pr, entry.ordinal))
        return self.report(results)

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

        return RunReport(
            run_id=self.ledger.run_id,
            config_fingerprint=self.config.fingerprint(),
            results=list(results),
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
