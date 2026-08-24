# Store provisioning (spec §10 step 2)

Verified against the live preview service on 2026-08-21 with
`python3 -m reviewbot memcheck`.

**Status: provisioned and verified.** All three types are registered on the demo
store, their fields are accepted, and the measured write→searchable lag is
**0.33s**.

**The memory agent cannot write anything until these three types exist.** An
unregistered type fails *every* create with
`400 memory type "repo_convention" is not registered on this store`, and there
is no registration endpoint on the data plane — `POST /v1/stores/{id}/memory-types`
404s, and `/v1/stores` answers 403 from a different host. So registration is a
Redis Iris console / control-plane action, not something the harness can do.

**Field types must match exactly, and a mismatch fails the entire create** —
not just the offending attribute:

```
400 attribute "convention_version" has the wrong type for
    memory type "repo_convention" (expected str)
```

The registered types declare their scalar fields as `str`, so the harness
encodes attributes on the wire (`memory.encode_attributes`). Ordinals are
**zero-padded to 3 digits** — `"010"`, not `"10"` — because a plain `str(n)`
sorts `"10"` before `"9"` and would silently break any range filter over
sequence position. Sending an extra field that the type does not declare is
also fatal: `400 unknown attribute "module"`. `memcheck` distinguishes all three
failure modes (unregistered / wrong type / unknown field) and names the field.

Re-run `python3 -m reviewbot memcheck` after registering; it writes one probe
record per type into the `harness-preflight` namespace, measures the
write→searchable lag, and deletes what it wrote.

## The three types

Attribute values may be `str`, `int`, `float`, `bool`, `list[str]`,
`list[float]`, or `object`. Field names below are exactly what
[reviewbot/agents.py](../reviewbot/agents.py) writes and what
[reviewbot/memory.py](../reviewbot/memory.py) filters on — a mismatch shows up
as an empty retrieval, not as an error.

### `repo_convention` — semantic memory

What the primer distills from the frozen repo: architecture, module
responsibilities, conventions, invariants. One durable fact per record.

| Field | Type | Purpose |
|---|---|---|
| `module` | `str` | Path the fact belongs to. Provenance and reporting only — **retrieval does not filter on it**; see "Module routing lives in `topics`" below. |
| `kind` | `str` | `architecture` / `convention` / `invariant` / `ownership`. |
| `topic` | `str` | Short stable slug; part of the record id, so re-priming is idempotent. |
| `source` | `str` | `style-guide` / `human-correction` / `inferred`. |
| `convention_version` | `str` | Bumped by the convention-change beat; this is the invalidation surface. |
| `pr_ordinal` | `str` | `"000"` for primed facts, zero-padded. Chronology lives here because `createdAt` is server-assigned and cannot be backdated. |

### `review_finding` — episodic memory

What past reviews learned, phrased as reusable rules rather than reports about a
specific diff.

| Field | Type | Purpose |
|---|---|---|
| `module` | `str` | Provenance, as above. |
| `finding_class` | `str` | Groups recurrences; drives the recurring-bug and false-positive beats. |
| `pr_ordinal` | `str` | Position in the frozen sequence, zero-padded (`"003"`). |
| `pr_number` | `str` | The real PR number, for drill-down in the replay page. |
| `source` | `str` | `review` / `human-correction`. |

### `review_policy` — procedural memory

Built last, per the spec's build order. Which suggestion classes this team
accepts, and the calibrated checklist.

| Field | Type | Purpose |
|---|---|---|
| `finding_class` | `str` | The class the policy applies to. |
| `action` | `str` | `suppress` / `promote`. |
| `evidence_pr_ordinals` | `list[str]` | Which reviews justified the policy. |
| `source` | `str` | `human-correction` / `inferred`. |

## Settings that must match the experiment design

- **Automatic extraction: OFF.** It runs on the service's own LLM, whose tokens
  are invisible to client-side accounting, which would make the headline chart
  wrong in memory's favour (spec §7a). The harness writes explicitly instead, so
  every write token is measured. Extraction instructions on the types are
  therefore unused — leave them minimal.
- **Session summarization: OFF**, for the same reason, unless you intend to
  measure or disclose its service-side cost.
- **Namespace per run.** `REVIEWBOT_NAMESPACE` (default `redis-py-run-1`).
  Alphanumerics and hyphens only — the service rejects `redis-py/run-1`, which
  is confirmed both client- and server-side.

## Module routing lives in `topics`, not `attributes`

Found by the first real run, 2026-08-24, and it cost that run its entire
retrieve phase. Every record therefore carries the raw path of each module it
concerns as a **topic**, and `scoped_filter()` fans out over
`topics: {in: [...]}`.

- An attribute clause is a *typed union*: `400 exactly one of string, number,
  boolean, or list must be set`. Anything operator-shaped — `eq`, `in`, `any`,
  `contains` — is `400 unknown filter clause member`.
- The dangerous variant is `list`. `{"module": {"list": [a, b]}}` compares
  against the **whole value**, so it is equality and not membership: a
  one-element list matches a scalar attribute, and a two-element list matches
  nothing. It returns `200 {"items": []}`, which is indistinguishable from "the
  store is empty".
- `topics` accepts `eq` / `ne` / `in` / `all` (the service enumerates them in its
  own error text), stores raw file paths verbatim — slashes, dots and all, no
  slug mapping needed — and round-trips them in search results. A 300-entry `in`
  list was accepted, against a 78-file worst case in the frozen sequence.
- **A search response omits `attributes` entirely.** Only `GET
  .../long-term-memory/{id}` returns them, and search results carry no relevance
  score either. Anything computed from retrieved memories must read `topics` or
  the id.

## Two behaviours to keep in mind while tuning retrieval

- **The search body silently ignores unknown fields.** `searchMode`, `mode`, and
  `keyword` all return `200 {"items": []}` rather than a 400. A mistyped
  retrieval knob therefore looks like it worked. It also means keyword/hybrid
  search cannot be confirmed by probing — spec §12's open question stands, and
  the answer has to come from Redis rather than from an experiment.
- **Bulk delete validates every id before deleting any.** One malformed id
  rejects the whole batch with a 400, so a per-run reset needs client-side id
  validation first (`memory.validate_id`, which the client applies).
- **Unknown *top-level* filter keys are ignored too.** A filter of
  `{"namespace": {...}, "bogusKey": {"eq": "x"}}` returns the unfiltered result
  set with a 200. Combined with the point above: never infer that a filter took
  effect from the fact that the call succeeded. The only proof is a result set
  that changes when the clause changes.
