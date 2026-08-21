"""Redis Agent Memory client (managed service in Redis Iris).

Built against the published OpenAPI (Redis Agent Memory API 1.0.0). The service
is in preview: pin it, and re-verify this surface before the demo is shown.

The wire surface used here, and nothing else:

    POST   /v1/stores/{storeId}/long-term-memory              bulk create (<=100)
    POST   /v1/stores/{storeId}/long-term-memory/search       text + filter + limit
    GET    /v1/stores/{storeId}/long-term-memory/{memoryId}   read-back
    PATCH  /v1/stores/{storeId}/long-term-memory/{memoryId}/fields   guarded update
    DELETE /v1/stores/{storeId}/long-term-memory              bulk delete (<=100)

Four constraints in that spec will silently corrupt a run if ignored, so they
are enforced here rather than documented:

1. `id` and `namespace` match ^[a-zA-Z0-9-]+$ -- no slashes, dots, or
   underscores. The design doc's example namespace `repo-x/run-3` is illegal;
   use `repo-x-run-3`. `memoryType` is the one field that allows underscores,
   so `repo_convention` is fine.
2. Bulk create returns `created` AND `errors`. A 200 can still mean half the
   records were rejected, so a caller that only checks the status code will
   review PR N+1 against memories that were never stored.
3. Scoped search uses filterOp `all` with `attributes.module: {in: [...]}`.
   filterOp `any` would OR the namespace clause with the module clause and
   return memories from *other runs* -- cross-run contamination that looks like
   a working retrieval.
4. A namespace filter's positive operators (`eq`, `in`) only match records that
   have a namespace; `ne` also matches records without one. Strict per-run
   isolation therefore means `eq`, never `ne`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from .accounting import MEMORY_OP, CallRecord, Ledger
from .claude import Tags

ID_PATTERN = re.compile(r"^[a-zA-Z0-9-]+$")
TYPE_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")
MAX_ID_LEN = 64
MAX_TEXT_LEN = 50_000
MAX_BULK = 100
MAX_SEARCH_LIMIT = 100
MAX_TOPICS = 50

# Custom memory types registered on the store (spec 4d).
REPO_CONVENTION = "repo_convention"
REVIEW_FINDING = "review_finding"
REVIEW_POLICY = "review_policy"
MEMORY_TYPES = (REPO_CONVENTION, REVIEW_FINDING, REVIEW_POLICY)

Transport = Callable[[str, str, dict, bytes | None, float], tuple[int, dict, bytes]]


class MemoryError_(RuntimeError):
    pass


class PartialWrite(MemoryError_):
    """A bulk create returned 200 with per-record errors."""

    def __init__(self, created: list[str], errors: list[dict[str, Any]]):
        super().__init__(
            f"{len(errors)} of {len(created) + len(errors)} memory writes failed: "
            + "; ".join(f"{e.get('id')}: {e.get('error')}" for e in errors[:5])
        )
        self.created = created
        self.errors = errors


class NotVisible(MemoryError_):
    """Written records did not become searchable within the timeout.

    Extraction and indexing are asynchronous and search is eventually
    consistent, so PR N+1 can otherwise read a store that has not settled.
    """


def slug(value: str) -> str:
    """Turn an arbitrary string into an id-safe fragment."""
    return re.sub(r"-+", "-", re.sub(r"[^a-zA-Z0-9]+", "-", value)).strip("-").lower()


def memory_id(*parts: str) -> str:
    """Deterministic, pattern-legal, <=64 chars.

    Client-supplied ids are what make writes idempotent -- re-priming the same
    repo must produce the same record set (spec 4e), so the id has to be a pure
    function of the content it identifies. Long inputs (file paths) are hashed
    rather than truncated, because truncation collides on exactly the paths this
    demo uses: redis/asyncio/cluster.py and redis/cluster.py.
    """
    joined = "-".join(slug(p) for p in parts if p)
    if not joined:
        raise ValueError("memory_id needs at least one non-empty part")
    if len(joined) <= MAX_ID_LEN:
        return joined
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]
    keep = MAX_ID_LEN - len(digest) - 1
    return f"{joined[:keep].rstrip('-')}-{digest}"


def validate_id(value: str) -> str:
    if not (1 <= len(value) <= MAX_ID_LEN) or not ID_PATTERN.match(value):
        raise ValueError(
            f"memory id {value!r} must match {ID_PATTERN.pattern} and be 1-{MAX_ID_LEN} "
            "chars (no slashes, dots, or underscores)"
        )
    return value


def validate_namespace(value: str) -> str:
    if not (1 <= len(value) <= MAX_ID_LEN) or not ID_PATTERN.match(value):
        raise ValueError(
            f"namespace {value!r} must match {ID_PATTERN.pattern}; a namespace like "
            "'repo/run-3' is rejected by the service -- use 'repo-run-3'"
        )
    return value


def validate_memory_type(value: str) -> str:
    if not TYPE_PATTERN.match(value or ""):
        raise ValueError(f"memoryType {value!r} must match {TYPE_PATTERN.pattern}")
    return value


@dataclass
class Memory:
    """One long-term memory record."""

    id: str
    text: str
    memory_type: str
    namespace: str
    owner_id: str
    topics: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        validate_id(self.id)
        validate_namespace(self.namespace)
        validate_memory_type(self.memory_type)
        validate_id(self.owner_id)
        if not self.text.strip():
            raise ValueError(f"memory {self.id} has empty text")
        if len(self.text) > MAX_TEXT_LEN:
            raise ValueError(
                f"memory {self.id} text is {len(self.text)} chars; the service caps "
                f"it at {MAX_TEXT_LEN}. Split the fact into several records."
            )
        if len(self.topics) > MAX_TOPICS:
            raise ValueError(f"memory {self.id} has {len(self.topics)} topics; max {MAX_TOPICS}")

    def to_create(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": self.id,
            "text": self.text,
            "memoryType": self.memory_type,
            "namespace": self.namespace,
            "ownerId": self.owner_id,
        }
        if self.topics:
            body["topics"] = list(self.topics)
        if self.attributes:
            body["attributes"] = dict(self.attributes)
        return body

    @classmethod
    def from_record(cls, d: dict[str, Any]) -> "Memory":
        return cls(
            id=d["id"],
            text=d["text"],
            memory_type=d.get("memoryType") or "",
            namespace=d.get("namespace") or "",
            owner_id=d.get("ownerId") or "",
            topics=list(d.get("topics") or []),
            attributes=dict(d.get("attributes") or {}),
            created_at=d.get("createdAt"),
            updated_at=d.get("updatedAt"),
        )

    @property
    def prompt_text(self) -> str:
        return self.text


def _urllib_transport(
    method: str, url: str, headers: dict, body: bytes | None, timeout: float
) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def scoped_filter(
    namespace: str,
    *,
    memory_types: Sequence[str] | None = None,
    modules: Sequence[str] | None = None,
    attributes: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the retrieval filter for one PR.

    `eq` on namespace (not `ne`), and module OR-ing expressed as `in` inside a
    single clause so the conjunction stays `all`. See the module docstring.
    """
    f: dict[str, Any] = {"namespace": {"eq": validate_namespace(namespace)}}
    if memory_types:
        f["memoryType"] = (
            {"eq": memory_types[0]} if len(memory_types) == 1 else {"in": list(memory_types)}
        )
    attrs: dict[str, Any] = dict(attributes or {})
    if modules:
        attrs["module"] = {"in": list(modules)}
    if attrs:
        f["attributes"] = attrs
    return f


