# Demo Spec: Agentic Memory for Token-Efficient PR Review

## 1. Thesis

A PR review agent without memory re-derives the same context on every pull request — repo conventions, architecture, past decisions, recurring bug patterns. That re-derivation is a recurring **input-token tax**. Agentic memory pays that tax once, distills it, and retrieves only the relevant slice per review. Over a sequence of PRs, cumulative token cost flattens while a memoryless baseline keeps paying full price.

The demo's job is to make that gap **visible, measurable, and honest** — including memory's warm-up cost, so the savings shown are *net*, not cherry-picked.

## 2. What the demo must prove (and must not fake)

Prove:
- Memory reduces input tokens per review as it warms up, and the *cumulative* cost curve crosses and diverges from the baseline.
- Review **quality holds or improves** — token savings while degrading review quality is not a win, it's a worse reviewer.
- Savings are **net of memory overhead** (writing memories and retrieving them both cost tokens).

Must not fake:
- No single-PR comparison presented as the headline. On PR #1 memory *loses* (write overhead, cold cache). The story only exists across a sequence.
- No hidden quality regression. If precision/recall drops, the demo says so.
- No token accounting that quietly excludes cost incurred inside the memory service. See §7a — this is the single most likely place for this demo to be accused of cheating, and the accusation would be fair unless we handle it explicitly.

## 3. Audience & framing (resolved)

**Confirmed: technically literate and skeptical** — eng leadership, platform/devex teams, design-partner engineering audiences. They will ask "isn't this just prompt caching?" (§5), "where did the extraction cost go?" (§7a), and "did you rig the sequence?" (§6). The spec answers all three on purpose. Instrumentation is exposed, not hidden behind a single hero chart; the cumulative-cost chart is the close, not the whole surface.

Consequence for build priority: the accounting harness (§7a) is load-bearing for credibility and ranks alongside the review loop itself, not after it.

## 4. System under test

Two agents review the **same PR sequence** under identical models, prompts, and tools. The only difference is the memory layer.

### 4a. Baseline agent (control)
On each PR it assembles context fresh: reads the diff, pulls the style guide / CONTRIBUTING doc, and reads the relevant source files it needs to understand the change. It produces review comments and logs token usage. It has no persistence between PRs and never contacts the memory service.

### 4b. Memory agent (treatment)
Same review loop, plus a memory layer that does three things:
- **Retrieve** relevant memories before assembling context, and skip re-fetching anything already summarized in memory.
- **Review** using retrieved memories as grounding.
- **Write** new/updated memories after the review (newly learned conventions, decisions, issue patterns, reviewer feedback).

The prompt-assembly code path is shared with the baseline; the memory agent's delta is a retrieve step that prepends memories and a write step after the review. Anything else that differs between the two agents is a confound.

### 4c. Memory substrate (resolved): Redis Agent Memory in Redis Iris

**Confirmed substrate: Redis Agent Memory, the managed service in Redis Iris** (Redis Cloud). The open-source `V0/` Agent Memory Server in the `redis/agent-memory-server` repo is explicitly **not** used — it is the older research foundation and not the supported path.

What the managed service gives us, and what the demo must build on top:

| Capability | Managed API surface | Use in this demo |
|---|---|---|
| Two-tier memory | Session memory (ordered events) + long-term memory | Session memory holds the review conversation; long-term memory is the thing under test |
| Long-term create | `POST /v1/stores/{storeId}/long-term-memory`, idempotent on a **client-supplied `id`** | Explicit memory writes; the client-supplied id makes reruns deterministic |
| Retrieval | `POST /v1/stores/{storeId}/long-term-memory/search` — `text`, `similarityThreshold`, `filter`, `filterOp`, `limit` (default 10, max 100), `pageToken` | The retrieve phase; `limit` and `similarityThreshold` are the retrieval-bloat knobs (§9) |
| Filtering | `sessionId`, `ownerId`, `namespace`, `topics`, `memoryType`, `createdAt`, and an arbitrary `attributes` map — each with `eq`/`ne`/`in`/`all` style clauses, combined with `filterOp: all\|any` | Scope retrieval to the module(s) a PR touches instead of pulling the whole store |
| Update | `PATCH .../{memoryId}` and `PATCH .../{memoryId}/fields`, the latter guarded — the write is rejected unless the caller's `memoryType` and `namespace` match the stored record | The convention-invalidation beat (§8/§9) |
| Delete | Bulk delete by id list on `DELETE .../long-term-memory` | Per-run store reset |
| Custom memory types | Define domain memory types with **structured fields and extraction instructions** | This is how the three memory types below are modeled — they are first-class, not a hack |
| Session summarization | Automatic, threshold-based; response exposes `summary.text`, `summarizedUpToEventId`, `summarizedEvents`, and a `summary.metadata` object documented to carry things like model name and token count | Optional; if enabled it is a service-side LLM cost — see §7a |
| Automatic extraction | Background extraction of long-term memories from session events | Optional, and **not** the recommended default here — see §7a |
| Access | Python SDK, TypeScript SDK, REST | Use the **Python SDK** — it matches the target repo's language (§6) and the harness's likely stack; same client for both agents' plumbing |

