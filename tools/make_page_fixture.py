"""Build web/scripts/fixtures/report-real.json from a real run directory.

The replay page's smoke test needs a report.json that the *harness* produced,
not one hand-written to match the page. Only the synthetic path was covered for
a while, and the harness's report grew a different shape than the page read --
so a real run would have rendered a broken page while every check passed.

    python3 tools/make_page_fixture.py runs/run-1

The fixture is committed so `npm run smoke` works without a run present.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reviewbot import analysis
from reviewbot.accounting import CallRecord

FIXTURE = Path("web/scripts/fixtures/report-real.json")


def build(run_dir: Path) -> dict:
    old = json.loads((run_dir / "report.json").read_text())
    records = [
        CallRecord.from_json(json.loads(line))
        for line in (run_dir / "calls.jsonl").read_text().splitlines()
        if line.strip()
    ]
    accounting = analysis.summary(records)

    detail = {
        (agent, result["ordinal"]): payload
        for result in old["results"]
        for agent, payload in result["outcomes"].items()
    }
    per_pr = []
    for row in accounting["per_pr"]:
        extra = detail.get((row["agent"], row["pr_ordinal"])) or {}
        used = extra.get("memories_used")
        per_pr.append(
            {
                **row,
                "files_read": extra.get("files_read"),
                "files_dropped_over_budget": extra.get("files_dropped_over_budget"),
                "retrieved": extra.get("retrieved"),
                "memories_used": len(used) if used is not None else None,
                "retrieval_precision": extra.get("retrieval_precision"),
                "findings": len(extra.get("findings") or [])
                if extra.get("findings") is not None
                else None,
            }
        )

    # Dataset rows for exactly the PRs this run reviewed, renumbered to the
    # run's own ordinals so the accounting table's join is meaningful.
    all_rows = {
        r["pr_number"]: r
        for r in json.loads(Path("web/src/data/sequence-rows.json").read_text())
    }
    rows = []
    for result in old["results"]:
        row = all_rows.get(result["pr_number"])
        if row is not None:
            rows.append({**row, "ordinal": result["ordinal"]})

    return {
        "run_id": old["run_id"],
        "config_fingerprint": old["config_fingerprint"],
        "per_pr": per_pr,
        "rows": rows,
        "accounting": accounting,
        "quality_gold": old["quality_gold"],
        "quality_proxy": old["quality_proxy"],
        "warnings": old["warnings"],
        "results": old["results"],
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    report = build(Path(argv[1]))
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        f"wrote {FIXTURE}: {len(report['per_pr'])} per_pr rows, "
        f"{len(report['rows'])} dataset rows, run {report['run_id']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
