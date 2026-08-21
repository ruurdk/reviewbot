import tempfile
import unittest

from reviewbot.github import FileChange, HumanComment, PullRequest
from reviewbot.quality import (
    MUST_NOT_FLAG,
    GoldItem,
    GoldLabels,
    aggregate_gold,
    aggregate_proxy,
    gold_score,
    load_gold_dir,
    proxy_score,
    quality_table,
    same_place,
)
from reviewbot.review import Finding


def finding(file="redis/connection.py", line=12, category="resource-leak"):
    return Finding(
        file=file,
        line=line,
        severity="major",
        category=category,
        message="m",
        confidence="high",
    )


def pr(comments):
    return PullRequest(
        repo="redis/redis-py",
        number=3411,
        title="t",
        body="",
        base_sha="b",
        head_sha="h",
        merge_commit_sha=None,
        merged_at=None,
        files=[FileChange("redis/connection.py", "modified", 1, 1, 2, patch="@@")],
        comments=comments,
    )


def human(line, path="redis/connection.py", bot=False, kind="inline"):
    return HumanComment(
        id=line or 0,
        kind=kind,
        path=path,
        line=line,
        body="b",
        author="codecov[bot]" if bot else "person",
        author_is_bot=bot,
    )


class TestSamePlace(unittest.TestCase):
    def test_window_tolerance(self):
        self.assertTrue(same_place("a.py", 10, "a.py", 14, 5))
        self.assertFalse(same_place("a.py", 10, "a.py", 20, 5))

    def test_different_file_never_matches(self):
        self.assertFalse(same_place("a.py", 10, "b.py", 10, 100))

    def test_missing_line_degrades_to_file_match(self):
        self.assertTrue(same_place("a.py", None, "a.py", 300, 5))


class TestProxy(unittest.TestCase):
    def test_agreement_and_coverage(self):
        score = proxy_score(pr([human(14), human(200)]), "baseline", [finding(line=12)])
        self.assertEqual(score.matched, 1)
        self.assertEqual(score.agreement, 1.0)
        self.assertEqual(score.coverage, 0.5)
        self.assertFalse(score.blind)

    def test_bot_comments_are_excluded(self):
        score = proxy_score(pr([human(12, bot=True)]), "baseline", [finding()])
        self.assertTrue(score.blind)
        self.assertIsNone(score.coverage)

    def test_blind_prs_are_flagged_not_scored_zero(self):
        score = proxy_score(pr([]), "memory", [finding()])
        self.assertTrue(score.blind)
        self.assertIsNone(score.coverage)
        agg = aggregate_proxy([score])
        self.assertEqual(agg["prs_blind"], 1)
        self.assertIsNone(agg["coverage"])
        self.assertIn("silent reviewer", agg["note"])

    def test_review_body_comments_are_not_location_matched(self):
        # A summary comment has no path/line, so it cannot confirm a location.
        score = proxy_score(pr([human(None, path=None, kind="review_body")]), "b", [finding()])
        self.assertTrue(score.blind)


class TestGold(unittest.TestCase):
    def labels(self):
        return GoldLabels(
            pr_number=3411,
            labeller="ruurd",
            items=[
                GoldItem(id="d1", file="redis/connection.py", line=12, description="socket leak"),
                GoldItem(id="d2", file="redis/cluster.py", line=88, description="slot cache race"),
                GoldItem(
                    id="t1",
                    file="redis/connection.py",
                    line=400,
                    kind=MUST_NOT_FLAG,
                    note="deliberate pattern the baseline keeps re-flagging",
                ),
            ],
        )

    def test_true_positive_false_negative_and_trap(self):
        score = gold_score(
            self.labels(),
            "baseline",
            [finding(line=13), finding(file="redis/connection.py", line=402)],
        )
        self.assertEqual(score.true_positives, 1)
        self.assertEqual(score.false_negatives, 1)  # missed the cluster.py defect
        self.assertEqual(score.false_positives, 1)  # the trap
        self.assertEqual(score.traps_flagged, 1)
        self.assertEqual(score.tripped_trap_ids, ["t1"])
        self.assertAlmostEqual(score.precision, 0.5)
        self.assertAlmostEqual(score.recall, 0.5)
        self.assertAlmostEqual(score.false_positive_rate, 0.5)

    def test_an_agent_that_avoids_the_trap_scores_better(self):
        clean = gold_score(self.labels(), "memory", [finding(line=12), finding(file="redis/cluster.py", line=88)])
        self.assertEqual(clean.traps_flagged, 0)
        self.assertEqual(clean.false_positives, 0)
        self.assertEqual(clean.false_positive_rate, 0.0)
        self.assertEqual(clean.recall, 1.0)

    def test_one_finding_cannot_claim_two_defects(self):
        labels = GoldLabels(
            pr_number=1,
            labeller="x",
            items=[
                GoldItem(id="d1", file="a.py", line=10),
                GoldItem(id="d2", file="a.py", line=12),
            ],
        )
        score = gold_score(labels, "b", [finding(file="a.py", line=11)])
        self.assertEqual(score.true_positives, 1)
        self.assertEqual(score.false_negatives, 1)

    def test_empty_review_on_a_clean_pr_is_perfect_not_undefined(self):
        labels = GoldLabels(pr_number=2, labeller="x", items=[])
        score = gold_score(labels, "memory", [])
        self.assertIsNone(score.precision)
        self.assertIsNone(score.recall)
        self.assertEqual(score.false_negatives, 0)

    def test_micro_aggregate(self):
        a = gold_score(self.labels(), "b", [finding(line=12)])
        b = gold_score(
            GoldLabels(pr_number=2, labeller="x", items=[GoldItem(id="d3", file="a.py", line=1)]),
            "b",
            [],
        )
        agg = aggregate_gold([a, b])
        self.assertEqual((agg["true_positives"], agg["false_negatives"]), (1, 2))
        self.assertEqual(agg["precision"], 1.0)
        self.assertAlmostEqual(agg["recall"], 1 / 3)
        self.assertEqual(agg["traps_total"], 1)

    def test_labels_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.labels().save(f"{tmp}/3411.json")
            loaded = load_gold_dir(tmp)
        self.assertEqual(list(loaded), [3411])
        self.assertEqual(len(loaded[3411].defects), 2)
        self.assertEqual(len(loaded[3411].traps), 1)

    def test_bad_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            GoldItem(id="x", file="a.py", line=1, kind="probably")


class TestTables(unittest.TestCase):
    def test_gold_and_proxy_render_separately(self):
        gold = quality_table(
            {
                "baseline": aggregate_gold([gold_score(GoldLabels(1, "x", [GoldItem(id="d", file="a.py", line=1)]), "baseline", [finding(file="a.py", line=1)])]),
            },
            kind="gold",
        )
        self.assertIn("precision", gold)
        self.assertIn("traps flagged", gold)
        proxy = quality_table({"baseline": aggregate_proxy([proxy_score(pr([human(12)]), "baseline", [finding()])])}, kind="proxy")
        self.assertIn("coverage", proxy)
        self.assertIn("blind", proxy)
        # The two tables never appear as one number.
        self.assertNotIn("precision", proxy)


if __name__ == "__main__":
    unittest.main()
