"""The frozen PR sequence, its beats, and the recurrence disclosure table.

Spec 6 requires the sequence to be curated *and* the curation to be disclosed.
So the manifest is explicit data (data/sequence.json), validation is a command
that fails loudly, and `disclosure_table()` emits the per-PR recurrence/diff-size
table straight from the ingested data rather than from a claim in a slide.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .github import PRStore, PullRequest

# Narrative beats the sequence must contain (spec 6/8).
RECURRING_BUG = "recurring_bug"
FALSE_POSITIVE_TRAP = "false_positive_trap"
CONVENTION_CHANGE = "convention_change"
BEATS = (RECURRING_BUG, FALSE_POSITIVE_TRAP, CONVENTION_CHANGE)

GOLD_MIN, GOLD_MAX = 5, 8


class DatasetError(ValueError):
    pass


@dataclass
class SequenceEntry:
    ordinal: int
    pr_number: int
    beats: list[str] = field(default_factory=list)
    gold_labeled: bool = False
    note: str = ""


@dataclass
class Sequence:
    repo: str
    entries: list[SequenceEntry]
    # The spine the sequence is curated around, and the primer's reading list.
    spine: list[str] = field(default_factory=list)
    style_guide_paths: list[str] = field(default_factory=list)
    # Pinned so the primer reads exactly the repo state the reviews assume.
    frozen_at_sha: str | None = None
    selection_rule: str = ""

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "Sequence":
        d = json.loads(Path(path).read_text())
        entries = [SequenceEntry(**e) for e in d.pop("entries")]
        return cls(entries=entries, **d)

    def save(self, path: str | os.PathLike[str]) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        d = asdict(self)
        p.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n")

    def __iter__(self):
        return iter(sorted(self.entries, key=lambda e: e.ordinal))

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def gold_subset(self) -> list[SequenceEntry]:
        return [e for e in self if e.gold_labeled]

    def beat_entries(self, beat: str) -> list[SequenceEntry]:
        return [e for e in self if beat in e.beats]


@dataclass
class SequenceRow:
    """One row of the disclosure table (spec 6)."""

    ordinal: int
    pr_id: str
    pr_number: int
    modules: list[str]
    n_files: int
    diff_size: int
    recurs_from: list[int]
    recurring_modules: list[str]
    beats: list[str]
    gold_labeled: bool
    human_comments: int


def rows(sequence: Sequence, store: PRStore) -> list[SequenceRow]:
    seen: dict[str, list[int]] = {}
    out: list[SequenceRow] = []
    for entry in sequence:
        pr = store.load(sequence.repo, entry.pr_number)
        recurring = sorted(m for m in pr.modules if m in seen)
        recurs_from = sorted({o for m in recurring for o in seen[m]})
        out.append(
            SequenceRow(
                ordinal=entry.ordinal,
                pr_id=pr.pr_id,
                pr_number=pr.number,
                modules=pr.modules,
                n_files=len(pr.files),
                diff_size=pr.diff_size,
                recurs_from=recurs_from,
                recurring_modules=recurring,
                beats=list(entry.beats),
                gold_labeled=entry.gold_labeled,
                human_comments=sum(1 for c in pr.comments if not c.author_is_bot),
            )
        )
        for m in pr.modules:
            seen.setdefault(m, []).append(entry.ordinal)
    return out


def recurrence_rate(table: Iterable[SequenceRow]) -> float:
    table = list(table)
    if len(table) <= 1:
        return 0.0
    # Fraction of PRs after the first that touch an already-seen module. This is
    # the number that answers "did you rig the sequence?" -- disclose it even
    # when it is high, because a high value is the point and a hidden one is
    # the accusation.
    later = table[1:]
    return sum(1 for r in later if r.recurs_from) / len(later)


def validate(sequence: Sequence, store: PRStore | None = None) -> list[str]:
    """Return a list of problems. Empty list means the dataset is usable."""
    problems: list[str] = []
    ordinals = [e.ordinal for e in sequence]
    if sorted(ordinals) != list(range(1, len(ordinals) + 1)):
        problems.append(f"ordinals must be 1..N with no gaps; got {sorted(ordinals)}")
    if len(set(e.pr_number for e in sequence)) != len(ordinals):
        problems.append("duplicate pr_number in the sequence")
    if not 15 <= len(sequence) <= 25:
        problems.append(
            f"sequence length {len(sequence)} is outside the 15-25 range in spec 6"
        )
    for beat in BEATS:
        if not sequence.beat_entries(beat):
            problems.append(
                f"no PR carries the {beat!r} beat -- the narrative needs it "
                "(spec 6). If memory invalidation is not built yet, keep the "
                "convention-change PR and disclose the gap as a limitation."
            )
    gold = sequence.gold_subset
    if not GOLD_MIN <= len(gold) <= GOLD_MAX:
        problems.append(
            f"gold subset has {len(gold)} PRs; spec 7c calls for {GOLD_MIN}-{GOLD_MAX}"
        )
    for beat in BEATS:
        beat_entries = sequence.beat_entries(beat)
        if beat_entries and not any(e.gold_labeled for e in beat_entries):
            problems.append(
                f"beat {beat!r} has no hand-labeled PR -- the gold subset must "
                "cover every beat (spec 7c)"
            )
    if not sequence.spine:
        problems.append("spine is empty; the primer has nothing to distill (spec 4e)")
    if not sequence.frozen_at_sha:
        problems.append("frozen_at_sha is unset; the repo state is not pinned (spec 6)")
    if not sequence.selection_rule:
        problems.append(
            "selection_rule is empty; spec 6 requires stating the diff-size "
            "selection rule openly"
        )
    if store is not None:
        for entry in sequence:
            if not store.has(sequence.repo, entry.pr_number):
                problems.append(
                    f"PR #{entry.pr_number} (ordinal {entry.ordinal}) is not ingested"
                )
    return problems


def disclosure_table(table: Iterable[SequenceRow]) -> str:
    table = list(table)
    lines = [
        "| # | PR | files | diff size | recurring modules | recurs from | beat | gold | human comments |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in table:
        lines.append(
            "| {ord} | {pr} | {nf} | {ds} | {rm} | {rf} | {beat} | {gold} | {hc} |".format(
                ord=r.ordinal,
                pr=r.pr_id,
                nf=r.n_files,
                ds=r.diff_size,
                rm=", ".join(r.recurring_modules) or "-",
                rf=", ".join(str(o) for o in r.recurs_from) or "-",
                beat=", ".join(r.beats) or "-",
                gold="yes" if r.gold_labeled else "-",
                hc=r.human_comments,
            )
        )
    rate = recurrence_rate(table)
    lines.append("")
    lines.append(
        f"Module recurrence: {rate:.0%} of PRs after the first touch a module "
        "seen earlier in the sequence."
    )
    zero = sum(1 for r in table if r.human_comments == 0)
    if zero:
        lines.append(
            f"{zero} of {len(table)} PRs have zero human review comments -- the "
            "proxy metric is blind on those (spec 7c)."
        )
    return "\n".join(lines)


def summary(sequence: Sequence, store: PRStore) -> dict[str, Any]:
    table = rows(sequence, store)
    return {
        "repo": sequence.repo,
        "n_prs": len(table),
        "frozen_at_sha": sequence.frozen_at_sha,
        "spine": sequence.spine,
        "recurrence_rate": recurrence_rate(table),
        "median_diff_size": sorted(r.diff_size for r in table)[len(table) // 2] if table else 0,
        "gold_subset": [r.ordinal for r in table if r.gold_labeled],
        "beats": {b: [r.ordinal for r in table if b in r.beats] for b in BEATS},
        "prs_without_human_comments": [r.ordinal for r in table if r.human_comments == 0],
        "rows": [asdict(r) for r in table],
    }