class AgentMemoryClient:
    def __init__(
        self,
        store_id: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        namespace: str = "default",
        owner_id: str = "memory-agent",
        ledger: Ledger | None = None,
        transport: Transport | None = None,
        timeout: float = 60.0,
    ):
        self.store_id = store_id
        self.base_url = (base_url or os.environ.get("REDIS_AGENT_MEMORY_URL") or "").rstrip("/")
        self.api_key = api_key or os.environ.get("REDIS_AGENT_MEMORY_API_KEY")
        self.namespace = validate_namespace(namespace)
        self.owner_id = validate_id(owner_id)
        self.ledger = ledger
        self.transport = transport or _urllib_transport
        self.timeout = timeout

    # -- plumbing -------------------------------------------------------------

    def _url(self, suffix: str) -> str:
        if not self.base_url:
            raise MemoryError_(
                "REDIS_AGENT_MEMORY_URL is not set (the store endpoint from the "
                "Redis Iris console)"
            )
        return f"{self.base_url}/v1/stores/{self.store_id}{suffix}"

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise MemoryError_("REDIS_AGENT_MEMORY_API_KEY is not set")
        return {
            "content-type": "application/json",
            "authorization": f"Bearer {self.api_key}",
            "accept": "application/json",
        }

    def _call(
        self, method: str, suffix: str, payload: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], int]:
        body = json.dumps(payload).encode() if payload is not None else None
        status, _, raw = self.transport(
            method, self._url(suffix), self._headers(), body, self.timeout
        )
        text = raw.decode(errors="replace")
        if not 200 <= status < 300:
            raise MemoryError_(f"{method} {suffix} -> HTTP {status}: {text[:400]}")
        return (json.loads(text) if text.strip() else {}), status

    def log_op(
        self,
        tags: Tags | None,
        op: str,
        latency_ms: int,
        *,
        returned: int | None = None,
        injected: int | None = None,
        limit: int | None = None,
        threshold: float | None = None,
        notes: dict[str, Any] | None = None,
    ) -> None:
        if not (self.ledger and tags):
            return
        self.ledger.record(
            CallRecord(
                run_id=self.ledger.run_id,
                agent=tags.agent,
                pr_id=tags.pr_id,
                pr_ordinal=tags.pr_ordinal,
                phase=tags.phase,
                kind=MEMORY_OP,
                latency_ms=latency_ms,
                memory_op=op,
                memories_returned=returned,
                injected_tokens=injected,
                search_limit=limit,
                similarity_threshold=threshold,
                notes=dict(notes or {}),
            )
        )

    # -- operations -----------------------------------------------------------

    def create(self, memories: Sequence[Memory], tags: Tags | None = None) -> list[str]:
        """Bulk create, batched at the service's 100-record limit.

        Raises PartialWrite when the service reports per-record errors, because
        a silently dropped write means later PRs retrieve an incomplete store.
        """
        created: list[str] = []
        errors: list[dict[str, Any]] = []
        for start in range(0, len(memories), MAX_BULK):
            batch = memories[start : start + MAX_BULK]
            started = time.monotonic()
            body, _ = self._call(
                "POST", "/long-term-memory", {"memories": [m.to_create() for m in batch]}
            )
            latency = int((time.monotonic() - started) * 1000)
            created.extend(body.get("created") or [])
            errors.extend(body.get("errors") or [])
            self.log_op(
                tags,
                "create",
                latency,
                returned=len(body.get("created") or []),
                notes={"batch": len(batch), "errors": len(body.get("errors") or [])},
            )
        if errors:
            raise PartialWrite(created, errors)
        return created

    def search(
        self,
        text: str,
        *,
        filter: dict[str, Any] | None = None,
        limit: int = 10,
        similarity_threshold: float | None = None,
        tags: Tags | None = None,
        page_token: str | None = None,
    ) -> tuple[list[Memory], str | None]:
        if not 1 <= limit <= MAX_SEARCH_LIMIT:
            raise ValueError(f"limit must be 1..{MAX_SEARCH_LIMIT}, got {limit}")
        payload: dict[str, Any] = {"text": text, "limit": limit, "filterOp": "all"}
        if filter:
            payload["filter"] = filter
        if similarity_threshold is not None:
            payload["similarityThreshold"] = similarity_threshold
        if page_token:
            payload["pageToken"] = page_token
        started = time.monotonic()
        body, _ = self._call("POST", "/long-term-memory/search", payload)
        latency = int((time.monotonic() - started) * 1000)
        items = [Memory.from_record(r) for r in body.get("items") or []]
        self.log_op(
            tags,
            "search",
            latency,
            returned=len(items),
            limit=limit,
            threshold=similarity_threshold,
        )
        return items, body.get("nextPageToken")

    def get(self, memory_id: str) -> Memory | None:
        try:
            body, _ = self._call("GET", f"/long-term-memory/{validate_id(memory_id)}")
        except MemoryError_ as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        return Memory.from_record(body)

    def patch_fields(
        self,
        memory_id: str,
        *,
        memory_type: str,
        text: str | None = None,
        attributes: dict[str, Any] | None = None,
        topics: Sequence[str] | None = None,
        tags: Tags | None = None,
    ) -> None:
        """Guarded update -- the service rejects it unless the stored record's
        memoryType and namespace match what we claim. This is the convention
        invalidation path (spec 8/9): correcting a primed convention in place,
        with a guard against overwriting another run's record."""
        payload: dict[str, Any] = {
            "memoryType": validate_memory_type(memory_type),
            "namespace": self.namespace,
        }
        if text is not None:
            payload["text"] = text
        if attributes is not None:
            payload["attributes"] = attributes
        if topics is not None:
            payload["topics"] = list(topics)
        started = time.monotonic()
        self._call("PATCH", f"/long-term-memory/{validate_id(memory_id)}/fields", payload)
        self.log_op(tags, "update", int((time.monotonic() - started) * 1000))

    def delete(self, memory_ids: Sequence[str], tags: Tags | None = None) -> int:
        deleted = 0
        for start in range(0, len(memory_ids), MAX_BULK):
            batch = [validate_id(m) for m in memory_ids[start : start + MAX_BULK]]
            started = time.monotonic()
            self._call("DELETE", "/long-term-memory", {"memoryIds": batch})
            self.log_op(tags, "delete", int((time.monotonic() - started) * 1000), returned=len(batch))
            deleted += len(batch)
        return deleted

    def wait_for_visibility(
        self,
        memory_ids: Sequence[str],
        *,
        timeout: float = 30.0,
        interval: float = 0.5,
        tags: Tags | None = None,
    ) -> float:
        """Poll until every written id is readable back.

        Returns seconds waited. Exclude this from the latency metric -- it is an
        artefact of replaying a sequence with no gaps between PRs, not a cost a
        real reviewer pays (spec: wait for write visibility between PRs).
        """
        pending = {validate_id(m) for m in memory_ids}
        started = time.monotonic()
        while pending and (time.monotonic() - started) < timeout:
            for mid in sorted(pending):
                if self.get(mid) is not None:
                    pending.discard(mid)
            if pending:
                time.sleep(interval)
        waited = time.monotonic() - started
        self.log_op(
            tags,
            "wait",
            int(waited * 1000),
            returned=len(memory_ids) - len(pending),
            notes={"excluded_from_latency": True, "still_pending": sorted(pending)},
        )
        if pending:
            raise NotVisible(
                f"{len(pending)} memory record(s) were not searchable after "
                f"{timeout}s: {sorted(pending)[:5]}"
            )
        return waited

    def reset_namespace(self, ids: Iterable[str], tags: Tags | None = None) -> int:
        """Per-run reset. Deletion is by explicit id list, so the run keeps its
        own manifest of what it wrote rather than trusting a wildcard."""
        return self.delete(list(ids), tags)
