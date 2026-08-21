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
import re
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
WRONG_TYPE = "has the wrong type for memory type"
UNKNOWN_ATTRIBUTE = "unknown attribute"

# Probed with each type's real attribute set, from docs/store-provisioning.md.
# Sending one generic set instead reports a schema mismatch on every type --
# review_policy has no `module` field -- which reads as a store problem when it
# is a probe problem.
PROBE_ATTRIBUTES: dict[str, dict[str, object]] = {
    "repo_convention": {
        "module": PROBE_MODULE,
        "kind": "invariant",
        "topic": "preflight-probe",
        "source": "inferred",
        "convention_version": 1,
        "pr_ordinal": 0,
    },
    "review_finding": {
        "module": PROBE_MODULE,
        "finding_class": "preflight-probe",
        "pr_ordinal": 0,
        "pr_number": 0,
        "source": "review",
    },
    "review_policy": {
        "finding_class": "preflight-probe",
        "action": "suppress",
        "evidence_pr_ordinals": ["000"],
        "source": "inferred",
    },
}

FIELD_ERROR = re.compile(
    r'attribute "(?P<field>[^"]+)"[^(]*(?:\(expected (?P<expected>[a-z\[\]]+)\))?'
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Mismatch:
    memory_type: str
    field_name: str
    problem: str  # wrong_type | unknown_attribute
    expected: str = ""


@dataclass
class Preflight:
    checks: list[Check] = field(default_factory=list)
    visibility_lag_s: float | None = None
    registered_types: list[str] = field(default_factory=list)
    unregistered_types: list[str] = field(default_factory=list)
    mismatches: list[Mismatch] = field(default_factory=list)
    skipped: list[Check] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))

    def skip(self, name: str, reason: str) -> None:
        """Record a check that never ran.

        A silently omitted check is worse than a failed one: the output looks
        complete and the reader cannot tell whether it passed or was skipped.
        """
        self.skipped.append(Check(name, False, reason))

    def render(self) -> str:
        lines: list[str] = []
        for c in self.checks:
            lines.append(f"  [{'ok  ' if c.ok else 'FAIL'}] {c.name}")
            # Detail is never truncated: on a failure it is the only text that
            # says what is actually wrong.
            for chunk in (c.detail or "").splitlines():
                if chunk.strip():
                    lines.append(f"           {chunk.strip()}")
        for c in self.skipped:
            lines.append(f"  [skip] {c.name} -- {c.detail}")
        if self.visibility_lag_s is not None:
            lines.append(
                f"  write->searchable lag: {self.visibility_lag_s:.2f}s "
                "(excluded from the latency metric; the runner waits on it)"
            )
        if self.unregistered_types:
            lines += [
                "",
                "  These memory types are not registered on the store. Register them",
                "  in the Redis Iris console (docs/store-provisioning.md has the",
                "  field definitions); there is no data-plane endpoint for it:",
            ] + [f"    - {t}" for t in self.unregistered_types]
        if self.mismatches:
            lines += [
                "",
                "  The types exist but their fields disagree with what the harness",
                "  writes. Either change the field in the console, or change the",
                "  attribute the harness sends (reviewbot/agents.py) -- they have to",
                "  match exactly, because a mismatch fails the whole create:",
            ]
            for m in self.mismatches:
                if m.problem == "unknown_attribute":
                    lines.append(
                        f"    - {m.memory_type}.{m.field_name}: not a field on this "
                        "type (register it, or stop sending it)"
                    )
                else:
                    expected = m.expected or "a different type"
                    lines.append(
                        f"    - {m.memory_type}.{m.field_name}: store expects "
                        f"{expected}"
                    )
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
                "memory type is registered and its fields match; safe to delete."
            ),
            memory_type=mtype,
            namespace=PROBE_NAMESPACE,
            owner_id=probe.owner_id,
            attributes=dict(PROBE_ATTRIBUTES.get(mtype, {})),
        )
        try:
            created.extend(probe.create([record]))
            pf.registered_types.append(mtype)
            pf.add(f"memory type {mtype}", True, "registered, fields accepted")
            continue
        except (MemoryError_, PartialWrite) as exc:
            message = str(exc)

        if UNREGISTERED in message:
            pf.unregistered_types.append(mtype)
            pf.add(f"memory type {mtype}", False, "not registered on this store")
            continue

        problem = (
            "unknown_attribute"
            if UNKNOWN_ATTRIBUTE in message
            else "wrong_type" if WRONG_TYPE in message else ""
        )
        if problem:
            # The type exists; only its field definitions disagree. Saying
            # "not registered" here would send the reader to re-create a type
            # that is already there.
            pf.registered_types.append(mtype)
            found = FIELD_ERROR.search(message)
            pf.mismatches.append(
                Mismatch(
                    memory_type=mtype,
                    field_name=found.group("field") if found else "?",
                    problem=problem,
                    expected=(found.group("expected") or "") if found else "",
                )
            )
        pf.add(f"memory type {mtype}", False, message)

    if not created and measure_visibility:
        pf.skip(
            "written records become searchable",
            "no probe record could be written, so visibility was not measured",
        )
    if not created:
        pf.skip("probe cleanup", "nothing was written")
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
