"""GitHub ingestion with an on-disk cache.

Two constraints shape this module. First, the unauthenticated rate limit (60
requests/hour) is exhausted well before 15-25 PRs' files, comments, and source
blobs are pulled, so GITHUB_TOKEN is required rather than optional. Second, the
dataset must be *frozen* (spec 6) -- so every response is cached to disk keyed
by its URL, and file contents are always fetched at a pinned SHA, never at a
branch name. A rerun re-reads the cache and cannot drift.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

API = "https://api.github.com"
Transport = Callable[[str, dict], tuple[int, dict, bytes]]


class RateLimited(RuntimeError):
    pass


class GitHubError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"HTTP {status} for {url}: {body[:400]}")
        self.status = status


def _urllib_transport(url: str, headers: dict) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


class GitHubClient:
    def __init__(
        self,
        cache_dir: str | os.PathLike[str] = "data/cache/github",
        *,
        token: str | None = None,
        transport: Transport | None = None,
        max_retries: int = 3,
    ):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self.transport = transport or _urllib_transport
        self.max_retries = max_retries
        self.requests_made = 0
        self.cache_hits = 0

    def _headers(self, accept: str) -> dict[str, str]:
        if not self.token:
            raise RateLimited(
                "GITHUB_TOKEN is not set. Ingesting 15-25 PRs with files, "
                "comments, and blobs exceeds the unauthenticated limit of 60 "
                "requests/hour."
            )
        return {
            "accept": accept,
            "authorization": f"Bearer {self.token}",
            "x-github-api-version": "2022-11-28",
            "user-agent": "reviewbot-demo-harness",
        }

    def _cache_path(self, url: str, accept: str) -> Path:
        key = hashlib.sha256(f"{accept}|{url}".encode()).hexdigest()[:32]
        return self.cache_dir / f"{key}.json"

    def get(
        self, path: str, *, accept: str = "application/vnd.github+json", raw: bool = False
    ) -> Any:
        url = path if path.startswith("http") else f"{API}{path}"
        cache = self._cache_path(url, accept)
        if cache.exists():
            self.cache_hits += 1
            payload = json.loads(cache.read_text())
            return payload["body"] if raw else json.loads(payload["body"])

        delay = 2.0
        for attempt in range(self.max_retries + 1):
            status, headers, body = self.transport(url, self._headers(accept))
            self.requests_made += 1
            text = body.decode(errors="replace")
            if 200 <= status < 300:
                cache.write_text(json.dumps({"url": url, "body": text}))
                return text if raw else json.loads(text)
            remaining = headers.get("x-ratelimit-remaining") or headers.get(
                "X-RateLimit-Remaining"
            )
            if status in (403, 429) and remaining == "0":
                reset = headers.get("x-ratelimit-reset") or headers.get("X-RateLimit-Reset")
                raise RateLimited(
                    f"GitHub rate limit exhausted (resets at epoch {reset}). "
                    "Cached responses are reusable; re-run after the reset."
                )
            if status in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                retry_after = headers.get("retry-after") or headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after else delay)
                delay = min(delay * 2, 60)
                continue
            raise GitHubError(status, url, text)
        raise GitHubError(0, url, "retries exhausted")

    def paginate(self, path: str, per_page: int = 100, cap: int = 10) -> list[Any]:
        """Page by explicit ?page= so each page has a stable, cacheable URL."""
        joiner = "&" if "?" in path else "?"
        out: list[Any] = []
        for page in range(1, cap + 1):
            chunk = self.get(f"{path}{joiner}per_page={per_page}&page={page}")
            if not chunk:
                break
            out.extend(chunk)
            if len(chunk) < per_page:
                break
        return out


@dataclass
class FileChange:
    filename: str
    status: str
    additions: int
    deletions: int
    changes: int
    patch: str | None = None
    previous_filename: str | None = None

    @property
    def module(self) -> str:
        """Coarse module tag used for attributes.module routing (spec 4d)."""
        return self.filename


@dataclass
class HumanComment:
    """A merged PR's real review comment -- the proxy gold signal (spec 7c)."""

    id: int
    kind: str  # inline | review_body
    path: str | None
    line: int | None
    body: str
    author: str | None
    author_is_bot: bool = False


