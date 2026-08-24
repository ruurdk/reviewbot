# Does agentic memory make PR review cheaper?

A **demo and experiment**, not a product. Two PR-review agents review the same
frozen sequence of 19 real `redis/redis-py` pull requests under an identical
model, prompt, tool set and effort level. The only difference between them is a
memory layer.

**The claim:** rebuilding an understanding of the repo is the largest recurring
line item in a review agent's token bill, and memory converts it from a *per-PR*
cost into a *per-repo* cost. Prompt caching cannot do that — its longest TTL is
one hour, and no real PR cadence fits inside that.

**The deliverable** is that claim measured, with the instrumentation exposed
rather than hidden behind a hero chart: two token series, a break-even PR number,
and a quality guardrail reported even when unflattering.

---

## How it works

```
        frozen sequence: 19 real redis-py PRs, pinned at 7021617890d4
                                    |
                                    v
   +---------------------------------------------------------------+
   |  ONE SHARED HARNESS: same model, prompt, effort, no tools,     |
   |  sequential, memory agent first on every PR                    |
   +---------------------------------------------------------------+
                |                                       |
                v                                       v
   +-------------------------+          +-------------------------+
   |   BASELINE  (control)   |          |   MEMORY  (treatment)   |
   +-------------------------+          +-------------------------+
   | repo conventions        |          | repo conventions        |
   | FULL SOURCE of every    |          | RETRIEVED MEMORIES for  |
   |   touched file          |          |   the touched modules   |
   | the diff                |          | the diff                |
   +-------------------------+          +-------------------------+
                |                         ^      |          ^
                |                retrieve |      | write    | prime
                |                         |      v          | (once
                |                       +--------------------+ per
                |                       | Redis Agent Memory | repo)
                |                       |  repo_convention   |
                |                       |  review_finding    |
                |                       +--------------------+
                |                                       |
                +-------------------+-------------------+
                                    v
        claude-opus-5 -- exact token usage returned on every call
                                    v
        append-only ledger: {agent, pr_id, pr_ordinal, phase}
                                    v
        context volume  |  billed cost, net of memory overhead
        break-even PR   |  cache integrity  |  quality guardrail
                                    v
                       report.json  -->  replay page
```

The substitution in the middle *is* the experiment: the baseline reads full
source to rebuild what it already knew last PR; the memory agent reads a
distilled slice of it. Both read the same diff, because neither can avoid that.

Every prompt is reproduced verbatim in **[docs/prompts.md](docs/prompts.md)**,
generated from the source with a test that fails if it drifts — "the only
difference is a memory layer" is a claim about prompts, so it should be
checkable without reading Python.

---

## Results

> **Run in progress: 16 of 19 PRs complete.** These are as-measured numbers from
> `runs/run-1`, not a finished result. They will be restated, including the
> quality panel, when the sequence completes.

| | context volume | billed | net of memory overhead |
|---|---|---|---|
| baseline | 3,022,070 tok | $24.32 | — |
| memory | 1,630,939 tok | $21.69 | **$2.64 cheaper** |

- **46% less context read**, net of the primer, retrieval and write tokens —
  which are all in the memory agent's column, tagged and counted. A gross saving
  is not a result.
