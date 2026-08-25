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
    MEMORY_TYPES,
    REPO_CONVENTION,
    REVIEW_FINDING,
    AgentMemoryClient,
    Memory,
    decode_ordinal_attr,
    memory_id,
    module_topics,
    resolve_modules,
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
- Return an empty list if the review taught nothing durable. That is a normal outcome.
- `module` must be copied verbatim from the `file` of one of the findings you were given. It is a retrieval key matched by exact string equality, not a description: any other value makes the record unreachable forever."""

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
        retrieval_limits: dict[str, int] | None = None,
        similarity_threshold: float | None = None,
        distill_writes: bool = True,
        dedupe_writes: bool = False,
        max_patch_chars: int | None = None,
    ):
        self.client = client
        self.memory = memory
        self.conventions = conventions
        self.retrieval_limit = retrieval_limit
        # Per-memory-type budgets. None keeps the pooled single search that
        # run-1 used, so that run stays reproducible; see retrieve().
        self.retrieval_limits = dict(retrieval_limits) if retrieval_limits else None
        self.similarity_threshold = similarity_threshold
        # When False the write phase persists findings verbatim and costs zero
        # model tokens. Distillation costs tokens but produces memories that are
        # reusable rather than diff-specific; either way the cost is measured.
        self.distill_writes = distill_writes
        # Off by default so run-1 reproduces: dedup changes the finding id
        # scheme, and reproducing an append-only store means reproducing the
        # ordinal-suffixed ids it wrote.
        self.dedupe_writes = dedupe_writes
        self.max_patch_chars = max_patch_chars
        self.primed_ids: list[str] = []

    # -- prime ------------------------------------------------------------

    def prime(
        self,
        sources: dict[str, str],
        *,
        pr_id: str = "prime",
        docs: dict[str, str] | None = None,
        after_create: Any = None,
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
                        # The module path is a *topic*, not just an attribute:
                        # topics carry the only membership filter the service
                        # offers, so this is what per-module retrieval matches.
                        topics=["convention", fact["kind"], *module_topics([path])],
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
            tags = Tags(self.name, pr_id, 0, "prime")
            created = self.memory.create(records, tags)
            self.primed_ids.extend(created)
            # Checkpoint before the visibility wait, not after. The wait can
            # fail on a service-side drop (see wait_for_visibility), and the
            # model calls above are the single most expensive thing the memory
            # agent does -- losing them to a failure *after* the store was
            # written means paying for them twice.
            if after_create is not None:
                after_create(records)
            self.memory.wait_for_visibility(created, tags=tags, records=records)
        return records

    # -- retrieve ---------------------------------------------------------

    def query_for(self, pr: PullRequest) -> str:
        """Query text from the change summary and touched modules (spec 4d).

        Contains no PR number and no timestamp -- it is prompt-adjacent, and
        keeping identifiers out of it keeps retrieval comparable across runs.
        """
        modules = ", ".join(pr.modules)
        return f"{pr.title}. Files changed: {modules}"

    def _search(
        self, pr: PullRequest, tags: Tags, memory_types: Sequence[str], limit: int
    ) -> list[Memory]:
        found, _ = self.memory.search(
            self.query_for(pr),
            filter=scoped_filter(
                self.memory.namespace,
                memory_types=list(memory_types),
                modules=pr.modules,
            ),
            limit=limit,
            similarity_threshold=self.similarity_threshold,
            tags=tags,
        )
        return found

    def retrieve(self, pr: PullRequest, ordinal: int) -> tuple[list[Memory], int]:
        """Pull the durable slice for this PR's modules.

        Two shapes, and which one is in force is a real experimental variable:

        **Pooled** (`retrieval_limits=None`, what run-1 did) -- one search over
        both memory types sharing one budget. Simple, and it is what makes run-1
        reproducible, but the budget saturated at the limit on *every* PR from
        the first one onward, so semantic conventions and episodic findings
        compete for the same fixed slots while only the finding count grows.

        **Split** -- one search per memory type, each with its own budget, so a
        growing pile of findings cannot squeeze conventions out (or vice versa)
        and the two are independently tunable. Costs one extra round trip and
        zero model tokens: searches are `billable=False`, so this changes the
        retrieval mix without touching the cost comparison.
        """
        tags = Tags(self.name, pr.pr_id, ordinal, "retrieve")
        if self.retrieval_limits:
            found: list[Memory] = []
            for memory_type in MEMORY_TYPES:
                limit = self.retrieval_limits.get(memory_type, 0)
                if limit > 0:
                    found.extend(self._search(pr, tags, [memory_type], limit))
        else:
            found = self._search(
                pr, tags, [REPO_CONVENTION, REVIEW_FINDING], self.retrieval_limit
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

        # A module topic that is not one of the PR's touched files can never
        # match a retrieval filter, so the record would be written, billed, and
        # unreachable. The model's `module` is free text: it has come back as a
        # sentence (which the service rejected outright, since topics cap at 100
        # chars) and as a bare basename. Resolve it, and count what will not
        # resolve rather than dropping it quietly.
        unrouted: list[str] = []
        routed_rows = []
        for row in rows:
            modules = resolve_modules(row.get("module", ""), pr.modules)
            if not modules:
                # Second chance: the finding this record came from names a real
                # file even when the distilled `module` does not.
                by_category = {f.category: f.file for f in outcome.findings}
                modules = resolve_modules(
                    by_category.get(row.get("topic", ""), ""), pr.modules
                )
            if not modules:
                unrouted.append(str(row.get("module", ""))[:80])
                continue
            routed_rows.append({**row, "module": modules[0], "modules": modules})

        records = [
            Memory(
                # With dedup on, the id drops the ordinal and so becomes a pure
                # function of (module, topic) -- which makes it the dedup key,
                # detectable with a GET instead of a fuzzy similarity search.
                # With dedup off it keeps the ordinal, which is what makes every
                # write an append; that is run-1's behaviour and reproducing it
                # requires reproducing the id scheme.
                id=(
                    memory_id("find", row["module"], row["topic"])
                    if self.dedupe_writes
                    else memory_id("find", row["module"], row["topic"], str(ordinal))
                ),
                text=row["text"],
                memory_type=REVIEW_FINDING,
                namespace=self.memory.namespace,
                owner_id=self.memory.owner_id,
                topics=["finding", *module_topics(row["modules"])],
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
            for row in routed_rows
            if row.get("text", "").strip()
        ]
        if unrouted:
            # Visible in the ledger, not swallowed: this is lost recall.
            self.memory.log_op(
                tags,
                "measure",
                0,
                returned=0,
                notes={
                    "unroutable_modules": unrouted,
                    "why": (
                        "no touched file matched; the record was dropped because a "
                        "memory with no module topic can never be retrieved"
                    ),
                },
            )
        if not records:
            return []
        if not self.dedupe_writes:
            created = self.memory.create(records, tags)
            self.memory.wait_for_visibility(created, tags=tags)
            return created
        return self._write_deduped(records, ordinal, tags)

    def _write_deduped(
        self, records: list[Memory], ordinal: int, tags: Tags
    ) -> list[str]:
        """Merge a repeat finding into its existing record instead of appending.

        Why this is a correctness fix and not an efficiency one: an append-only
        store is what turns a wrong belief into a *growing* one. Run-1 restated
        one false convention 11 times across 9 PRs, and each restatement became
        another record competing for the same fixed retrieval window -- the
        model reads the claim, repeats it, and the write phase persists it again
        under a new id. Collapsing on (module, topic) breaks that loop.

        What this does NOT do is make a false claim less likely to be repeated;
        it only stops the copies accumulating. Suppressing a refuted claim is
        `review_policy`'s job (spec 4d), which is still unbuilt.

        Recurrence becomes explicit rather than implicit in a copy count:
        `occurrences` and `last_pr_ordinal` are recorded, while `pr_ordinal`
        keeps its first-seen value so chronology still means what it says.
        """
        # Same-PR collisions first: two findings can distill to one
        # (module, topic), and two records with one id in a single bulk create
        # is not a shape the service promises anything about.
        unique: dict[str, Memory] = {}
        collapsed_in_batch = 0
        for rec in records:
            if rec.id in unique:
                collapsed_in_batch += 1
                continue
            unique[rec.id] = rec

        fresh: list[Memory] = []
        merged: list[str] = []
        for rec in unique.values():
            existing = self.memory.get(rec.id)
            if existing is None:
                fresh.append(rec)
                continue
            attrs = dict(existing.attributes or {})
            # Decoded, not read raw: attributes come back as strings, so a raw
            # read would put "001" where an int belongs and quietly poison any
            # arithmetic downstream.
            first_seen = decode_ordinal_attr(attrs.get("pr_ordinal"), ordinal)
            seen = decode_ordinal_attr(attrs.get("occurrences"), 1) or 1
            self.memory.patch_fields(
                rec.id,
                memory_type=rec.memory_type,
                # The newest distillation is the most current understanding.
                # Synthesizing a combined narrative would be a model call --
                # that is consolidation, the separate and output-costing option.
                text=rec.text,
                # Union, not replace: a recurrence in a *different* file must
                # stay retrievable from both, and replacing topics would make
                # the earlier module unreachable.
                topics=sorted(set(existing.topics or []) | set(rec.topics)),
                attributes={
                    **attrs,
                    **rec.attributes,
                    "pr_ordinal": first_seen,
                    "last_pr_ordinal": ordinal,
                    "occurrences": seen + 1,
                },
                tags=tags,
            )
            merged.append(rec.id)

        created = self.memory.create(fresh, tags) if fresh else []
        # Only new records need a visibility wait; a patch updates a record that
        # is already searchable.
        if created:
            self.memory.wait_for_visibility(created, tags=tags)
        if merged or collapsed_in_batch:
            self.memory.log_op(
                tags,
                "measure",
                0,
                returned=len(merged),
                notes={
                    "deduped_writes": len(merged),
                    "collapsed_within_pr": collapsed_in_batch,
                    "why": (
                        "a repeat finding updated its existing record instead of "
                        "appending a copy that would compete for the same "
                        "retrieval slots"
                    ),
                },
            )
        return created + merged
