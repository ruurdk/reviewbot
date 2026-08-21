"""Command line: python3 -m reviewbot <command>"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import analysis, curate
from .accounting import Ledger, load_records
from .agents import BaselineAgent, MemoryAgent
from .claude import ClaudeClient
from .config import ModelConfig
from .dataset import Sequence, disclosure_table, rows, summary as dataset_summary, validate
from .env import DEFAULT_ENV_FILE, REQUIRED_BY, is_placeholder, load_env, missing
from .github import GitHubClient, PRStore
from . import preflight
from .memory import AgentMemoryClient
from .quality import load_gold_dir
from .repo import GitHubSourceProvider, LocalSourceProvider, read_docs
from .runner import SequenceRunner, render_report, save_report

DEFAULT_SEQUENCE = "data/sequence.json"


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = ModelConfig()
    env_file = Path(args.env)
    if env_file.exists():
        print(f"  {env_file} found; keys taken from it: {', '.join(LOADED_ENV) or 'none (all already set in the shell)'}")
    else:
        print(f"  no {env_file} (copy .env.example to {env_file} -- see that file's header)")

    checks: list[tuple[bool, str]] = [
        (sys.version_info >= (3, 11), f"python {sys.version.split()[0]} (need >= 3.11)")
    ]
    for key, needed_by in REQUIRED_BY.items():
        value = os.environ.get(key)
        label = f"{key} -- needed by: {', '.join(needed_by)}"
        if is_placeholder(value):
            print(f"  [PLACEHOLDER] {label} (still template text, not a real value)")
            checks.append((False, None))
            continue
        checks.append((bool(value), label))
    checks.append((Path(args.sequence).exists(), f"{args.sequence} exists"))
    for ok, label in checks:
        if label is not None:
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


def cmd_run(args: argparse.Namespace) -> int:
    """Execute the frozen sequence. Needs ANTHROPIC_API_KEY, and the memory
    agent additionally needs the Iris store endpoint and key."""
    sequence = Sequence.load(args.sequence)
    store = PRStore(args.store)
    problems = validate(sequence, store)
    if problems and not args.force:
        print("the dataset is not usable (pass --force to run anyway):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    config = ModelConfig(
        effort=args.effort,
        max_tokens=args.max_tokens,
        enable_caching=not args.no_cache,
        cache_ttl=args.cache_ttl,
    )
    provider = (
        LocalSourceProvider(args.checkout)
        if args.checkout
        else GitHubSourceProvider(GitHubClient(args.cache), sequence.repo)
    )
    run_dir = Path(args.runs) / args.run_id
    ledger = Ledger(run_dir, args.run_id)
    conventions = read_docs(provider, sequence.style_guide_paths, sequence.frozen_at_sha or "HEAD")
    if not conventions:
        print(
            f"warning: none of {sequence.style_guide_paths} were readable; both "
            "agents will run without a conventions block",
            file=sys.stderr,
        )

    wanted = {"baseline", "memory"} if args.agents == "both" else {args.agents}
    needed = ["ANTHROPIC_API_KEY"]
    if "memory" in wanted:
        needed += [
            "REDIS_AGENT_MEMORY_URL",
            "REDIS_AGENT_MEMORY_API_KEY",
            "REDIS_AGENT_MEMORY_STORE_ID",
        ]
    absent = missing(needed) + (
        [] if args.store_id or "memory" not in wanted else ["--store-id"]
    )
    if absent:
        print(
            "missing credentials: " + ", ".join(absent) + "\n"
            "copy .env.example to .env and fill it in, then re-run "
            "(python3 -m reviewbot doctor shows what is still unset)",
            file=sys.stderr,
        )
        return 1
    baseline = memory = None
    if "baseline" in wanted:
        baseline = BaselineAgent(
            ClaudeClient(config, ledger), provider, conventions=conventions
        )
    if "memory" in wanted:
        memory = MemoryAgent(
            ClaudeClient(config, ledger),
            AgentMemoryClient(
                args.store_id,
                namespace=args.namespace,
                owner_id="memory-agent",
                ledger=ledger,
            ),
            conventions=conventions,
            retrieval_limit=args.retrieval_limit,
            similarity_threshold=args.similarity_threshold,
            distill_writes=not args.no_distill,
        )

    runner = SequenceRunner(
        sequence=sequence,
        store=store,
        ledger=ledger,
        provider=provider,
        config=config,
        baseline=baseline,
        memory=memory,
        gold=load_gold_dir(args.gold),
    )
    report = runner.run()
    print(render_report(report))
    print(f"\nwrote {save_report(report, run_dir)}")
    return 0


def cmd_memcheck(args: argparse.Namespace) -> int:
    """Verify the Agent Memory store is usable before spending review tokens."""
    absent = missing(
        ["REDIS_AGENT_MEMORY_URL", "REDIS_AGENT_MEMORY_API_KEY", "REDIS_AGENT_MEMORY_STORE_ID"]
    )
    if absent and not args.store_id:
        print("missing credentials: " + ", ".join(absent), file=sys.stderr)
        return 1
    client = AgentMemoryClient(
        args.store_id or os.environ.get("REDIS_AGENT_MEMORY_STORE_ID", ""),
        namespace=args.namespace,
    )
    result = preflight.run(client, timeout=args.timeout)
    print(result.render())
    print()
    print("store is ready" if result.ready else "store is NOT ready to run the memory agent")
    return 0 if result.ready else 1


def cmd_curate(args: argparse.Namespace) -> int:
    """Scan redis-py, apply the stated selection rule, write a candidate sequence."""
    if missing(["GITHUB_TOKEN"]):
        print("missing credentials: GITHUB_TOKEN", file=sys.stderr)
        return 1
    client = GitHubClient(args.cache)
    spine = args.spine.split(",") if args.spine else curate.DEFAULT_SPINE
    candidates = curate.scan(client, args.repo, spine=spine, pages=args.pages)
    selected, stats = curate.select(candidates, spine=spine, target=args.target)
    print(curate.report(selected, stats, spine))
    if stats["style_guide_candidates"]:
        print(
            "\nstyle-guide files seen in the scanned window: "
            + ", ".join(stats["style_guide_candidates"])
        )
    if not selected:
        print("\nnothing selected -- widen --pages or the spine", file=sys.stderr)
        return 1

    sequence = curate.build_sequence(args.repo, selected, spine=spine)
    out = Path(args.out)
    if out.exists() and not args.force:
        print(
            f"\n{out} already exists; refusing to overwrite a frozen sequence "
            "(pass --force)",
            file=sys.stderr,
        )
        return 1
    sequence.save(out)
    print(f"\nwrote {out} ({len(sequence)} PRs, frozen at {sequence.frozen_at_sha[:12]})")

    store = PRStore(args.store)
    prs = store.ingest(client, args.repo, [e.pr_number for e in sequence])
    print(
        f"ingested {len(prs)} PRs ({client.requests_made} API requests, "
        f"{client.cache_hits} cache hits)"
    )
    problems = validate(sequence, store)
    if problems:
        print("\nstill to do before this sequence is usable:")
        for p in problems:
            print(f"  - {p}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reviewbot", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="check the environment and print the config fingerprint")
    d.add_argument("--env", default=DEFAULT_ENV_FILE)
    d.add_argument("--sequence", default=DEFAULT_SEQUENCE)
    d.set_defaults(func=cmd_doctor)

    i = sub.add_parser("ingest", help="fetch PRs from GitHub into the on-disk store")
    i.add_argument("--repo", default="redis/redis-py")
    i.add_argument("--prs", default="", help="comma-separated PR numbers; default: the sequence")
    i.add_argument("--sequence", default=DEFAULT_SEQUENCE)
    i.add_argument("--store", default="data/prs")
    i.add_argument("--cache", default="data/cache/github")
    i.add_argument("--refresh", action="store_true", help="re-fetch even if cached")
    i.set_defaults(func=cmd_ingest)

    cu = sub.add_parser("curate", help="scan the repo and propose a frozen PR sequence")
    cu.add_argument("--repo", default="redis/redis-py")
    cu.add_argument("--spine", default="", help="comma-separated paths; default: the connection/cluster spine")
    cu.add_argument("--pages", type=int, default=3, help="pages of 100 closed PRs to scan")
    cu.add_argument("--target", type=int, default=18, help="sequence length (spec calls for 15-25)")
    cu.add_argument("--out", default=DEFAULT_SEQUENCE)
    cu.add_argument("--store", default="data/prs")
    cu.add_argument("--cache", default="data/cache/github")
    cu.add_argument("--force", action="store_true", help="overwrite an existing sequence.json")
    cu.set_defaults(func=cmd_curate)

    ds = sub.add_parser("dataset", help="validate or describe the frozen sequence")
    ds.add_argument("action", choices=["validate", "table", "summary"])
    ds.add_argument("--sequence", default=DEFAULT_SEQUENCE)
    ds.add_argument("--store", default="data/prs")
    ds.set_defaults(func=cmd_dataset)

    run = sub.add_parser("run", help="execute the frozen sequence for both agents")
    run.add_argument("run_id")
    run.add_argument("--sequence", default=DEFAULT_SEQUENCE)
    run.add_argument("--store", default="data/prs")
    run.add_argument("--cache", default="data/cache/github")
    run.add_argument("--runs", default="runs")
    run.add_argument("--gold", default="data/gold")
    run.add_argument(
        "--checkout",
        default="",
        help="path to a redis-py checkout; reads source at the pinned SHA via git "
        "instead of the GitHub contents API",
    )
    run.add_argument("--agents", choices=["both", "baseline", "memory"], default="both")
    run.add_argument(
        "--namespace",
        default=os.environ.get("REVIEWBOT_NAMESPACE", "redis-py-run-1"),
        help="per-run memory namespace; dashes only (the service rejects slashes)",
    )
    run.add_argument(
        "--store-id",
        default=os.environ.get("REDIS_AGENT_MEMORY_STORE_ID", ""),
        help="Agent Memory store id from Redis Iris (default: $REDIS_AGENT_MEMORY_STORE_ID)",
    )
    run.add_argument("--effort", default="xhigh", choices=["low", "medium", "high", "xhigh", "max"])
    run.add_argument("--max-tokens", type=int, default=32000)
    run.add_argument("--cache-ttl", default="5m", choices=["5m", "1h"])
    run.add_argument("--no-cache", action="store_true", help="disable prompt caching on BOTH agents")
    run.add_argument("--retrieval-limit", type=int, default=20)
    run.add_argument("--similarity-threshold", type=float, default=None)
    run.add_argument("--no-distill", action="store_true", help="write findings verbatim (zero write tokens)")
    run.add_argument("--force", action="store_true", help="run even if the dataset does not validate")
    run.set_defaults(func=cmd_run)

    mc = sub.add_parser("memcheck", help="verify the Agent Memory store is provisioned and usable")
    mc.add_argument("--store-id", default="")
    mc.add_argument("--namespace", default=os.environ.get("REVIEWBOT_NAMESPACE", "redis-py-run-1"))
    mc.add_argument("--timeout", type=float, default=30.0)
    mc.set_defaults(func=cmd_memcheck)

    r = sub.add_parser("report", help="aggregate a run's ledger into the headline numbers")
    r.add_argument("run", help="run directory containing calls.jsonl")
    r.set_defaults(func=cmd_report)
    return p


LOADED_ENV: list[str] = []


def main(argv: list[str] | None = None, *, env_file: str | None = DEFAULT_ENV_FILE) -> int:
    """`env_file=None` skips loading .env.

    Explicit rather than implicit because loading mutates os.environ for the
    whole process: an in-process caller (a test) would otherwise pick up real
    credentials and make live calls.
    """
    if env_file:
        # Before parsing, so argument defaults that read the environment see it.
        LOADED_ENV.extend(load_env(env_file))
    args = build_parser().parse_args(argv)
    return args.func(args)
