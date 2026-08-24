# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository state

[docs/agentic-memory-pr-review-demo-spec.md](docs/agentic-memory-pr-review-demo-spec.md) is the source of truth for what this project is — read it before changing anything. Build order is spec §10.

**Built and verified against the live services** (steps 1–7): the shared harness, both agents, the primer, the Agent Memory client, quality scoring, the sequence runner, the frozen 19-PR dataset with 7 gold-labelled PRs, the provisioned store, and the replay page — 167 tests, none of which need network or credentials.

**Not built:** `review_policy` memories, i.e. procedural memory (the spec sequences it last, and nothing depends on it). Memory invalidation for the convention-change beat is also absent — spec §6 says to keep that PR and disclose the gap rather than drop it.

**In flight:** the first full run (`runs/run-1`). Until a real `report.json` exists, `web/` renders a synthetic fixture, and the gold labels are `CANDIDATE` — written by Claude, which is the model family under evaluation.

## Stack: Python 3.12, standard library only

**This machine has no package manager.** No pip, no ensurepip, no venv module, and `files.pythonhosted.org` is blocked by the sandbox — so nothing can be installed, including the `anthropic` and Redis Agent Memory SDKs the spec calls for. (node/npm *are* installed now, which is what unblocked the replay page; Python remains stdlib-only.)

