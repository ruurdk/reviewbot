import tempfile
import unittest
from pathlib import Path

from reviewbot.github import FileChange, PullRequest
from reviewbot.repo import (
    DictSourceProvider,
    LocalSourceProvider,
    touched_sources,
)


def pr(files):
    return PullRequest(
        repo="redis/redis-py",
        number=4052,
        title="big change",
        body="",
        base_sha="base",
        head_sha="head",
        merge_commit_sha=None,
        merged_at=None,
        files=files,
    )


class TestSourceBudget(unittest.TestCase):
    def test_the_most_changed_files_win_the_budget(self):
        files = [
            FileChange("small_but_huge_file.py", "modified", 1, 0, 1),
            FileChange("redis/cluster.py", "modified", 400, 100, 500),
            FileChange("redis/connection.py", "modified", 80, 20, 100),
        ]
        sources = {name.filename: "x" * 4_000 for name in files}
        ctx = touched_sources(pr(files), DictSourceProvider(sources), max_total_chars=8_000)
        # Budget fits two of three; the diff's biggest touches are admitted.
        self.assertEqual(sorted(ctx.files), ["redis/cluster.py", "redis/connection.py"])
        self.assertEqual(ctx.dropped, ["small_but_huge_file.py"])
        self.assertEqual(ctx.n_read, 2)
        self.assertEqual(ctx.total_chars, 8_000)

    def test_dropping_is_disclosed_not_silent(self):
        files = [FileChange(f"m{i}.py", "modified", 10, 0, 10 - i) for i in range(5)]
        ctx = touched_sources(
            pr(files), DictSourceProvider({f.filename: "y" * 1_000 for f in files}),
            max_total_chars=2_500,
        )
        notes = ctx.as_notes()
        self.assertEqual(notes["files_read"], 2)
        self.assertEqual(notes["files_dropped_over_budget"], 3)
        self.assertEqual(notes["budget_chars"], 2_500)
        self.assertTrue(notes["dropped"])

    def test_at_least_one_file_is_always_read(self):
        # A single file larger than the whole budget must still be included,
        # otherwise the review has no source context at all.
        files = [FileChange("huge.py", "modified", 1, 1, 2)]
        ctx = touched_sources(
            pr(files), DictSourceProvider({"huge.py": "z" * 50_000}), max_total_chars=1_000
        )
        self.assertEqual(list(ctx.files), ["huge.py"])
        self.assertEqual(ctx.dropped, [])

    def test_file_order_is_deterministic_regardless_of_admission_order(self):
        files = [
            FileChange("b.py", "modified", 1, 1, 2),
            FileChange("a.py", "modified", 90, 10, 100),
        ]
        provider = DictSourceProvider({"a.py": "a", "b.py": "b"})
        first = touched_sources(pr(files), provider)
        second = touched_sources(pr(list(reversed(files))), provider)
        # Path-sorted output keeps the prompt bytes -- and so the cache prefix --
        # stable even though admission is by diff size.
        self.assertEqual(list(first.files), ["a.py", "b.py"])
        self.assertEqual(list(first.files), list(second.files))

    def test_removed_and_binary_files_are_skipped(self):
        files = [
            FileChange("gone.py", "removed", 0, 10, 10),
            FileChange("logo.png", "modified", 1, 1, 2),
            FileChange("kept.py", "modified", 5, 1, 6),
        ]
        ctx = touched_sources(
            pr(files), DictSourceProvider({"gone.py": "x", "logo.png": "x", "kept.py": "x"})
        )
        self.assertEqual(list(ctx.files), ["kept.py"])

    def test_unreadable_files_are_recorded(self):
        files = [FileChange("missing.py", "modified", 1, 1, 2)]
        ctx = touched_sources(pr(files), DictSourceProvider({}))
        self.assertEqual(ctx.unreadable, ["missing.py"])
        self.assertEqual(ctx.files, {})


class TestLocalProvider(unittest.TestCase):
    def test_reads_from_a_worktree_when_git_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.py").write_text("contents")
            provider = LocalSourceProvider(tmp, use_git=False)
            self.assertEqual(provider.read("a.py", "anysha"), "contents")
            self.assertTrue(provider.fell_back_to_worktree)
            self.assertIsNone(provider.read("missing.py", "anysha"))

    def test_reads_are_cached_and_counted(self):
        provider = DictSourceProvider({"a.py": "x"})
        provider.read("a.py", "sha")
        provider.read("a.py", "sha")
        self.assertEqual(provider.reads, 1)


if __name__ == "__main__":
    unittest.main()
