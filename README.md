# Does agentic memory make PR review cheaper?

A **demo and experiment**, not a product. Two PR-review agents run over the same
frozen sequence of 19 real `redis/redis-py` pull requests, under an identical
model, prompt, tool set and effort level. The only difference between them is a
memory layer. The deliverable is evidence — with the instrumentation exposed —
that memory reduces cumulative review cost without degrading review quality.

**The claim in one sentence:** rebuilding an understanding of the repo is the
largest recurring line item in a review agent's token bill, and memory converts
it from a *per-PR* cost into a *per-repo* cost. Prompt caching cannot do that —
its longest TTL is one hour, and no real PR cadence fits inside that.

The full design and its defences live in
[docs/agentic-memory-pr-review-demo-spec.md](docs/agentic-memory-pr-review-demo-spec.md).
That document is the source of truth; this README is the map.

---

## Status

**The first full run is in flight; no completed run exists yet.** Everything
around it is built and verified against live services.

| Piece | State |
|---|---|
| Harness: accounting, ledger, Messages API client, review loop | built, 169 tests |
| Baseline agent, memory agent, one-time repo primer | built; both verified end to end against the live APIs |
| Redis Agent Memory client | built; store **provisioned and verified** (`memcheck` green, ~0.4s write→searchable) |
| Frozen dataset | 19 PRs curated, ingested, cached; 7 hand-labelled; all 3 narrative beats assigned |
| Quality scoring: proxy + gold, false-positive traps | built |
| Replay page | built; renders a synthetic fixture until a real `report.json` is dropped into `web/public/` |
| **The full sequence run** | **in progress** (`runs/run-1`) — single-process-locked, checkpointed, resumable |
| Gold labels | **candidate only — need a human pass** (see below) |
| `review_policy` (procedural memory) | not built; the spec sequences it last |
| Memory invalidation for the convention-change beat | not built; spec §6 says disclose the gap rather than drop the PR |

Two caveats, stated here rather than in a footnote:

- **The gold labels were written by Claude, and the reviewer under evaluation is
  Claude.** A label set produced by the model being measured is not an
  independent standard, however well grounded in maintainer quotes. They are
  marked `CANDIDATE` and a test asserts they stay marked. See
  [docs/sequence-beats.md](docs/sequence-beats.md).
- **The gold set is small enough that quality is a guardrail, not a
  measurement**: 6 labelled defects and 3 false-positive traps across 7 PRs, and
  4 of those 7 have no labelled defect at all, so **recall is computed over 3
  PRs**. One finding moves recall by ~17 points. Read it as "did review quality
  collapse?" — which is what spec §7 asks of it — and not as a precision/recall
  figure worth quoting to two significant figures. Precision and the
  false-positive rate are on firmer ground, since every agent finding counts
  toward them.
- **The replay page currently shows synthetic numbers.** The PRs, diffs, modules,
  comment counts and beats in the fixture are real; every token count, dollar
  figure and quality score is a placeholder. The page says so in a permanent
  banner and reports its run id as `SYNTHETIC-no-run-executed`. Its smoke test
  also renders a real harness report, so the two shapes cannot drift apart
  unnoticed — that failure mode already happened once.

---

## Quickstart

Python 3.11+ (stdlib only — no pip needed) and, for the page, Node 20+.

[.env.example](.env.example) documents every variable, what breaks without it, and
where to get it. `.env` is gitignored; the real environment always wins over the
file, so a one-off `ANTHROPIC_API_KEY=... python3 -m reviewbot run ...` works.

```bash
cp .env.example .env
python3 -m reviewbot doctor  # says exactly what is still missing (and flags placeholders)

python3 -m unittest discover -s tests -t .   # 169 tests, no network, no keys
```

Then, in dependency order:

```bash
python3 -m reviewbot memcheck                  # is the memory store usable?
python3 -m reviewbot dataset validate          # is the frozen sequence complete?
git clone https://github.com/redis/redis-py .checkouts/redis-py
python3 -m reviewbot run my-run-1 --checkout .checkouts/redis-py
python3 -m reviewbot report runs/my-run-1      # headline numbers + summary.json
cp runs/my-run-1/report.json web/public/            # the page now renders the real run
python3 tools/make_page_fixture.py runs/my-run-1   # and its smoke test checks that shape
```

`run --checkout` reads source from a local `redis-py` clone via
`git show <sha>:<path>`, so only PR *metadata* needs a GitHub token, not file
contents.

**A run takes hours and spends real money, so three things guard it.** Only one
process may execute a run — `runs/<id>/run.lock` refuses a second, because two
processes cannot share a prompt cache entry and their misses would be recorded as
context volume. Each PR is checkpointed as it finishes and a completed primer is
never re-paid, so re-running the same command resumes rather than restarting.
And a `max_tokens` stop raises a named error instead of a JSON parse failure: a
truncated review is unparseable, not a review with fewer findings, and reporting
it as empty would credit an agent with zero findings it never made.

### Commands

| Command | What it does |
|---|---|
| `doctor` | environment + credential check, prints the config fingerprint |
| `memcheck` | verifies the memory store: auth, health, registered types, write-visibility lag |
| `ingest` | fetch PRs into the on-disk store (cached by URL; a re-run makes 0 requests) |
| `curate` | scan the repo, apply the stated selection rule, freeze a sequence |
| `beats` | assign a narrative beat or gold flag to a PR |
| `dataset` | `validate` / `table` / `summary` for the frozen sequence |
| `run` | execute the sequence for both agents |
| `report` | aggregate a ledger into the headline numbers |

