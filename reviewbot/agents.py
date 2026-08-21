"""The two agents.

Both call `review.review()` with the same client, config, and system prompt.
The entire difference is which blocks go into `ReviewContext.volatile_blocks`:

    baseline  full source of every touched file, re-read per PR
    memory    the retrieved slice of durable memories for those modules

Plus, for the memory agent only, a one-time `prime` phase before the sequence
and a `write` phase after each review. Every model call is tagged with its
phase, so the primer and the writes appear in the cumulative total rather than
off to one side.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from .claude import ClaudeClient, Tags
from .github import PullRequest
from .memory import (
    REPO_CONVENTION,
    REVIEW_FINDING,
    AgentMemoryClient,
    Memory,
    memory_id,
    scoped_filter,
)
from .repo import MAX_TOTAL_SOURCE_CHARS, SourceContext, SourceProvider, touched_sources
from .review import (
    Finding,
    ReviewContext,
    ReviewResult,
    conventions_block,
    prior_knowledge_block,
    review,
    source_context_block,
    text_block,
)

# --- the primer ------------------------------------------------------------

PRIMER_SYSTEM = """You are building a durable knowledge base about a Python library so that future code reviews do not have to re-read the whole module to understand it.

Read the module given to you and extract the facts a reviewer would otherwise have to re-derive every time they see a diff touching it: what the module is responsible for, the invariants callers depend on, the conventions the code follows, and the ownership boundaries between it and its neighbours.

Rules:
- One fact per record. A record that contains two facts cannot be invalidated independently when one of them changes.
- Write each fact so it is useful without the source in front of you. "Uses a lock" is useless; "every mutation of the slot cache must hold _lock, because concurrent MOVED handling races otherwise" is a fact a reviewer can act on.
- Prefer facts that would let a reviewer judge a diff: invariants, required call ordering, error-handling conventions, what must never happen.
- Do not describe individual functions line by line, do not restate the code, and do not include anything version-specific or dated.
- 8 to 20 records per module is the useful range. Fewer means you skipped things; more means you are transcribing."""

PRIMER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "2-5 word slug naming the fact, stable across re-primes",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["architecture", "convention", "invariant", "ownership"],
                    },
                    "fact": {"type": "string"},
                },
                "required": ["topic", "kind", "fact"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["facts"],
    "additionalProperties": False,
}

WRITE_SYSTEM = """You are recording what a code review learned, so that a future review of the same repository does not have to re-derive it.

You are given the findings from one review. Turn them into durable records. A record is worth writing only if it would change how a later review of *different* code in this repository behaves.

