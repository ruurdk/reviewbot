# Demo Spec: Agentic Memory for Token-Efficient PR Review

## 1. Thesis

A PR review agent without memory re-derives the same context on every pull request — repo conventions, architecture, past decisions, recurring bug patterns. Before it can judge a single diff it has to rebuild its understanding of what the repo *is*, and it does that again on the next PR, and the next. That re-derivation is a recurring **input-token tax**, and it is the largest line item on the bill.

Agentic memory converts it from a **per-PR cost into a per-repo cost**: pay once to distill the repo, then retrieve only the relevant slice per review. Over a sequence of PRs, cumulative cost flattens while a memoryless baseline keeps paying full price. Prompt caching cannot do this — its longest TTL is one hour, and no real PR cadence fits inside that (§5).

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

**The baseline reads under a stated budget, not without limit.** In the frozen sequence, PR #4052 touches 62 Python files; reading them all in full would approach the 1M context window, cost several dollars for one review, and let two outlier PRs dominate the comparison. So source context is capped (400k chars, ~100k tokens — roughly a human reviewer's working set), filled with the files the diff changes most, and every dropped file is counted and reported per review. The direction matters: the budget makes the *baseline* cheaper, so the measured gap stays conservative.

### 4b. Memory agent (treatment)
Same review loop, plus a **one-time primer** and a three-phase memory layer:
- **Prime** (once per repo, before the sequence) — distill the repo's architecture and conventions into durable memories. See §4e; this is the mechanism that makes repo understanding a per-repo cost.
- **Retrieve** relevant memories before assembling context, and skip re-fetching anything already captured in memory.
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
| Filtering | `sessionId`, `ownerId`, `namespace`, `topics`, `memoryType`, `createdAt` take `eq`/`ne`/`in`/`all` clauses; the `attributes` map takes a *typed* clause (`string`/`number`/`boolean`/`list`) with **no membership operator**. Combined with `filterOp: all\|any` | Scope retrieval to the module(s) a PR touches instead of pulling the whole store — via `topics`, which is the only field that can express "any of these modules" (§4d) |
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
**Verified against the live preview service (2026-08-21, via `python3 -m reviewbot memcheck`):**

- Auth is `Authorization: Bearer <key>` against `{store endpoint}/v1/stores/{storeId}/...`; `x-api-key` gets a 403. The base URL and header shape were previously inferred (the published OpenAPI declares neither `servers` nor `securitySchemes`) and are now confirmed.
- A request carrying urllib's default `Python-urllib/3.12` User-Agent is rejected by Cloudflare with `403 error_code 1010` ("browser_signature_banned"). The body looks nothing like an auth failure, so this costs an hour if you meet it cold. Any explicit UA works.
- **Field types must match the registered type exactly, and a mismatch fails the whole create** — `400 attribute "convention_version" has the wrong type for memory type "repo_convention" (expected str)`. The provisioned types declare scalar fields as `str`, so the harness encodes attributes on the wire and zero-pads ordinals (`"010"`, not `"10"`, since string ordering would otherwise put 10 before 9). An undeclared field is equally fatal: `400 unknown attribute "module"`.
- **Measured write→searchable lag: 0.33s** on the demo store. Small, but non-zero and asynchronous, which is why the runner waits for write visibility between PRs and excludes that wait from the latency metric.
- **Custom memory types must be registered on the store before any write.** An unregistered type fails every create with `400 memory type "repo_convention" is not registered on this store`, and there is no registration endpoint on the data plane (`POST .../memory-types` 404s, `/v1/stores` 403s from another host) — so provisioning is a console/control-plane action, not something the harness can do. See [store-provisioning.md](store-provisioning.md) for the exact field definitions.
- **The search body silently ignores unknown fields.** `searchMode`, `mode`, and `keyword` each return `200 {"items": []}` rather than a 400. So a mistyped retrieval knob looks like it worked, and §12's keyword/hybrid question cannot be settled by probing — the answer has to come from Redis.
- `id` and `namespace` are validated server-side to alphanumerics-and-hyphens, confirming the correction in §4d. Bulk delete validates every id before deleting any, so one malformed id rejects the whole batch.

- **Preview status.** Redis Agent Memory is currently in preview and behavior may change. Pin the SDK version, record the API version used, and re-verify the surface before the demo is shown.

### 4d. Memory model mapped onto the product

Three memory types, because they save tokens in different ways. Each becomes a **custom memory type** on the store, with structured fields and extraction instructions:

| Spec type | Custom memory type | Holds | Token-saving mechanism |
|---|---|---|---|
| **Semantic** | `repo_convention` | Repo facts: conventions, architecture, module ownership, distilled style guide | Replaces re-reading docs and re-inferring conventions by scanning files every time |
| **Episodic** | `review_finding` | Past reviews: findings, resolutions, "flagged in PR #234, resolved by X" | Recall a prior finding instead of re-analyzing an identical pattern from scratch |
| **Procedural** | `review_policy` | How to review here: calibrated checklist, which suggestion classes this team accepts/rejects | Suppresses low-value comments and the review rounds they trigger |

Conventions for the store, so retrieval can be scoped tightly:
- **`namespace`** — one per repo per experiment run (e.g. `repo-x-run-3`). Gives a clean per-run reset and prevents cross-run contamination. **The service validates `namespace` (and `id`) against `^[a-zA-Z0-9-]+$`**, so a slash-separated name like `repo-x/run-3` is rejected — dashes only. `memoryType` is the one field that permits underscores, so `repo_convention` is fine.
- **`ownerId`** — the agent identity. Baseline never writes, so this is constant for the treatment agent.
- **`topics`** — the routing field, and the one module retrieval actually filters on: `convention`, `finding`, `policy`, **plus the raw path of every module the record concerns**.
- **`attributes`** — provenance and reporting metadata: `module`, `pr_ordinal` (position in the frozen sequence), `pr_number`, `finding_class` (for the recurring-pattern and false-positive beats), `convention_version` (for invalidation), `source` (`style-guide` / `human-correction` / `inferred`). Readable via `GET`, usable for equality filters — but *not* for the module fan-out, per the next paragraph.

Retrieval for a given PR is then a scoped search: text query built from the diff's touched modules and change summary, filtered to the run namespace with `topics` in the PR's touched-module set, and a `limit` tuned in §9 — not an unfiltered semantic sweep.

**Module routing must use `topics`, not `attributes` — verified live 2026-08-24.** An earlier draft of this spec said to filter with `attributes.module: {in: [...]}`. The service rejects that with `400 unknown filter clause member`, and every retrieval in the first real run failed on it. An attribute clause is a *typed union* — "exactly one of string, number, boolean, or list must be set" — and offers no membership operator at all. The trap is `list`: `{"module": {"list": [a, b]}}` is whole-value equality, not an IN, so it returns **200 with zero items**, which is indistinguishable from "nothing written yet". `topics` supports `eq`/`ne`/`in`/`all`, accepts raw file paths verbatim (slashes and dots included), round-trips them in search results, and accepted a 300-entry `in` list — comfortably above the 78-file worst case in the frozen sequence. Two related facts from the same probe: **a search response carries no `attributes`** (only `GET` does) and no relevance score, so anything computed from retrieved memories must read `topics` or the id; and **an unknown top-level filter key is silently ignored**, so a typo'd clause returns 200 and an unfiltered result set.

