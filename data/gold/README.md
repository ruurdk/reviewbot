# Hand-labelled gold set

One file per PR, named `<pr_number>.json`, for the 5–8 beat PRs (spec §7c). The
proxy metric covers the whole sequence; this covers the PRs the narrative rests
on, and it is the only place false-positive rate can be stated rigorously.

```json
{
  "pr_number": 3411,
  "labeller": "your-name",
  "note": "why this PR is in the subset — which beat it serves",
  "items": [
    {
      "id": "d1",
      "file": "redis/connection.py",
      "line": 412,
      "kind": "defect",
      "category": "resource-leak",
      "severity": "major",
      "description": "socket is not closed when the handshake raises"
    },
    {
      "id": "t1",
      "file": "redis/connection.py",
      "line": 88,
      "kind": "must_not_flag",
      "description": "deliberate retry-loop pattern that is correct here",
      "note": "the false-positive trap: the baseline re-flags this on every PR that touches the file"
    }
  ]
}
```

`kind` is `defect` or `must_not_flag`. The `must_not_flag` items are what make
the false-positive trap measurable instead of anecdotal — an agent that reports
one is scored for it (`traps_flagged` in the gold table), and decision memory is
expected to suppress it after the first occurrence.

Two labelling rules that keep the scores honest:

- **Label before running the agents**, or at least before looking at their
  output. Labels written after reading a review drift toward whatever the review
  said.
- **A line is matched with a ±5 line tolerance** (`quality.DEFAULT_LINE_WINDOW`),
  so the label only needs to be near the defect, not exactly on it. Omit `line`
  when no single line is determinable; the match then degrades to file level.
