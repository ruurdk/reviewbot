"""Messages API client (stdlib only).

Why raw HTTP instead of the `anthropic` SDK: this machine has no package
manager (no pip, no ensurepip, and files.pythonhosted.org is unreachable), so
the SDK cannot be installed here. The wire surface is small and stable, and
owning the serialisation has a real upside for this experiment -- prompt
caching is a byte-exact prefix match, so controlling exactly what goes on the
wire is how we can hash the cached prefix and *prove* the cache behaved. See
`SDK_SWAP` below for the migration note.

SDK_SWAP: `ClaudeClient.messages()` mirrors `client.messages.create()` /
`client.messages.parse()`. To move to the SDK, reimplement `_send()` on top of
it and keep the ledger plumbing; nothing above this module knows the difference.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Sequence

from .accounting import MODEL_CALL, CallRecord, Ledger, Usage
from .config import ModelConfig

API_BASE = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504, 529})

# transport(url, headers, body, timeout) -> (status, headers, line_iterator)
Transport = Callable[[str, dict, bytes, float], tuple[int, dict, Iterable[bytes]]]


class ClaudeError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:800]}")
        self.status = status
        self.body = body


class Refusal(RuntimeError):
    """stop_reason == "refusal". Claude Opus 5's safety classifiers can decline
    a request with HTTP 200, so this must be handled before reading content --
    an unhandled refusal would silently log a review with zero findings."""


class Truncated(RuntimeError):
    """stop_reason == "max_tokens": the structured output never closed.

    A truncated review is not a review with fewer findings, it is unparseable --
    `output_config.format` guarantees valid JSON only for a *completed*
    response. Raising here is the difference between "the ceiling was too low
    for PR N" and a bare JSONDecodeError forty frames down."""


@dataclass
class Tags:
    """Required on every call. Spec 7: an untagged call is an unmeasurable one."""

    agent: str
    pr_id: str
    pr_ordinal: int
    phase: str


@dataclass
class ClaudeResult:
    text: str
    thinking: str
    stop_reason: str | None
    usage: Usage
    record: CallRecord
    raw: dict[str, Any] | None = None

    @property
    def truncated(self) -> bool:
        return self.stop_reason == "max_tokens"

    def json(self) -> Any:
        """Parse the response as JSON.

        `output_config.format` guarantees valid JSON for a completed response,
        so the two failure modes worth naming are a truncated one and an empty
        one -- both of which arrive as HTTP 200.
        """
        if self.truncated:
            raise Truncated(
                f"{self.record.agent}/{self.record.pr_id}/{self.record.phase} hit "
                f"max_tokens ({self.usage.output_tokens:,} output tokens) and its "
                "JSON never closed. At xhigh, thinking and response text share the "
                "ceiling. Raise --max-tokens, or lower --effort, and re-run: the "
                "checkpoint keeps the PRs already reviewed."
            )
        if not self.text.strip():
            raise Truncated(
                f"{self.record.agent}/{self.record.pr_id}/{self.record.phase} "
                f"returned no text (stop_reason={self.stop_reason!r}); there is "
                "nothing to parse. Reporting this as an empty review would credit "
                "the agent with zero findings it never made."
            )
        return json.loads(self.text)


def _urllib_transport(
    url: str, headers: dict, body: bytes, timeout: float
) -> tuple[int, dict, Iterable[bytes]]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, dict(resp.headers), resp
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a useful body
        return exc.code, dict(exc.headers or {}), [exc.read()]


