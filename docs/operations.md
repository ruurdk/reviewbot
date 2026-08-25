# Operations

Everything needed to set this up, run it, and not break a run in progress. The
[README](../README.md) is the high-level map; the
[spec](agentic-memory-pr-review-demo-spec.md) is the source of truth for *why*
any of it is shaped this way.

## Requirements

Python 3.11+ and, for the replay page, Node 20+. **The harness is standard
library only** — there is nothing to `pip install`. Every external surface sits
behind one thin client (`reviewbot/claude.py`, `reviewbot/memory.py`,
`reviewbot/github.py`) that an official SDK can replace without touching
anything above it.

## Credentials

[.env.example](../.env.example) documents every variable, what breaks without it,
and where to get it. `.env` is gitignored, and the real environment always wins
over the file — so a one-off `ANTHROPIC_API_KEY=... python3 -m reviewbot run ...`
works.

| Variable | Needed by |
|---|---|
| `ANTHROPIC_API_KEY` | `run` |
| `GITHUB_TOKEN` | `ingest`, `curate` |
| `REDIS_AGENT_MEMORY_URL` / `_API_KEY` / `_STORE_ID` | `run` with the memory agent |
| `REVIEWBOT_NAMESPACE` | optional default for `--namespace` (not a secret) |

```bash
cp .env.example .env
python3 -m reviewbot doctor   # says what is missing, and flags placeholders
```

`run` checks for the keys its chosen `--agents` actually need and exits with the
full list *before* making any call, rather than failing partway through a
sequence.

## Setup and a full run, in dependency order

```bash
python3 -m unittest discover -s tests -t .     # 255 tests, no network, no keys

python3 -m reviewbot memcheck                  # is the memory store usable?
python3 -m reviewbot dataset validate          # is the frozen sequence complete?

git clone https://github.com/redis/redis-py .checkouts/redis-py
python3 -m reviewbot run my-run-1 --checkout .checkouts/redis-py

python3 -m reviewbot report runs/my-run-1              # headline numbers
python3 tools/inspect_namespace.py <namespace>         # is every memory retrievable?
cp runs/my-run-1/report.json web/public/               # page renders the real run
python3 tools/make_page_fixture.py runs/my-run-1       # its smoke test checks that shape
```

When it finishes, freeze it: `cp -r runs/my-run-1 runs/frozen/my-run-1`, then
`python3 -m reviewbot report runs/frozen/my-run-1` so the frozen `summary.json`
and the `accounting` block of its `report.json` are the ones current code
produces. Nothing in the harness writes `runs/frozen/` — it is a convention, and
the point of it is that `runs/<id>/` is a working directory a resume can still
append to, while the frozen copy is what a number in the README refers to.
`runs/frozen/run-1` and `runs/frozen/run-2` are both there.

`run --checkout` reads source at the pinned SHA via `git show <sha>:<path>`, so
only PR *metadata* needs a GitHub token, not file contents. A dirty working tree
in that clone therefore cannot silently change the frozen dataset.

## Commands

| Command | What it does |
|---|---|
| `doctor` | environment + credential check; prints the config fingerprint |
| `memcheck` | verifies the store: auth, health, registered types, write-visibility lag |
| `ingest` | fetch PRs into the on-disk store (cached by URL; a re-run makes 0 requests) |
| `curate` | scan the repo, apply the stated selection rule, freeze a sequence |
| `beats` | assign a narrative beat or gold flag to a PR |
| `dataset` | `validate` / `table` / `summary` for the frozen sequence |
| `run` | execute the sequence for both agents |
| `report` | aggregate a ledger into the headline numbers + `summary.json` |

### `run` flags that change what is being measured

Everything below is an experimental variable, not a preference. Both agents always
get the identical model config — `assert_comparable()` raises `ConfoundError`
otherwise — so these knobs are the legitimate ones.

