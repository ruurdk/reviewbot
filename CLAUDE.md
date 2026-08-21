# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository state

[docs/agentic-memory-pr-review-demo-spec.md](docs/agentic-memory-pr-review-demo-spec.md) is the source of truth for what this project is — read it before changing anything. Build order is spec §10; the harness (step 1) exists, steps 2–8 do not.

Built: shared config with the confound guard, token accounting + ledger, Messages API client, the shared review loop, aggregation and the §7d repricing, GitHub ingestion with a frozen on-disk cache, the sequence/beat manifest and its validator, and a CLI — 70 tests. Not built yet: the memory client and store provisioning, the primer, the two agents, the curated sequence itself, the quality labeling, and the replay page.

## Stack: Python 3.12, standard library only

**This machine has no package manager.** No pip, no ensurepip, no venv module, and `files.pythonhosted.org` is blocked by the sandbox — so nothing can be installed, including the `anthropic` and Redis Agent Memory SDKs the spec calls for. There is no node/npm either, which is why the redis-ui page cannot be built here yet.

So the harness is stdlib-only, and every external surface is isolated behind one thin client that a SDK can replace without touching anything above it:

- [reviewbot/claude.py](reviewbot/claude.py) — Messages API over `urllib` (streaming SSE, retries, `count_tokens`). Mirrors `client.messages.create()`; see its `SDK_SWAP` note.
- [reviewbot/github.py](reviewbot/github.py) — GitHub REST over `urllib`, with the disk cache.

Owning the serialisation is not purely a workaround: prompt caching is a byte-exact prefix match, and `claude.prefix_id()` hashes the exact cached prefix, which is what makes the cache assertions in §7d/§5 checkable rather than assumed. Keep that property if you swap in the SDK.

## Commands

```bash
python3 -m unittest discover -s tests -t .           # all tests (no network, no API key)
python3 -m unittest tests.test_analysis -v           # one module
python3 -m unittest tests.test_analysis.TestBreakeven.test_memory_loses_early_and_wins_later
python3 -m reviewbot doctor                          # env + config fingerprint
python3 -m reviewbot ingest --prs 3001,3002          # fetch PRs into data/prs (needs GITHUB_TOKEN)
python3 -m reviewbot dataset validate|table|summary  # check/describe the frozen sequence
python3 -m reviewbot report runs/<run-id>            # headline numbers + summary.json
```

Tests use `unittest` with injected transports — they never touch the network, so they run without `ANTHROPIC_API_KEY` or `GITHUB_TOKEN`. Keep it that way: a test that needs a key is a test nobody runs. There is no linter or formatter available; match the surrounding style by hand.

## Code layout and the invariants each file enforces

The accounting layer is not bookkeeping around the experiment, it *is* the experiment — so the spec's rules are compiled into it rather than left to discipline:

| File | Enforces |
|---|---|
| [reviewbot/config.py](reviewbot/config.py) | One `ModelConfig` for both agents. `fingerprint()` goes in every run manifest and `assert_comparable()` raises `ConfoundError` on a mismatch. Rejects `budget_tokens` and `thinking: disabled` at `xhigh`. Carries pricing and the per-model cache minimum. |
| [reviewbot/accounting.py](reviewbot/accounting.py) | `Usage.context_volume` is the *sum* of the three input fields, so `input_tokens` can't be reported as prompt size. `CallRecord` requires `{agent, pr_id, pr_ordinal, phase}`. Memory ops are `billable=False` with `injected_tokens` as attribution only — adding them to a total double-counts. Ledger is append-only JSONL and resumes `seq`. |
| [reviewbot/claude.py](reviewbot/claude.py) | Tagging is a required argument. `prefix_id()` hashes the prefix up to the last `cache_control` breakpoint; a repeat with zero `cache_read` raises a cache warning. Refusals and `max_tokens` truncation are surfaced, never silently logged as an empty review. |
| [reviewbot/analysis.py](reviewbot/analysis.py) | The §7d repricing rule, stated in code: a cache read whose prefix was written under a *different* PR ordinal is charged at 1.0x, applied to **both** agents. `breakeven_ordinal()` ignores a crossing that reverses. `cache_integrity()` is the pre-flight before trusting any cost number. |
| [reviewbot/review.py](reviewbot/review.py) | **One** frozen `SYSTEM_PROMPT` for both agents — it names the optional "Prior knowledge" section so its bytes don't change when memories are absent. Stable blocks (conventions) precede the breakpoint; per-PR source, memories, and the diff follow it. `Finding`/`FINDINGS_SCHEMA` make gold-set comparison mechanical, and `memories_used` is the retrieval-precision signal. |
| [reviewbot/github.py](reviewbot/github.py) | `GITHUB_TOKEN` required (not optional); every response cached by URL; file reads take a pinned SHA, never a branch. Bot comments flagged so the proxy metric can exclude them. |
| [reviewbot/dataset.py](reviewbot/dataset.py) | `validate()` fails on a missing beat, a beat with no gold label, a gold subset outside 5–8, an unpinned SHA, or an empty selection rule. `disclosure_table()` computes recurrence from the data instead of asserting it. |

`data/sequence.example.json` is the manifest template; it deliberately fails `dataset validate` until real PR numbers and a pinned SHA replace the placeholders.

## What this project is

A **demo/experiment**, not a product: two PR-review agents run over the *same frozen sequence of 15–25 real PRs* under identical models, prompts, and tools. The only difference between them is a memory layer. The deliverable is evidence that agentic memory reduces cumulative input tokens per review without degrading review quality.