**Use `filterOp: all`, not `any`.** The conjunction is global: `any` ORs the namespace clause with the module clause, so a memory from *another run* that happens to touch the same module satisfies the filter and comes back. That is cross-run contamination which looks exactly like working retrieval. The module set is OR-ed *within* the single `topics` clause instead, which keeps the namespace isolation strict. Relatedly, isolate with `eq`/`in` and never `ne`: the positive operators require the field to be present, while `ne` also matches records that have no namespace at all.

**A model-generated routing key must be validated against a closed set.** The write phase asks Claude to distill findings into records, and one field of that record is the `module` the fact belongs to — which is the retrieval key, matched by exact string equality. Free text from a model is not a path. Measured on the first real run: of six written findings, **five** named a directory (`redis/commands/search`, `tests`, `repo`) and one named a file. All six wrote successfully, and five of them could never be retrieved by any scoped search — written, billed, and dead, with nothing in the harness to say so, because the write returned 200.

Three rules follow, all now enforced in `agents.write()` and `memory.resolve_modules()`:

- **Resolve, do not trust.** A candidate is matched against the PR's touched files: exact path, then unique basename, then directory prefix. A directory resolves to *every* touched file beneath it — a fact about `redis/commands/search` genuinely concerns each touched file there, and a later PR touching any of them should see it. This converts the commonest failure into correct routing rather than a dropped record.
- **Never guess between candidates.** A bare `connection.py` when both `redis/connection.py` and `redis/asyncio/connection.py` were touched resolves to *nothing*. Routing a memory to the wrong module is worse than not routing it, because a wrong module surfaces the fact in reviews where it does not apply.
- **Report what will not resolve.** An unroutable record is dropped *and* logged to the ledger as lost recall. Silence here would show up as "memory did not help on PR 9" — a quality result attributed to the design rather than to a bug.

Audit it from outside the harness with `python3 tools/inspect_namespace.py <namespace>`, which re-derives the touched-file set from the frozen sequence and checks every written record against it. Note that a single unfiltered search caps at 100 records, so it filters by `memoryType` and follows `pageToken` — the first version of that check missed the findings entirely behind ~100 primed conventions and reported clean.

### 4e. The repo-knowledge primer: making repo understanding a one-time cost

Before a reviewer can judge a diff it has to know what the repo *is* — architecture, module boundaries, conventions, what `connection.py` is responsible for and how it relates to `cluster.py`. The baseline re-derives that on **every** PR: it re-reads the style guide, re-reads the modules the diff touches, re-infers the conventions. That re-derivation is the single largest recurring line item in the token bill, and it is almost entirely redundant — the answer barely changes between PR #3 and PR #17.

**The primer turns that recurring cost into a one-time cost per repo.** It is a distinct phase, run once against the frozen repo state before the sequence starts:

1. Read the style guide / CONTRIBUTING doc and the spine modules (`connection.py`, `cluster.py`, `asyncio/cluster.py`, `commands/core.py`).
2. Distill them into `repo_convention` memories — architecture notes, module responsibilities, conventions, invariants — one record per durable fact, each carrying its module path as a **topic** (and in `attributes.module` for provenance) so per-PR retrieval can pull just the relevant slice.
3. Write them with client-supplied ids, so re-priming the same repo is idempotent and a re-run reproduces the same record set exactly.

Per-PR, the memory agent then retrieves the slice for the touched modules instead of re-reading source. Repo understanding is paid for once and read cheaply thereafter; the baseline keeps paying full price per PR. **That gap is the thing the demo measures.**

Three disciplines keep this honest:

- **The primer's tokens are the memory agent's tokens.** Log them under `phase: prime` and include them in the cumulative total. On the chart they are a visible upfront step, which *strengthens* Act 2 — the investment becomes a spike you can point at rather than a hand-wave.
- **The baseline must never see primed output.** It re-derives per PR. That is the control condition, not a handicap.
- **The primer creates a staleness surface.** Primed conventions are exactly what the style-guide PR invalidates, so §9's invalidation requirement now has real stakes: a primer whose conventions can't be updated is a reviewer working from a dead rulebook.

Report the primer's amortization directly: primer cost ÷ number of PRs, alongside the per-PR saving. That single ratio is the "is this worth it" number a skeptical audience actually wants, and it makes the break-even PR count explicit rather than something they have to infer from a curve.

### 4f. Claude API configuration (identical across both agents)

The reviewer itself runs on the **Claude API** — that is what makes the token accounting first-party and exact: every call returns a `usage` object we read directly rather than estimating (§7).

- **Model: `claude-opus-5`** ($5 / $25 per MTok input / output, 1M context). Code review is the workload it is strongest on, and its prompt-cache minimum is 512 tokens — half Opus 4.8's — so more of the memory payloads are cacheable at all.
- **Thinking is on by default on `claude-opus-5`.** Omitting the parameter runs adaptive thinking; it is not off-by-default as on Opus 4.8. Thinking tokens are billed and counted, and `max_tokens` caps thinking *plus* response text together — size it with headroom or reviews truncate mid-finding.
- **Effort:** start at `xhigh` (the recommended setting for coding and agentic work), then sweep down — `low` and `medium` are unusually strong on this model. Whatever is chosen, it must be **identical for both agents**, and stated on the methodology slide.
- **Structured outputs, not prefill.** Get review findings as JSON via `output_config.format` with a JSON schema. Assistant-turn prefill returns a 400 on this model, and structured findings make the gold-set comparison (§7c) mechanical instead of a parsing exercise.
- **Count tokens with `messages.count_tokens`, never a third-party tokenizer.** `tiktoken` is OpenAI's and undercounts Claude by ~15–20% on prose and considerably more on code — which is exactly what a diff is.

Anything that differs between the two agents beyond the memory layer is a confound. That explicitly includes model id, effort, thinking configuration, `max_tokens`, the tool set, and the system prompt.

## 5. The "prompt caching" rebuttal (now with a number)

A skeptic will say memory is just prompt caching. The conceptual answer is that prompt caching is **verbatim and ephemeral** while memory is **distilled, persistent, and semantically retrieved** — but the decisive answer is arithmetic:

**Prompt caching's maximum TTL is one hour.** The default is five minutes; the longest available is 1h, and it costs a 2x write premium to get it. No real PR cadence stays inside that window — PRs on redis-py arrive hours or days apart. So on PR #7, a caching-only reviewer has a cold cache and pays full price to re-read the style guide and the spine modules, exactly as it did on PR #1. Caching cannot make repo understanding a one-time cost per repo, because its unit of persistence is an hour, not a repo.

