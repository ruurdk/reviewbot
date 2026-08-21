"""Quality scoring: the mandatory guardrail (spec 7c).

Two scores, deliberately never combined into one number:

- **Proxy**, across the whole sequence: agreement with the merged PR's real
  human review comments. Cheap and complete, but blind on the PRs where the
  humans said nothing -- a third of sampled redis-py PRs have zero inline
  comments, and on those the proxy cannot tell "the agent said nothing useful"
  from "nobody said anything".
- **Gold**, on the 5-8 hand-labelled beat PRs: precision, recall, and
  false-positive rate against labels written by a person.

The gold format carries `must_not_flag` items as well as real defects. That is
what makes the false-positive trap (spec 8) measurable rather than anecdotal:
the trap is a labelled pattern, and an agent that flags it is scored for it.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .github import HumanComment, PullRequest
from .review import Finding

DEFAULT_LINE_WINDOW = 5

TRUE_DEFECT = "defect"
MUST_NOT_FLAG = "must_not_flag"


@dataclass
class GoldItem:
    """One hand-written label."""

    id: str
    file: str
    line: int | None
    kind: str = TRUE_DEFECT  # defect | must_not_flag
    category: str = ""
    severity: str = ""
    description: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in (TRUE_DEFECT, MUST_NOT_FLAG):
            raise ValueError(f"gold item {self.id}: kind must be defect|must_not_flag")


@dataclass
class GoldLabels:
    pr_number: int
    labeller: str
    items: list[GoldItem] = field(default_factory=list)
    note: str = ""

    @property
    def defects(self) -> list[GoldItem]:
        return [i for i in self.items if i.kind == TRUE_DEFECT]

    @property
    def traps(self) -> list[GoldItem]:
        return [i for i in self.items if i.kind == MUST_NOT_FLAG]

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "GoldLabels":
        d = json.loads(Path(path).read_text())
        items = [GoldItem(**i) for i in d.pop("items", [])]
        return cls(items=items, **d)

    def save(self, path: str | os.PathLike[str]) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n")


def same_place(
    file_a: str, line_a: int | None, file_b: str, line_b: int | None, window: int
) -> bool:
    """Location match with a tolerance window.

    Line-level equality is too strict -- a reviewer and an agent can flag the
    same defect a few lines apart -- and file-level equality is too loose on a
    2000-line module. A missing line on either side degrades to file match,
    which is the only defensible reading of "no line was determinable".
    """
    if file_a != file_b:
        return False
    if line_a is None or line_b is None:
        return True
    return abs(line_a - line_b) <= window


@dataclass
class ProxyScore:
    pr_number: int
    agent: str
    n_findings: int
    n_human_comments: int
    matched: int
    agreement: float | None  # matched / findings
    coverage: float | None  # matched / human comments
    blind: bool  # no human comments: coverage is undefined, not zero

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def proxy_score(
    pr: PullRequest,
    agent: str,
    findings: Sequence[Finding],
    *,
    window: int = DEFAULT_LINE_WINDOW,
    include_bots: bool = False,
) -> ProxyScore:
    comments: list[HumanComment] = [
        c
        for c in pr.comments
        if (include_bots or not c.author_is_bot) and c.kind == "inline" and c.path
    ]
    matched_pairs: set[int] = set()
    matched_findings = 0
    for finding in findings:
        for i, comment in enumerate(comments):
            if same_place(finding.file, finding.line, comment.path or "", comment.line, window):
                matched_findings += 1
                matched_pairs.add(i)
                break
    blind = not comments
    return ProxyScore(
        pr_number=pr.number,
        agent=agent,
        n_findings=len(findings),
        n_human_comments=len(comments),
        matched=matched_findings,
        agreement=(matched_findings / len(findings)) if findings else None,
        coverage=(len(matched_pairs) / len(comments)) if comments else None,
        blind=blind,
    )


@dataclass
class GoldScore:
    pr_number: int
    agent: str
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    traps_flagged: int = 0
    traps_total: int = 0
    matched_ids: list[str] = field(default_factory=list)
    tripped_trap_ids: list[str] = field(default_factory=list)

    @property
    def precision(self) -> float | None:
        denom = self.true_positives + self.false_positives
        return (self.true_positives / denom) if denom else None

    @property
    def recall(self) -> float | None:
        denom = self.true_positives + self.false_negatives
        return (self.true_positives / denom) if denom else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if not p or not r:
            return None
        return 2 * p * r / (p + r)

    @property
    def false_positive_rate(self) -> float | None:
        """Share of reported findings that are not real defects.

        This is the number decision memory is expected to improve, and the one
        the false-positive trap exists to expose.
        """
        total = self.true_positives + self.false_positives
        return (self.false_positives / total) if total else None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.update(
            precision=self.precision,
            recall=self.recall,
            f1=self.f1,
            false_positive_rate=self.false_positive_rate,
        )
        return d


def gold_score(
    labels: GoldLabels,
    agent: str,
    findings: Sequence[Finding],
    *,
    window: int = DEFAULT_LINE_WINDOW,
) -> GoldScore:
    score = GoldScore(
        pr_number=labels.pr_number, agent=agent, traps_total=len(labels.traps)
    )
    unmatched_defects = list(labels.defects)
    for finding in findings:
        hit = next(
            (
                item
                for item in unmatched_defects
                if same_place(finding.file, finding.line, item.file, item.line, window)
            ),
            None,
        )
        if hit is not None:
            score.true_positives += 1
            score.matched_ids.append(hit.id)
            unmatched_defects.remove(hit)
            continue
        score.false_positives += 1
        trap = next(
            (
                item
                for item in labels.traps
                if same_place(finding.file, finding.line, item.file, item.line, window)
            ),
            None,
        )
        if trap is not None and trap.id not in score.tripped_trap_ids:
            score.traps_flagged += 1
            score.tripped_trap_ids.append(trap.id)
    score.false_negatives = len(unmatched_defects)
    return score


def aggregate_gold(scores: Iterable[GoldScore]) -> dict[str, Any]:
    """Micro-averaged over the gold subset.

    Micro rather than macro: a PR with one labelled defect should not weigh as
    much as one with ten, and the subset is small enough that macro-averaging
    would be dominated by whichever beat PR had the fewest labels.
    """
    rows = list(scores)
    tp = sum(s.true_positives for s in rows)
    fp = sum(s.false_positives for s in rows)
    fn = sum(s.false_negatives for s in rows)
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    return {
        "prs": len(rows),
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "precision": precision,
        "recall": recall,
        "f1": (2 * precision * recall / (precision + recall)) if precision and recall else None,
        "false_positive_rate": (fp / (tp + fp)) if (tp + fp) else None,
        "traps_flagged": sum(s.traps_flagged for s in rows),
        "traps_total": sum(s.traps_total for s in rows),
    }


def aggregate_proxy(scores: Iterable[ProxyScore]) -> dict[str, Any]:
    rows = list(scores)
    scored = [s for s in rows if not s.blind]
    matched = sum(s.matched for s in scored)
    findings = sum(s.n_findings for s in scored)
    comments = sum(s.n_human_comments for s in scored)
    return {
        "prs": len(rows),
        "prs_with_human_comments": len(scored),
        "prs_blind": len(rows) - len(scored),
        "agreement": (matched / findings) if findings else None,
        "coverage": (matched / comments) if comments else None,
        "note": (
            "Blind PRs (no human inline comments) are excluded from these ratios "
            "rather than counted as zero -- the proxy cannot distinguish an "
            "unhelpful agent from a silent reviewer."
        ),
    }


def quality_table(by_agent: dict[str, dict[str, Any]], *, kind: str) -> str:
    """Render one of the two tables. Never merge them: the hand-labelled subset
    deserves far more weight than the proxy, and averaging hides that."""
    if kind == "gold":
        head = "| agent | PRs | TP | FP | FN | precision | recall | F1 | FP rate | traps flagged |"
        sep = "|---|---|---|---|---|---|---|---|---|---|"
        lines = [f"Hand-labelled gold subset ({kind})", head, sep]
        for agent, s in sorted(by_agent.items()):
            lines.append(
                "| {a} | {prs} | {tp} | {fp} | {fn} | {p} | {r} | {f} | {fpr} | {t}/{tt} |".format(
                    a=agent,
                    prs=s["prs"],
                    tp=s["true_positives"],
                    fp=s["false_positives"],
                    fn=s["false_negatives"],
                    p=_pct(s["precision"]),
                    r=_pct(s["recall"]),
                    f=_pct(s["f1"]),
                    fpr=_pct(s["false_positive_rate"]),
                    t=s["traps_flagged"],
                    tt=s["traps_total"],
                )
            )
        return "\n".join(lines)
    head = "| agent | PRs | scored | blind | agreement | coverage |"
    lines = ["Merged-human-comment proxy (whole sequence)", head, "|---|---|---|---|---|---|"]
    for agent, s in sorted(by_agent.items()):
        lines.append(
            "| {a} | {prs} | {sc} | {bl} | {ag} | {cv} |".format(
                a=agent,
                prs=s["prs"],
                sc=s["prs_with_human_comments"],
                bl=s["prs_blind"],
                ag=_pct(s["agreement"]),
                cv=_pct(s["coverage"]),
            )
        )
    return "\n".join(lines)


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def load_gold_dir(path: str | os.PathLike[str]) -> dict[int, GoldLabels]:
    root = Path(path)
    if not root.exists():
        return {}
    out: dict[int, GoldLabels] = {}
    for file in sorted(root.glob("*.json")):
        labels = GoldLabels.load(file)
        out[labels.pr_number] = labels
    return out
