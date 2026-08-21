"""Command line: python3 -m reviewbot <command>"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import analysis
from .accounting import Ledger, load_records
from .config import ModelConfig
from .dataset import Sequence, disclosure_table, rows, summary as dataset_summary, validate
from .github import GitHubClient, PRStore

DEFAULT_SEQUENCE = "data/sequence.json"


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = ModelConfig()
    checks: list[tuple[bool, str]] = [
        (sys.version_info >= (3, 11), f"python {sys.version.split()[0]} (need >= 3.11)"),
        (
            bool(os.environ.get("ANTHROPIC_API_KEY")),
            "ANTHROPIC_API_KEY set (required: the harness reads first-party "
            "usage rather than estimating)",
        ),
        (
            bool(os.environ.get("GITHUB_TOKEN")),
            "GITHUB_TOKEN set (required: unauthenticated ingestion hits the "
            "60 req/hour limit)",
        ),
        (Path(DEFAULT_SEQUENCE).exists(), f"{DEFAULT_SEQUENCE} exists"),
    ]
    for ok, label in checks:
        print(f"  [{'ok ' if ok else 'MISSING'}] {label}")
    print()
    print(f"  model config fingerprint: {cfg.fingerprint()}")
    print(f"  {cfg.canonical()}")
    try:
        import anthropic  # noqa: F401

        print("  anthropic SDK: present (reviewbot.claude can be swapped to it)")
    except ImportError:
        print("  anthropic SDK: absent -- reviewbot.claude speaks raw HTTP (stdlib only)")
    return 0 if all(ok for ok, _ in checks) else 1


def cmd_ingest(args: argparse.Namespace) -> int:
    store = PRStore(args.store)
    client = GitHubClient(args.cache)
    if args.prs:
        numbers = [int(n) for n in args.prs.split(",")]
        repo = args.repo
    else:
        seq = Sequence.load(args.sequence)
        repo, numbers = seq.repo, [e.pr_number for e in seq]
    prs = store.ingest(client, repo, numbers, refresh=args.refresh)
    print(
        f"ingested {len(prs)} PRs from {repo} "
        f"({client.requests_made} API requests, {client.cache_hits} cache hits)"
    )
    for pr in prs:
        flag = " [FILES TRUNCATED]" if pr.truncated_files else ""
        print(
            f"  #{pr.number:<6} {len(pr.files):>3} files  {pr.diff_size:>5} changes  "
            f"base={pr.base_sha[:8]}  {sum(1 for c in pr.comments if not c.author_is_bot):>2} human comments{flag}"
        )
    return 0


def cmd_dataset(args: argparse.Namespace) -> int:
    seq = Sequence.load(args.sequence)
    store = PRStore(args.store)
    problems = validate(seq, store)
    if args.action == "validate":
        if problems:
            print(f"{len(problems)} problem(s):")
            for p in problems:
                print(f"  - {p}")
            return 1
        print(f"sequence ok: {len(seq)} PRs, {len(seq.gold_subset)} hand-labeled")
        return 0
    if args.action == "table":
        print(disclosure_table(rows(seq, store)))
        return 0
    print(json.dumps(dataset_summary(seq, store), indent=2))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    path = Path(args.run) / "calls.jsonl"
    if not path.exists():
        print(f"no ledger at {path}", file=sys.stderr)
        return 1
    records = load_records(path)
    out = analysis.summary(records)
    Path(args.run, "summary.json").write_text(json.dumps(out, indent=2) + "\n")

    print(f"run: {args.run}   {len(records)} ledger rows")
    manifest = Ledger(args.run, "").read_manifest()
    if manifest.get("config_fingerprint"):
        print(f"config fingerprint: {manifest['config_fingerprint']}")
    print()
    header = f"{'agent':<10}{'context vol':>14}{'billed $':>12}{'prod-equiv $':>14}{'overhead $':>12}"
    print(header)
    print("-" * len(header))
    for agent, a in sorted(out["agents"].items()):
        print(
            f"{agent:<10}{a['context_volume']:>14,}{a['billed_usd']:>12.4f}"
            f"{a['billed_usd_production']:>14.4f}{a['memory_overhead_usd']:>12.4f}"
        )
    print()
    primer = out["primer"]
    if primer["primer_calls"]:
        per_pr = primer["primer_usd_per_pr"]
        print(
            f"primer: ${primer['primer_usd']:.4f} over {primer['prs']} PRs = "
            f"${per_pr:.4f}/PR" if per_pr else f"primer: ${primer['primer_usd']:.4f}"
        )
    print(f"break-even PR (as measured):        {out['breakeven']['as_measured']}")
    print(f"break-even PR (production cadence): {out['breakeven']['production_equivalent']}")
    truncated = sum(a["truncated_calls"] for a in out["agents"].values())
    if truncated:
        print(f"WARNING: {truncated} call(s) hit max_tokens -- reviews were cut off")
    for problem in out["cache_integrity"]:
        print(f"WARNING: {problem}")
    print(f"\nwrote {Path(args.run, 'summary.json')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reviewbot", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="check the environment and print the config fingerprint")
    d.set_defaults(func=cmd_doctor)

    i = sub.add_parser("ingest", help="fetch PRs from GitHub into the on-disk store")
    i.add_argument("--repo", default="redis/redis-py")
    i.add_argument("--prs", default="", help="comma-separated PR numbers; default: the sequence")
    i.add_argument("--sequence", default=DEFAULT_SEQUENCE)
    i.add_argument("--store", default="data/prs")
    i.add_argument("--cache", default="data/cache/github")
    i.add_argument("--refresh", action="store_true", help="re-fetch even if cached")
    i.set_defaults(func=cmd_ingest)

    ds = sub.add_parser("dataset", help="validate or describe the frozen sequence")
    ds.add_argument("action", choices=["validate", "table", "summary"])
    ds.add_argument("--sequence", default=DEFAULT_SEQUENCE)
    ds.add_argument("--store", default="data/prs")
    ds.set_defaults(func=cmd_dataset)

    r = sub.add_parser("report", help="aggregate a run's ledger into the headline numbers")
    r.add_argument("run", help="run directory containing calls.jsonl")
    r.set_defaults(func=cmd_report)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
