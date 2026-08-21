"""Test package init.

Tests are hermetic: they use injected transports and must never reach a real
service. A populated .env broke that -- one test calls the CLI in-process, which
loads .env into os.environ, and other tests then found real credentials where
they expected none and made live calls to api.github.com and the Agent Memory
endpoint.

Scrubbing here, at import time, makes the isolation structural rather than
per-test discipline.
"""

import os

for _key in (
    "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN",
    "REDIS_AGENT_MEMORY_URL",
    "REDIS_AGENT_MEMORY_API_KEY",
    "REDIS_AGENT_MEMORY_STORE_ID",
    "REVIEWBOT_NAMESPACE",
):
    os.environ.pop(_key, None)
