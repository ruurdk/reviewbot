"""Audit a run's namespace: is every written memory actually retrievable?

    python3 tools/inspect_namespace.py redis-py-run-1

A memory whose module topic is not one of the touched files can never come back
from a scoped search -- it was written, billed, and is dead. That is invisible
from the harness side, because the write succeeded. On the first real run, five
of six findings were in that state: the distiller had named directories
(`redis/commands/search`, `tests`, `repo`) where paths were expected.

Filters by memoryType and follows pageToken. A single unfiltered search caps at
100 records, so on a primed namespace (~100 conventions) the findings fall off
the end and an incomplete check reads as a clean one -- which it did.
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from reviewbot import env

env.load_env(".env")
from reviewbot.memory import REPO_CONVENTION, REVIEW_FINDING, AgentMemoryClient

NS = sys.argv[1] if len(sys.argv) > 1 else "redis-py-run-1"
c = AgentMemoryClient(store_id=os.environ["REDIS_AGENT_MEMORY_STORE_ID"], namespace=NS)

ROOT = pathlib.Path(__file__).resolve().parent.parent
seq = json.load(open(ROOT / "data/sequence.json"))
store_dir = ROOT / "data/prs/redis__redis-py"
real_paths = set()
for entry in seq["entries"]:
    pr = json.load(open(f"{store_dir}/{entry['pr_number']}.json"))
    real_paths.update(f["filename"] for f in pr["files"])


def all_of(memory_type):
    out, token, pages = [], None, 0
    while pages < 20:
        items, token = c.search(
            "",
            filter={"namespace": {"eq": NS}, "memoryType": {"eq": memory_type}},
            limit=100,
            page_token=token,
        )
        out.extend(items)
        pages += 1
        if not token or not items:
            break
    return out, token


findings, more_f = all_of(REVIEW_FINDING)
conventions, _ = all_of(REPO_CONVENTION)
print(f"{NS}: {len(findings)} findings, {len(conventions)} conventions")
if more_f:
    print("  WARNING: more findings remain unread (pageToken still set)")

bad = []
for m in findings:
    modules = [t for t in m.topics if t != "finding"]
    if not modules:
        bad.append((m.id, "<no module topic>"))
    for t in modules:
        if t not in real_paths:
            bad.append((m.id, t))

print(f"  findings with an unroutable module topic: {len(bad)}")
for mid, topic in bad[:10]:
    print(f"    BAD {mid}: {topic!r}")
if findings and not bad:
    print("    none -- every finding routes to a real touched path")
multi = [m for m in findings if len([t for t in m.topics if t != "finding"]) > 1]
print(f"  findings routed to more than one module (directory rule): {len(multi)}")
for m in multi[:3]:
    print(f"    {m.id} -> {[t for t in m.topics if t != 'finding']}")