Rules:
- One fact per record, phrased as a reusable rule or a recurring pattern, not as a report about this specific diff. "PR 3411 leaked a socket" is useless; "connection setup in connection.py must close the socket if the handshake raises, this was missed once" is reusable.
- Skip findings that are purely local to the diff and teach nothing general.
- If a finding is an instance of a pattern likely to recur, say so in the pattern field.
- Return an empty list if the review taught nothing durable. That is a normal outcome."""

WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "records": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "module": {"type": "string"},
                    "topic": {"type": "string"},
                    "pattern": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["module", "topic", "pattern", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["records"],
    "additionalProperties": False,
}


@dataclass
class ReviewOutcome:
    agent: str
    pr_id: str
    pr_ordinal: int
    findings: list[Finding]
    result: ReviewResult
    retrieved: list[Memory] = field(default_factory=list)
    memories_used: list[str] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    files_read: int = 0
    files_dropped: int = 0
    injected_tokens: int = 0

    @property
    def retrieval_precision(self) -> float | None:
        """Used / retrieved. The failure mode this catches (spec 7b) is pulling
        40 memories to use 3, which silently eats the saving."""
        if not self.retrieved:
            return None
        return len(self.memories_used) / len(self.retrieved)


class BaselineAgent:
    """Control: assembles context fresh on every PR, never touches memory."""

    name = "baseline"

    def __init__(
        self,
        client: ClaudeClient,
        provider: SourceProvider,
        *,
        conventions: dict[str, str],
        max_patch_chars: int | None = None,
        source_budget: int = MAX_TOTAL_SOURCE_CHARS,
    ):
        self.client = client
        self.provider = provider
        self.conventions = conventions
        self.max_patch_chars = max_patch_chars
        self.source_budget = source_budget

    def context_for(self, pr: PullRequest) -> tuple[ReviewContext, SourceContext]:
        sources = touched_sources(pr, self.provider, max_total_chars=self.source_budget)
        stable = [conventions_block(self.conventions)] if self.conventions else []
        volatile = [source_context_block(sources.files)] if sources.files else []
        return ReviewContext(stable_blocks=stable, volatile_blocks=volatile), sources

    def review_pr(self, pr: PullRequest, ordinal: int) -> ReviewOutcome:
        context, sources = self.context_for(pr)
        result = review(
            self.client,
            Tags(self.name, pr.pr_id, ordinal, "review"),
            pr,
            context,
            max_patch_chars=self.max_patch_chars,
            notes=sources.as_notes(),
        )
        return ReviewOutcome(
            agent=self.name,
            pr_id=pr.pr_id,
            pr_ordinal=ordinal,
            findings=result.findings,
            result=result,
            files_read=sources.n_read,
            files_dropped=len(sources.dropped),
        )


class MemoryAgent:
    """Treatment: prime once, then retrieve -> review -> write per PR."""

    name = "memory"

    def __init__(
        self,
        client: ClaudeClient,
        memory: AgentMemoryClient,
        *,
        conventions: dict[str, str],
        retrieval_limit: int = 20,
        similarity_threshold: float | None = None,
        distill_writes: bool = True,
        max_patch_chars: int | None = None,
    ):
        self.client = client
        self.memory = memory
        self.conventions = conventions
        self.retrieval_limit = retrieval_limit
        self.similarity_threshold = similarity_threshold
        # When False the write phase persists findings verbatim and costs zero
        # model tokens. Distillation costs tokens but produces memories that are
        # reusable rather than diff-specific; either way the cost is measured.
        self.distill_writes = distill_writes
        self.max_patch_chars = max_patch_chars
        self.primed_ids: list[str] = []

    # -- prime ------------------------------------------------------------

    def prime(
        self,
        sources: dict[str, str],
        *,
        pr_id: str = "prime",
        docs: dict[str, str] | None = None,
    ) -> list[Memory]:
        """One distillation pass over the frozen repo (spec 4e).

        One model call per module, so a re-prime of a single changed module does
        not re-pay for the rest. Tagged `phase: prime` at ordinal 0 -- these
        tokens are the memory agent's tokens and belong in the cumulative total.
        """
        records: list[Memory] = []
        for path in sorted(sources):
            blocks = [text_block(f"Module under study: {path}\n\n{sources[path]}")]
            if docs:
                blocks.insert(0, conventions_block(docs))
            result = self.client.messages(
                Tags(self.name, pr_id, 0, "prime"),
                system=[text_block(PRIMER_SYSTEM)],
                messages=[{"role": "user", "content": blocks}],
                output_schema=PRIMER_SCHEMA,
                notes={"module": path},
            )
            for fact in result.json().get("facts", []):
                records.append(
                    Memory(
                        id=memory_id("conv", path, fact["topic"]),
                        text=fact["fact"],
                        memory_type=REPO_CONVENTION,
                        namespace=self.memory.namespace,
                        owner_id=self.memory.owner_id,
                        topics=["convention", fact["kind"]],
                        attributes={
                            "module": path,
                            "kind": fact["kind"],
                            "topic": fact["topic"],
                            "source": "inferred",
                            "convention_version": 1,
                            "pr_ordinal": 0,
                        },
                    )
                )
        if records:
            created = self.memory.create(records, Tags(self.name, pr_id, 0, "prime"))
            self.memory.wait_for_visibility(created, tags=Tags(self.name, pr_id, 0, "prime"))
            self.primed_ids.extend(created)
        return records

    # -- retrieve ---------------------------------------------------------

    def query_for(self, pr: PullRequest) -> str:
        """Query text from the change summary and touched modules (spec 4d).

        Contains no PR number and no timestamp -- it is prompt-adjacent, and
        keeping identifiers out of it keeps retrieval comparable across runs.
        """
        modules = ", ".join(pr.modules)
        return f"{pr.title}. Files changed: {modules}"

    def retrieve(self, pr: PullRequest, ordinal: int) -> tuple[list[Memory], int]:
        tags = Tags(self.name, pr.pr_id, ordinal, "retrieve")
        found, _ = self.memory.search(
            self.query_for(pr),
            filter=scoped_filter(
                self.memory.namespace,
                memory_types=[REPO_CONVENTION, REVIEW_FINDING],
                modules=pr.modules,
            ),
            limit=self.retrieval_limit,
            similarity_threshold=self.similarity_threshold,
            tags=tags,
        )
        injected = 0
        if found:
            block = prior_knowledge_block([(m.id, m.prompt_text) for m in found])
            # Counted in isolation: the block's own tokens, excluding message
            # framing. Free API call, and it is what spec 7b asks for.
            injected = self.client.count_tokens(
                system="", messages=[{"role": "user", "content": [block]}]
            )
            self.memory.log_op(tags, "measure", 0, returned=len(found), injected=injected)
        return found, injected

    # -- review + write ---------------------------------------------------

    def review_pr(self, pr: PullRequest, ordinal: int) -> ReviewOutcome:
        retrieved, injected = self.retrieve(pr, ordinal)
        stable = [conventions_block(self.conventions)] if self.conventions else []
        volatile = (
            [prior_knowledge_block([(m.id, m.prompt_text) for m in retrieved])]
            if retrieved
            else []
        )
        result = review(
            self.client,
            Tags(self.name, pr.pr_id, ordinal, "review"),
            pr,
            ReviewContext(stable_blocks=stable, volatile_blocks=volatile),
            max_patch_chars=self.max_patch_chars,
            notes={"retrieved": len(retrieved), "injected_tokens": injected},
        )
        outcome = ReviewOutcome(
            agent=self.name,
            pr_id=pr.pr_id,
            pr_ordinal=ordinal,
            findings=result.findings,
            result=result,
            retrieved=retrieved,
            memories_used=result.memories_used,
            injected_tokens=injected,
        )
        outcome.written = self.write(pr, ordinal, outcome)
        return outcome

    def write(self, pr: PullRequest, ordinal: int, outcome: ReviewOutcome) -> list[str]:
        """Persist what the review learned, as explicit client-side writes.

        Automatic extraction stays off (spec 7a): it would run on the service's
        own LLM, whose tokens we cannot see, and the net-savings claim would
        need an asterisk.
        """
        if not outcome.findings:
            return []
        tags = Tags(self.name, pr.pr_id, ordinal, "write")
        if self.distill_writes:
            payload = {
                "findings": [
                    {
                        "file": f.file,
                        "line": f.line,
                        "severity": f.severity,
                        "category": f.category,
                        "message": f.message,
                    }
                    for f in outcome.findings
                ]
            }
            result = self.client.messages(
                tags,
                system=[text_block(WRITE_SYSTEM)],
                messages=[
                    {
                        "role": "user",
                        "content": [text_block("Findings from this review:\n" + json.dumps(payload, indent=1))],
                    }
                ],
                output_schema=WRITE_SCHEMA,
                notes={"findings": len(outcome.findings)},
            )
            rows = result.json().get("records", [])
        else:
            rows = [
                {
                    "module": f.file,
                    "topic": f.category,
                    "pattern": f.category,
                    "text": f.message,
                }
                for f in outcome.findings
            ]

        records = [
            Memory(
                id=memory_id("find", row["module"], row["topic"], str(ordinal)),
                text=row["text"],
                memory_type=REVIEW_FINDING,
                namespace=self.memory.namespace,
                owner_id=self.memory.owner_id,
                topics=["finding"],
                attributes={
                    "module": row["module"],
                    "finding_class": row["pattern"],
                    # createdAt is server-assigned, so sequence position lives
                    # in attributes -- real PR dates cannot be backdated onto a
                    # long-term record.
                    "pr_ordinal": ordinal,
                    "pr_number": pr.number,
                    "source": "review",
                },
            )
            for row in rows
            if row.get("text", "").strip()
        ]
        if not records:
            return []
        created = self.memory.create(records, tags)
        self.memory.wait_for_visibility(created, tags=tags)
        return created
