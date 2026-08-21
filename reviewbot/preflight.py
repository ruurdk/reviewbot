"""Store preflight: is the memory substrate actually usable?

Verified against the live preview service on 2026-08-21. Findings worth keeping,
because each one fails in a way that is easy to misread:

- Auth is `Authorization: Bearer <key>`; `x-api-key` gets a 403 from nginx.
- A request carrying urllib's default `Python-urllib/3.12` User-Agent is
  rejected by Cloudflare with 403 `error_code: 1010`
  ("browser_signature_banned"), whose body looks nothing like an auth error.
  Any explicit UA works.
- **A custom memory type must be registered on the store before any write.**
  An unregistered type fails every create with
  `400 memory type "x" is not registered on this store`. There is no
  registration endpoint on the data plane (`/memory-types` 404s), so this is a
  console / control-plane action -- see docs/store-provisioning.md.
- The search body **silently ignores unknown fields**: `searchMode`, `mode`, and
  `keyword` all return 200 with no error. So a mistyped or unsupported retrieval
  knob looks like it worked, and keyword/hybrid search cannot be confirmed by
  probing (spec 12).
- `id` and `namespace` are validated server-side to alphanumerics-and-hyphens,
  matching this package's client-side rules.
- Bulk delete validates every id before deleting any: one malformed id rejects
  the whole batch with a 400.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .memory import (
    MEMORY_TYPES,
    AgentMemoryClient,
    Memory,
    MemoryError_,
    PartialWrite,
)

PROBE_NAMESPACE = "harness-preflight"
PROBE_MODULE = "preflight/probe.py"
UNREGISTERED = "is not registered on this store"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Preflight:
    checks: list[Check] = field(default_factory=list)
    visibility_lag_s: float | None = None
    registered_types: list[str] = field(default_factory=list)
    unregistered_types: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))

    def render(self) -> str:
        lines = [
            f"  [{'ok ' if c.ok else 'FAIL'}] {c.name}" + (f" -- {c.detail}" if c.detail else "")
            for c in self.checks
        ]
        if self.visibility_lag_s is not None:
            lines.append(
                f"  write->searchable lag: {self.visibility_lag_s:.2f}s "
                "(excluded from the latency metric; the runner waits on it)"
            )
        if self.unregistered_types:
            lines += [
                "",
                "  Register these memory types on the store before running the",
                "  memory agent (see docs/store-provisioning.md):",
            ] + [f"    - {t}" for t in self.unregistered_types]
        return "\n".join(lines)


def run(client: AgentMemoryClient, *, measure_visibility: bool = True, timeout: float = 30.0) -> Preflight:
    """Probe a store without disturbing experiment data.

    Everything is written to `harness-preflight` and deleted afterwards, so a
    preflight never contaminates a run namespace.
    """
    pf = Preflight()
    probe = AgentMemoryClient(
        client.store_id,
        base_url=client.base_url,
        api_key=client.api_key,
        namespace=PROBE_NAMESPACE,
        owner_id=client.owner_id,
        transport=client.transport,
    )

    try:
        body, _ = probe._call("GET", "/health")
        healthy = str(body.get("status")) == "healthy"
        pf.add("store health", healthy, json.dumps(body.get("features") or body)[:160])
    except MemoryError_ as exc:
        pf.add("store health", False, str(exc)[:200])
        return pf

    created: list[str] = []
    for mtype in MEMORY_TYPES:
        record = Memory(
            id=f"preflight-{mtype.replace('_', '-')}",
            text=(
                "Preflight probe record. Written by reviewbot to verify that this "
                "memory type is registered; safe to delete."
            ),
            memory_type=mtype,
            namespace=PROBE_NAMESPACE,
            owner_id=probe.owner_id,
            attributes={"module": PROBE_MODULE, "pr_ordinal": 0},
        )
        try:
            created.extend(probe.create([record]))
            pf.registered_types.append(mtype)
            pf.add(f"memory type {mtype}", True, "registered")
        except (MemoryError_, PartialWrite) as exc:
            message = str(exc)
            pf.unregistered_types.append(mtype)
            pf.add(
                f"memory type {mtype}",
                False,
                "not registered on this store" if UNREGISTERED in message else message[:160],
            )

    if created and measure_visibility:
        started = time.monotonic()
        lag: float | None = None
        while (time.monotonic() - started) < timeout:
            found, _ = probe.search(
                "Preflight probe record",
                filter={"namespace": {"eq": PROBE_NAMESPACE}},
                limit=10,
            )
            if any(m.id in created for m in found):
                lag = time.monotonic() - started
                break
            time.sleep(0.5)
        pf.visibility_lag_s = lag
        pf.add(
            "written records become searchable",
            lag is not None,
            "" if lag is not None else f"not searchable within {timeout:.0f}s",
        )

    if created:
        try:
            probe.delete(created)
            pf.add("probe cleanup", True, f"deleted {len(created)} record(s)")
        except MemoryError_ as exc:
            pf.add("probe cleanup", False, str(exc)[:160])
    return pf
