"""In-process fakes standing in for the two external services.

`FakeMemoryService` implements enough of the Agent Memory wire contract --
including filter evaluation, bulk-error reporting, the guarded field update, and
optional eventual-consistency delay -- that the harness's filter construction
and write-visibility handling are actually exercised rather than mocked away.
"""

from __future__ import annotations

import json
from typing import Any


class FakeMemoryService:
    def __init__(self, *, visibility_lag: int = 0, reject_ids: set[str] | None = None):
        self.records: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, int] = {}
        self.visibility_lag = visibility_lag
        self.reject_ids = reject_ids or set()
        self.calls: list[tuple[str, str, Any]] = []
        self.clock = 0

    # transport signature: (method, url, headers, body, timeout)
    def transport(self, method, url, headers, body, timeout):
        payload = json.loads(body) if body else None
        path = url.split("/v1/stores/", 1)[1].split("/", 1)[1]
        self.calls.append((method, path, payload))
        handler = {
            ("POST", "long-term-memory"): self._create,
            ("POST", "long-term-memory/search"): self._search,
            ("DELETE", "long-term-memory"): self._delete,
        }.get((method, path))
        if handler:
            return self._ok(handler(payload))
        if path.startswith("long-term-memory/") and path.endswith("/fields"):
            return self._patch_fields(path.split("/")[1], payload)
        if method == "GET" and path.startswith("long-term-memory/"):
            return self._get(path.split("/", 1)[1])
        return 404, {}, b'{"message":"no route"}'

    def _ok(self, body):
        return 200, {}, json.dumps(body).encode()

    def _create(self, payload):
        created, errors = [], []
        for rec in payload["memories"]:
            if rec["id"] in self.reject_ids:
                errors.append({"id": rec["id"], "error": "simulated rejection"})
                continue
            stored = dict(rec)
            stored["createdAt"] = "2026-01-01T00:00:00Z"  # server-assigned
            stored["updatedAt"] = stored["createdAt"]
            self.records[rec["id"]] = stored
            if self.visibility_lag:
                self.pending[rec["id"]] = self.visibility_lag
            created.append(rec["id"])
        return {"created": created, "errors": errors}

    def _visible(self, mid):
        remaining = self.pending.get(mid, 0)
        if remaining <= 0:
            return True
        self.pending[mid] = remaining - 1
        return False

    def _get(self, mid):
        if mid not in self.records or not self._visible(mid):
            return 404, {}, b'{"message":"not found"}'
        return self._ok(self.records[mid])

    def _matches(self, rec, flt, op):
        def clause(value, spec):
            for key, want in spec.items():
                if key == "eq" and value != want:
                    return False
                if key == "ne" and value == want:
                    return False
                if key == "in":
                    if isinstance(value, list):
                        if not set(value) & set(want):
                            return False
                    elif value not in want:
                        return False
                if key == "all":
                    have = value if isinstance(value, list) else [value]
                    if not set(want) <= set(have):
                        return False
            return True

        results = []
        for key, spec in (flt or {}).items():
            if key == "attributes":
                for attr, aspec in spec.items():
                    results.append(clause((rec.get("attributes") or {}).get(attr), aspec))
            else:
                wire = {"memoryType": "memoryType", "ownerId": "ownerId"}.get(key, key)
                results.append(clause(rec.get(wire), spec))
        if not results:
            return True
        return all(results) if op != "any" else any(results)

    def _search(self, payload):
        text = (payload.get("text") or "").lower()
        terms = {t for t in text.replace(",", " ").split() if len(t) > 3}
        hits = []
        for rec in self.records.values():
            if not self._visible(rec["id"]):
                continue
            if not self._matches(rec, payload.get("filter"), payload.get("filterOp")):
                continue
            body = rec["text"].lower()
            score = sum(1 for t in terms if t in body)
            hits.append((score, rec))
        hits.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
        limit = payload.get("limit", 10)
        return {"items": [r for _, r in hits[:limit]]}

    def _patch_fields(self, mid, payload):
        rec = self.records.get(mid)
        if rec is None:
            return 404, {}, b'{"message":"not found"}'
        if rec.get("memoryType") != payload.get("memoryType") or rec.get(
            "namespace"
        ) != payload.get("namespace"):
            return 409, {}, b'{"message":"type/namespace guard failed"}'
        for key in ("text", "attributes", "topics"):
            if key in payload:
                rec[key] = payload[key]
        rec["updatedAt"] = "2026-01-02T00:00:00Z"
        return self._ok(rec)

    def _delete(self, payload):
        for mid in payload["memoryIds"]:
            self.records.pop(mid, None)
            self.pending.pop(mid, None)
        return {}


class FakeClaude:
    """Messages transport that answers by request shape.

    The harness makes three structurally different calls (primer, review,
    write) plus count_tokens, so routing on the requested output schema keeps
    the fake honest without hard-coding call order.
    """

    def __init__(
        self,
        *,
        facts=None,
        findings=None,
        memories_used=None,
        records=None,
        count_tokens=1_234,
        usage=None,
    ):
        self.facts = facts or []
        self.findings = findings or []
        self.memories_used = memories_used or []
        self.records = records or []
        self.count = count_tokens
        self.usage = usage or {
            "input_tokens": 1_000,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 200,
        }
        self.sent: list[dict] = []
        self.urls: list[str] = []

    def transport(self, url, headers, body, timeout):
        payload = json.loads(body)
        self.sent.append(payload)
        self.urls.append(url)
        if url.endswith("/count_tokens"):
            return 200, {}, [json.dumps({"input_tokens": self.count}).encode()]
        schema = (
            payload.get("output_config", {}).get("format", {}).get("schema", {})
        )
        required = set(schema.get("required") or [])
        if "facts" in required:
            out = {"facts": self.facts}
        elif "records" in required:
            out = {"records": self.records}
        else:
            out = {"findings": self.findings, "memories_used": self.memories_used}
        return (
            200,
            {},
            [
                json.dumps(
                    {
                        "content": [{"type": "text", "text": json.dumps(out)}],
                        "stop_reason": "end_turn",
                        "usage": dict(self.usage),
                    }
                ).encode()
            ],
        )
