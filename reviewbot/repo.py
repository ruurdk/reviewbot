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
from pathlib import Path
from typing import Protocol

from .github import GitHubClient, PullRequest, fetch_file_at

# Files whose contents are never useful as review context.
SKIP_SUFFIXES = (".png", ".jpg", ".gif", ".ico", ".pdf", ".whl", ".so", ".pyc", ".lock")
MAX_SOURCE_CHARS = 200_000


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


def touched_sources(
    pr: PullRequest,
    provider: SourceProvider,
    *,
    sha: str | None = None,
    max_chars: int = MAX_SOURCE_CHARS,
) -> dict[str, str]:
    """Full contents of the files this PR touches, at the PR's base commit.

    Base rather than head on purpose: the reviewer sees the code as it was plus
    the diff, which is what a human reviewer sees. Deleted files are skipped --
    there is nothing to read.
    """
    ref = sha or pr.base_sha
    out: dict[str, str] = {}
    for f in sorted(pr.files, key=lambda f: f.filename):
        if f.status == "removed" or not is_readable_source(f.filename):
            continue
        text = provider.read(f.filename, ref)
        if text is None:
            continue
        if len(text) > max_chars:
            text = text[:max_chars] + "\n# [truncated by the harness]\n"
        out[f.filename] = text
    return out


def read_docs(provider: SourceProvider, paths: list[str], sha: str) -> dict[str, str]:
    """The style guide / contributor doc, read once at the frozen SHA."""
    docs: dict[str, str] = {}
    for path in paths:
        text = provider.read(path, sha)
        if text is not None:
            docs[path] = text
    return docs