### The page

```bash
cd web
npm install                 # set npm_config_cache if ~/.npm is unwritable
npm run check               # palette validation + DOM smoke test
npm run dev                 # http://localhost:5173
```

---

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
  analysis.py         aggregation, the 7d repricing, break-even, cache integrity
  quality.py          proxy + gold scoring, false-positive traps
  curate.py           sequence selection and splicing
  repo.py github.py dataset.py runner.py cli.py env.py
web/                  the replay page (React + styled-components v5 + redis-ui)
data/
  sequence.json       the frozen 19-PR sequence
  gold/               hand-labelled subset (candidate)
  prs/ cache/         ingested PRs and raw API responses (gitignored)
docs/                 spec, beat evidence, store provisioning
tests/                154 tests; hermetic by construction
```

---

## What keeps the result honest

These are not style preferences. Each is enforced in code, because violating it
invalidates the experiment.

**One config, hashed.** Both agents receive the same `ModelConfig`;
`assert_comparable()` raises on a mismatch and the fingerprint goes in every run
manifest. Model, effort, thinking, `max_tokens`, tools and system prompt are all
confounds if they differ.

**`input_tokens` is not the prompt size.** It is the uncached remainder only.
Context volume is the sum of all three input fields, and the harness reports that
alongside price-weighted billed cost.

**Savings are always net.** Primer, retrieval and write tokens are tagged
`{agent, pr_id, phase}` and included in the totals. An untagged call is an
unmeasurable one, so tagging is a required argument rather than an option.

**Memory writes are explicit and client-side.** Automatic extraction runs on the
memory service's own LLM, whose tokens client-side accounting cannot see. Using
it would make the headline chart wrong in memory's favour, so it stays off.

**Compressed replay is repriced.** Replaying 19 PRs in minutes hands both agents
cache hits production would never see. Cross-PR cache reads are recharged at the
full input rate under a stated rule, applied to **both** agents — the memory
agent's primed prefix goes cold in production too.

**The agents share a cache entry, so the memory agent runs first.** Both send a
byte-identical cacheable prefix; whichever runs second free-rides on the other's
cache write. Running memory first aims that bias *against* the thesis, and the
report quantifies it.

**Quality is a mandatory guardrail**, on its own chart, reported even when
unflattering. Cost and quality never share an axis.

**The curation is disclosed, not hidden.** The selection rule is code, and
`dataset table` prints per-PR recurrence, diff size, beat and human-comment
counts. Module recurrence is 100% both before and after trimming — so recurrence
is a property of redis-py, not of the selection.

---

## Findings worth knowing before you touch this

Verified against the live services. Each one fails in a way that reads like
something else:

- **Custom memory types must be registered on the store before any write.** An
  unregistered type fails every create with `400 memory type "x" is not
  registered on this store`, and there is no data-plane endpoint for
  registration. Field types must match exactly, and scalars are `str` — ordinals
  are zero-padded so string ordering stays correct.
  [docs/store-provisioning.md](docs/store-provisioning.md)
- **The memory search body silently ignores unknown fields.** `searchMode`,
  `mode` and `keyword` all return `200` with no error, so a mistyped retrieval
  knob looks like it worked.
- **Retrieval must use `filterOp: all`.** With `any`, the namespace clause is
  OR-ed with the module clause and memories from other runs come back —
  cross-run contamination that looks like working retrieval.
- **Module retrieval filters on `topics`, not `attributes`.** An attribute
  clause is a typed union with no membership operator, and its `list` variant is
  whole-value equality — so the natural-looking
  `attributes.module: {list: [a, b]}` returns `200` with zero results,
  indistinguishable from an empty store. `topics` takes `in` and holds raw file
  paths verbatim. Search responses also omit `attributes` altogether; only
  `GET` returns them.
- **Requests need an explicit `User-Agent`**; Cloudflare 403s urllib's default
  with `error_code 1010`, which reads like an auth failure.
- **Prompt caching does not survive even a back-to-back replay.** An `xhigh`
  review of a 200k-token prompt takes 6–8 minutes, so two consecutive calls on
  one PR are 6.2 minutes apart against the 5-minute TTL. Both agents write the
  shared prefix and neither reads it. Inconvenient as a cost saving, useful as
  evidence: caching could not bridge two reviews of the *same* PR, let alone a
  real PR cadence.
- **redis-ui has no chart components and no `Table`** — 56 components, `Gauge` is
  the only visualization-adjacent one. It supplies the shell; the charts and the
  accounting table are hand-built against its theme tokens.
- **Series colours are validated, not chosen.** `npm run validate-colors` grades
  six palettes against the installed `@redis-ui/styles`. The obvious blue+violet
  pairing has a colourblind ΔE of **1.7** — indistinguishable to a deutan viewer.

---

## Reproducing the dataset

The sequence is frozen at `7021617890d4` and every API response is cached, so a
re-run of `curate` makes zero network requests and produces the same 19 PRs.
Beats: `convention_change` at ordinal 3, `recurring_bug` at 15 and 18,
`false_positive_trap` at 16 and 17 — each assigned from the merged human review,
with the evidence quoted in [docs/sequence-beats.md](docs/sequence-beats.md).

The false-positive trap did not have to be manufactured: redis-py's history
contains maintainers rejecting automated-review findings with reasons, including
one on the same file and the same class as a *real* defect one PR earlier. That
makes it a sharper test than "does the agent repeat itself" — the memory agent
has to remember which instance was real.