Notable absences to design around, not against:
- **No `memory_prompt`-style convenience endpoint.** The harness assembles the final prompt itself. This is an advantage for this demo: every injected token is ours to count.
- **No MCP interface** on the managed service. The agent talks to memory over the SDK/REST, and memory access is a harness-controlled phase rather than a model-chosen tool call. Keep it that way — a model-chosen tool call would make retrieval cost vary run to run and weaken the comparison.
- **Search modes:** the product docs advertise semantic, keyword, and hybrid search, but the published OpenAPI search body exposes only `text` + `similarityThreshold`. Resolve this before building the retrieve phase (§12) rather than assuming hybrid is reachable.
- **Preview status.** Redis Agent Memory is currently in preview and behavior may change. Pin the SDK version, record the API version used, and re-verify the surface before the demo is shown.

### 4d. Memory model mapped onto the product

Three memory types, because they save tokens in different ways. Each becomes a **custom memory type** on the store, with structured fields and extraction instructions:

| Spec type | Custom memory type | Holds | Token-saving mechanism |
|---|---|---|---|
| **Semantic** | `repo_convention` | Repo facts: conventions, architecture, module ownership, distilled style guide | Replaces re-reading docs and re-inferring conventions by scanning files every time |
| **Episodic** | `review_finding` | Past reviews: findings, resolutions, "flagged in PR #234, resolved by X" | Recall a prior finding instead of re-analyzing an identical pattern from scratch |
| **Procedural** | `review_policy` | How to review here: calibrated checklist, which suggestion classes this team accepts/rejects | Suppresses low-value comments and the review rounds they trigger |

Conventions for the store, so retrieval can be scoped tightly:
- **`namespace`** — one per repo per experiment run (e.g. `repo-x/run-3`). Gives a clean per-run reset and prevents cross-run contamination.
- **`ownerId`** — the agent identity. Baseline never writes, so this is constant for the treatment agent.
- **`topics`** — coarse routing: `convention`, `finding`, `policy`, plus a module tag.
- **`attributes`** — the precise, filterable metadata: `module` (path prefix), `pr_ordinal` (position in the frozen sequence), `pr_number`, `finding_class` (for the recurring-pattern and false-positive beats), `convention_version` (for invalidation), `source` (`style-guide` / `human-correction` / `inferred`).

Retrieval for a given PR is then a scoped search: text query built from the diff's touched modules and change summary, filtered to the run namespace with `attributes.module` in the PR's touched-module set, `filterOp: any`, and a `limit` tuned in §9 — not an unfiltered semantic sweep.

## 5. The "prompt caching" rebuttal (preempt this on a slide)

A skeptic will say memory is just prompt caching. It isn't:
- Prompt caching is **verbatim and ephemeral** — it caches exact token prefixes within a session/TTL.
- Memory is **distilled, persistent, and semantically retrieved** — it carries forward learned facts and decisions across sessions and PRs, and retrieves the relevant slice rather than replaying a fixed prefix.

They're complementary. If the environment supports prompt caching, enable it on **both** agents so the comparison isolates memory's contribution rather than conflating the two. Log cache-read vs cache-write vs uncached input tokens separately for both agents, so "the memory agent just got luckier with the cache" is answerable with data.

Related but distinct: Iris also includes **LangCache** (semantic response caching). It is deliberately **out of scope** here — mixing it in would conflate two different savings mechanisms in one chart. Name it on the slide as a separate, additive lever so the audience doesn't think we're double-counting.

## 6. Dataset / scenario (resolved: `redis/redis-py`)

**Repo: `redis/redis-py`.** Chosen over `node-redis` on measured evidence — a sample of the 20 most recent merged PRs shows the recurrence property §6 needs is *already present in the real history*, so the sequence can be curated without engineering it:

