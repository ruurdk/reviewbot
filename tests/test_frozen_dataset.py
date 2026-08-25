"""Guards on the committed dataset itself.

sequence.json and data/gold/*.json are experiment inputs, not code -- but a
silent drift between them (a gold-flagged PR with no label file, a beat with no
evidence) invalidates the quality half of the result. These are cheap and catch
it at commit time.
"""

import unittest
from pathlib import Path

from reviewbot.dataset import BEATS, Sequence, validate
from reviewbot.github import PRStore
from reviewbot.quality import MUST_NOT_FLAG, load_gold_dir

ROOT = Path(__file__).resolve().parent.parent
SEQUENCE = ROOT / "data" / "sequence.json"
GOLD = ROOT / "data" / "gold"


# data/prs is gitignored -- it is reproducible from the pinned SHAs, and 3.9 MB
# of PR JSON does not belong in git. But two tests below read it, and on a fresh
# clone they failed with a diff of 19 "not ingested" strings, which reads like a
# broken repo rather than a missing step. Skip with the fix instead.
PRS = ROOT / "data" / "prs"
NEEDS_INGEST = (
    f"{PRS} is empty -- run `python3 -m reviewbot ingest` (needs GITHUB_TOKEN). "
    "It defaults to the frozen sequence and fetches exactly its 19 PRs."
)


def ingested() -> bool:
    return PRS.exists() and any(PRS.glob("*/*.json"))


@unittest.skipUnless(SEQUENCE.exists(), "no frozen sequence in this checkout")
class TestFrozenSequence(unittest.TestCase):
    def setUp(self):
        self.sequence = Sequence.load(SEQUENCE)
        self.gold = load_gold_dir(GOLD)

    @unittest.skipUnless(ingested(), NEEDS_INGEST)
    def test_the_sequence_validates(self):
        self.assertEqual(validate(self.sequence, PRStore(PRS)), [])

    def test_every_gold_flagged_pr_has_a_label_file(self):
        flagged = {e.pr_number for e in self.sequence.gold_subset}
        self.assertEqual(flagged - set(self.gold), set())

    def test_every_label_file_belongs_to_the_sequence(self):
        numbers = {e.pr_number for e in self.sequence}
        self.assertEqual(set(self.gold) - numbers, set())

    def test_every_beat_has_at_least_one_labelled_pr(self):
        for beat in BEATS:
            prs = [e.pr_number for e in self.sequence.beat_entries(beat)]
            self.assertTrue(prs, f"beat {beat} is unassigned")
            self.assertTrue(
                any(n in self.gold for n in prs), f"beat {beat} has no gold labels"
            )

    def test_the_false_positive_trap_actually_carries_trap_labels(self):
        """A trap beat whose labels contain no must_not_flag item measures
        nothing -- the false-positive rate would have no ground truth."""
        for entry in self.sequence.beat_entries("false_positive_trap"):
            labels = self.gold.get(entry.pr_number)
            if labels is None:
                continue
            self.assertTrue(
                labels.traps,
                f"#{entry.pr_number} carries the trap beat but has no "
                f"{MUST_NOT_FLAG} label",
            )

    @unittest.skipUnless(ingested(), NEEDS_INGEST)
    def test_trap_labels_recur_so_a_memoryless_reviewer_can_re_flag_them(self):
        trap_files = {
            item.file
            for labels in self.gold.values()
            for item in labels.traps
        }
        store = PRStore(ROOT / "data" / "prs")
        touched = {}
        for entry in self.sequence:
            pr = store.load(self.sequence.repo, entry.pr_number)
            for path in pr.modules:
                touched.setdefault(path, []).append(entry.ordinal)
        recurring = [f for f in trap_files if len(touched.get(f, [])) > 1]
        self.assertTrue(
            recurring,
            "no trap file is touched by more than one PR, so no trap can recur",
        )

    def test_candidate_labels_are_marked_as_such(self):
        """Until a human confirms them, the provenance has to say so: the labels
        were written by the same model family under evaluation."""
        for number, labels in self.gold.items():
            self.assertIn(
                "CANDIDATE",
                labels.labeller.upper(),
                f"#{number} labels claim confirmed provenance",
            )


if __name__ == "__main__":
    unittest.main()
