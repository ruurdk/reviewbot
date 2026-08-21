"""Select and freeze the PR sequence (spec §6).

The sequence is deliberately curated, and the spec's defence of that is that the
curation rule is *stated* rather than hidden. So the rule lives here as code:

    1. merged only (an unmerged PR has no human review to score against)
    2. touches at least one spine module -- the connection/cluster spine is
       genuinely hot in redis-py, so recurrence is a property of the real repo
    3. diff size at or above the median of the candidate pool. The median
       redis-py PR touches 2 files; drawn from there, context assembly costs
       almost nothing and the curve bends unimpressively. The honest framing is
       "PRs substantial enough for context assembly to cost something", not
       "PRs where we win".
    4. chronological order, so recurrence unfolds the way it really did
    5. trimmed to the target length, preferring PRs that revisit an
       already-seen module

Beat assignment is deliberately *not* automated. A convention-change PR is
mechanically detectable (it edits the style guide) and gets flagged; the
recurring-bug pattern and the false-positive trap require reading the diffs, so
`curate` leaves those fields empty rather than guessing and calling it data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any, Sequence as Seq

from .dataset import CONVENTION_CHANGE, Sequence, SequenceEntry
from .github import GitHubClient, PRStore

DEFAULT_SPINE = [
    "redis/connection.py",
    "redis/cluster.py",
    "redis/asyncio/cluster.py",
    "redis/commands/core.py",
]
DEFAULT_STYLE_GUIDE = ["CONTRIBUTING.md", "docs/CONTRIBUTING.md", "README.md"]


def _recurrence(file_lists: list[list[str]]) -> float:
    """Share of entries after the first that touch an already-seen file."""
    if len(file_lists) <= 1:
        return 0.0
    seen: set[str] = set(file_lists[0])
    hits = 0
    for files in file_lists[1:]:
        if seen & set(files):
            hits += 1
        seen.update(files)
    return hits / (len(file_lists) - 1)


@dataclass
class Candidate:
    number: int
    title: str
    merged_at: str
    base_sha: str
    merge_commit_sha: str | None
    files: list[str] = field(default_factory=list)
    diff_size: int = 0
    n_files: int = 0

    def spine_hits(self, spine: Seq[str]) -> list[str]:
        return sorted(f for f in self.files if f in spine)

    def touches_style_guide(self, paths: Seq[str]) -> list[str]:
        return sorted(f for f in self.files if f in paths)


def scan(
    client: GitHubClient,
    repo: str,
    *,
    spine: Seq[str] = tuple(DEFAULT_SPINE),
    pages: int = 3,
    per_page: int = 100,
) -> list[Candidate]:
    """List merged PRs and fetch each one's file list.

    Two requests per page for the listing, then one per PR for its files. Every
    response is cached to disk by URL, so a re-scan costs nothing.
    """
    listed: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        chunk = client.get(
            f"/repos/{repo}/pulls?state=closed&sort=updated&direction=desc"
            f"&per_page={per_page}&page={page}"
        )
        if not chunk:
            break
        listed.extend(chunk)

    out: list[Candidate] = []
    for pr in listed:
        if not pr.get("merged_at"):
            continue
        files = client.paginate(f"/repos/{repo}/pulls/{pr['number']}/files")
        names = [f["filename"] for f in files]
        cand = Candidate(
            number=pr["number"],
            title=pr.get("title") or "",
            merged_at=pr["merged_at"],
            base_sha=pr["base"]["sha"],
            merge_commit_sha=pr.get("merge_commit_sha"),
            files=names,
            diff_size=sum(f.get("changes", 0) for f in files),
            n_files=len(names),
        )
        out.append(cand)
    return out


def select(
    candidates: Seq[Candidate],
    *,
    spine: Seq[str] = tuple(DEFAULT_SPINE),
    target: int = 18,
    style_guide_paths: Seq[str] = tuple(DEFAULT_STYLE_GUIDE),
) -> tuple[list[Candidate], dict[str, Any]]:
    """Apply the rule above. Returns (selected, stats) with every cut counted."""
    touching = [c for c in candidates if c.spine_hits(spine)]
    sizes = [c.diff_size for c in touching]
    cutoff = median(sizes) if sizes else 0
    substantial = [c for c in touching if c.diff_size >= cutoff]
    chronological = sorted(substantial, key=lambda c: c.merged_at)

    # Trim to target, preferring PRs that revisit an already-seen module -- that
    # is what memory is supposed to exploit, and dropping them would test the
    # weakest version of the thesis.
    selected: list[Candidate] = []
    if len(chronological) <= target:
        selected = chronological
    else:
        seen: set[str] = set()
        scored: list[tuple[int, int, Candidate]] = []
        for i, cand in enumerate(chronological):
            revisits = len(set(cand.files) & seen)
            scored.append((-revisits, i, cand))
            seen.update(cand.files)
        keep = {id(c) for _, _, c in sorted(scored)[:target]}
        selected = [c for c in chronological if id(c) in keep]

    stats = {
        "scanned": len(candidates),
        # Recurrence of the untrimmed pool, reported next to the selected
        # sequence's. The trim step prefers PRs that revisit a module, so the
        # selected figure is partly a curatorial artefact; showing both is the
        # only honest way to present it (spec 6/9).
        "recurrence_untrimmed": _recurrence([c.files for c in chronological]),
        "recurrence_selected": _recurrence([c.files for c in selected]),
        "merged_touching_spine": len(touching),
        "median_diff_size_of_pool": cutoff,
        "above_median": len(substantial),
        "selected": len(selected),
        "dropped_below_median": len(touching) - len(substantial),
        "dropped_to_fit_target": max(0, len(substantial) - len(selected)),
        "spine": list(spine),
        "style_guide_candidates": sorted(
            {
                path
                for c in candidates
                for path in c.touches_style_guide(style_guide_paths)
            }
        ),
    }
    return selected, stats


def build_sequence(
    repo: str,
    selected: Seq[Candidate],
    *,
    spine: Seq[str] = tuple(DEFAULT_SPINE),
    style_guide_paths: Seq[str] = tuple(DEFAULT_STYLE_GUIDE),
) -> Sequence:
    entries: list[SequenceEntry] = []
    for ordinal, cand in enumerate(selected, start=1):
        touched_guide = cand.touches_style_guide(style_guide_paths)
        entries.append(
            SequenceEntry(
                ordinal=ordinal,
                pr_number=cand.number,
                # Only the mechanically detectable beat is filled in. The other
                # two need a human (or a Claude pass) reading the diffs.
                beats=[CONVENTION_CHANGE] if touched_guide else [],
                gold_labeled=False,
                note=(
                    f"{cand.title[:90]} | +/-{cand.diff_size} in {cand.n_files} files"
                    + (f" | edits {', '.join(touched_guide)}" if touched_guide else "")
                ),
            )
        )
    return Sequence(
        repo=repo,
        entries=entries,
        spine=list(spine),
        style_guide_paths=list(style_guide_paths),
        # The primer must read the repo as it stood before the sequence began,
        # so the frozen state is the first PR's base commit.
        frozen_at_sha=selected[0].base_sha if selected else None,
        selection_rule=(
            "merged PRs touching the connection/cluster spine, at or above the "
            "median diff size of that pool, in chronological order, trimmed to "
            "the target length preferring PRs that revisit an already-seen module"
        ),
    )


def report(selected: Seq[Candidate], stats: dict[str, Any], spine: Seq[str]) -> str:
    lines = [
        f"scanned {stats['scanned']} merged PRs",
        f"  touching the spine:            {stats['merged_touching_spine']}",
        f"  median diff size of that pool: {stats['median_diff_size_of_pool']:.0f} changes",
        f"  at or above the median:        {stats['above_median']}",
        f"  dropped below the median:      {stats['dropped_below_median']}",
        f"  dropped to fit the target:     {stats['dropped_to_fit_target']}",
        f"  selected:                      {stats['selected']}",
        "",
        "| # | PR | merged | files | changes | spine modules touched |",
        "|---|---|---|---|---|---|",
    ]
    seen: set[str] = set()
    recurring = 0
    for ordinal, cand in enumerate(selected, start=1):
        hits = cand.spine_hits(spine)
        if set(cand.files) & seen:
            recurring += 1
        seen.update(cand.files)
        lines.append(
            f"| {ordinal} | #{cand.number} | {cand.merged_at[:10]} | {cand.n_files} | "
            f"{cand.diff_size} | {', '.join(h.replace('redis/', '') for h in hits)} |"
        )
    if len(selected) > 1:
        lines += [
            "",
            f"module recurrence, selected sequence:  {recurring}/{len(selected) - 1} "
            f"({stats['recurrence_selected']:.0%})",
            f"module recurrence, untrimmed pool:     {stats['recurrence_untrimmed']:.0%}",
            "  The trim step prefers PRs that revisit a module, so the selected",
            "  figure is partly curatorial. The untrimmed figure is the property of",
            "  the real repo -- quote both.",
        ]
    return "\n".join(lines)