| Flag | Effect |
|---|---|
| `--effort low\|medium\|high\|xhigh\|max` | The **dominant cost knob**. ~97% of output is thinking tokens, and output is priced 5x input, so it is 41-64% of the bill (spec §7g). Must be identical for both agents; a sweep is a separate run each. |
| `--retrieval-limit N` | Pooled retrieval budget across both memory types. Run-1 used 20 and saturated it on **every** PR, so the window was reporting the limit rather than relevance (spec §7f). |
| `--retrieval-split conv=10,find=10` | One search per memory type with its own budget instead of one pooled search, so a growing pile of findings cannot squeeze conventions out. Costs one extra round trip and **zero model tokens** (searches are `billable=False`), so it moves the retrieval mix without touching the cost comparison. Pooled stays the default so run-1 remains reproducible. |
| `--dedupe-writes` | Merge a repeat finding into its existing record instead of appending a copy, and record `occurrences` / `last_pr_ordinal` on it. Changes the finding **id scheme** (drops the ordinal), so a run with it on and one with it off produce stores that cannot be compared record-for-record — which is why the manifest records it. Costs one non-billable GET per finding. Off by default so run-1 reproduces. |
| `--no-distill` | Write findings verbatim instead of distilling them — a zero-model-token write phase. Distillation cost $1.01 of run-1's $25.76, mostly output. |
| `--cache-ttl 1h` | Do not reach for this to "fix" the zero cache reads. It costs a 2x write premium, reintroduces the free-riding the run order is designed to bias against, and makes the replay *less* like production. The zero-read result is evidence, not a bug (spec §7e). |

After a run with a split budget, `analysis.retrieval_mix()` (in `summary.json`)
reports what each window actually contained, by memory type. A run whose search
rows predate id logging reports `instrumented: false` rather than an empty mix.

Both knobs have now been exercised over the full sequence, and what they measured
is worth knowing before spending another run on them:

| | run-1 (frozen) | run-2 (frozen) |
|---|---|---|
| retrieval | pooled, 20 | split, `conv=10,find=10` |
| writes | append-only | `--dedupe-writes` |
| saving per review, size-weighted | +27.7% | +31.6% |
| worst PR (#4131) | −46.8% | +6.8% |
| break-even | PR 5 | PR 6 |
| retrieval precision | 19% (74/380) | 25% (80/316) |

- **Split retrieval answered run-1's open question.** Conventions returned their
  full 10 on all 19 PRs while findings started at 0 and reached their cap only
  from PR 7 — so in a pooled window it is conventions that crowd, and capping
  them is the plausible mechanism behind #4131 flipping.
- **`--dedupe-writes` fired once in 101 writes** (PR #4177, logged as
  `deduped_writes: 1`). The id keys on `(module, topic)` and `topic` is
  model-generated free text, which is not a stable key — the same concept on the
  same file came back as `...python-3-9-compatibi...` and
  `...python-version-compa...`. Treat the flag as unbuilt until it keys on
  something stable or does a similarity check before writing.
- The two runs differ in **two** knobs at once plus generation nondeterminism, so
  neither difference above is attributed to one knob. The control is what makes
  that visible: both baselines read byte-identical context (3,890,269 tokens) and
  still billed differently ($32.67 against $33.54).

The manifest records **both** knobs (`manifest.retrieval` and `manifest.writes`)
because neither is part of `ModelConfig.fingerprint()`. Without that, two runs
differing only in retrieval shape or write policy produce byte-identical
manifests, and the single variable under test leaves no trace in the artifacts.

Tools alongside the CLI:

| Tool | What it does |
|---|---|
| `tools/inspect_namespace.py` | audits a run's memories: is every one actually retrievable? |
| `tools/make_page_fixture.py` | builds the real-report fixture the page's smoke test needs |
| `tools/dump_prompts.py` | regenerates [prompts.md](prompts.md) from the source (`--check` to verify) |

## Running safely

A run takes hours and spends real money. Three guards, each added after the
failure it prevents:

**One process per run.** `runs/<id>/run.lock` refuses a second. Two processes on
one run interleave rows in the append-only ledger under duplicate `seq`, re-pay
for the primer, and — worst — cannot share a prompt cache entry, so their misses
get recorded as context volume. This is not hypothetical: four processes once ran
one sequence concurrently because `ps` inside the tool sandbox could not see
them. **Check for a live run with the sandbox disabled**, or the process list
looks empty when it is not.

**Resumable, and it never re-pays the primer.** Each PR is checkpointed to
`runs/<id>/checkpoint.jsonl` as it completes, and `primed.json` records the
finished primer. Re-run the identical command to resume. A PR counts as complete
only when *every* agent has an outcome for it, so an interruption costs at most
the one PR in flight. Each run directory gets a `RESUME.md` with its exact
command.

**A half-finished PR is not counted twice.** A crash between the review and write
phases leaves billed calls in the ledger. On resume the runner appends an
`ABANDONED` marker naming those rows; `analysis` excludes them from the per-PR
series and reports their spend separately. The report says so in its warnings —
that line is the mechanism working, not a problem.

Two things not to do: **never change `ModelConfig` mid-run** (the manifest states
one fingerprint for the whole run — start a fresh run id and wipe the namespace
instead), and **never wipe the namespace to tidy up** while a run is live, since
retrieval depends on it.

### Sandbox note

`api.anthropic.com` is not in the Claude Code sandbox allowlist, so any command
that reaches the Messages API fails with
`URLError: Tunnel connection failed: 403 Forbidden` until it runs with
`dangerouslyDisableSandbox`. The memory store's host and `github.com` are
allowed, so `memcheck`, `ingest` and `curate` work inside the sandbox.

## The page

```bash
cd web
npm install       # set npm_config_cache if ~/.npm is unwritable
npm run check     # palette validation + DOM smoke test -- run before committing
npm run dev       # http://localhost:5173
```

`npm run check` is not optional decoration: it renders the page in jsdom and
asserts 24 viewer-visible properties, twice — once against the synthetic
fallback and once against a real harness report. It has caught a hooks-order
violation and five wrong component APIs that `vite build` accepted silently.
See [web/README.md](../web/README.md).

## Layout

```
reviewbot/            the harness (stdlib only)
  config.py           one ModelConfig for both agents + the confound guard
  accounting.py       Usage, CallRecord, append-only JSONL ledger
  claude.py           Messages API over urllib: streaming, retries, prefix hashing
  review.py           the shared review loop -- both agents call this
  agents.py           BaselineAgent, MemoryAgent, the primer
  memory.py           Redis Agent Memory client
  preflight.py        store verification (what `memcheck` runs)
  analysis.py         aggregation, 7d repricing, break-even, cache integrity
  quality.py          proxy + gold scoring, false-positive traps
  curate.py           sequence selection and splicing
  repo.py github.py dataset.py runner.py cli.py env.py
web/                  the replay page (React + styled-components v5 + redis-ui)
tools/                namespace audit, page fixture, prompt dump
data/
  sequence.json       the frozen 19-PR sequence
  gold/               hand-labelled subset (candidate)
  prs/ cache/         ingested PRs and raw API responses (gitignored)
runs/<id>/            manifest, ledger, checkpoint, report, RESUME.md
runs/frozen/<id>/     a finished run, kept for reproduction (run-1, run-2)
docs/                 spec, prompts, operations, provisioning, beat evidence
tests/                255 tests; hermetic by construction
```

Tests use injected transports and never touch the network, so they run with no
credentials at all. `tests/__init__.py` scrubs every credential from the
environment at import, and `main()` takes `env_file=None`, because a populated
`.env` once leaked into the suite and two tests made live calls instead of
failing closed. There is no linter or formatter available here — match the
surrounding style by hand.

## Findings worth knowing before you touch this

Verified against the live services. Each one fails in a way that reads like
something else.

- **Custom memory types must be registered on the store before any write.** An
  unregistered type fails every create with `400 memory type "x" is not
  registered on this store`, and there is no data-plane endpoint for
  registration — it is a console action. `memcheck` reports which are missing.
  Field types must match exactly, and scalars are `str`, so ordinals are
  zero-padded to keep string ordering correct.
  [store-provisioning.md](store-provisioning.md)
- **Module retrieval filters on `topics`, not `attributes`.** An attribute clause
  is a typed union with no membership operator, and its `list` variant is
  whole-value equality — so the natural-looking
  `attributes.module: {list: [a, b]}` returns `200` with **zero** results,
  indistinguishable from an empty store. `topics` takes `in`, holds raw file
  paths verbatim, and accepted a 300-entry list. Search responses also omit
  `attributes` entirely; only `GET` returns them.
- **A model-generated routing key must be validated against a closed set.** The
  write phase asks a model for the `module` a finding belongs to, and that value
  is a retrieval key matched by exact string equality. On the first real run, 5
  of 6 written findings named a *directory* — they wrote successfully and were
  unretrievable forever. `resolve_modules()` now maps a candidate onto the PR's
  touched files, and anything unresolvable is dropped **and logged** as lost
  recall. Audit it with `tools/inspect_namespace.py`.
- **Unknown filter fields are silently ignored** — both unknown top-level keys
  and unknown search-body knobs return `200`. Never infer that a filter took
  effect because the call succeeded; the only proof is a result set that changes
  when the clause changes.
- **`filterOp: all`, never `any`.** With `any`, the namespace clause is OR-ed
  with the module clause and memories from other runs come back — cross-run
  contamination that looks like working retrieval.
- **Requests need an explicit `User-Agent`.** Cloudflare 403s urllib's default
  with `error_code 1010`, which reads like an auth failure.
- **A CLI default can silently shadow `ModelConfig`.** Raising
  `ModelConfig.max_tokens` changed nothing while `cli.py` hard-coded its own
  argparse default, and the run truncated at the old ceiling anyway. Defaults now
  derive from the dataclass.
- **`max_tokens` caps thinking + response text together.** At `xhigh`, the
  largest PR's review spent 32,000 tokens and stopped mid-finding. A truncated
  review is *unparseable*, not a review with fewer findings, so
  `ClaudeResult.json()` raises a named error rather than letting an empty review
  be recorded as zero findings.
- **Measure latency after draining a streamed body.** Timing at header arrival
  logged a 38k-output review that took seven minutes as six seconds; `ttfb_ms`
  keeps the headers-only figure.
- **Prompt caching does not survive even a back-to-back replay.** Two
  consecutive calls on one PR land 6.2 minutes apart against the 5-minute TTL, so
  both agents write the shared prefix and neither reads it. Inconvenient as a
  saving, useful as evidence.
- **redis-ui has no chart components and no `Table`** — 56 components, `Gauge` is
  the only visualization-adjacent one. It supplies the shell; the charts and the
  accounting table are hand-built against its theme tokens. `Badge` takes a
  `label` string and renders children as *nothing*.
- **Series colours are validated, not chosen.** `npm run validate-colors` grades
  six palettes against the installed `@redis-ui/styles`. The obvious blue+violet
  pairing has a colourblind ΔE of **1.7** — indistinguishable to a deutan viewer.

## Reproducing the dataset

The sequence is frozen at `7021617890d4` and every API response is cached, so a
re-run of `curate` makes zero network requests and produces the same 19 PRs.
Beats: `convention_change` at ordinal 3, `recurring_bug` at 15 and 18,
`false_positive_trap` at 16 and 17 — each assigned from the merged human review,
with the evidence quoted in [sequence-beats.md](sequence-beats.md).

The false-positive trap did not have to be manufactured: redis-py's history
contains maintainers rejecting automated-review findings with reasons, including
one on the same file and the same class as a *real* defect one PR earlier. That
makes it a sharper test than "does the agent repeat itself" — the memory agent
has to remember which instance was real.
