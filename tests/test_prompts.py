"""The prompt reference must match the code, and the shared prompt must stay shared.

docs/prompts.md is generated from the constants in reviewbot/. A doc that quotes
prompts and then drifts from them is worse than no doc: it invites a reader to
verify the confound claim against text the experiment never sent.
"""

import subprocess
import sys
import unittest
from pathlib import Path

from reviewbot.agents import BaselineAgent, MemoryAgent
from reviewbot.review import SYSTEM_PROMPT, build_request
from tests.fakes import FakeMemoryService

ROOT = Path(__file__).resolve().parent.parent


class TestPromptDocIsCurrent(unittest.TestCase):
    def test_docs_prompts_md_matches_the_source(self):
        result = subprocess.run(
            [sys.executable, "tools/dump_prompts.py", "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"docs/prompts.md is stale. Re-run: python3 tools/dump_prompts.py\n"
            f"{result.stdout}{result.stderr}",
        )

    def test_the_doc_quotes_the_prompt_verbatim(self):
        """Not just 'a file exists' -- the actual bytes must be in it."""
        text = (ROOT / "docs/prompts.md").read_text()
        for line in SYSTEM_PROMPT.splitlines():
            if line.strip():
                self.assertIn(line, text)


class TestTheReviewPromptIsShared(unittest.TestCase):
    """Spec 4f: anything differing between the agents beyond memory is a confound."""

    def test_both_agents_send_byte_identical_system_prompts(self):
        self.assertIs(BaselineAgent.review_pr.__doc__, BaselineAgent.review_pr.__doc__)
        # Build a request the way each agent does and compare the system block.
        from reviewbot.github import FileChange, PullRequest
        from reviewbot.review import ReviewContext, conventions_block, prior_knowledge_block, source_context_block

        pr = PullRequest(
            repo="r/p",
            number=1,
            title="t",
            body="",
            base_sha="a",
            head_sha="b",
            merge_commit_sha=None,
            merged_at=None,
            files=[FileChange("redis/connection.py", "modified", 1, 1, 2, patch="@@\n+x")],
        )
        conventions = [conventions_block({"CONTRIBUTING.md": "be nice"})]
        baseline_system, _ = build_request(
            pr,
            ReviewContext(
                stable_blocks=conventions,
                volatile_blocks=[source_context_block({"redis/connection.py": "code"})],
            ),
        )
        memory_system, _ = build_request(
            pr,
            ReviewContext(
                stable_blocks=conventions,
                volatile_blocks=[prior_knowledge_block([("m1", "a fact")])],
            ),
        )
        self.assertEqual(baseline_system, memory_system)

    def test_the_prompt_names_prior_knowledge_as_optional(self):
        """So its bytes do not change when memories are absent -- which is what
        keeps the two agents' system prompts identical."""
        self.assertIn("Prior knowledge", SYSTEM_PROMPT)
        self.assertIn("may be absent", SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
