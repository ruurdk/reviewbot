import json
import tempfile
import unittest

from reviewbot.github import (
    GitHubClient,
    GitHubError,
    PRStore,
    RateLimited,
    fetch_pull_request,
)


def canned(routes, calls=None):
    calls = calls if calls is not None else []

    def transport(url, headers):
        calls.append(url)
        for suffix, payload in routes.items():
            if suffix in url:
                if isinstance(payload, tuple):
                    status, body = payload
                    return status, {"x-ratelimit-remaining": "0"}, json.dumps(body).encode()
                return 200, {}, json.dumps(payload).encode()
        return 404, {}, b'{"message":"not found"}'

    transport.calls = calls
    return transport


PR_ROUTES = {
    "/pulls/42/files?per_page=100&page=1": [
        {
            "filename": "redis/connection.py",
            "status": "modified",
            "additions": 30,
            "deletions": 4,
            "changes": 34,
            "patch": "@@ -1 +1 @@\n-old\n+new",
        },
        {
            "filename": "redis/cluster.py",
            "status": "modified",
            "additions": 5,
            "deletions": 1,
            "changes": 6,
            "patch": "@@ -2 +2 @@",
        },
    ],
    "/pulls/42/comments?per_page=100&page=1": [
        {
            "id": 1,
            "path": "redis/connection.py",
            "line": 12,
            "body": "this leaks a socket",
            "user": {"login": "a-human"},
        },
        {
            "id": 2,
            "path": "redis/cluster.py",
            "line": 3,
            "body": "codecov nit",
            "user": {"login": "codecov[bot]"},
        },
    ],
    "/pulls/42/reviews?per_page=100&page=1": [
        {"id": 9, "body": "LGTM once the socket is closed", "user": {"login": "a-human"}},
        {"id": 10, "body": "", "user": {"login": "a-human"}},
    ],
    "/pulls/42": {
        "title": "Fix connection leak",
        "body": "closes #1",
        "base": {"sha": "base111"},
        "head": {"sha": "head222"},
        "merge_commit_sha": "merge333",
        "merged_at": "2024-01-01T00:00:00Z",
    },
}


class TestGitHubClient(unittest.TestCase):
    def test_token_is_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = GitHubClient(tmp, token=None, transport=canned({}))
            with self.assertRaises(RateLimited) as ctx:
                client.get("/repos/redis/redis-py/pulls/1")
            self.assertIn("GITHUB_TOKEN", str(ctx.exception))

    def test_responses_are_cached_to_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = canned({"/pulls/42": PR_ROUTES["/pulls/42"]})
            client = GitHubClient(tmp, token="t", transport=transport)
            first = client.get("/repos/r/r/pulls/42")
            second = client.get("/repos/r/r/pulls/42")
            self.assertEqual(first, second)
            self.assertEqual(client.requests_made, 1)
            self.assertEqual(client.cache_hits, 1)

            # A fresh client on the same cache dir makes no requests at all --
            # this is what keeps a "frozen" dataset actually frozen.
            reopened = GitHubClient(tmp, token="t", transport=canned({}))
            self.assertEqual(reopened.get("/repos/r/r/pulls/42"), first)
            self.assertEqual(reopened.requests_made, 0)

    def test_exhausted_rate_limit_is_distinguishable(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = canned({"/pulls/1": (403, {"message": "rate limit"})})
            client = GitHubClient(tmp, token="t", transport=transport)
            with self.assertRaises(RateLimited):
                client.get("/repos/r/r/pulls/1")

    def test_other_errors_raise_github_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = GitHubClient(tmp, token="t", transport=canned({}))
            with self.assertRaises(GitHubError):
                client.get("/repos/r/r/pulls/999")

    def test_pagination_stops_on_short_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = canned({"page=1": [1, 2, 3]})
            client = GitHubClient(tmp, token="t", transport=transport)
            self.assertEqual(client.paginate("/x", per_page=100), [1, 2, 3])
            self.assertEqual(client.requests_made, 1)


class TestFetchPullRequest(unittest.TestCase):
    def test_assembles_files_comments_and_pinned_shas(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = GitHubClient(tmp, token="t", transport=canned(PR_ROUTES))
            pr = fetch_pull_request(client, "redis/redis-py", 42)
        self.assertEqual(pr.pr_id, "redis/redis-py#42")
        self.assertEqual(pr.base_sha, "base111")
        self.assertEqual(pr.modules, ["redis/cluster.py", "redis/connection.py"])
        self.assertEqual(pr.diff_size, 40)
        # Empty review bodies are dropped; bot comments are kept but flagged so
        # the proxy metric can exclude them.
        self.assertEqual(len(pr.comments), 3)
        self.assertEqual(sum(1 for c in pr.comments if c.author_is_bot), 1)
        self.assertEqual(
            sorted(c.kind for c in pr.comments), ["inline", "inline", "review_body"]
        )

    def test_store_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = GitHubClient(f"{tmp}/cache", token="t", transport=canned(PR_ROUTES))
            store = PRStore(f"{tmp}/prs")
            pr = fetch_pull_request(client, "redis/redis-py", 42)
            store.save(pr)
            back = store.load("redis/redis-py", 42)
            self.assertEqual(back.to_json(), pr.to_json())
            self.assertTrue(store.has("redis/redis-py", 42))

    def test_ingest_prefers_the_store_over_the_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PRStore(f"{tmp}/prs")
            client = GitHubClient(f"{tmp}/cache", token="t", transport=canned(PR_ROUTES))
            store.ingest(client, "redis/redis-py", [42])
            offline = GitHubClient(f"{tmp}/cache2", token="t", transport=canned({}))
            prs = store.ingest(offline, "redis/redis-py", [42])
            self.assertEqual(prs[0].number, 42)
            self.assertEqual(offline.requests_made, 0)


if __name__ == "__main__":
    unittest.main()
