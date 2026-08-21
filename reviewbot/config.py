"""Frozen run configuration.

Spec 4f: anything that differs between the two agents beyond the memory layer
is a confound. So the model configuration is built once per run, hashed, and
handed to both agents. `ModelConfig.fingerprint()` is recorded in every run
manifest; `assert_comparable()` refuses to compare two runs whose fingerprints
disagree. That turns "we promise the configs matched" into a check.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Effort = Literal["low", "medium", "high", "xhigh", "max"]
CacheTTL = Literal["5m", "1h"]
Phase = Literal["prime", "retrieve", "review", "write"]
Agent = Literal["baseline", "memory"]

PHASES: tuple[str, ...] = ("prime", "retrieve", "review", "write")
AGENTS: tuple[str, ...] = ("baseline", "memory")


@dataclass(frozen=True)
class Pricing:
    """USD per token. Multipliers are relative to the input rate."""

    model: str
    input_per_token: float
    output_per_token: float
    cache_write_5m_multiplier: float = 1.25
    cache_write_1h_multiplier: float = 2.0
    cache_read_multiplier: float = 0.1

    def cache_write_multiplier(self, ttl: str) -> float:
        if ttl == "1h":
            return self.cache_write_1h_multiplier
        if ttl == "5m":
            return self.cache_write_5m_multiplier
        raise ValueError(f"unknown cache ttl {ttl!r}")


# $/MTok from the model catalogue, converted to $/token.
PRICING: dict[str, Pricing] = {
    "claude-opus-5": Pricing("claude-opus-5", 5.0 / 1e6, 25.0 / 1e6),
    "claude-opus-4-8": Pricing("claude-opus-4-8", 5.0 / 1e6, 25.0 / 1e6),
    "claude-sonnet-5": Pricing("claude-sonnet-5", 3.0 / 1e6, 15.0 / 1e6),
    "claude-haiku-4-5": Pricing("claude-haiku-4-5", 1.0 / 1e6, 5.0 / 1e6),
}

# Minimum cacheable prefix length. A cache_control breakpoint on a shorter
# prefix is silently ignored by the API, which looks identical to a caching
# bug -- so the prompt builder checks against this instead of guessing.
CACHE_MIN_TOKENS: dict[str, int] = {
    "claude-opus-5": 512,
    "claude-opus-4-8": 1024,
    "claude-sonnet-5": 1024,
    "claude-haiku-4-5": 2048,
}


def pricing_for(model: str) -> Pricing:
    try:
        return PRICING[model]
    except KeyError:
        raise KeyError(
            f"no pricing for {model!r}; add it to PRICING rather than guessing"
        ) from None


@dataclass(frozen=True)
class ModelConfig:
    """Identical for both agents, by construction.

    `thinking=None` means the parameter is omitted from the request, which on
    claude-opus-5 runs adaptive thinking (it is on by default on this model,
    unlike Opus 4.8). Thinking tokens are billed and count against max_tokens
    together with the response text.
    """

    model: str = "claude-opus-5"
    effort: Effort = "xhigh"
    max_tokens: int = 32000
    thinking: dict[str, Any] | None = field(
        default_factory=lambda: {"type": "adaptive", "display": "summarized"}
    )
    enable_caching: bool = True
    cache_ttl: CacheTTL = "5m"
    stream: bool = True
    # Bumped whenever the shared system prompt or findings schema changes, so a
    # fingerprint mismatch is loud instead of subtle.
    prompt_version: str = "1"
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if self.model not in PRICING:
            raise ValueError(f"unpriced model {self.model!r}")
        if self.thinking is not None and "budget_tokens" in self.thinking:
            # budget_tokens is rejected with a 400 on this model family.
            raise ValueError("budget_tokens is not accepted on claude-opus-5; use adaptive")
        if self.thinking == {"type": "disabled"} and self.effort in ("xhigh", "max"):
            raise ValueError("thinking disabled is a 400 at effort xhigh/max")

    @property
    def pricing(self) -> Pricing:
        return pricing_for(self.model)

    @property
    def cache_min_tokens(self) -> int:
        return CACHE_MIN_TOKENS.get(self.model, 1024)

    def canonical(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()[:16]

    def request_params(self) -> dict[str, Any]:
        """The confound-bearing half of a Messages request body.

        Deliberately excludes system/messages: those differ per phase, while
        everything here must be byte-identical across both agents.
        """
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "output_config": {"effort": self.effort},
        }
        if self.thinking is not None:
            body["thinking"] = dict(self.thinking)
        return body


class ConfoundError(AssertionError):
    """Raised when two runs cannot be honestly compared."""


def assert_comparable(*configs: ModelConfig) -> None:
    fps = {c.fingerprint(): c for c in configs}
    if len(fps) > 1:
        detail = "\n".join(f"  {fp}: {c.canonical()}" for fp, c in fps.items())
        raise ConfoundError(
            "agents ran under different model configs -- the comparison is "
            f"invalid (spec 4f):\n{detail}"
        )
