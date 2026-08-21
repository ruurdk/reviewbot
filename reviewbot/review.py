"""The shared review loop.

Both agents run this code. The memory agent's only delta is the content of
`ReviewContext.volatile_blocks` (retrieved memories instead of freshly-read
source) plus a write phase afterwards. Nothing else may differ -- so the system
prompt below is a single frozen string used by both, and it mentions the
optional "Prior knowledge" section so that its bytes are identical whether or
not memories are present. A per-agent system prompt would be a confound *and*
would give the two agents different cache prefixes.

Block ordering is caching-driven (render order is tools -> system -> messages):

    system   : frozen instructions            <- stable
    messages : style guide / repo doc         <- stable, breakpoint on the last one
               per-PR source or memories      <- volatile
               the diff                       <- volatile

Nothing volatile precedes the breakpoint, and no timestamp, PR id, or run id
appears anywhere in the stable part. That last rule is load-bearing: a PR id in
the system prompt silently zeroes caching for both agents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .claude import ClaudeClient, ClaudeResult, Tags
from .github import PullRequest

SEVERITIES = ("blocker", "major", "minor", "nit")
CATEGORIES = (
    "correctness",
    "resource-leak",
    "concurrency",
    "api-contract",
    "error-handling",
    "performance",
    "test-coverage",
    "style",
)

SYSTEM_PROMPT = """You are reviewing a pull request against a Python library that other software depends on at runtime. Report defects a maintainer would act on.

You may be given some or all of the following, in this order:
- Repo conventions: a contributor guide or distilled conventions for this repository.
- Prior knowledge: durable facts about this repo, past review findings, and review policy. This section may be absent. When it is present, treat it as established context and do not re-derive it; when it is absent, work from the source provided.
- Source context: the current contents of files the change touches. This section may be absent.
- The diff: the change under review, as unified patches per file.

Rules for findings:
- Report only defects introduced or left unaddressed by this diff. Do not report pre-existing issues in unchanged code, and do not restate what the diff already does correctly.
- Anchor every finding to a file and, where the patch makes it determinable, a line.
- Prefer one precise finding over three speculative ones. If you are not confident a reader would agree it is a defect, either omit it or mark confidence low.
- Style points are findings only when they violate a stated convention.
- If the change is clean, return an empty findings list. An empty review is a valid review.

If a Prior knowledge section is present, list in `memories_used` the id of every item you actually relied on. Do not list items you were merely shown."""

FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": ["integer", "null"]},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "message": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["file", "line", "severity", "category", "message", "confidence"],
                "additionalProperties": False,
            },
        },
        "memories_used": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["findings", "memories_used"],
    "additionalProperties": False,
}


@dataclass
class Finding:
    file: str
    line: int | None
    severity: str
    category: str
    message: str
    confidence: str

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Finding":
        return cls(
            file=d.get("file", ""),
            line=d.get("line"),
            severity=d.get("severity", "minor"),
            category=d.get("category", "correctness"),
            message=d.get("message", ""),
            confidence=d.get("confidence", "medium"),
        )


@dataclass
class ReviewContext:
    """What the agent was allowed to look at.

    `stable_blocks` is the cacheable prefix -- identical across every PR in a
    run. `volatile_blocks` is the per-PR payload, and the difference in its size
    between the two agents is the thing the demo measures.
    """

    stable_blocks: list[dict[str, Any]] = field(default_factory=list)
    volatile_blocks: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ReviewResult:
    findings: list[Finding]
    memories_used: list[str]
    raw: dict[str, Any]
    call: ClaudeResult

    @property
    def truncated(self) -> bool:
        return self.call.truncated


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def render_diff(pr: PullRequest, *, max_patch_chars: int | None = None) -> str:
    """The diff, rendered deterministically.

    Deliberately excludes the PR title/body author metadata that would vary in
    ways unrelated to the change, and excludes anything time-like.
    """
    parts = [f"Pull request: {pr.title}".rstrip()]
    if pr.body.strip():
        parts.append(f"Description:\n{pr.body.strip()}")
    parts.append(f"Files changed: {len(pr.files)}")
    for f in sorted(pr.files, key=lambda f: f.filename):
        header = f"--- {f.filename} ({f.status}, +{f.additions}/-{f.deletions})"
        if f.patch is None:
            # GitHub omits the patch for very large or binary files.
            parts.append(f"{header}\n[no patch available from the API]")
            continue
        patch = f.patch
        if max_patch_chars is not None and len(patch) > max_patch_chars:
            patch = patch[:max_patch_chars] + "\n[patch truncated by the harness]"
        parts.append(f"{header}\n{patch}")
    return "\n\n".join(parts)


def source_context_block(files: dict[str, str]) -> dict[str, Any]:
    """Full contents of the touched files -- what the baseline re-reads per PR."""
    parts = ["Source context:"]
    for path in sorted(files):
        parts.append(f"===== {path} =====\n{files[path]}")
    return text_block("\n\n".join(parts))


def conventions_block(docs: dict[str, str]) -> dict[str, Any]:
    parts = ["Repo conventions:"]
    for path in sorted(docs):
        parts.append(f"===== {path} =====\n{docs[path]}")
    return text_block("\n\n".join(parts))


def prior_knowledge_block(items: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """Retrieved memories as (id, text). Ids are exposed so the model can name
    which ones it used, which is the retrieval-precision signal in spec 7b."""
    parts = ["Prior knowledge:"]
    for mid, text in items:
        parts.append(f"[{mid}] {text}")
    return text_block("\n".join(parts))


def build_request(
    pr: PullRequest,
    context: ReviewContext,
    *,
    enable_caching: bool = True,
    max_patch_chars: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    system = [text_block(SYSTEM_PROMPT)]
    blocks: list[dict[str, Any]] = [dict(b) for b in context.stable_blocks]
    if enable_caching and blocks:
        # The breakpoint caches tools + system + every stable block before it.
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
    elif enable_caching:
        system[-1]["cache_control"] = {"type": "ephemeral"}
    blocks.extend(dict(b) for b in context.volatile_blocks)
    blocks.append(text_block("The diff:\n\n" + render_diff(pr, max_patch_chars=max_patch_chars)))
    return system, [{"role": "user", "content": blocks}]


def review(
    client: ClaudeClient,
    tags: Tags,
    pr: PullRequest,
    context: ReviewContext,
    *,
    max_patch_chars: int | None = None,
    notes: dict[str, Any] | None = None,
) -> ReviewResult:
    system, messages = build_request(
        pr,
        context,
        enable_caching=client.config.enable_caching,
        max_patch_chars=max_patch_chars,
    )
    result = client.messages(
        tags,
        system=system,
        messages=messages,
        output_schema=FINDINGS_SCHEMA,
        notes=notes,
    )
    payload = result.json()
    return ReviewResult(
        findings=[Finding.from_json(f) for f in payload.get("findings", [])],
        memories_used=list(payload.get("memories_used") or []),
        raw=payload,
        call=result,
    )