**`api.anthropic.com` is not in the tool sandbox's allowlist.** Any command that reaches the Messages API — `run`, or a probe script — fails with `URLError: Tunnel connection failed: 403 Forbidden` until it is run with `dangerouslyDisableSandbox`. The memory store's host and `github.com` are allowed, so `memcheck`, `ingest` and `curate` work inside the sandbox.

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
python3 -m reviewbot curate --pages 2 --target 18     # scan, select, freeze + ingest the sequence
python3 -m reviewbot memcheck                        # is the memory store provisioned and usable?
python3 -m reviewbot beats --pr 4114 --add recurring_bug --gold   # assign a beat / gold flag
python3 -m reviewbot dataset validate|table|summary  # check/describe the frozen sequence
python3 -m reviewbot run <run-id> --checkout .checkouts/redis-py   # execute the sequence (needs keys)
python3 -m reviewbot report runs/<run-id>            # headline numbers + summary.json
```

`run --checkout <path>` reads source from a local redis-py clone via `git show <sha>:<path>`, so only the PR *metadata* needs a GitHub token, not the file contents. Clone it to `.checkouts/redis-py` (gitignored); `/home/ruurd` outside this repo is read-only here.

**A run is a multi-hour, real-money operation. Three things protect it, all learned the hard way:**

- **`runs/<id>/run.lock` refuses a second process.** Concurrency corrupts a run three ways that do not announce themselves: interleaved ledger rows under duplicate `seq`, a re-paid primer writing the same namespace, and — worst — requests that cannot share a prompt cache entry, so cache misses get recorded as context volume. Four processes once ran one sequence at once because `ps` inside the tool sandbox could not see them; **check for a live run with `dangerouslyDisableSandbox`, or the process list will look empty when it is not**.
- **`runs/<id>/checkpoint.jsonl` + `primed.json` make a rerun resume.** Each PR is appended as it completes and the primer is never re-paid. A PR whose agents did not all finish is redone, not reported half-measured. Resumed PRs are disclosed in the report's warnings.
- **Never re-use a run directory across a config change.** The manifest states one fingerprint for the whole run; rows from an older config would silently sit under it. `CallRecord` now carries `model`/`effort`/`max_tokens`/`cache_ttl` per row so such a mismatch is at least visible, but the fix is a clean run dir and a wiped namespace.

Tests use `unittest` with injected transports — they never touch the network, so they run without any credentials at all. Keep it that way: a test that needs a key is a test nobody runs. There is no linter or formatter available; match the surrounding style by hand.

## Credentials

`cp .env.example .env`, fill it in, then `python3 -m reviewbot doctor`. [.env.example](.env.example) documents every variable, what breaks without it, and where to get it; `.env` is gitignored and a test asserts both facts. [reviewbot/env.py](reviewbot/env.py) loads it at CLI startup and **never overrides an already-set variable**, so an exported shell value or a one-off `KEY=... python3 -m reviewbot ...` wins over the file.

| Variable | Needed by |
|---|---|
| `ANTHROPIC_API_KEY` | `run` |
| `GITHUB_TOKEN` | `ingest` |
| `REDIS_AGENT_MEMORY_URL` / `_API_KEY` / `_STORE_ID` | `run` with the memory agent |
| `REVIEWBOT_NAMESPACE` | optional default for `--namespace` (not a secret) |

`run` checks for the keys its chosen `--agents` actually need and exits with the full list before making any call, rather than failing partway through a sequence. Add any new variable to `env.REQUIRED_BY` — `doctor` and the example-file coverage test both read from it.

**The Iris base URL and auth header are the one inferred part of the stack.** The published Agent Memory OpenAPI declares no `servers` block and no `securitySchemes`, so `memory.py` sends `Authorization: Bearer` against `{URL}/v1/stores/{id}/...` on inference. Verify both against the Iris console before the first real run; `_headers()` and `_url()` are deliberately the only places that know.

## Code layout and the invariants each file enforces

The accounting layer is not bookkeeping around the experiment, it *is* the experiment — so the spec's rules are compiled into it rather than left to discipline:

| File | Enforces |
|---|---|
| [reviewbot/config.py](reviewbot/config.py) | One `ModelConfig` for both agents, and **the only place a default lives** — `cli.py` derives its argparse defaults from `ModelConfig()`, because a literal there silently shadows the dataclass (raising `max_tokens` to 64000 changed nothing, and the run truncated at 32000 anyway). `fingerprint()` goes in every run manifest and `assert_comparable()` raises `ConfoundError` on a mismatch. Rejects `budget_tokens` and `thinking: disabled` at `xhigh`. `max_tokens` is 96000: at `xhigh` the largest PR's review spent 32000 on thinking plus findings and stopped mid-finding, and headroom is nearly free (output bills per token generated, and the ceiling does not itself make the model verbose — effort does). |
| [reviewbot/accounting.py](reviewbot/accounting.py) | `Usage.context_volume` is the *sum* of the three input fields, so `input_tokens` can't be reported as prompt size. `latency_ms` covers draining a streamed body and `ttfb_ms` is the headers-only figure — they differ by *minutes* on a long review, so reporting time-to-first-byte as latency makes every streamed call look instant. **Rows in `runs/run-1` predate this fix and carry TTFB in `latency_ms`; they have no `ttfb_ms` at all, which is how to spot them.** `CallRecord` requires `{agent, pr_id, pr_ordinal, phase}`. Memory ops are `billable=False` with `injected_tokens` as attribution only — adding them to a total double-counts. Ledger is append-only JSONL and resumes `seq`. |
| [reviewbot/claude.py](reviewbot/claude.py) | Tagging is a required argument. `prefix_id()` hashes the prefix up to the last `cache_control` breakpoint; a repeat with zero `cache_read` raises a cache warning. `ClaudeResult.json()` raises `Truncated` — naming the PR, the token count and the knob — for both a `max_tokens` stop and an empty body, because `output_config.format` guarantees valid JSON only for a *completed* response. Reporting either as an empty review would credit an agent with zero findings it never made. |
| [reviewbot/analysis.py](reviewbot/analysis.py) | The §7d repricing rule, stated in code: a cache read whose prefix was written under a *different* PR ordinal is charged at 1.0x, applied to **both** agents. `breakeven_ordinal()` ignores a crossing that reverses. `cache_integrity()` is the pre-flight before trusting any cost number. |
| [reviewbot/review.py](reviewbot/review.py) | **One** frozen `SYSTEM_PROMPT` for both agents — it names the optional "Prior knowledge" section so its bytes don't change when memories are absent. Stable blocks (conventions) precede the breakpoint; per-PR source, memories, and the diff follow it. `Finding`/`FINDINGS_SCHEMA` make gold-set comparison mechanical, and `memories_used` is the retrieval-precision signal. |
| [reviewbot/github.py](reviewbot/github.py) | `GITHUB_TOKEN` required (not optional); every response cached by URL; file reads take a pinned SHA, never a branch. Bot comments flagged so the proxy metric can exclude them. |
| [reviewbot/dataset.py](reviewbot/dataset.py) | `validate()` fails on a missing beat, a beat with no gold label, a gold subset outside 5–8, an unpinned SHA, or an empty selection rule. `disclosure_table()` computes recurrence from the data instead of asserting it. |

| [reviewbot/repo.py](reviewbot/repo.py) | Source reads always take a pinned SHA. `LocalSourceProvider` uses `git show <sha>:<path>` so a dirty working tree cannot silently change the frozen dataset; it records `fell_back_to_worktree` when git is unavailable. Read counts are tracked, so "how much did the baseline read" is measured. |
| [reviewbot/agents.py](reviewbot/agents.py) | **A model-generated `module` is resolved against the PR's touched files before it becomes a topic** (`memory.resolve_modules`): exact path, then unique basename, then every touched file under a named directory. Free text from a model is not a path — on the first real run 5 of 6 written findings named a directory, wrote successfully, and were unretrievable forever. An ambiguous basename resolves to *nothing* rather than guessing, and anything unresolvable is dropped **and logged** as lost recall. The only difference between the agents is `volatile_blocks`. The primer runs one call per module (so re-priming one changed module doesn't re-pay for the rest) at `pr_ordinal=0`, and writes with deterministic ids for idempotence. Write-phase distillation is a measured model call; `distill_writes=False` gives a zero-model-token variant. Chronology rides in `attributes.pr_ordinal` because `createdAt` is server-assigned. |
| [reviewbot/memory.py](reviewbot/memory.py) | Enforces the four Agent Memory constraints that silently corrupt runs: the `^[a-zA-Z0-9-]+$` id/namespace pattern, bulk-create `errors` (a 200 can still mean half the writes failed), `filterOp: all` + `topics: {in: […]}` for module retrieval (attribute clauses have no membership operator), and `eq`-not-`ne` namespace isolation. `wait_for_visibility()` polls for eventual consistency and logs the wait as excluded from latency. |
| [reviewbot/quality.py](reviewbot/quality.py) | Two scores that are never averaged. Gold labels carry `must_not_flag` items, which is what makes the false-positive trap measurable. Blind PRs (no human comments) are excluded from proxy ratios rather than scored zero. |
| [reviewbot/runner.py](reviewbot/runner.py) | Sequential execution — enforced by `single_process()`, not just documented — and the **memory agent runs first on every PR**: both agents share one cache entry, so the second to run free-rides on the other's cache write, and putting memory first aims that bias against the thesis. Checkpoints each PR as it completes and skips a completed primer, so a rerun resumes instead of re-paying. |

`data/sequence.example.json` is the manifest template; it deliberately fails `dataset validate` until real PR numbers and a pinned SHA replace the placeholders. `data/gold/README.md` documents the label format.

## Replay page (web/)

Built and verified: React + **styled-components v5** (`@redis-ui/styles` peer-depends on ^5, not v6), redis-ui shell, hand-built SVG charts. See [web/README.md](web/README.md).

```bash
cd web && npm install     # set npm_config_cache to a writable dir; ~/.npm is not writable here
npm run check             # palette validation + DOM smoke test -- run this before committing web changes
npm run dev
```

Corrections to earlier claims, from the installed packages: **there is no `Table` component** in `@redis-ui/components` 51.2.0 (only `TableHeading`, a styled `div` with no sort props), so the accounting table is hand-built too. `Typography` renders a `div` and needs `as="h1"` for a real heading; `Banner` has no `"warning"` variant (`informative|notice|danger|attention|success`) and takes `message`; `Switch` uses `onCheckedChange`; `Tabs` is not Radix-shaped.

Series colours are **two chromatic hues** (`discovery400` + `primary400`), not the gray-plus-brand emphasis pairing recorded earlier — emphasis fails the dark-mode lightness band, because no redis-ui neutral light enough to read as a line sits inside it. The phase stack is graded with `validateOrdinal`, not the categorical checks.

`npm run smoke` renders the page in jsdom and asserts 24 viewer-visible properties — do not delete it. It has now caught a hooks-order violation and **four** wrong component APIs that `vite build` accepted silently, the latest being `Badge`, which takes a `label` string and renders children as nothing: the run id and config fingerprint, i.e. the provenance of every number on the page, were silently absent.

The smoke test renders **twice**: once with `fetch` rejecting (the synthetic fallback and its banner) and once against `web/scripts/fixtures/report-real.json`, a report the *harness* produced — regenerate it with `python3 tools/make_page_fixture.py runs/<id>`. Covering only the synthetic path is how `report.per_pr` came to be missing from the harness while every check passed; a real run would have rendered a broken page. It also clicks through all four acts, because an act renders only while selected, so a broken chart or table in acts 2–4 is otherwise invisible.

**The report's shape is a contract**, asserted from both sides: `tests/test_runner.py::TestReplayPageContract` lists the exact fields `web/src/data/contract.js` reads. Dataset rows keep the harness's field names (`ordinal`, `n_files`), while accounting rows use `pr_ordinal` — joining on the wrong one silently blanks the PR number and title for every row.

## The frozen dataset

19 PRs in `data/sequence.json`, all ingested and cached; 7 hand-labelled in `data/gold/`. Beats, evidence, and caveats: [docs/sequence-beats.md](docs/sequence-beats.md). `tests/test_frozen_dataset.py` guards the sequence and labels against drift (a gold-flagged PR with no label file, a trap beat with no `must_not_flag` item, a trap file that only one PR touches, labels claiming confirmed provenance).

Two things not to redo from an older draft: **`README.md` is not convention-change evidence** (a version-badge edit auto-tagged three PRs with a fake invalidation beat before this was narrowed to `CONTRIBUTING.md` + `specs/redis_commands_guide.md`), and the **gold labels are `CANDIDATE`** — written by Claude, which is the same model family under evaluation, so they need a human pass before any quality number is quoted.

## Audit what a run wrote, from outside the harness

```bash
python3 tools/inspect_namespace.py redis-py-run-1
```

Re-derives the touched-file set from the frozen sequence and checks every written memory against it. A write that returns 200 proves nothing about retrievability: a module topic outside the touched set can never match a scoped search. **Filter by `memoryType` and follow `pageToken`** — an unfiltered search caps at 100 records, and the first version of this check found ~100 primed conventions, never reached the findings, and reported clean.

## Prompt caching is inert in this run, and that is a finding

Measured in `runs/run-1`: an `xhigh` review of a 200k-token prompt takes 6-8 minutes, so the two agents' calls on one PR land **6.2 minutes apart** against the **5-minute** TTL. Both write the shared 7,066-token prefix; neither reads it. So `cache_integrity()` legitimately reports "wrote N cache tokens and read none" for both agents, `shared_prefix_freeriding()` is empty, and the as-measured and production-equivalent series converge.

Do not "fix" this by reaching for `--cache-ttl 1h`. It costs a 2x write premium, reintroduces the free-riding the run order is designed to bias against, and makes the replay *less* like production. The zero-read result is worth more than the saved tokens: it is evidence for spec §3's claim that caching cannot substitute for memory — caching did not survive two back-to-back reviews of the same PR, never mind a real PR cadence. See spec §7e.

## Verified against the live services

`memcheck` and `curate` have both been run for real. The findings are in the spec (§4c, §6) and [docs/store-provisioning.md](docs/store-provisioning.md); the two that will bite hardest:

- **The store needs its three custom memory types registered before any write succeeds** — there is no data-plane endpoint for it, so it is a console action. `memcheck` reports exactly which types are missing.
- **The search body silently ignores unknown fields.** A mistyped retrieval knob returns 200 with no error, so never assume a search parameter took effect because the call succeeded.
- Requests must send an explicit `User-Agent`; Cloudflare 403s urllib's default with `error_code 1010`, which reads like an auth failure.

Tests are hermetic and enforce it: [tests/__init__.py](tests/__init__.py) scrubs every credential from `os.environ` at import, and `main()` takes `env_file=None` so an in-process CLI call cannot load a real `.env`. A populated `.env` previously leaked into the suite and two tests made live network calls instead of failing closed.

## Two spec corrections found while building

Both were verified against the saved Agent Memory OpenAPI and are now fixed in the spec — don't reintroduce them from memory of an older draft:

- A namespace like `repo-x/run-3` is **rejected** by the service (pattern is dashes only). Use `repo-x-run-3`.
- Retrieval must use `filterOp: all`, not `any`. With `any`, the namespace clause is OR-ed with the module clause, so a memory from another run touching the same module comes back — cross-run contamination that looks like working retrieval.
- **Module routing filters on `topics`, not `attributes`** (found by the first real run, 2026-08-24). `attributes.module: {in: […]}` is a `400 unknown filter clause member`: an attribute clause is a typed union (`string`/`number`/`boolean`/`list`) with no membership operator, and its `list` variant is whole-value equality — a two-element list returns 200 with zero items, indistinguishable from an empty store. `topics` takes `eq`/`ne`/`in`/`all`, holds raw file paths verbatim, and accepted a 300-entry `in`. Also: **search responses omit `attributes`** (only `GET` returns them) and carry no relevance score, so read `topics` or the id for anything computed from retrieved memories.

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
