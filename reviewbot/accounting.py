"""Token accounting.

The one thing this module exists to prevent (spec 7): reporting
`usage.input_tokens` as "input tokens per review". That field is the *uncached
remainder only*. With caching on it measures cache misses, not context volume,
and it flatters whichever agent caches better.

    context volume = input_tokens + cache_creation + cache_read
    billed cost    = the same tokens weighted by their price multipliers

Both series are reported. Every model call and every memory operation lands in
an append-only JSONL ledger tagged {agent, pr_id, phase}; an untagged call is
an unmeasurable one, so tagging is a constructor argument, not an option.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import Pricing, pricing_for

MODEL_CALL = "model_call"
MEMORY_OP = "memory_op"


@dataclass(frozen=True)
class Usage:
    """The four fields that matter, plus the per-TTL cache-write breakdown.

    Recent API responses carry `usage.cache_creation` as an object splitting
    writes by TTL. When present we price each bucket at its own multiplier
    instead of assuming the configured TTL applied.
    """

    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_5m: int = 0
    cache_creation_1h: int = 0

    @classmethod
    def from_response(cls, usage: dict[str, Any] | None) -> "Usage":
        u = usage or {}
        breakdown = u.get("cache_creation") or {}
        five = int(breakdown.get("ephemeral_5m_input_tokens", 0) or 0)
        hour = int(breakdown.get("ephemeral_1h_input_tokens", 0) or 0)
        return cls(
            input_tokens=int(u.get("input_tokens", 0) or 0),
            cache_creation_input_tokens=int(u.get("cache_creation_input_tokens", 0) or 0),
            cache_read_input_tokens=int(u.get("cache_read_input_tokens", 0) or 0),
            output_tokens=int(u.get("output_tokens", 0) or 0),
            cache_creation_5m=five,
            cache_creation_1h=hour,
        )

    def merge(self, other: "Usage") -> "Usage":
        """Fold a streaming message_delta usage into the message_start usage.

        Input-side fields are only reported on message_start; output_tokens
        grows and the last value wins.
        """
        return Usage(
            input_tokens=self.input_tokens or other.input_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens
            or other.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens
            or other.cache_read_input_tokens,
            output_tokens=max(self.output_tokens, other.output_tokens),
            cache_creation_5m=self.cache_creation_5m or other.cache_creation_5m,
            cache_creation_1h=self.cache_creation_1h or other.cache_creation_1h,
        )

    @property
    def context_volume(self) -> int:
        """Full prompt size: what "the agent reads less" actually claims.

        Caching-independent by construction.
        """
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    def billed_usd(self, pricing: Pricing, cache_ttl: str = "5m") -> float:
        write_5m = self.cache_creation_5m
        write_1h = self.cache_creation_1h
        if not (write_5m or write_1h) and self.cache_creation_input_tokens:
            # No per-TTL breakdown in the response; attribute to the TTL we asked for.
            if cache_ttl == "1h":
                write_1h = self.cache_creation_input_tokens
            else:
                write_5m = self.cache_creation_input_tokens
        return (
            self.input_tokens * pricing.input_per_token
            + write_5m * pricing.input_per_token * pricing.cache_write_5m_multiplier
            + write_1h * pricing.input_per_token * pricing.cache_write_1h_multiplier
            + self.cache_read_input_tokens
            * pricing.input_per_token
            * pricing.cache_read_multiplier
            + self.output_tokens * pricing.output_per_token
        )


@dataclass
class CallRecord:
    """One row of the ledger.

    `kind` distinguishes billable model calls from memory-service operations.
    Memory ops cost wall-clock and (for searches) contribute tokens to the
    review prompt, but they are not themselves billed by the model API --
    their `injected_tokens` is attribution metadata for spec 7b, already
    included in the review call's context volume. Adding it to a total would
    double-count.
    """

    run_id: str
    agent: str
    pr_id: str
    pr_ordinal: int
    phase: str
    kind: str = MODEL_CALL
    seq: int = 0
    ts: float = 0.0
    latency_ms: int = 0

    # model_call fields
    model: str | None = None
    effort: str | None = None
    cache_ttl: str = "5m"
    usage: Usage = field(default_factory=Usage)
    stop_reason: str | None = None
    prefix_id: str | None = None
    truncated: bool = False

    # memory_op fields (spec 7b)
    memory_op: str | None = None  # search | create | update | delete | wait | measure
    memories_returned: int | None = None
    injected_tokens: int | None = None
    search_limit: int | None = None
    similarity_threshold: float | None = None

    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def billable(self) -> bool:
        return self.kind == MODEL_CALL

    @property
    def context_volume(self) -> int:
        return self.usage.context_volume if self.billable else 0

    def billed_usd(self) -> float:
        if not self.billable:
            return 0.0
        return self.usage.billed_usd(pricing_for(self.model or ""), self.cache_ttl)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["billable"] = self.billable
        d["context_volume"] = self.context_volume
        d["billed_usd"] = self.billed_usd()
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "CallRecord":
        d = dict(d)
        for derived in ("billable", "context_volume", "billed_usd"):
            d.pop(derived, None)
        usage = d.pop("usage", None) or {}
        rec = cls(**d)
        return replace(rec, usage=Usage(**usage))


class Ledger:
    """Append-only JSONL ledger, one file per run.

    Append-only on purpose: a rerun that overwrites history cannot be audited,
    and the whole point of this layer is that a skeptic can re-add the numbers.
    """

    def __init__(
        self,
        run_dir: str | os.PathLike[str],
        run_id: str,
        *,
        manifest: dict[str, Any] | None = None,
    ):
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "calls.jsonl"
        self.manifest_path = self.run_dir / "manifest.json"
        self._seq = self._resume_seq()
        if manifest is not None:
            self.write_manifest(manifest)

    def _resume_seq(self) -> int:
        if not self.path.exists():
            return 0
        last = 0
        with self.path.open() as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        last = max(last, int(json.loads(line).get("seq", 0)))
                    except (ValueError, json.JSONDecodeError):
                        continue
        return last

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        self.manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )

    def read_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {}
        return json.loads(self.manifest_path.read_text())

    def record(self, rec: CallRecord) -> CallRecord:
        self._seq += 1
        rec.seq = self._seq
        rec.run_id = rec.run_id or self.run_id
        rec.ts = rec.ts or time.time()
        with self.path.open("a") as fh:
            fh.write(json.dumps(rec.to_json(), sort_keys=True) + "\n")
        return rec

    def records(self) -> Iterator[CallRecord]:
        if not self.path.exists():
            return
        with self.path.open() as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield CallRecord.from_json(json.loads(line))


def load_records(path: str | os.PathLike[str]) -> list[CallRecord]:
    out: list[CallRecord] = []
    with Path(path).open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(CallRecord.from_json(json.loads(line)))
    return out


def total_context_volume(records: Iterable[CallRecord]) -> int:
    return sum(r.context_volume for r in records)


def total_billed_usd(records: Iterable[CallRecord]) -> float:
    return sum(r.billed_usd() for r in records)
