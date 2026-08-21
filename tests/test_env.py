import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from reviewbot.env import REQUIRED_BY, load_env, missing, parse_env

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestParse(unittest.TestCase):
    def test_handles_comments_blanks_exports_and_quotes(self):
        parsed = parse_env(
            "\n".join(
                [
                    "# a comment",
                    "",
                    "PLAIN=value",
                    "export EXPORTED=value2",
                    'QUOTED="has spaces"',
                    "SINGLE='sq'",
                    "EMPTY=",
                    "  SPACED  =  trimmed  ",
                    "no_equals_sign",
                ]
            )
        )
        self.assertEqual(
            parsed,
            {
                "PLAIN": "value",
                "EXPORTED": "value2",
                "QUOTED": "has spaces",
                "SINGLE": "sq",
                "EMPTY": "",
                "SPACED": "trimmed",
            },
        )

    def test_values_containing_equals_survive(self):
        self.assertEqual(parse_env("K=a=b=c")["K"], "a=b=c")


class TestLoad(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def test_the_real_environment_wins(self):
        os.environ["REVIEWBOT_TEST_KEY"] = "from-shell"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, ".env")
            path.write_text("REVIEWBOT_TEST_KEY=from-file\nREVIEWBOT_TEST_OTHER=from-file\n")
            applied = load_env(path)
        self.assertEqual(os.environ["REVIEWBOT_TEST_KEY"], "from-shell")
        self.assertEqual(os.environ["REVIEWBOT_TEST_OTHER"], "from-file")
        self.assertEqual(applied, ["REVIEWBOT_TEST_OTHER"])

    def test_absent_file_is_not_an_error(self):
        self.assertEqual(load_env("/nonexistent/.env"), [])

    def test_missing_reports_unset_keys(self):
        os.environ.pop("REVIEWBOT_TEST_ABSENT", None)
        os.environ["REVIEWBOT_TEST_PRESENT"] = "x"
        self.assertEqual(
            missing(["REVIEWBOT_TEST_PRESENT", "REVIEWBOT_TEST_ABSENT"]),
            ["REVIEWBOT_TEST_ABSENT"],
        )


class TestExampleFile(unittest.TestCase):
    def test_example_covers_every_variable_the_code_reads(self):
        keys = set(parse_env(Path(REPO_ROOT, ".env.example").read_text()))
        self.assertTrue(set(REQUIRED_BY) <= keys, set(REQUIRED_BY) - keys)

    def test_every_credential_placeholder_is_obviously_a_placeholder(self):
        """A committed example that accidentally holds a real key is the worst
        failure mode this file has, so it is asserted rather than trusted."""
        for key, value in parse_env(Path(REPO_ROOT, ".env.example").read_text()).items():
            if key not in REQUIRED_BY:
                continue  # non-secret run defaults carry real values on purpose
            self.assertIn("REPLACE_ME", value, f"{key} may hold a real value")

    def test_real_env_file_is_ignored_by_git(self):
        proc = subprocess.run(
            ["git", "check-ignore", "-v", ".env"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, "a real .env would be committable")
        self.assertIn(".env", proc.stdout)

    def test_the_example_itself_stays_committable(self):
        proc = subprocess.run(
            ["git", "check-ignore", ".env.example"], cwd=REPO_ROOT, capture_output=True
        )
        self.assertEqual(proc.returncode, 1, ".env.example must not be ignored")


class TestHermeticity(unittest.TestCase):
    def test_no_real_credentials_are_visible_to_tests(self):
        """Regression guard. A populated .env once leaked into the test process
        via an in-process CLI call, and two tests silently made live network
        calls instead of failing closed."""
        for key in REQUIRED_BY:
            self.assertIsNone(
                os.environ.get(key),
                f"{key} is set during tests -- something loaded a real .env",
            )


class TestRunRefusesWithoutCredentials(unittest.TestCase):
    def test_run_names_every_missing_key_instead_of_failing_midway(self):
        env = {k: v for k, v in os.environ.items() if k not in REQUIRED_BY}
        env["PYTHONPATH"] = str(REPO_ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            seq = Path(tmp, "sequence.json")
            seq.write_text(
                '{"repo":"redis/redis-py","entries":[],"spine":["a.py"],'
                '"style_guide_paths":[],"frozen_at_sha":"abc","selection_rule":"r"}'
            )
            proc = subprocess.run(
                [sys.executable, "-m", "reviewbot", "run", "t", "--sequence", str(seq),
                 "--store", tmp, "--force", "--checkout", tmp],
                cwd=tmp,  # away from the repo, so a real .env cannot leak in
                capture_output=True,
                text=True,
                env=env,
            )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("missing credentials", proc.stderr)
        for key in ("ANTHROPIC_API_KEY", "REDIS_AGENT_MEMORY_URL", "REDIS_AGENT_MEMORY_STORE_ID"):
            self.assertIn(key, proc.stderr)
        self.assertIn(".env.example", proc.stderr)


if __name__ == "__main__":
    unittest.main()