| Signal | Measured on 20 recent merged redis-py PRs |
|---|---|
| Module recurrence | `redis/connection.py` 4x, `redis/asyncio/cluster.py` 4x, `redis/cluster.py` 3x, `redis/commands/core.py` 3x — in 20 PRs |
| Inline review comments per PR | mean 4.0, median 1.0, max 25, **7 of 20 PRs had zero** |
| Changed files per PR | median 2, max 23 |

(`node-redis` was not measured — the unauthenticated GitHub API rate limit blocked it. Revisit only if redis-py curation hits a wall.)

Three consequences for curation, straight out of those numbers:

- **The connection/cluster spine is the sequence.** `connection.py`, `cluster.py`, `asyncio/cluster.py`, and `commands/core.py` are genuinely hot. Build the 15–25 PR sequence around that cluster so repeated-module reuse is a property of the real repo rather than a curatorial thumb on the scale — which is a materially stronger answer to "did you rig it?" than an argument about representativeness alone.
- **Select for diff size, deliberately.** A median PR touches 2 files. If the sequence is drawn from the median, the baseline's per-PR re-read cost is small and so is the absolute gap — the curve bends, but unimpressively. Draw from the upper half of the size distribution and state that selection rule openly; the honest framing is "we chose PRs substantial enough for context assembly to cost something," not "we chose PRs where we win."
- **Proxy-only quality labeling is not viable here.** With a median of 1 inline comment and a third of PRs having none, the merged human review is too sparse to serve as the sole gold standard — which is why §7c is a hybrid.

The sequence must deliberately include:
- **Repeated modules** — several PRs touching the same files, so semantic memory gets reused. Use the connection/cluster spine.
- **A recurring bug pattern** — the same class of issue (e.g., an unchecked error path or a missing async cleanup) appearing in 2–3 PRs, so episodic recall fires.
- **A false-positive trap** (the key story beat, see §8) — an intentional pattern the baseline keeps re-flagging.
- **A convention change** — one PR that edits the style guide or CONTRIBUTING doc, to test memory *invalidation* (see §9).

Freeze the sequence and commit SHAs so runs are reproducible. Record, per PR: number and identity of touched modules, whether it is a recurrence of an earlier module, diff size, and which beat (if any) it serves. That table is what makes the recurrence-rate disclosure in §9 concrete.

**Ingestion note:** PR ingestion needs an authenticated `GITHUB_TOKEN`. Pulling 15–25 PRs with their files and review comments exhausts the unauthenticated rate limit quickly. Cache ingested PR data to disk so reruns don't re-hit the API — this also keeps the frozen sequence genuinely frozen.

## 7. Metrics & instrumentation

Primary:
- **Input tokens per review** (where memory should shrink cost).
- **Cumulative total tokens** across the sequence — this is the headline chart.

Secondary:
- Output tokens per review.
- **Memory overhead** logged separately: write tokens + retrieval tokens. Savings are reported *net* of these.
- Wall-clock latency per review — with the caveat in §9 that the memory agent pays network round-trips to a managed service that the baseline does not.

Quality (mandatory guardrail — report even if unflattering):
- Precision / recall of review comments against a human-labeled gold set for the sequence.
- **False-positive rate** — expected to *drop* for the memory agent thanks to decision memory.
- Human-preference or acceptance rate of comments, if you can get a reviewer to rate them blind.

Instrument by tagging every model call with `{agent, pr_id, phase: retrieve|review|write}` so you can attribute every token.

### 7a. The token-accounting boundary (the credibility crux)

The memory service can do work with **its own LLM**, server-side: automatic extraction of long-term memories from session events, and automatic session summarization. Those tokens are real, they are part of memory's true cost, and they are **not** returned in our client-side usage accounting. The product docs frame automatic extraction as requiring "no LLM token usage from your application" — which is true and also exactly the sentence a skeptical audience will pounce on, because *application* is not *system*.

If the demo runs automatic extraction and reports only client-side tokens, the headline chart is wrong in memory's favour.

**Resolved: explicit client-side writes.** The review agent decides what to remember and persists it with a direct long-term-memory create call, using a client-supplied `id` for idempotence. Automatic extraction stays off. Every write token is an agent token we emit and count, so the net-savings claim needs no asterisk. This costs nothing narratively — direct memory creation is a documented first-class path, and "the reviewer decides what to remember" is a *better* story for a review agent than opaque background extraction.

