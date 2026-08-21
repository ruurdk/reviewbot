"""Where source text comes from.

The baseline re-reads the touched files on every PR; that is the cost the demo
measures, so how those reads are served has to be identical for both agents and
must never vary between runs. Three providers, one interface:

- `LocalSourceProvider`  a checkout pinned to the frozen SHA (no credentials)
- `GitHubSourceProvider` the contents API at a pinned SHA (needs a token)
- `DictSourceProvider`   tests

Reads are cached in-process and counted, so "how many files did the baseline
read" is a measured number rather than an assumption.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .github import GitHubClient, PullRequest, fetch_file_at

# Files whose contents are never useful as review context.
SKIP_SUFFIXES = (".png", ".jpg", ".gif", ".ico", ".pdf", ".whl", ".so", ".pyc", ".lock")
MAX_SOURCE_CHARS = 200_000

# Total source-context budget for one review, in characters (~100k tokens).
#
# Measured need for this: in the frozen redis-py sequence, PR #4052 touches 62
# Python files. Reading all of them in full would approach the 1M context window
# and cost several dollars for a single baseline review, letting two outlier PRs
# dominate the whole comparison. So the baseline gets a budget the size of a
# human reviewer's working set, filled with the files the diff touches most, and
# every dropped file is reported rather than silently omitted.
#
# Note the direction: the budget makes the *baseline* cheaper, so it is a
# conservative choice for the thesis rather than a convenient one.
MAX_TOTAL_SOURCE_CHARS = 400_000


class SourceProvider(Protocol):
    def read(self, path: str, sha: str) -> str | None: ...


class _Counting:
    def __init__(self) -> None:
        self.reads = 0
        self.misses = 0
        self._cache: dict[tuple[str, str], str | None] = {}

    def _cached(self, path: str, sha: str, load) -> str | None:
        key = (path, sha)
        if key in self._cache:
            return self._cache[key]
        self.reads += 1
        try:
            text = load()
        except FileNotFoundError:
            text = None
        if text is None:
            self.misses += 1
        self._cache[key] = text
        return text


class DictSourceProvider(_Counting):
    def __init__(self, files: dict[str, str]):
        super().__init__()
        self.files = files

    def read(self, path: str, sha: str) -> str | None:
        return self._cached(path, sha, lambda: self.files.get(path))


class LocalSourceProvider(_Counting):
    """Read from a git checkout.

    `sha` is honoured via `git show <sha>:<path>` when the directory is a repo,
    so a stale working tree cannot silently change the frozen dataset. Falls
    back to the working tree only when git is unavailable, and says so.
    """

    def __init__(self, root: str | os.PathLike[str], *, use_git: bool = True):
        super().__init__()
        self.root = Path(root)
        self.use_git = use_git and (self.root / ".git").exists()
        self.fell_back_to_worktree = False

    def read(self, path: str, sha: str) -> str | None:
        def load() -> str | None:
            if self.use_git:
                proc = subprocess.run(
                    ["git", "-C", str(self.root), "show", f"{sha}:{path}"],
                    capture_output=True,
                    text=True,
                )
                if proc.returncode == 0:
                    return proc.stdout
                return None
            self.fell_back_to_worktree = True
            candidate = self.root / path
            return candidate.read_text(errors="replace") if candidate.exists() else None

        return self._cached(path, sha, load)


class GitHubSourceProvider(_Counting):
    def __init__(self, client: GitHubClient, repo: str):
        super().__init__()
        self.client = client
        self.repo = repo

    def read(self, path: str, sha: str) -> str | None:
        def load() -> str | None:
            try:
                return fetch_file_at(self.client, self.repo, path, sha)
            except Exception:
                return None

        return self._cached(path, sha, load)


def is_readable_source(path: str) -> bool:
    return not path.lower().endswith(SKIP_SUFFIXES)


@dataclass
class SourceContext:
    """What the agent actually got to read, and what it did not."""

    files: dict[str, str] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    total_chars: int = 0
    budget_chars: int = MAX_TOTAL_SOURCE_CHARS

    @property
    def n_read(self) -> int:
        return len(self.files)

    def as_notes(self) -> dict[str, object]:
        return {
            "files_read": self.n_read,
            "files_dropped_over_budget": len(self.dropped),
            "dropped": self.dropped[:10],
            "source_chars": self.total_chars,
            "budget_chars": self.budget_chars,
        }


def touched_sources(
    pr: PullRequest,
    provider: SourceProvider,
    *,
    sha: str | None = None,
    max_chars: int = MAX_SOURCE_CHARS,
    max_total_chars: int = MAX_TOTAL_SOURCE_CHARS,
) -> SourceContext:
    """Contents of the files this PR touches, at the PR's base commit, up to a
    budget.

    Base rather than head on purpose: the reviewer sees the code as it was plus
    the diff, which is what a human reviewer sees. Deleted files are skipped --
    there is nothing to read.

    Files are admitted in descending order of how much the diff changes them, so
    the budget buys the most review-relevant context first. Ties break on
    filename, and the result is re-sorted by path, so the prompt bytes are
    deterministic and the cache prefix stays stable.
    """
    ref = sha or pr.base_sha
    ctx = SourceContext(budget_chars=max_total_chars)
    for f in sorted(pr.files, key=lambda f: (-f.changes, f.filename)):
        if f.status == "removed" or not is_readable_source(f.filename):
            continue
        text = provider.read(f.filename, ref)
        if text is None:
            ctx.unreadable.append(f.filename)
            continue
        if len(text) > max_chars:
            text = text[:max_chars] + "\n# [truncated by the harness]\n"
        if ctx.files and ctx.total_chars + len(text) > max_total_chars:
            ctx.dropped.append(f.filename)
            continue
        ctx.files[f.filename] = text
        ctx.total_chars += len(text)
    ctx.files = {k: ctx.files[k] for k in sorted(ctx.files)}
    ctx.dropped.sort()
    ctx.unreadable.sort()
    return ctx


def read_docs(provider: SourceProvider, paths: list[str], sha: str) -> dict[str, str]:
    """The style guide / contributor doc, read once at the frozen SHA."""
    docs: dict[str, str] = {}
    for path in paths:
        text = provider.read(path, sha)
        if text is not None:
            docs[path] = text
    return docs