Memory's unit of persistence is the repo. That is the whole difference, and §4e is the mechanism.

They remain complementary, and the demo treats them as such: enable caching on **both** agents so the comparison isolates memory's contribution, and log `cache_creation_input_tokens`, `cache_read_input_tokens`, and `input_tokens` separately for both so "the memory agent just got luckier with the cache" is answerable with data rather than assertion.

Two caching mechanics that will bite this harness specifically:

- **Caches are model-scoped and prefix-matched.** Any byte change in the prefix invalidates everything after it, and render order is `tools` → `system` → `messages`. Keep the system prompt frozen and the tool list deterministically ordered; inject per-PR content *after* the last cache breakpoint. A timestamp or a PR id interpolated into the system prompt silently destroys caching for both agents — and if it lands in only one, it destroys the comparison.
- **Concurrent requests cannot share a cache entry.** A cache becomes readable only once the first response starts streaming, so running the two agents (or several PRs) in parallel means each pays full price where a sequential run would have hit. Run the sequence sequentially, or accept and disclose that parallel runs inflate both agents' costs.

Related but out of scope: Iris also includes **LangCache** (semantic response caching). Naming it on the slide as a separate, additive lever prevents the audience from thinking we are double-counting mechanisms in one chart.

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

**The sequence is now curated and frozen** (`python3 -m reviewbot curate`), from a scan of 127 merged PRs:

| Step | Count |
|---|---|
| Merged PRs scanned | 127 |
| Touching the connection/cluster spine | 58 |
| Median diff size of that pool | 285 changes |
| At or above the median | 29 |
| Selected (chronological, trimmed) | 18 |

Frozen at `7021617890d4` — the base commit of the first PR, so the primer reads the repo as it stood before the sequence began.

**Module recurrence is 100% both before and after trimming.** The trim step prefers PRs that revisit an already-seen module, which would normally make the selected figure a curatorial artefact — but the untrimmed above-median pool recurs just as much, so recurrence here is a property of the real repo rather than of the selection. Quote both numbers; the second is the one that answers "did you rig it?".

**All three beats are now assigned from the merged human review**, with the evidence trail in [sequence-beats.md](sequence-beats.md). The load-bearing find: the false-positive trap did not have to be manufactured. redis-py's history contains maintainers rejecting automated-review findings with reasons — at ordinal 16, *"Not valid — the revert is covered via disconnect"*, on the same file and the same class as a **real** defect at ordinal 15. So the trap tests something sharper than "does the agent repeat itself": the memory agent must remember that one instance of a class was real and another was ruled invalid.