- **Break-even at PR 5**, in both the as-measured and production-cadence regimes.
- **Per review, past the cold start, the gap is 5–7×**: the baseline reads
  150–200k tokens where the memory agent reads 20–30k. The cumulative figure is
  lower because of the one-time primer and two outlier PRs (#4025 at 48 files,
  #4052 at 78) where the *diff itself* dominates the prompt and memory cannot
  help. That is the honest ceiling on this technique: it removes the cost of
  re-deriving repo understanding, not the cost of reading the change.

An unexpected result worth its own line: **prompt caching turned out to be
inert.** An `xhigh` review of a 200k-token prompt takes 6–8 minutes, so two
consecutive calls on the same PR land 6.2 minutes apart against a 5-minute TTL.
Both agents write the shared prefix and neither reads it. That is why the
as-measured and production-cadence columns are within $0.31 of each other — and
it is direct evidence for the argument above: caching could not bridge two
back-to-back reviews of the *same* PR, let alone a real PR cadence.

---

## Status

| Piece | State |
|---|---|
| Harness, both agents, primer, memory client, quality scoring | built · 195 tests · verified against live services |
| Frozen dataset | 19 PRs, pinned and cached; 7 hand-labelled; all 3 narrative beats assigned |
| Replay page | built; renders a synthetic fixture until a real `report.json` lands in `web/public/` |
| **The full sequence run** | **in progress** (`runs/run-1`) — locked to one process, checkpointed, resumable |
| Gold labels | **candidate only — need a human pass** |
| `review_policy` (procedural memory) | not built; the spec sequences it last |
| Memory invalidation for the convention-change beat | not built; spec §6 says disclose the gap rather than drop the PR |

Three caveats that belong up front rather than in a footnote:

- **The gold labels were written by Claude, and the reviewer under evaluation is
  Claude.** A label set produced by the model being measured is not an
  independent standard. They are marked `CANDIDATE` and a test asserts they stay
  marked. [docs/sequence-beats.md](docs/sequence-beats.md)
- **The gold set makes quality a guardrail, not a measurement.** 6 labelled
  defects and 3 false-positive traps across 7 PRs, and 4 of those 7 have no
  labelled defect, so recall rests on 3 PRs — one finding moves it ~17 points.
  Read it as "did quality collapse?", not as a precision figure worth quoting.
- **The replay page shows synthetic numbers until a run lands.** The PRs, diffs
  and beats in that fixture are real; every dollar figure is a placeholder, and
  the page says so in a permanent banner.

---

## What keeps the result honest

Each of these is enforced in code, because violating it invalidates the
experiment. The reasoning behind each is in [the spec](docs/agentic-memory-pr-review-demo-spec.md).

- **One hashed config for both agents.** Model, effort, thinking, `max_tokens`,
  tools and system prompt are confounds if they differ; `assert_comparable()`
  raises, and the fingerprint goes in every run manifest.
- **`input_tokens` is not the prompt size** — it is the uncached remainder.
  Context volume is the sum of all three input fields.
- **Savings are always net.** Primer, retrieval and write tokens are tagged and
  counted. Tagging is a required argument, because an untagged call is an
  unmeasurable one.
- **Memory writes are explicit and client-side.** Automatic extraction runs on
  the memory service's own LLM, whose tokens client-side accounting cannot see.
- **Compressed replay is repriced**, under a stated rule, applied to *both*
  agents — the memory agent's primed prefix goes cold in production too.
- **The memory agent runs first**, so the free ride on the shared cache prefix
  falls to the baseline and the bias runs *against* the thesis.
- **Quality is a mandatory guardrail** on its own chart. Cost and quality never
  share an axis.
- **The curation is disclosed.** `dataset table` prints per-PR recurrence, diff
  size, beat and human-comment counts. Module recurrence is 100% both before and
  after trimming, so recurrence is a property of redis-py, not of the selection.

---

## Where to look next

| Document | What it covers |
|---|---|
| [docs/agentic-memory-pr-review-demo-spec.md](docs/agentic-memory-pr-review-demo-spec.md) | **The source of truth.** Full design, every methodological defence, the open questions |
| [docs/operations.md](docs/operations.md) | Running it: setup, credentials, every command, run safety, repo layout |
| [docs/prompts.md](docs/prompts.md) | Every prompt sent, verbatim and generated from source |
| [docs/store-provisioning.md](docs/store-provisioning.md) | The Agent Memory store: types, settings, and the API behaviours that bite |
| [docs/sequence-beats.md](docs/sequence-beats.md) | The 19 PRs, their narrative beats, and the evidence for each |
| [web/README.md](web/README.md) | The replay page: redis-ui, hand-built charts, validated palettes |
| [CLAUDE.md](CLAUDE.md) | Instructions for agents working in this repo |
