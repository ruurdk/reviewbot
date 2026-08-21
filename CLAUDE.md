# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository state

Pre-implementation. As of this writing the repo has **no commits and no source code** — its entire content is one design document: [docs/agentic-memory-pr-review-demo-spec.md](docs/agentic-memory-pr-review-demo-spec.md). There is no build, test, or lint tooling yet, and no language/runtime has been chosen. Read the spec before doing anything else; it is the source of truth for what this project is.

When adding the first code, ask the user for the stack rather than assuming one, then record the real build/test/lint commands here (including how to run a single test).

## What this project is

A **demo/experiment**, not a product: two PR-review agents run over the *same frozen sequence of 15–25 real PRs* under identical models, prompts, and tools. The only difference between them is a memory layer. The deliverable is evidence that agentic memory reduces cumulative input tokens per review without degrading review quality.

- **Baseline agent (control)** — assembles context fresh on every PR (diff + style guide + relevant source), no persistence.
- **Memory agent (treatment)** — same review loop wrapped in three phases: `retrieve` → `review` → `write`.
- **Memory types** — semantic (repo conventions/architecture), episodic (past findings and their resolutions), procedural (calibrated checklist, which suggestion classes this team accepts). Build order: semantic + episodic first, procedural last.

## Memory substrate: Redis Agent Memory in Redis Iris

The demo runs against the **managed** Redis Agent Memory service in Redis Iris (Redis Cloud) — store-scoped REST (`/v1/stores/{storeId}/...`) plus Python/TypeScript SDKs. The service is in **preview**; pin SDK versions and re-verify the API surface before relying on it.

**The `V0/` directory in `redis/agent-memory-server` is explicitly out of scope.** It is the older open-source research implementation and is not the supported path. Do not model the demo on V0's surface — it differs materially from the managed API: V0 has `memory_prompt`, an MCP interface, hybrid/keyword search knobs, query optimization, recency boost, and `entities`; the managed API has none of those. Managed instead offers custom memory types with structured fields and extraction instructions, an arbitrary filterable `attributes` map, and guarded field updates.

The three memory types are modeled as **custom memory types on the store** (`repo_convention`, `review_finding`, `review_policy`), scoped per run by `namespace` and routed by `attributes` (`module`, `pr_ordinal`, `finding_class`, `convention_version`, `source`).

## Architectural constraints that the experiment depends on

These are not style preferences — violating them invalidates the results:

- **The harness is shared.** Both agents must run the identical review loop; the memory layer is the *only* difference. Build the harness first (PR ingestion, review loop, logging); both agents depend on it.
- **Tag every model call** with `{agent, pr_id, phase: retrieve|review|write}`. Token attribution is the whole point — an untagged call is an unmeasurable one.
- **Memory overhead is logged separately** (write tokens + retrieval tokens) and savings are always reported *net* of it. Never report a gross saving.
- **The unit of evaluation is the sequence, not the PR.** Memory *loses* on PR #1 (cold start, write cost). Any single-PR comparison is meaningless here and must not be presented as a headline.
- **Prompt caching, if enabled, must be enabled on both agents** so the comparison isolates memory's contribution.
- **Quality is a mandatory guardrail, reported even when unflattering** — precision/recall against a human-labeled gold set, plus false-positive rate. Token savings bought with worse reviews are a regression, not a result.
- **Freeze the dataset.** Pin commit SHAs for the PR sequence so runs are reproducible.
- **Count tokens spent inside the memory service, or don't use them.** Automatic extraction and session summarization run on the service's own LLM and are invisible to client-side usage accounting. Default to explicit client-side memory writes so every write token is measurable; if automatic extraction is used, either measure the service-side cost or disclose it as excluded. Reporting client-only tokens while using background extraction makes the headline chart wrong in memory's favour.
- **Wait for write visibility between PRs.** Extraction/promotion is asynchronous and searches are eventually consistent, so PR N+1 can read a store that hasn't settled. Poll until written records are retrievable, and exclude that wait from the latency metric.
- **Don't depend on wall-clock aging.** A long-term memory's `createdAt` is server-assigned and not client-settable, so real PR dates can't be backdated onto long-term records. Carry chronology in `attributes`. (Session-event `createdAt` *is* client-supplied.)

## Curated dataset requirements

The PR sequence is deliberately curated (the spec argues at length why this is fair, not cheating — recurrence is what production actually looks like). It must contain: repeated modules, a recurring bug pattern spanning 2–3 PRs, a **false-positive trap** (a deliberate pattern the baseline re-flags every time — this is the key narrative beat), and a **style-guide-changing PR** to exercise memory invalidation.

If memory invalidation isn't built yet, surface that as an honest limitation rather than dropping the convention-change PR.

## Resolved experiment decisions

All four of the spec's original open questions are settled, plus one the research surfaced. Don't re-litigate these:

- **Audience** — technical and skeptical. Instrumentation is exposed, not hidden behind a hero chart.
- **Substrate** — managed Redis Agent Memory in Iris, Python SDK (see above).
- **Repo** — `redis/redis-py`, sequence curated around the genuinely hot connection/cluster spine (`connection.py`, `cluster.py`, `asyncio/cluster.py`, `commands/core.py`), drawn from the upper half of the diff-size distribution because the median PR touches only 2 files.
- **Quality labeling** — hybrid: merged-human-comment proxy across the whole sequence plus a hand-labeled gold set on a 5-8 PR subset covering every narrative beat. Proxy-only is not viable — a third of sampled redis-py PRs have zero inline human comments.
- **Extraction mode** — explicit client-side memory writes; automatic extraction off, so all write cost is measurable.

Still open, and neither blocks the harness: whether service-side extraction/summarization token usage is observable, and whether keyword/hybrid search is reachable through the managed API today.

## Practical notes

PR ingestion needs an authenticated `GITHUB_TOKEN` — the unauthenticated rate limit is exhausted well before 15-25 PRs' files and comments are pulled. Cache ingested PR data to disk so reruns don't re-hit the API. There is no `gh` CLI on this machine.