The convention-change PR had to be spliced in (#4030 edits `CONTRIBUTING.md` and creates `specs/redis_commands_guide.md`, and the style guide is not a spine module, so the selection rule could never pick it). The splice is disclosed in the entry note. It bites because ordinals 8–10 all add or remove command APIs *after* that date, while the primer reads the frozen SHA and never sees the new guide.

The sequence must deliberately include:
- **Repeated modules** — several PRs touching the same files, so semantic memory gets reused. Use the connection/cluster spine.
- **A recurring bug pattern** — the same class of issue (e.g., an unchecked error path or a missing async cleanup) appearing in 2–3 PRs, so episodic recall fires.
- **A false-positive trap** (the key story beat, see §8) — an intentional pattern the baseline keeps re-flagging.
- **A convention change** — one PR that edits the style guide or CONTRIBUTING doc, to test memory *invalidation* (see §9).

Freeze the sequence and commit SHAs so runs are reproducible. Record, per PR: number and identity of touched modules, whether it is a recurrence of an earlier module, diff size, and which beat (if any) it serves. That table is what makes the recurrence-rate disclosure in §9 concrete.

**Ingestion note:** PR ingestion needs an authenticated `GITHUB_TOKEN`. Pulling 15–25 PRs with their files and review comments exhausts the unauthenticated rate limit quickly. Cache ingested PR data to disk so reruns don't re-hit the API — this also keeps the frozen sequence genuinely frozen.

## 7. Metrics & instrumentation

Every Claude API response carries a `usage` object; read it directly rather than estimating. Four fields matter, and the first one is a trap:

| Field | What it is | Priced at |
|---|---|---|
| `input_tokens` | **The uncached remainder only** — not the prompt size | 1x |
| `cache_creation_input_tokens` | Prompt tokens written to cache this call | 1.25x (5m TTL) / 2x (1h) |
| `cache_read_input_tokens` | Prompt tokens served from cache this call | ~0.1x |
| `output_tokens` | Generated tokens, including billed thinking tokens | 1x |

**Total prompt size = `input_tokens` + `cache_creation_input_tokens` + `cache_read_input_tokens`.** Reporting "input tokens per review" as `input_tokens` alone would be wrong in a way that flatters whichever agent happens to cache better — with caching enabled it measures cache misses, not context volume. So the spec defines two headline series and reports both:

- **Context volume** — the full prompt size per review (the sum above). This is what "memory means the agent reads less" actually claims, and it is caching-independent.
- **Billed cost** — the same tokens weighted by their price multipliers. This is what the invoice says.

Primary:
- **Context volume per review**, and **billed cost per review**.
- **Cumulative billed cost** across the sequence — the headline chart.

Secondary:
- Output tokens per review, with thinking tokens called out (they are billed and non-trivial at `xhigh` effort).
- **Memory overhead** logged separately: primer tokens (`phase: prime`), write tokens, retrieval tokens. Savings are reported *net* of all three.
- Wall-clock latency per review — with the §9 caveat that the memory agent makes network calls the baseline does not.
- **Primer amortization**: primer cost ÷ PR count, and the break-even PR number.

Quality (mandatory guardrail — report even if unflattering):
- Precision / recall of review comments against the hand-labeled gold subset (§7c).
- **False-positive rate** — expected to *drop* for the memory agent thanks to decision memory.
- Human-preference or acceptance rate of comments, if a reviewer can rate them blind.

Instrument by tagging every model call with `{agent, pr_id, phase: prime|retrieve|review|write}` so every token is attributable. An untagged call is an unmeasurable one.

### 7a. The token-accounting boundary (the credibility crux)

The memory service can do work with **its own LLM**, server-side: automatic extraction of long-term memories from session events, and automatic session summarization. Those tokens are real, they are part of memory's true cost, and they are **not** returned in our client-side usage accounting. The product docs frame automatic extraction as requiring "no LLM token usage from your application" — which is true and also exactly the sentence a skeptical audience will pounce on, because *application* is not *system*.

If the demo runs automatic extraction and reports only client-side tokens, the headline chart is wrong in memory's favour.

**Resolved: explicit client-side writes.** The review agent decides what to remember and persists it with a direct long-term-memory create call, using a client-supplied `id` for idempotence. Automatic extraction stays off. Every write token is an agent token we emit and count, so the net-savings claim needs no asterisk. This costs nothing narratively — direct memory creation is a documented first-class path, and "the reviewer decides what to remember" is a *better* story for a review agent than opaque background extraction.

Say this on the methodology slide in one sentence, and preempt the obvious follow-up ("why not use the automatic extraction?") with the real reason: it would make the central measurement unverifiable. That answer strengthens the demo rather than weakening it.

Two fallbacks, documented in case the decision is revisited:
- *Automatic extraction with service-side cost measured* — viable only if service-side LLM usage is observable per store or per call (session `summary.metadata` is documented to carry model and token count, which is a start, but extraction needs an equivalent). Confirm the observability exists end-to-end before relying on it (§12).
- *Automatic extraction with cost disclosed as unmeasured* — acceptable only with an explicit slide excluding service-side tokens and bounding them by estimate (tokens in x the extraction model's rate). Weakest option; use only if forced. Also log the **embedding** calls for writes and searches — small per call, but they are part of memory's cost and a careful viewer will ask.

### 7b. Retrieval cost is a measured quantity, not an assumption

Log, per review: **which** memories were retrieved (their ids, not merely a count), their total token count as injected into the prompt, the `limit` and `similarityThreshold` used, and how many retrieved memories the model actually referenced in its output. The last one is the retrieval-precision signal — retrieving 40 memories to use 3 is the failure mode that silently eats the savings (§9). The ids matter for a reason only visible once the store grows: a count cannot say whether a saturated window is filling with semantic conventions or episodic findings, which is the measurement §7f turns out to need.

### 7c. Quality labeling (resolved: hybrid)

Sparse human review in redis-py (§6) rules out a proxy-only gold standard, and hand-labeling 25 PRs is the most expensive line in the build plan. So:

- **Proxy across the whole sequence.** Score each agent's comments for agreement with the merged PR's actual human review comments. Cheap, already in the ingested data, and gives full-sequence coverage of precision-like signal.
- **Hand-labeled gold set on a 5–8 PR subset**, chosen to include *every* beat PR: the recurring-bug-pattern PRs, the false-positive trap and its subsequent touches, and the convention-change PR. These are the PRs the narrative rests on and the only place false-positive rate can be stated rigorously.

**The current labels are `CANDIDATE`, and two independence caveats have to travel with them until a human confirms them:**

- **Same-family bias.** They were written by Claude, and the reviewer under evaluation is Claude. A label set produced by the model being measured is not an independent standard, however well grounded in maintainer quotes.
- **Partial dependence on the proxy.** The `defect` labels are derived from merged human comments — which is what the proxy scores against — so for those items the two metrics are not independent and must not be presented as corroborating each other. The `must_not_flag` items are free of this: nothing in the proxy rewards *not* commenting, which is why false-positive rate is the more informative half of the quality table.

Report the two separately and never average them into one number. State the subset size and how it was chosen — a skeptical viewer will (correctly) weight the hand-labeled subset far more heavily, and the beat PRs are exactly the ones they'll want labeled.

Because 7 of 20 sampled PRs had zero inline human comments, the proxy has a known blind spot: it cannot distinguish "the agent said nothing useful" from "the humans said nothing either." Note that limitation where the proxy is reported.

**What the frozen gold set actually contains, and what that permits.** 6 labelled defects and 3 false-positive traps across 7 PRs; 4 of the 7 carry no labelled defect (they exist for the trap and convention-change beats). So **recall is computed over 3 PRs and 6 defects** — one finding moves it by roughly 17 points. Two consequences for how the quality panel must be read and presented:

- **Report it as a guardrail, not a metric.** The question this set can answer is "did review quality collapse while tokens fell?", which is the question §7 asks. It cannot support a claim that one agent's recall beats the other's by a few points, and presenting it that way invites exactly the objection the panel exists to pre-empt.
- **Precision and false-positive rate are firmer than recall.** Every finding either matches a label or does not, so both agents' denominators are their own output, not the label count. The trap count (`traps_flagged / traps_total`) is the sharpest number in the panel: it is a direct, countable behaviour with a known correct answer of zero.

Widening the gold set is the single highest-value manual task left on this project, and it needs a human who is not the model under test.

### 7d. Compressed replay makes the baseline look cheaper than it is

The sequence replays 15-25 PRs in minutes. Real PRs arrive hours or days apart. With prompt caching enabled, that compression hands the **baseline** a cache-hit rate it would never see in production: its style-guide-and-spine prefix stays warm across consecutive PRs inside the 5-minute (or 1-hour) TTL, so it re-reads the same context at ~0.1x instead of 1x.

This cuts *against* the thesis, which is why it belongs in the spec rather than in a footnote. A compressed replay **understates** memory's real-world advantage, and a reviewer who spots it will (correctly) discount the result unless we got there first.

Report both regimes from the same run:

- **As-measured** — actual billed cost, caches warm. The conservative number; memory still has to win here.
- **Production-equivalent** — the same context volume repriced with the baseline's cross-PR cache reads charged at full rate, on the grounds that a real PR cadence exceeds the maximum TTL. State the repricing rule explicitly so it can be checked.

The honest headline is the as-measured chart, with production-equivalent shown alongside as the bound that a real cadence would produce. If memory wins on as-measured, the demo is safe; the second series shows how much bigger the real gap is.

### 7e. Both agents share one cache entry, so run order matters

The two agents send a byte-identical cacheable prefix — same system prompt, same conventions block — and that identity is exactly what makes the comparison clean (§4f). But it also means they share a single prompt-cache entry: whichever agent runs second on a given PR reads a prefix the other one paid to write, and as-measured that is a discount it did not earn.

Two mitigations, both in the harness:

- **Run the memory agent first on every PR.** The free ride then falls to the baseline, so the bias runs *against* the thesis. A skeptical audience will accept a conservative bias; it will not accept a convenient one.
- **Quantify it.** The run report includes cache-read tokens each agent received on a prefix it never wrote, and the production-equivalent series prices them out entirely, since its cache provenance is tracked per agent.

Runs are also strictly **sequential** — concurrent requests cannot share a cache entry, so a parallel run would report cache misses as context volume. This is enforced by a lockfile (`runs/<id>/run.lock`), not by discipline: four processes once executed one sequence concurrently, primed four times over, and interleaved rows in the append-only ledger under duplicate `seq` values before it was noticed.

**Measured 2026-08-24: in this run, the free ride never happens, because the cache expires first.** A single `xhigh` review of a 200k-token prompt takes six to eight minutes wall-clock, so consecutive calls on the same PR are **6.2 minutes apart** against a **5-minute** cache TTL. Both agents therefore *write* the shared 7,066-token prefix and neither reads it: measured cross-agent free-riding is zero, and `cache_integrity()` reports "wrote N cache tokens and read none" for both.

Three consequences, all worth stating plainly rather than tidying away:

1. **The mitigations above are correct but inert here.** Running memory first still aims the bias against the thesis; there is simply no bias to aim.
2. **The as-measured and production-equivalent series converge**, because §7d repricing only bites on cross-PR cache *reads* and there are none. That removes the single biggest objection to a compressed replay — the baseline is not getting unrealistic cache hits, because nothing is getting cache hits.
3. **It is direct evidence for the argument in §3.** The claim there is that prompt caching cannot substitute for memory because real PR cadence exceeds the maximum TTL. The stronger version, measured: caching does not survive even a *back-to-back* replay of two reviews of the same PR. The 1-hour TTL would cover this gap, at a 2x write premium — but a run that has to buy the long TTL to keep a prefix warm between two consecutive calls is not evidence that caching solves the per-PR cost problem.

### 7f. The retrieval window is fixed while the store grows — measured, and why compaction is not the fix yet

Sequential PRs mean the store only ever gets bigger. Nothing in the harness merges, supersedes, or expires a memory: every write is an append with a fresh deterministic id. Measured over the 19-PR sequence:

| | records |
|---|---|
| after the primer | 102 `repo_convention` |
| after 19 PRs | 198 (96 `review_finding` appended, 0 merged, 0 superseded) |

The obvious worry is that per-PR retrieval cost therefore grows with sequence position, eating the saving that §7d and §7e work so hard to price honestly. **It does not, and the reason matters more than the reassurance.** Retrieval ran with `limit: 20` and returned *exactly* 20 records on every PR from the first one onward. Injected memory tokens are consequently flat at ~3,705 per PR — **70,397 tokens across the sequence, 3.7% of the memory agent's total input.** Store growth is absorbed by truncation, not by the bill.

So the cost case for compacting memories is absent: there is nothing to save on a line item that is both 3.7% and constant by construction. The *retrieval* case is the opposite, and it is the one worth stating:

- **A saturated window measures the limit, not relevance.** Whatever ranked 21st is invisible, and the managed API returns **no relevance score** (§4d) and offers no recency weighting — so we cannot even ask whether slot 20 was worth having.
- **Competition for those slots roughly doubled** across the sequence (102 → 198 candidate records) while the window stayed at 20.
- **Retrieval precision is 19%** — 74 of 380 retrieved memories were ever cited, per-PR between 0.05 and 0.35, with no upward trend. The accumulating records are not earning their slots. This is precisely the failure mode §7b names: retrieving many to use few.

**The one measurement the first run could not make.** `log_op` recorded `returned: <count>` and not *which* records came back, so the question that decides whether compaction is worth building — are the 102 primed conventions crowding out the 96 episodic findings? — was unanswerable from the ledger. Conventions are semantically closer to the retrieval query (`"{title}. Files changed: {modules}"`), which makes crowding-out plausible but unproven. The harness now logs retrieved ids and `analysis.retrieval_mix()` attributes each window by memory type; run-1's rows predate this and report `instrumented: false` rather than an empty mix, because an empty mix would read as "retrieval returned nothing", which is the opposite of what happened.

**Where compaction does belong, in two distinct roles.** They are worth separating because only one of them is about tokens:

1. **Dedup-on-write — built, behind `--dedupe-writes`. A correctness fix, not an efficiency one.** An append-only store is what turns a wrong belief into a growing one. The clearest instance in run-1: a false claim that redis-py's PEP 604 unions need `from __future__ import annotations` (untrue — `requires-python = ">=3.10"`) appeared **11 times across 9 PRs** for the memory agent versus **3 across 3** for the baseline. The loop is mechanical: the claim sits in the retrieval window → the model restates it → the write phase persists it again under a new id → it now has more copies competing for the window. Merging into the existing record via the guarded `PATCH .../fields` breaks the loop, and costs one non-billable GET per finding. The implementation makes the finding id a pure function of (module, topic) rather than (module, topic, ordinal), which is what turns the id itself into the dedup key — no similarity search and no fuzzy matching. It records `occurrences` and `last_pr_ordinal` while leaving `pr_ordinal` at its first-seen value, so recurrence becomes an explicit, measurable attribute instead of an implicit copy count — and that count is the raw material `review_policy` needs. **What it does not do is make a false claim less likely to be restated**; it only stops the copies accumulating. Suppressing a refuted claim is `review_policy`'s job, still unbuilt. This is the same gap `review_policy` (§4d) and memory invalidation (§6) address from the other side.
2. **Consolidation, later — a slot-density play that must be measured.** Distilling N findings on a module into one raises information per slot, which is the actual scarce resource once the window saturates. Chronology already rides in `attributes.pr_ordinal` (§4d), so "the last N findings on this module" is expressible today. But a distillation pass spends **output** tokens, and output is the expensive half of the bill (§7g) — so it has to be measured against the saving, not assumed. At 198 records it will not pay for itself; at a few hundred PRs it should.

**Cheapest experiment, and it needs no compaction at all:** split the single pooled search into one search per memory type, each with its own budget (`--retrieval-split conv=10,find=10`), so a growing pile of findings cannot squeeze conventions out or vice versa. Two round trips, zero model tokens — searches are `billable=False`, so the knob changes the retrieval mix without touching the cost comparison. Pooled remains the default so run-1 stays reproducible.

### 7g. Halving the input does not halve the bill — the output side, decomposed

Measured on run-1, and worth stating before anyone quotes a headline: the memory agent used **51% less context** and cost **21% less**. Both numbers are right, and the gap between them is arithmetic, not an accounting error.

| | context (input) | output | input $ | output $ | total |
|---|---|---|---|---|---|
| baseline | 3,890,269 | 535,048 | $19.29 (59%) | $13.38 (41%) | **$32.67** |
| memory | 1,890,035 | 661,822 | $9.21 (36%) | $16.55 (64%) | **$25.76** |
| delta | −51% | **+24%** | −$10.08 | **+$3.17** | −$6.91 (21%) |

Two multiplications get from 51% to 21%:

1. **The 51% applies to only 59% of the bill.** Output is priced 5x input ($25 vs $5 per MTok). 0.51 × 0.59 = 30% — the ceiling *before* anything else happens.
2. **The memory agent then gives $3.17 back on output**, taking 30% → 21%.

Serialized findings account for only ~13.2k and ~16.8k of those output totals, so **~97% of output is thinking tokens**: the memory agent is not writing longer reviews, it is reasoning more per review (median 27,600 vs 25,564). The plausible mechanism is the same one §7f describes — retrieved memories hand the model more leads to chase, and at 9% gold precision a large share of those leads are false. Memory bought its input saving partly by spending output on false hypotheses.

Three consequences for how this is reported and where optimization effort goes:

- **Report both series, as §7a already requires.** "51% less context" and "21% cheaper" are answers to different questions; quoting the first alone invites exactly the objection a skeptical audience should raise.
- **Effort is the dominant cost knob in the whole experiment**, not retrieval. Because memory's cost is output-weighted, cutting output cuts *more* from memory: halving both agents' output takes the net saving from 21% to ~33%. Sweeping effort down (§4f) is therefore both a cost and a sensitivity experiment, and it stays confound-free as long as the rung is identical for both agents.
- **The design's absolute ceiling is 59%** — memory input at literally zero, output at parity. Any promise above that is a promise the price ratio cannot keep.

## 8. Demo narrative (what the viewer sees)

Three acts.

**Act 1 — The tax.** Run the baseline over the sequence. Show its cost-per-PR line staying flat and high; each PR re-reads the style guide and the same modules. Point at the repeated context in the logs.

**Act 2 — The investment.** Run the memory agent. Show the primer (§4e) as an upfront bar *before* PR #1 — this is the one-time cost of understanding the repo, paid once. Then PR #1–#2 still cost more than baseline (memory writes, cold cache). Don't hide either; name them together as the investment. Then watch per-PR cost fall as primed conventions get retrieved instead of re-derived and recalled patterns start firing. Put the break-even PR number on the slide.

**Act 3 — The false-positive loop (the money beat).** Take the intentional pattern. Baseline flags it → a human explains it's deliberate → baseline re-reviews on the next touch and **flags it again**, burning tokens on a settled question every time. The memory agent stores the human's correction once (as a `review_policy` memory with `source: human-correction`) and never re-flags it. This is the clean demonstration that memory saves tokens *and* improves quality simultaneously, and that the savings compound across the multi-turn human↔agent loop, not just within a single review.

Because the correction is a stored record with a stable id, this act has a screenshot that lands: the retrieved `review_policy` memory shown next to the review that *didn't* fire.

Close on the **cumulative cost chart**: baseline as a steep straight line, memory agent starting higher then bending toward a floor, with the crossover point and the widening gap annotated.

### 8a. The narrative surface (resolved: web page, Redis-branded)

**Surface: a web page that replays the frozen run.** Not a live run — the sequence and its commit SHAs are already frozen (§6), so replaying recorded output is honest provided the page says so, and it avoids the failure modes §9 already flags (preview-stage API, minutes-long turns at `xhigh`, rate limits). A demo that breaks live is worse than one that claims less.

The page is the credibility artifact, not decoration. This audience's real question is "show me PR #7's breakdown," and the whole accounting apparatus in §7 exists so that question has an answer. **Every number on the page must be drillable to the per-call log that produced it.**

**Styling: Redis brand tokens, supplied.** The token slots the page needs are listed at the end of this section — I will not invent Redis hex values, and the categorical order must be validated rather than chosen by eye (see the gate below).

#### Forms — chosen by the data's job, before any color

| Element | Form | Why this form |
|---|---|---|
| The headline number | **Hero figure** — cumulative net saving over the sequence, ≥48px. **Exactly one per view.** | A single value the view leads with; not a one-bar chart |
| Supporting numbers | **KPI row of stat tiles**: break-even PR number · primer cost and its per-PR amortization · context-volume reduction · false-positive-rate delta | A handful of headline numbers is a stat row, never a grouped bar chart |
| Cumulative cost | **Line chart**, two series (baseline, memory), crossover point annotated | Trend over time where telling two distinct series apart *is* the job |
| Per-PR composition | **Stacked bar** per PR — faceted two ways behind a toggle: by phase (`prime` / `retrieve` / `review` / `write`) and by cache tier (`input` / `cache_creation` / `cache_read`) | Part-to-whole. Four phase segments is within the safe band for stacks but makes direct labels mandatory |
| Quality | **A separate chart** — precision / recall / false-positive rate | See the hard rule below |
| The full accounting | **A table**, always reachable | With this audience the table is not an accessibility afterthought, it is the evidence |

**Hard rule: never put cost and quality on one chart.** A dual-axis chart (two y-scales) is the single most common charting mistake, and here it would also be a credibility own-goal — the one place a skeptic is most likely to suspect a rigged visual is the place where the flattering series shares an axis with an unrelated scale. Tokens and precision are different measures; they get separate charts, side by side.

**The §7d dual regime is a toggle, not a second axis.** As-measured and production-equivalent are two states of the same chart on the same scale. Default to as-measured — the conservative number memory has to win on — with production-equivalent as an explicit, labeled switch.

#### The three-act reveal

The acts are **progressive disclosure over one cumulative chart**, not three separate charts and not an animation that withholds data:

1. **Act 1** — baseline series only. Flat and high.
2. **Act 2** — memory series draws in, primer shown as the upfront step before PR #1. Crossover annotation appears with the break-even PR number.
3. **Act 3** — the false-positive beat: the stored `review_policy` record shown beside the review that didn't fire, with the baseline's repeated re-flagging cost called out on the same timeline.

Two constraints on the reveal, both about not looking like a magic trick:

- **A "show everything" control and the table view are reachable at any point.** A skeptic who wants to skip the choreography and read the raw numbers must be able to, immediately. A reveal that can only be watched front-to-back reads as showmanship.
- **The reveal never changes the data or the axes** — only what is drawn. Axis ranges are fixed from the full dataset at load, so no series appears to grow because the scale moved under it.

#### Interaction (default, not optional)

An HTML chart is interactive by nature; ship it that way. Crosshair plus tooltip on the cumulative line; per-mark hover on the stacked bars surfacing that PR's four token fields and its phase attribution; hit targets larger than the marks. Filters — PR range, agent, regime — in a single row above the charts, never interleaved. Clicking any PR opens its per-call log: the drill-down is the point.

#### Accessibility and honesty checks

- Legend always present for two or more series, **plus** direct labels — identity is never carried by color alone.
- Dark mode is a **selected** set of steps validated against the dark surface, not an automatic inversion of the light values.
- Texture fill available for the full-CVD, print, and `forced-colors` cases.
- Text stays in ink tokens; a colored mark beside a number carries identity. Values never wear their series color.
- A 2px surface gap between stacked segments; thin marks; recessive grid and axes.
- Label the page as a **recorded replay of the frozen run**, with the run's date and the commit SHAs, in the page chrome — not buried in a footnote.

#### Design system: redis-ui

**Source: [redis-ui](https://redislabsdev.github.io/redis-ui/)** — a Storybook-published React component library on styled-components. Packages: `@redis-ui/components` (v51.2.0) and `@redis-ui/styles` (v21.2.0).

**It has no chart components.** Of 691 stories — 547 components, 98 table stories — the only visualization-adjacent component is `Gauge`. There is no line chart, bar chart, legend, or axis primitive. So the split is:

- **redis-ui supplies the shell**: layout, typography, the `Table` (98 stories — the table view in §8a is a real component, not a bespoke one), filter controls, cards, badges, and the theme provider with its light/dark switching (`SwitchableModeThemeProvider`).
- **The charts are built to the visualization method against redis-ui's theme tokens.** Nothing is invented; the tokens below are read from `@redis-ui/styles`.

Type is **Geist / Geist Sans** with **Source Code Pro** for mono — the hero figure uses Geist, never a display or serif face. Spacing comes from the `space000`–`space800` scale. Light and dark share ramp *values*; the theme swaps which *step* each role uses, so dark mode is a selected set of steps rather than an inversion.

#### Corrections after building against the installed packages

Three claims in earlier drafts of this section were wrong. They were made from reading Storybook and are corrected here from the installed packages:

- **There is no `Table` component** in `@redis-ui/components` 51.2.0 — only `TableHeading`, which is a plain styled `div` (`HTMLAttributes<HTMLDivElement>`, no sort props). The accounting view is therefore hand-built on semantic `<table>` markup, exactly like the charts. The library ships 56 components; `Gauge` remains the only visualization-adjacent one.
- **`@redis-ui/styles` peer-depends on styled-components ^5**, not v6. Installing v6 fails resolution outright.
- The rejected `primary` + `notice` pair is **CVD ΔE 1.7 (deutan) / 11.3 normal**, not the 1.0 / 13.3 quoted earlier — `notice400` is `#8b5cf6`, and the earlier figure came from a slightly different hex. The conclusion is unchanged and if anything firmer: it fails both the CVD gate and the normal-vision floor.

Component APIs that had to be read rather than assumed (each was wrong first time, and the DOM smoke test caught each): `Typography` sizes are uppercase unions (`'XXL' | 'XL' | 'L' | 'M' | 'S' | 'XS'`) and render a `div`, so real headings need `as="h1"`; `Banner` takes `message` plus a variant from `informative | notice | danger | attention | success` (**no `warning`**); `Switch` uses `onCheckedChange`; `Tabs` is not a Radix-style `Root/List/Trigger` set.

#### Validated color assignments

Redis's chromatic ramps are **semantically named** — `success`, `danger`, `attention`, `notice`, `informative` are status families. Only `primary` (brand blue) and `discovery` (magenta) are status-neutral. That constraint drives the assignments below, and every pair was validated rather than chosen by eye. Surfaces used: `#ffffff` light, `secondary950 #091a23` dark.

**Cumulative line — two series.** Two options, both passing every gate in both modes:

| Option | Baseline | Memory agent | CVD ΔE | Normal ΔE |
|---|---|---|---|---|
| Emphasis *(rejected: fails the dark-mode lightness band)* | `neutral700 #6d6e71` light / `neutral500 #a7a9ac` dark | `primary400 #0070f3` | 20.9 / 23.3 | 21.3 / 26.4 |
| Two chromatic *(chosen)* | `discovery400 #D90B78` | `primary400 #0070f3` | 20.5 | 34.2 |

Emphasis is recommended: the baseline is context and the memory agent is the intervention, so brand blue on gray reads correctly. It is also the conservative choice — graying the *high-cost* series understates the win rather than dramatizing it. The baseline stays fully drawn, labeled, and present in the table; nothing is hidden.

**Phase stack — an ordinal ramp, not four categorical hues.** `prime → retrieve → review → write` is an ordered pipeline, so a single-hue ramp is the right form *and* it sidesteps the status-name collision (a `danger`-red "write" segment would read as an error). Validated steps from the `primary` ramp:

| Mode | Steps (light → dark) | Min adjacent ΔL | Lightest vs surface |
|---|---|---|---|
| Light | `#52a9ff` · `#0091ff` · `#0070f3` · `#064ea2` | 0.068 | 2.48:1 |
| Dark | `#8cc4fc` · `#52a9ff` · `#0091ff` · `#0060d1` | 0.068 | 9.65:1 |

Cache tiers (`input` / `cache_creation` / `cache_read`) are also ordered — by price multiplier — and take the same treatment with three steps.

**Status colors stay reserved.** `success` / `danger` / `attention` / `notice` keep their semantic meaning for the quality-guardrail badges and pass/fail states, and are never reused as series identity.

**Resolved by running the validator, not the port: two chromatic hues.** The emphasis pairing (gray baseline + brand blue) passes in light mode but **fails the dark-mode lightness band** — every redis-ui neutral light enough to read as a series line sits above the band, so there is no neutral step that works in dark mode. `discovery400 #D90B78` + `primary400 #0070f3` passes every check in **both** modes (CVD ΔE 20.5, normal 34.2), so series identity does not change between themes — which matters, because a reader who switches theme mid-talk should not have to re-learn the legend.

The phase stack is graded as an **ordinal ramp**, not a categorical palette (the validator exports `validateOrdinal` for exactly this): running the categorical checks on a correct ramp fails it by design, since a ramp spans the lightness band and its pale steps fall below the chroma floor. The light and dark ramps differ by one step at the pale end — `primary100 #8cc4fc` reads at only 1.79:1 against the light surface, under the 2:1 floor for the palest step, so light mode starts at `primary200`.

`web/scripts/validate-series-colors.mjs` runs all six palettes (series x 2 modes, phase ramp x 2, cache ramp x 2) against the installed `@redis-ui/styles` and exits non-zero on any failure. Re-run it after any version bump; the numbers in this section are only valid for 21.2.0.

#### The pairs we rejected, and why it matters

These are computed values, and several contradict what looks fine on a designer's screen:

| Pair | CVD ΔE | Normal ΔE | Verdict |
|---|---|---|---|
| `primary` + `notice` (blue + violet) | **1.0** | 13.3 | Effectively identical to protan/deutan viewers |
| `primary` + `informative` (two blues) | 8.4 | **9.1** | Fails even for full colour vision |
| `attention` + `danger` | **2.8** | **9.9** | Fails both gates |
| `discovery` + `danger` | 10.1 | **11.3** | Fails the normal-vision floor |
| `success` + `danger` | 5.0 light / 8.6 dark | 34.1 | Mode-dependent — passes dark, fails light |

Blue-and-violet is the trap worth naming: it is an obvious-looking pair that a colorblind viewer in the third row cannot separate at all. In a demo whose entire argument is "these numbers are trustworthy," a chart that some of the audience literally cannot read undercuts the thesis in the room. Threshold reference: CVD ΔE ≥ 8.0 target, normal-vision ΔE ≥ 15.0 hard floor, ≥ 3:1 contrast against surface, OKLCH lightness in band, both modes.

Re-run the validation if redis-ui's ramps change — the package is versioned, and these values are pinned to `@redis-ui/styles` v21.2.0.

## 9. Risks & honest caveats (put these on a slide too — credibility)

- **Cold start:** memory helps nothing on the first PR. The value is an amortized bet on recurrence.
- **Write cost:** persisting memories costs tokens; net savings only appear after reuse. This is why the sequence, not the single PR, is the unit of evaluation.
- **Retrieval bloat:** retrieving too much memory eats the savings. Retrieval precision is a first-class concern — measure tokens pulled in during the retrieve phase (§7b). Concretely: `limit` defaults to 10 and caps at 100, and `similarityThreshold` gates on normalized cosine similarity. Do a small sweep over both and report the setting used; a demo tuned to `limit: 100` would be self-sabotaging.
- **Stale memory:** when the style-guide PR lands, memory must update or the agent reviews against dead conventions. The guarded `PATCH .../fields` call (which rejects the write unless `memoryType` and `namespace` match) plus a bumped `convention_version` attribute is the intended mechanism; verify the *old* convention stops being retrieved, not just that a new record exists. If invalidation isn't built, surface it as a limitation rather than quietly dropping the convention-change PR.
- **Overfitting to recurrence:** a sequence engineered for heavy recurrence flatters memory. State the recurrence rate you chose and argue it's representative; ideally show sensitivity to it.
- **Asynchronous extraction / eventual consistency.** Long-term memory extraction and promotion happen in the background, and the docs warn that recently written or deleted records may briefly not reflect in searches. A sequential PR replay that writes after PR N and searches at PR N+1 can therefore read a store that hasn't settled — which would understate memory's benefit for non-obvious reasons. The harness must explicitly wait for write visibility (poll the search or get-by-id until the record appears) between PRs, and that wait must be excluded from the latency metric.
- **Time compression.** The demo replays 15–25 PRs in minutes, but a long-term memory's `createdAt` is server-assigned at write time and is not client-settable — so real PR dates cannot be backdated onto long-term records, and any day-scale time decay is inert in a compressed run. Carry chronology in `attributes` (`pr_ordinal`, `pr_number`, and the real merge date) and do not design any beat that depends on wall-clock aging. Session event `createdAt` *is* client-supplied, so session-tier chronology can be made faithful if a beat needs it.
- **Latency is not apples-to-apples.** The memory agent makes network calls to a managed service; the baseline reads local files. Report latency, but do not headline it, and say why.
- **Cache-warmth artifact.** Compressed replay gives the baseline unrealistic cache hits; report as-measured and production-equivalent (§7d).
- **Prompt-cache fragility.** A timestamp, PR id, UUID, or non-deterministically serialized tool list anywhere in the prefix silently zeroes caching. Verify `cache_read_input_tokens` is non-zero across repeated calls before trusting any cost number; if it is zero, there is a silent invalidator, not a finding.
- **Thinking tokens are real cost.** `claude-opus-5` thinks by default and those tokens are billed. A `max_tokens` sized for the answer alone will truncate reviews mid-finding. Both agents must run identical thinking and effort settings.
- **Rate limits are model-scoped.** `claude-opus-5` draws on a separate bucket from the Opus 4.x pool; confirm the tier's limits before running the full sequence twice back to back.
- **Preview-stage API.** Redis Agent Memory is in preview; features and behavior may change. Pin SDK versions, record the API surface used, and re-verify before showing the demo. A demo that breaks live is worse than a demo that claims less.

## 10. Build plan

1. **Harness** — PR ingestion, the shared review loop, and the token/quality accounting layer with per-call tagging (§7/§7a). Highest priority; both agents depend on it, and the accounting layer is what makes the result defensible.
2. **Store provisioning** — create the Agent Memory service on Redis Cloud, register the three custom memory types (`repo_convention`, `review_finding`, `review_policy`) with their structured fields and extraction instructions, and script per-run namespace reset.
3. **Baseline agent** — fresh-context assembly each PR.
4. **Repo-knowledge primer** — the one-time distillation pass over the frozen redis-py state that writes `repo_convention` memories (§4e). This is the mechanism that converts per-PR repo understanding into a per-repo cost, so build it before the per-PR retrieve loop and measure it on its own.
5. **Memory layer** — retrieve and write phases against the managed API, scoped-search construction, write-visibility waiting. Start with `repo_convention` + `review_finding`; `review_policy` last (it is the Act 3 beat, so it lands late but matters most).
6. **Dataset curation** — select and freeze the 15–25 PR sequence from `redis/redis-py` around the connection/cluster spine, cache the ingested PR data, hand-label the 5–8 PR gold subset, record the per-PR beat/recurrence/diff-size table.
7. **Runs & analysis** — execute both agents, produce the cumulative chart and the quality table.
8. **Narrative surface** — the replay page (§8a) built on redis-ui: hero figure, KPI row, cumulative line with crossover annotation, per-PR stacked breakdown, separate quality chart, a hand-built accounting table for the full detail, and per-PR drill-down. Charts are hand-built against redis-ui theme tokens — the library ships no chart components. Colors are already validated; re-validate if the `@redis-ui/styles` version moves.

Suggested slice for a first end-to-end signal: harness + baseline + primer + `repo_convention`-only memory over a 10-PR sequence centred on one recurring module (`redis/connection.py` is the strongest candidate), using explicit client-side writes (§7a). The primer is in the first slice deliberately — it is the largest single lever, and a slice without it tests the weakest version of the thesis. That alone should show the curve bending and validates the accounting story before investing in the other two memory types.

## 11. Success criteria

The demo succeeds if, over the frozen sequence: cumulative billed tokens for the memory agent finish **meaningfully below** the baseline *net of primer, write, and retrieval overhead*, with the accounting boundary (§7a) and the caching regime (§7d) both stated; the **break-even PR number** is explicit rather than inferred from a curve; review quality is **statistically no worse** on the hand-labeled subset (ideally better on false-positive rate); and a skeptical viewer leaves able to explain both *why* it works and *when it wouldn't* (low-recurrence, high-churn repos), and able to see where every counted token came from.

The one-sentence version a viewer should be able to repeat: **repo understanding is a per-repo cost, not a per-PR cost — and prompt caching can't make it one, because its longest TTL is an hour.**

---

## 12. Open decisions

Resolved:
- ~~**Audience**~~ -> technical/skeptical (§3).
- ~~**Memory substrate**~~ -> Redis Agent Memory in Redis Iris, managed; OSS `V0/` explicitly excluded (§4c).
- ~~**Repo**~~ -> `redis/redis-py`, curated around the connection/cluster spine, drawn from the upper half of the diff-size distribution (§6).
- ~~**Quality labeling**~~ -> hybrid: merged-human-comment proxy across the sequence, plus a hand-labeled gold set on a 5–8 PR subset covering every beat PR (§7c).
- ~~**Extraction mode**~~ -> explicit client-side writes; automatic extraction off (§7a).
- ~~**Presentation surface**~~ -> a Redis-branded web page replaying the frozen run, with drill-down to per-call accounting (§8a).
- ~~**Visual styling**~~ -> redis-ui design system; series colors validated against colorblind and contrast gates rather than chosen by eye (§8a).

Still open — both are internal verification questions, and neither blocks starting the harness:
- **Service-side observability:** is extraction/summarization LLM token usage observable per store or per call? Only gates the §7a fallbacks, not the chosen path.
- **Search modes:** are keyword and hybrid search reachable through the managed API and SDKs today, given the published OpenAPI search body exposes only `text` and `similarityThreshold`?
- ~~**Design tokens**~~ -> resolved: redis-ui (`@redis-ui/components` v51.2.0 / `@redis-ui/styles` v21.2.0); validated color assignments in §8a. Affects how the retrieve phase constructs queries (§4d) — semantic-only is workable, but worth knowing before tuning retrieval precision.