- **Baseline agent (control)** — assembles context fresh on every PR (diff + style guide + relevant source), no persistence.
- **Memory agent (treatment)** — a one-time `prime` pass per repo, then the same review loop wrapped in `retrieve` → `review` → `write`.
- **Memory types** — semantic (repo conventions/architecture), episodic (past findings and their resolutions), procedural (calibrated checklist, which suggestion classes this team accepts). Build order: semantic + episodic first, procedural last.

## Memory substrate: Redis Agent Memory in Redis Iris

The demo runs against the **managed** Redis Agent Memory service in Redis Iris (Redis Cloud) — store-scoped REST (`/v1/stores/{storeId}/...`) plus Python/TypeScript SDKs. The service is in **preview**; pin SDK versions and re-verify the API surface before relying on it.

**The `V0/` directory in `redis/agent-memory-server` is explicitly out of scope.** It is the older open-source research implementation and is not the supported path. Do not model the demo on V0's surface — it differs materially from the managed API: V0 has `memory_prompt`, an MCP interface, hybrid/keyword search knobs, query optimization, recency boost, and `entities`; the managed API has none of those. Managed instead offers custom memory types with structured fields and extraction instructions, an arbitrary filterable `attributes` map, and guarded field updates.

The three memory types are modeled as **custom memory types on the store** (`repo_convention`, `review_finding`, `review_policy`), scoped per run by `namespace` and routed by `attributes` (`module`, `pr_ordinal`, `finding_class`, `convention_version`, `source`).

## The core mechanism: repo understanding is a per-repo cost

The largest recurring cost is Claude rebuilding its understanding of the repo before it even looks at the diff. The **primer** (spec §4e) pays that once: one distillation pass over the frozen repo writes `repo_convention` memories tagged by module, and per-PR retrieval pulls only the slice for the touched modules. The baseline re-derives per PR — that gap is what the demo measures.

Primer tokens are the memory agent's tokens: tag them `phase: prime`, include them in the cumulative total, and report primer-cost ÷ PR-count plus the break-even PR number. The baseline must never see primed output.

**Prompt caching cannot substitute for this** — its maximum TTL is one hour (5 min default), and real PR cadence exceeds that. That number is the spec's sharpest answer to "isn't this just prompt caching?"

## Claude API configuration

The reviewer runs on the Claude API — that's what makes token accounting exact rather than estimated. Model `claude-opus-5` ($5/$25 per MTok, 1M context, 512-token prompt-cache minimum). Notes that bite:

- **Thinking is ON by default** on `claude-opus-5` (unlike Opus 4.8), thinking tokens are billed, and `max_tokens` caps thinking + response text together — undersize it and reviews truncate mid-finding.
- **Effort:** start `xhigh` (best for coding/agentic), then sweep down; `low`/`medium` are unusually strong on this model. Must be identical for both agents.
- **Structured outputs (`output_config.format`), never assistant prefill** — prefill returns a 400 on this model.
- **Count tokens with `messages.count_tokens`**, never `tiktoken` (it's OpenAI's; undercounts Claude badly on code).
- Anything differing between the agents beyond the memory layer is a confound: model id, effort, thinking config, `max_tokens`, tools, system prompt.

## Token accounting: `input_tokens` is not the prompt size

`usage.input_tokens` is the **uncached remainder only**. Total prompt = `input_tokens` + `cache_creation_input_tokens` + `cache_read_input_tokens`. Reporting `input_tokens` alone as "input tokens per review" measures cache misses, not context volume, and flatters whichever agent caches better. The spec reports two series: **context volume** (caching-independent) and **billed cost** (price-weighted; cache writes 1.25x at 5m TTL / 2x at 1h, reads ~0.1x).

Two caching traps: a timestamp, PR id, or non-deterministically serialized tool list anywhere in the prefix silently zeroes caching (verify `cache_read_input_tokens` is non-zero before trusting any cost number), and concurrent requests can't share a cache entry — so run the sequence sequentially.

Compressed replay also hands the *baseline* unrealistic cache hits, understating memory's real-world advantage. Report as-measured and production-equivalent (spec §7d).

## Narrative surface

The deliverable is a **web page replaying the frozen run**, not a live run and not a static deck (spec §8a). Built on **redis-ui** (`@redis-ui/components` v51.2.0, `@redis-ui/styles` v21.2.0 — Storybook at redislabsdev.github.io/redis-ui; React + styled-components).

**redis-ui ships no chart components** — 691 stories, and `Gauge` is the only viz-adjacent one. So redis-ui supplies the shell (layout, typography Geist/Source Code Pro, the `Table` for the accounting view, filters, theme provider with light/dark switching) and the charts are hand-built against its theme tokens.

Forms, already decided: one hero figure (cumulative net saving), a KPI row of stat tiles, a cumulative line with the crossover annotated, per-PR stacked bars faceted by phase and by cache tier, and a **separate** quality chart. Never put cost and quality on one chart — a dual-axis chart is both the most common charting error and, here, the place a skeptic would most suspect a rigged visual. The §7d dual regime is a toggle on one scale, not a second axis.

Series colors are **validated, not chosen by eye** (§8a has the numbers). Redis's ramps are semantically named, so only `primary` and `discovery` are status-neutral; the ordered phase stack uses a single-hue `primary` ordinal ramp rather than four categorical hues. Two rejected pairs worth remembering: `primary`+`notice` (blue+violet) has a colorblind ΔE of **1.0**, and `primary`+`informative` fails even for full colour vision. Re-run the validation if the `@redis-ui/styles` version moves.

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
