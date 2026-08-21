"""Minimal .env loader (stdlib only -- python-dotenv is not installable here).

One rule worth stating: **the real environment always wins.** A value already
present in `os.environ` is never overwritten by the file, so exporting a key in
your shell overrides `.env` rather than silently losing to it. That ordering is
what makes `ANTHROPIC_API_KEY=... python3 -m reviewbot run ...` behave the way
anyone would expect.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ENV_FILE = ".env"

# Everything the harness reads, and what breaks without it.
REQUIRED_BY: dict[str, tuple[str, ...]] = {
    "ANTHROPIC_API_KEY": ("run",),
    "GITHUB_TOKEN": ("ingest",),
    "REDIS_AGENT_MEMORY_URL": ("run --agents both", "run --agents memory"),
    "REDIS_AGENT_MEMORY_API_KEY": ("run --agents both", "run --agents memory"),
    "REDIS_AGENT_MEMORY_STORE_ID": ("run --agents both", "run --agents memory"),
}


def parse_env(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines. Tolerates `export ` prefixes, quotes, and blanks."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def load_env(path: str | os.PathLike[str] = DEFAULT_ENV_FILE) -> list[str]:
    """Load `path` into os.environ without overriding what is already set.

    Returns the names of the keys it actually applied, so callers can say where
    a value came from instead of leaving it a mystery.
    """
    p = Path(path)
    if not p.exists():
        return []
    applied: list[str] = []
    for key, value in parse_env(p.read_text()).items():
        if key in os.environ:
            continue
        os.environ[key] = value
        applied.append(key)
    return applied


PLACEHOLDER_MARKERS = ("REPLACE_ME", "your-api-key", "changeme")


def is_placeholder(value: str | None) -> bool:
    """True when a variable is set but still holds template text.

    Presence is not usability: a copied .env with one field left unfilled reads
    as configured and then fails on the first call, after the run has already
    started spending tokens.
    """
    upper = (value or "").upper()
    return bool(value) and any(m.upper() in upper for m in PLACEHOLDER_MARKERS)


def missing(keys: tuple[str, ...] | list[str]) -> list[str]:
    """Keys that are unset *or* still placeholders."""
    return [k for k in keys if not os.environ.get(k) or is_placeholder(os.environ.get(k))]