@dataclass
class PullRequest:
    repo: str
    number: int
    title: str
    body: str
    base_sha: str
    head_sha: str
    merge_commit_sha: str | None
    merged_at: str | None
    files: list[FileChange] = field(default_factory=list)
    comments: list[HumanComment] = field(default_factory=list)
    truncated_files: bool = False

    @property
    def pr_id(self) -> str:
        return f"{self.repo}#{self.number}"

    @property
    def modules(self) -> list[str]:
        return sorted({f.filename for f in self.files})

    @property
    def diff_size(self) -> int:
        return sum(f.changes for f in self.files)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "PullRequest":
        d = dict(d)
        files = [FileChange(**f) for f in d.pop("files", [])]
        comments = [HumanComment(**c) for c in d.pop("comments", [])]
        return cls(files=files, comments=comments, **d)


BOT_SUFFIX = "[bot]"


def fetch_pull_request(client: GitHubClient, repo: str, number: int) -> PullRequest:
    pr = client.get(f"/repos/{repo}/pulls/{number}")
    raw_files = client.paginate(f"/repos/{repo}/pulls/{number}/files")
    files = [
        FileChange(
            filename=f["filename"],
            status=f["status"],
            additions=f["additions"],
            deletions=f["deletions"],
            changes=f["changes"],
            patch=f.get("patch"),
            previous_filename=f.get("previous_filename"),
        )
        for f in raw_files
    ]

    comments: list[HumanComment] = []
    for c in client.paginate(f"/repos/{repo}/pulls/{number}/comments"):
        login = (c.get("user") or {}).get("login")
        comments.append(
            HumanComment(
                id=c["id"],
                kind="inline",
                path=c.get("path"),
                line=c.get("line") or c.get("original_line"),
                body=c.get("body") or "",
                author=login,
                author_is_bot=bool(login and login.endswith(BOT_SUFFIX)),
            )
        )
    for r in client.paginate(f"/repos/{repo}/pulls/{number}/reviews"):
        if not (r.get("body") or "").strip():
            continue
        login = (r.get("user") or {}).get("login")
        comments.append(
            HumanComment(
                id=r["id"],
                kind="review_body",
                path=None,
                line=None,
                body=r["body"],
                author=login,
                author_is_bot=bool(login and login.endswith(BOT_SUFFIX)),
            )
        )

    return PullRequest(
        repo=repo,
        number=number,
        title=pr.get("title") or "",
        body=pr.get("body") or "",
        base_sha=pr["base"]["sha"],
        head_sha=pr["head"]["sha"],
        merge_commit_sha=pr.get("merge_commit_sha"),
        merged_at=pr.get("merged_at"),
        files=files,
        comments=comments,
        # The files endpoint caps at 3000 files; flag it rather than silently
        # reviewing a partial diff.
        truncated_files=len(raw_files) >= 3000,
    )


def fetch_file_at(client: GitHubClient, repo: str, path: str, sha: str) -> str:
    """Read a file at a pinned commit. Never pass a branch name as `sha` --
    the frozen dataset depends on content being immutable."""
    quoted = urllib.parse.quote(path)
    return client.get(
        f"/repos/{repo}/contents/{quoted}?ref={sha}",
        accept="application/vnd.github.raw",
        raw=True,
    )


class PRStore:
    """Assembled PRs on disk, so a replay never depends on the network."""

    def __init__(self, root: str | os.PathLike[str] = "data/prs"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, repo: str, number: int) -> Path:
        return self.root / repo.replace("/", "__") / f"{number}.json"

    def save(self, pr: PullRequest) -> Path:
        p = self.path_for(pr.repo, pr.number)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(pr.to_json(), indent=2, sort_keys=True) + "\n")
        return p

    def load(self, repo: str, number: int) -> PullRequest:
        return PullRequest.from_json(json.loads(self.path_for(repo, number).read_text()))

    def has(self, repo: str, number: int) -> bool:
        return self.path_for(repo, number).exists()

    def ingest(
        self, client: GitHubClient, repo: str, numbers: Iterable[int], *, refresh: bool = False
    ) -> list[PullRequest]:
        out = []
        for n in numbers:
            if self.has(repo, n) and not refresh:
                out.append(self.load(repo, n))
                continue
            pr = fetch_pull_request(client, repo, n)
            self.save(pr)
            out.append(pr)
        return out