def prefix_id(payload: dict[str, Any]) -> str | None:
    """Hash the cacheable prefix of a request.

    Render order is tools -> system -> messages, and a cache_control breakpoint
    caches everything before and including its own block. So the prefix is
    every block up to and including the *last* breakpoint. Hashing it gives two
    things: a key for detecting cross-PR cache reads (spec 7d repricing), and a
    tripwire for the silent-invalidator trap -- if the same prefix_id recurs and
    cache_read_input_tokens is still zero, something in the prefix is not
    byte-stable.

    Returns None when the request has no breakpoint.
    """
    parts: list[Any] = []
    found = False

    def scan(blocks: Sequence[Any], label: str) -> None:
        nonlocal found
        for i, block in enumerate(blocks):
            parts.append((label, i, block))
            if isinstance(block, dict) and block.get("cache_control"):
                found = True

    tools = payload.get("tools") or []
    scan(tools, "tools")
    system = payload.get("system")
    if isinstance(system, str):
        parts.append(("system", 0, system))
    elif system:
        scan(system, "system")
    for mi, msg in enumerate(payload.get("messages") or []):
        content = msg.get("content")
        if isinstance(content, str):
            parts.append((f"messages[{mi}]", 0, content))
        elif content:
            scan(content, f"messages[{mi}]")

    if not found:
        return None
    # Truncate at the last breakpoint: content after it is not part of the key.
    last = max(
        i
        for i, (_, _, b) in enumerate(parts)
        if isinstance(b, dict) and b.get("cache_control")
    )
    canonical = json.dumps(parts[: last + 1], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


class ClaudeClient:
    def __init__(
        self,
        config: ModelConfig,
        ledger: Ledger | None = None,
        *,
        api_key: str | None = None,
        base_url: str = API_BASE,
        transport: Transport | None = None,
        timeout: float = 900.0,
        max_retries: int = 5,
    ):
        self.config = config
        self.ledger = ledger
        self.base_url = base_url.rstrip("/")
        self.transport = transport or _urllib_transport
        self.timeout = timeout
        self.max_retries = max_retries
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        # prefix_id -> (pr_ordinal, seq) of the call that last wrote that prefix
        self.cache_writes: dict[str, tuple[int, int]] = {}
        self.cache_warnings: list[str] = []

    # -- request construction -------------------------------------------------

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise ClaudeError(
                0,
                "ANTHROPIC_API_KEY is not set. The harness will not estimate "
                "tokens in its absence -- exact first-party usage is the point.",
            )
        return {
            "content-type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
        }

    def build_payload(
        self,
        *,
        system: Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        output_schema: dict[str, Any] | None = None,
        stream: bool | None = None,
    ) -> dict[str, Any]:
        payload = dict(self.config.request_params())
        payload["system"] = system
        payload["messages"] = messages
        if tools:
            # Deterministic order: tool-list order is part of the cache prefix.
            payload["tools"] = sorted(tools, key=lambda t: t["name"])
        if output_schema is not None:
            payload.setdefault("output_config", {})["format"] = {
                "type": "json_schema",
                "schema": output_schema,
            }
        do_stream = self.config.stream if stream is None else stream
        if do_stream:
            payload["stream"] = True
        return payload

    # -- the call -------------------------------------------------------------

    def messages(
        self,
        tags: Tags,
        *,
        system: Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        output_schema: dict[str, Any] | None = None,
        stream: bool | None = None,
        notes: dict[str, Any] | None = None,
    ) -> ClaudeResult:
        payload = self.build_payload(
            system=system,
            messages=messages,
            tools=tools,
            output_schema=output_schema,
            stream=stream,
        )
        pid = prefix_id(payload)
        started = time.monotonic()
        status, _, body = self._send("/v1/messages", payload)
        # Time to first byte only: on a streamed request `_send` returns as soon
        # as the headers land, and the response body is still arriving.
        ttfb_ms = int((time.monotonic() - started) * 1000)

        if payload.get("stream"):
            text, thinking, stop_reason, usage, raw = _parse_sse(body)
        else:
            raw = json.loads(b"".join(body).decode())
            text, thinking, stop_reason, usage = _parse_message(raw)
        # Measured after the stream is drained, or a 38k-output review that took
        # seven minutes gets logged as six seconds -- which is what happened.
        latency_ms = int((time.monotonic() - started) * 1000)

        rec = CallRecord(
            run_id=self.ledger.run_id if self.ledger else "",
            agent=tags.agent,
            pr_id=tags.pr_id,
            pr_ordinal=tags.pr_ordinal,
            phase=tags.phase,
            kind=MODEL_CALL,
            latency_ms=latency_ms,
            ttfb_ms=ttfb_ms,
            model=self.config.model,
            effort=self.config.effort,
            max_tokens=self.config.max_tokens,
            cache_ttl=self.config.cache_ttl,
            usage=usage,
            stop_reason=stop_reason,
            prefix_id=pid,
            truncated=stop_reason == "max_tokens",
            notes=dict(notes or {}),
        )
        self._audit_cache(rec)
        if self.ledger:
            self.ledger.record(rec)
        if stop_reason == "refusal":
            raise Refusal(
                f"model declined {tags.agent}/{tags.pr_id}/{tags.phase}; "
                "the call is logged but produced no review"
            )
        return ClaudeResult(text, thinking, stop_reason, usage, rec, raw)

    def count_tokens(
        self, *, system: Any, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> int:
        """Exact token count for a prospective prompt.

        Used to measure how many tokens retrieved memories add to the review
        prompt (spec 7b). Never substitute a third-party tokenizer: tiktoken is
        OpenAI's and undercounts Claude badly on code, which a diff is.
        """
        payload: dict[str, Any] = {
            "model": self.config.model,
            "system": system,
            "messages": messages,
        }
        if tools:
            payload["tools"] = sorted(tools, key=lambda t: t["name"])
        _, _, body = self._send("/v1/messages/count_tokens", payload)
        return int(json.loads(b"".join(body).decode())["input_tokens"])

    # -- plumbing -------------------------------------------------------------

    def _send(
        self, path: str, payload: dict[str, Any]
    ) -> tuple[int, dict, Iterable[bytes]]:
        url = f"{self.base_url}{path}"
        body = json.dumps(payload).encode()
        headers = self._headers()
        delay = 1.0
        last: ClaudeError | None = None
        for attempt in range(self.max_retries + 1):
            status, resp_headers, stream = self.transport(url, headers, body, self.timeout)
            if 200 <= status < 300:
                return status, resp_headers, stream
            text = b"".join(stream).decode(errors="replace")
            last = ClaudeError(status, text)
            if status not in RETRY_STATUSES or attempt == self.max_retries:
                raise last
            retry_after = resp_headers.get("retry-after") or resp_headers.get("Retry-After")
            wait = float(retry_after) if retry_after else delay
            time.sleep(wait)
            delay = min(delay * 2, 60.0)
        raise last  # unreachable

    def _audit_cache(self, rec: CallRecord) -> None:
        """Catch the two caching traps before they corrupt a cost number."""
        if not (self.config.enable_caching and rec.prefix_id):
            return
        prior = self.cache_writes.get(rec.prefix_id)
        if rec.usage.cache_creation_input_tokens:
            self.cache_writes[rec.prefix_id] = (rec.pr_ordinal, rec.seq)
        elif prior is not None and rec.usage.cache_read_input_tokens == 0:
            self.cache_warnings.append(
                f"prefix {rec.prefix_id} recurred for {rec.agent} at PR "
                f"ordinal {rec.pr_ordinal} but cache_read_input_tokens is 0 -- "
                "something in the prefix is not byte-stable"
            )


def _parse_message(raw: dict[str, Any]) -> tuple[str, str, str | None, Usage]:
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    for block in raw.get("content") or []:
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "thinking":
            thinking_parts.append(block.get("thinking", "") or "")
    return (
        "".join(text_parts),
        "".join(thinking_parts),
        raw.get("stop_reason"),
        Usage.from_response(raw.get("usage")),
    )


def _iter_lines(body: Iterable[bytes]) -> Iterator[bytes]:
    if hasattr(body, "readline"):
        for line in body:  # file-like objects iterate by line
            yield line
        return
    buf = b""
    for chunk in body:
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line + b"\n"
    if buf:
        yield buf


def _parse_sse(
    body: Iterable[bytes],
) -> tuple[str, str, str | None, Usage, dict[str, Any]]:
    """Accumulate a streamed message.

    Streaming is the default: at xhigh effort on a large diff a single review
    can run for minutes, and a non-streamed request risks a read timeout.
    Input-side usage arrives once on message_start; output_tokens grows and the
    final value lands on message_delta.
    """
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    usage = Usage()
    stop_reason: str | None = None
    events: list[str] = []

    for rawline in _iter_lines(body):
        line = rawline.decode(errors="replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            evt = json.loads(payload)
        except json.JSONDecodeError:
            continue
        etype = evt.get("type", "")
        events.append(etype)
        if etype == "message_start":
            usage = usage.merge(Usage.from_response(evt.get("message", {}).get("usage")))
        elif etype == "content_block_delta":
            delta = evt.get("delta", {})
            if delta.get("type") == "text_delta":
                text_parts.append(delta.get("text", ""))
            elif delta.get("type") == "thinking_delta":
                thinking_parts.append(delta.get("thinking", ""))
        elif etype == "message_delta":
            stop_reason = evt.get("delta", {}).get("stop_reason") or stop_reason
            if evt.get("usage"):
                usage = Usage.from_response(evt["usage"]).merge(usage)
        elif etype == "error":
            raise ClaudeError(200, json.dumps(evt.get("error", evt)))

    raw = {"stop_reason": stop_reason, "events": events}
    return "".join(text_parts), "".join(thinking_parts), stop_reason, usage, raw