Say this on the methodology slide in one sentence, and preempt the obvious follow-up ("why not use the automatic extraction?") with the real reason: it would make the central measurement unverifiable. That answer strengthens the demo rather than weakening it.

Two fallbacks, documented in case the decision is revisited:
- *Automatic extraction with service-side cost measured* — viable only if service-side LLM usage is observable per store or per call (session `summary.metadata` is documented to carry model and token count, which is a start, but extraction needs an equivalent). Confirm the observability exists end-to-end before relying on it (§12).
- *Automatic extraction with cost disclosed as unmeasured* — acceptable only with an explicit slide excluding service-side tokens and bounding them by estimate (tokens in x the extraction model's rate). Weakest option; use only if forced. Also log the **embedding** calls for writes and searches — small per call, but they are part of memory's cost and a careful viewer will ask.

### 7b. Retrieval cost is a measured quantity, not an assumption

Log, per review: number of memories retrieved, their total token count as injected into the prompt, the `limit` and `similarityThreshold` used, and how many retrieved memories the model actually referenced in its output. The last one is the retrieval-precision signal — retrieving 40 memories to use 3 is the failure mode that silently eats the savings (§9).

### 7c. Quality labeling (resolved: hybrid)

Sparse human review in redis-py (§6) rules out a proxy-only gold standard, and hand-labeling 25 PRs is the most expensive line in the build plan. So:

- **Proxy across the whole sequence.** Score each agent's comments for agreement with the merged PR's actual human review comments. Cheap, already in the ingested data, and gives full-sequence coverage of precision-like signal.
- **Hand-labeled gold set on a 5–8 PR subset**, chosen to include *every* beat PR: the recurring-bug-pattern PRs, the false-positive trap and its subsequent touches, and the convention-change PR. These are the PRs the narrative rests on and the only place false-positive rate can be stated rigorously.

Report the two separately and never average them into one number. State the subset size and how it was chosen — a skeptical viewer will (correctly) weight the hand-labeled subset far more heavily, and the beat PRs are exactly the ones they'll want labeled.

Because 7 of 20 sampled PRs had zero inline human comments, the proxy has a known blind spot: it cannot distinguish "the agent said nothing useful" from "the humans said nothing either." Note that limitation where the proxy is reported.

## 8. Demo narrative (what the viewer sees)

Three acts.

**Act 1 — The tax.** Run the baseline over the sequence. Show its cost-per-PR line staying flat and high; each PR re-reads the style guide and the same modules. Point at the repeated context in the logs.

**Act 2 — The investment.** Run the memory agent. On PR #1–#2 it costs *more* (writing memories, no cache to draw on). Don't hide this — name it as the investment. Then watch per-PR cost fall as reused modules and recalled patterns start paying off.

**Act 3 — The false-positive loop (the money beat).** Take the intentional pattern. Baseline flags it → a human explains it's deliberate → baseline re-reviews on the next touch and **flags it again**, burning tokens on a settled question every time. The memory agent stores the human's correction once (as a `review_policy` memory with `source: human-correction`) and never re-flags it. This is the clean demonstration that memory saves tokens *and* improves quality simultaneously, and that the savings compound across the multi-turn human↔agent loop, not just within a single review.

Because the correction is a stored record with a stable id, this act has a screenshot that lands: the retrieved `review_policy` memory shown next to the review that *didn't* fire.

Close on the **cumulative cost chart**: baseline as a steep straight line, memory agent starting higher then bending toward a floor, with the crossover point and the widening gap annotated.

## 9. Risks & honest caveats (put these on a slide too — credibility)

- **Cold start:** memory helps nothing on the first PR. The value is an amortized bet on recurrence.
- **Write cost:** persisting memories costs tokens; net savings only appear after reuse. This is why the sequence, not the single PR, is the unit of evaluation.
- **Retrieval bloat:** retrieving too much memory eats the savings. Retrieval precision is a first-class concern — measure tokens pulled in during the retrieve phase (§7b). Concretely: `limit` defaults to 10 and caps at 100, and `similarityThreshold` gates on normalized cosine similarity. Do a small sweep over both and report the setting used; a demo tuned to `limit: 100` would be self-sabotaging.
- **Stale memory:** when the style-guide PR lands, memory must update or the agent reviews against dead conventions. The guarded `PATCH .../fields` call (which rejects the write unless `memoryType` and `namespace` match) plus a bumped `convention_version` attribute is the intended mechanism; verify the *old* convention stops being retrieved, not just that a new record exists. If invalidation isn't built, surface it as a limitation rather than quietly dropping the convention-change PR.
- **Overfitting to recurrence:** a sequence engineered for heavy recurrence flatters memory. State the recurrence rate you chose and argue it's representative; ideally show sensitivity to it.
- **Asynchronous extraction / eventual consistency.** Long-term memory extraction and promotion happen in the background, and the docs warn that recently written or deleted records may briefly not reflect in searches. A sequential PR replay that writes after PR N and searches at PR N+1 can therefore read a store that hasn't settled — which would understate memory's benefit for non-obvious reasons. The harness must explicitly wait for write visibility (poll the search or get-by-id until the record appears) between PRs, and that wait must be excluded from the latency metric.
- **Time compression.** The demo replays 15–25 PRs in minutes, but a long-term memory's `createdAt` is server-assigned at write time and is not client-settable — so real PR dates cannot be backdated onto long-term records, and any day-scale time decay is inert in a compressed run. Carry chronology in `attributes` (`pr_ordinal`, `pr_number`, and the real merge date) and do not design any beat that depends on wall-clock aging. Session event `createdAt` *is* client-supplied, so session-tier chronology can be made faithful if a beat needs it.
- **Latency is not apples-to-apples.** The memory agent makes network calls to a managed service; the baseline reads local files. Report latency, but do not headline it, and say why.
- **Preview-stage API.** Redis Agent Memory is in preview; features and behavior may change. Pin SDK versions, record the API surface used, and re-verify before showing the demo. A demo that breaks live is worse than a demo that claims less.

## 10. Build plan

1. **Harness** — PR ingestion, the shared review loop, and the token/quality accounting layer with per-call tagging (§7/§7a). Highest priority; both agents depend on it, and the accounting layer is what makes the result defensible.
2. **Store provisioning** — create the Agent Memory service on Redis Cloud, register the three custom memory types (`repo_convention`, `review_finding`, `review_policy`) with their structured fields and extraction instructions, and script per-run namespace reset.
3. **Baseline agent** — fresh-context assembly each PR.
4. **Memory layer** — retrieve and write phases against the managed API, scoped-search construction, write-visibility waiting. Start with `repo_convention` + `review_finding`; `review_policy` last (it is the Act 3 beat, so it lands late but matters most).
5. **Dataset curation** — select and freeze the 15–25 PR sequence from `redis/redis-py` around the connection/cluster spine, cache the ingested PR data, hand-label the 5–8 PR gold subset, record the per-PR beat/recurrence/diff-size table.
6. **Runs & analysis** — execute both agents, produce the cumulative chart and the quality table.
7. **Narrative surface** — the three-act walkthrough and the annotated chart.

Suggested slice for a first end-to-end signal: harness + baseline + `repo_convention`-only memory over a 10-PR sequence centred on one recurring module (`redis/connection.py` is the strongest candidate), using explicit client-side writes (§7a). That alone should show the curve bending and validates both the thesis and the accounting story before investing in the other two memory types.

## 11. Success criteria

The demo succeeds if, over the frozen sequence: cumulative tokens for the memory agent finish **meaningfully below** the baseline *net of memory overhead*, with the accounting boundary stated; review quality is **statistically no worse** (ideally better on false-positive rate); and a skeptical viewer leaves able to explain both *why* it works and *when it wouldn't* (low-recurrence, high-churn repos), and able to see where every counted token came from.

---

## 12. Open decisions

Resolved:
- ~~**Audience**~~ -> technical/skeptical (§3).
- ~~**Memory substrate**~~ -> Redis Agent Memory in Redis Iris, managed; OSS `V0/` explicitly excluded (§4c).
- ~~**Repo**~~ -> `redis/redis-py`, curated around the connection/cluster spine, drawn from the upper half of the diff-size distribution (§6).
- ~~**Quality labeling**~~ -> hybrid: merged-human-comment proxy across the sequence, plus a hand-labeled gold set on a 5–8 PR subset covering every beat PR (§7c).
- ~~**Extraction mode**~~ -> explicit client-side writes; automatic extraction off (§7a).

Still open — both are internal verification questions, and neither blocks starting the harness:
- **Service-side observability:** is extraction/summarization LLM token usage observable per store or per call? Only gates the §7a fallbacks, not the chosen path.
- **Search modes:** are keyword and hybrid search reachable through the managed API and SDKs today, given the published OpenAPI search body exposes only `text` and `similarityThreshold`? Affects how the retrieve phase constructs queries (§4d) — semantic-only is workable, but worth knowing before tuning retrieval precision.
