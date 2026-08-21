"""The shared review path -- the place a confound would hide."""

import json
import tempfile
import unittest

from reviewbot.accounting import Ledger
from reviewbot.claude import ClaudeClient, Tags, prefix_id
from reviewbot.config import ModelConfig
from reviewbot.github import FileChange, PullRequest
from reviewbot.review import (
    FINDINGS_SCHEMA,
    SYSTEM_PROMPT,
    ReviewContext,
    build_request,
    conventions_block,
    prior_knowledge_block,
    render_diff,
    review,
    source_context_block,
)

PR = PullRequest(
    repo="redis/redis-py",
    number=42,
    title="Close the socket on handshake failure",
    body="Fixes a leak.",
    base_sha="base",
    head_sha="head",
    merge_commit_sha="merge",
    merged_at=None,
    files=[
        FileChange("redis/connection.py", "modified", 12, 3, 15, patch="@@ -1 +1 @@\n-a\n+b"),
        FileChange("redis/cluster.py", "modified", 2, 0, 2, patch="@@ -9 +9 @@\n+c"),
    ],
)

CONVENTIONS = conventions_block({"CONTRIBUTING.md": "Use type hints." * 200})


def responder(payload, capture=None):
    def transport(url, headers, body, timeout):
        if capture is not None:
            capture.append(json.loads(body))
        return 200, {}, [
            json.dumps(
                {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                }
            ).encode()
        ]

    return transport


class TestPromptShape(unittest.TestCase):
    def test_both_agents_share_one_system_prompt(self):
        """A per-agent system prompt would be a confound and would split the
        cache prefix. The optional-section wording is what makes one prompt
        serve both."""
        baseline_ctx = ReviewContext(
            stable_blocks=[CONVENTIONS],
            volatile_blocks=[source_context_block({"redis/connection.py": "src"})],
        )
        memory_ctx = ReviewContext(
            stable_blocks=[CONVENTIONS],
            volatile_blocks=[prior_knowledge_block([("m1", "connection.py owns the socket")])],
        )
        sys_a, msgs_a = build_request(PR, baseline_ctx)
        sys_b, msgs_b = build_request(PR, memory_ctx)
        self.assertEqual(sys_a, sys_b)
        self.assertEqual(sys_a[0]["text"], SYSTEM_PROMPT)
        self.assertIn("Prior knowledge", SYSTEM_PROMPT)
        # Same cacheable prefix for both agents, different volatile payload.
        self.assertEqual(
            prefix_id({"system": sys_a, "messages": msgs_a}),
            prefix_id({"system": sys_b, "messages": msgs_b}),
        )
        self.assertNotEqual(msgs_a, msgs_b)

    def test_breakpoint_sits_at_the_end_of_the_stable_prefix(self):
        ctx = ReviewContext(
            stable_blocks=[CONVENTIONS],
            volatile_blocks=[source_context_block({"a.py": "x"})],
        )
        _, messages = build_request(PR, ctx)
        blocks = messages[0]["content"]
        self.assertIn("cache_control", blocks[0])
        # Everything after the conventions varies per PR and must not be marked.
        for block in blocks[1:]:
            self.assertNotIn("cache_control", block)
        self.assertTrue(blocks[-1]["text"].startswith("The diff:"))

    def test_caching_off_marks_nothing(self):
        ctx = ReviewContext(stable_blocks=[CONVENTIONS])
        system, messages = build_request(PR, ctx, enable_caching=False)
        self.assertNotIn("cache_control", system[0])
        for block in messages[0]["content"]:
            self.assertNotIn("cache_control", block)

    def test_the_stable_prefix_is_identical_across_prs(self):
        """The silent-invalidator check: a PR id, SHA, or timestamp anywhere in
        the prefix zeroes caching, and if it lands in only one agent it destroys
        the comparison. So the prefix must be byte-identical across PRs."""
        other = PullRequest(
            **{
                **PR.to_json(),
                "number": 99,
                "title": "Something else entirely",
                "base_sha": "otherbase",
                "files": [FileChange("redis/cluster.py", "modified", 1, 1, 2, patch="@@")],
            }
        )
        ctx = ReviewContext(stable_blocks=[CONVENTIONS])
        sys_a, msgs_a = build_request(PR, ctx)
        sys_b, msgs_b = build_request(other, ctx)
        self.assertEqual(sys_a, sys_b)
        self.assertEqual(msgs_a[0]["content"][0], msgs_b[0]["content"][0])
        self.assertEqual(
            prefix_id({"system": sys_a, "messages": msgs_a}),
            prefix_id({"system": sys_b, "messages": msgs_b}),
        )
        # ... while the volatile tail genuinely differs.
        self.assertNotEqual(msgs_a[0]["content"][-1], msgs_b[0]["content"][-1])


class TestRenderDiff(unittest.TestCase):
    def test_files_are_ordered_deterministically(self):
        first = render_diff(PR)
        shuffled = PullRequest(**{**PR.to_json(), "files": list(reversed(PR.files))})
        self.assertEqual(render_diff(shuffled), first)

    def test_missing_patch_is_labelled_not_dropped(self):
        pr = PullRequest(
            **{
                **PR.to_json(),
                "files": [FileChange("big.bin", "modified", 0, 0, 0, patch=None)],
            }
        )
        self.assertIn("[no patch available from the API]", render_diff(pr))

    def test_patch_truncation_is_disclosed(self):
        text = render_diff(PR, max_patch_chars=5)
        self.assertIn("[patch truncated by the harness]", text)


class TestReviewCall(unittest.TestCase):
    def test_findings_and_memory_usage_are_parsed(self):
        payload = {
            "findings": [
                {
                    "file": "redis/connection.py",
                    "line": 12,
                    "severity": "major",
                    "category": "resource-leak",
                    "message": "socket is not closed when the handshake raises",
                    "confidence": "high",
                }
            ],
            "memories_used": ["m1"],
        }
        captured = []
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(tmp, "run-r")
            client = ClaudeClient(
                ModelConfig(stream=False),
                ledger,
                api_key="t",
                transport=responder(payload, captured),
            )
            result = review(
                client,
                Tags("memory", PR.pr_id, 3, "review"),
                PR,
                ReviewContext(
                    stable_blocks=[CONVENTIONS],
                    volatile_blocks=[prior_knowledge_block([("m1", "fact"), ("m2", "other")])],
                ),
                notes={"retrieved": 2},
            )
            records = list(ledger.records())
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].category, "resource-leak")
        # Retrieval precision (spec 7b): shown 2, used 1.
        self.assertEqual(result.memories_used, ["m1"])
        self.assertFalse(result.truncated)

        sent = captured[0]
        self.assertEqual(sent["output_config"]["format"]["schema"], FINDINGS_SCHEMA)
        (rec,) = records
        self.assertEqual(rec.notes, {"retrieved": 2})
        self.assertEqual(rec.phase, "review")

    def test_an_empty_review_is_a_valid_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ClaudeClient(
                ModelConfig(stream=False),
                Ledger(tmp, "run-r"),
                api_key="t",
                transport=responder({"findings": [], "memories_used": []}),
            )
            result = review(
                client, Tags("baseline", PR.pr_id, 1, "review"), PR, ReviewContext()
            )
        self.assertEqual(result.findings, [])


if __name__ == "__main__":
    unittest.main()
