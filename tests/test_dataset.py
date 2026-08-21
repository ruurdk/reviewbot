import tempfile
import unittest

from reviewbot.dataset import (
    BEATS,
    CONVENTION_CHANGE,
    FALSE_POSITIVE_TRAP,
    RECURRING_BUG,
    Sequence,
    SequenceEntry,
    disclosure_table,
    recurrence_rate,
    rows,
    validate,
)
from reviewbot.github import FileChange, HumanComment, PRStore, PullRequest

REPO = "redis/redis-py"
SPINE = ["redis/connection.py", "redis/cluster.py"]


def make_pr(store, number, modules, comments=1):
    pr = PullRequest(
        repo=REPO,
        number=number,
        title=f"pr {number}",
        body="",
        base_sha="base",
        head_sha="head",
        merge_commit_sha="merge",
        merged_at="2024-01-01T00:00:00Z",
        files=[
            FileChange(filename=m, status="modified", additions=10, deletions=2, changes=12)
            for m in modules
        ],
        comments=[
            HumanComment(id=i, kind="inline", path=modules[0], line=1, body="x", author="h")
            for i in range(comments)
        ],
    )
    store.save(pr)
    return pr


def full_sequence(gold=(1, 2, 3, 4, 5)):
    entries = []
    for i in range(1, 16):
        beats = []
        if i in (3, 4):
            beats.append(RECURRING_BUG)
        if i == 5:
            beats.append(FALSE_POSITIVE_TRAP)
        if i == 2:
            beats.append(CONVENTION_CHANGE)
        entries.append(
            SequenceEntry(ordinal=i, pr_number=100 + i, beats=beats, gold_labeled=i in gold)
        )
    return Sequence(
        repo=REPO,
        entries=entries,
        spine=SPINE,
        style_guide_paths=["CONTRIBUTING.md"],
        frozen_at_sha="deadbeef",
        selection_rule="upper half of the diff-size distribution",
    )


class TestRows(unittest.TestCase):
    def test_recurrence_is_computed_from_the_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PRStore(tmp)
            make_pr(store, 101, ["redis/connection.py"])
            make_pr(store, 102, ["redis/cluster.py"])
            make_pr(store, 103, ["redis/connection.py", "redis/x.py"])
            seq = Sequence(
                repo=REPO,
                entries=[SequenceEntry(ordinal=i, pr_number=100 + i) for i in (1, 2, 3)],
                spine=SPINE,
                frozen_at_sha="sha",
                selection_rule="rule",
            )
            table = rows(seq, store)
        self.assertEqual(table[0].recurs_from, [])
        self.assertEqual(table[1].recurs_from, [])
        self.assertEqual(table[2].recurs_from, [1])
        self.assertEqual(table[2].recurring_modules, ["redis/connection.py"])
        # 1 of the 2 PRs after the first recurs.
        self.assertAlmostEqual(recurrence_rate(table), 0.5)

    def test_disclosure_table_flags_comment_free_prs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PRStore(tmp)
            make_pr(store, 101, ["a.py"], comments=0)
            make_pr(store, 102, ["a.py"], comments=2)
            seq = Sequence(
                repo=REPO,
                entries=[SequenceEntry(ordinal=i, pr_number=100 + i) for i in (1, 2)],
                spine=SPINE,
                frozen_at_sha="sha",
                selection_rule="rule",
            )
            text = disclosure_table(rows(seq, store))
        self.assertIn("Module recurrence: 100%", text)
        self.assertIn("zero human review comments", text)


class TestValidate(unittest.TestCase):
    def _store(self, seq):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = PRStore(tmp.name)
        for entry in seq:
            make_pr(store, entry.pr_number, ["redis/connection.py"])
        return store

    def test_a_complete_sequence_validates(self):
        seq = full_sequence()
        self.assertEqual(validate(seq, self._store(seq)), [])

    def test_missing_beat_is_reported(self):
        seq = full_sequence()
        for entry in seq:
            entry.beats = [b for b in entry.beats if b != FALSE_POSITIVE_TRAP]
        problems = validate(seq, self._store(seq))
        self.assertTrue(any(FALSE_POSITIVE_TRAP in p for p in problems))

    def test_beat_without_a_gold_label_is_reported(self):
        seq = full_sequence(gold=(1, 6, 7, 8, 9))  # no beat PR labeled
        problems = validate(seq, self._store(seq))
        self.assertEqual(
            sum(1 for p in problems if "no hand-labeled PR" in p), len(BEATS)
        )

    def test_gold_subset_size_is_enforced(self):
        seq = full_sequence(gold=(2, 3, 4, 5))  # 4 < 5
        problems = validate(seq, self._store(seq))
        self.assertTrue(any("gold subset has 4" in p for p in problems))

    def test_unpinned_repo_state_is_reported(self):
        seq = full_sequence()
        seq.frozen_at_sha = None
        self.assertTrue(any("frozen_at_sha" in p for p in validate(seq, self._store(seq))))

    def test_uningested_pr_is_reported(self):
        seq = full_sequence()
        store = self._store(seq)
        seq.entries.append(SequenceEntry(ordinal=16, pr_number=999, gold_labeled=False))
        self.assertTrue(any("not ingested" in p for p in validate(seq, store)))

    def test_sequence_round_trips_through_json(self):
        seq = full_sequence()
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/sequence.json"
            seq.save(path)
            back = Sequence.load(path)
        self.assertEqual(len(back), len(seq))
        self.assertEqual(back.frozen_at_sha, "deadbeef")
        self.assertEqual([e.beats for e in back], [e.beats for e in seq])


if __name__ == "__main__":
    unittest.main()
