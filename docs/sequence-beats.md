# Narrative beats and the gold set: the evidence trail

Every beat below was assigned from the **merged human review of the real PRs**,
not from a rule and not from a guess. This file is the audit trail, because a
curated sequence is only defensible if the curation is inspectable.

Sequence: 19 PRs, `data/sequence.json`. Reproduce the assignments with
`python3 -m reviewbot beats --pr N --add BEAT`.

## `convention_change` — ordinal 3, PR #4030

Adds an *AI-driven contributions* section to `CONTRIBUTING.md` and creates
`specs/redis_commands_guide.md` (+92 lines), a new best-practices doc for adding
Redis command APIs.

It had to be **spliced in**: it touches only `redis/commands/core.py` of the
spine and the style guide is not a spine module, so the selection rule could
never pick it. The splice is recorded in the entry's `note` and is a documented
addition, not a silent one.

Why it bites: ordinals 8, 9 and 10 (#4059 removing GCRA commands, #4067 INCREX,
#4055 array commands) all add or remove command APIs *after* this date. The
primer reads the repo at the frozen SHA — the base of ordinal 1 — so it never
sees the new guide. A memory agent whose conventions cannot be invalidated
reviews three command PRs against a dead rulebook.

## `recurring_bug` — ordinals 15 and 18

The class: **unguarded or uncleaned shared mutable state across an async/threaded
boundary.** Both instances are confirmed real by the maintainers themselves.

**Ordinal 15, PR #4114** — `redis/asyncio/maint_notifications.py`
- `_scheduled_tasks` are not cancelled on pool/client close, leaving the
  temporary MOVING configuration applied. Reviewer: *"`_scheduled_tasks` aren't
  cancelled on pool/client disconnect"*; the author agreed cancel belongs on
  close and added it.
- The async `BlockingConnectionPool` lacks the maintenance-mode guard the sync
  implementation has.
- The pool lock must be held across `await connection.disconnect()` — otherwise
  the connection sits in neither `_in_use_connections` nor
  `_available_connections` while the await is suspended. Author: *"Good catch.
  Fixed it."*

**Ordinal 18, PR #4205** — `redis/himport.py:338`
- Reads of shared HIMPORT state are not guarded by `lock()`. Reviewer: *"Reads
  also should be guarded with `lock()`. In multi-threaded environment we may end
  up reading stale data."*

Different module, same class — which is what episodic recall has to notice.

## `false_positive_trap` — ordinals 16 and 17

This is the narrative's key beat, and it is **not manufactured**: redis-py's
history contains maintainers rejecting automated-review findings, with reasons.

**Ordinal 16, PR #4131** — `redis/asyncio/maint_notifications.py:548`. A missing
state revert on disconnect. Maintainer verdict: *"Not valid — the revert is
covered via disconnect — for async all connections in all nodes are marked for
disconnect during which the state is reset"*, plus a new unit test proving it.

This is the **same class, in the same file, as the real defect at ordinal 15**.
That makes it a genuinely sharp test: the memory agent must remember that one
instance was real and another was ruled invalid, rather than collapsing both into
"flag anything about cleanup on disconnect".

**Ordinal 17, PR #4177** touches the same file again. No human raised the concern
there, so this label is *constructed*: a memoryless reviewer sees the same code
shape and flags it again, while one that recorded the ordinal-16 rejection should
stay silent. Weaker evidence than ordinal 16 — flagged as such in the label file.

There is a second documented false positive at **ordinal 6, PR #4044**
(`redis/event.py:118`): a maintainer rejecting a bot's finding about an `RLock`,
*"a deliberate tradeoff against deadlock from `weakref.finalize` re-entry … the
lost-update window the bot describes results only in a self-recovering, bounded,
no-op stale listener entry; not worth changing."* It is labelled but not used as
the trap beat, because `redis/event.py` is touched by only one PR in the
sequence and a trap has to recur to be a trap.

## Gold set: 7 PRs, and two independence caveats

| PR | Ordinal | Beat | Defects | Traps |
|---|---|---|---|---|
| #4030 | 3 | convention_change | 0 | 0 |
| #4044 | 6 | — | 0 | 1 |
| #4063 | 13 | — | 2 | 0 |
| #4114 | 15 | recurring_bug | 3 | 0 |
| #4131 | 16 | false_positive_trap | 0 | 1 |
| #4177 | 17 | false_positive_trap | 0 | 1 |
| #4205 | 18 | recurring_bug | 1 | 0 |

#4030's empty defect list is deliberate: it is in the subset because it changes
the rulebook, and an empty review is the correct review of it.

**These labels are marked `CANDIDATE` and need human confirmation.** Two reasons
they are not yet a gold standard:

1. **Same-family bias.** They were written by Claude, and the reviewer under
   evaluation is Claude. A label set produced by the model being measured is not
   an independent standard, however well grounded. The remedy is a human pass;
   the evidence quotes above are there to make that pass fast rather than to
   substitute for it.
2. **Partial dependence on the proxy metric.** The `defect` labels are derived
   from merged human review comments — which is also what the §7c proxy scores
   against. So for those items, gold precision/recall is not independent of the
   proxy, and the two numbers should not be presented as corroborating each
   other. The `must_not_flag` items do not have this problem: nothing in the
   proxy rewards *not* commenting, which is exactly why the false-positive rate
   is the more informative half of the quality table.

A human relabelling pass should focus on the `defect` items. The
`must_not_flag` items rest on explicit maintainer verdicts and are the strongest
labels in the set.
