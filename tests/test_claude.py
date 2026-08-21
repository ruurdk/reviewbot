"""Client tests run against an injected transport -- no network, no API key."""

import json
import tempfile
import unittest

from reviewbot import claude as claude_mod
from reviewbot.accounting import Ledger
from reviewbot.claude import ClaudeClient, ClaudeError, Refusal, Tags, prefix_id
from reviewbot.config import ModelConfig

SYSTEM = [{"type": "text", "text": "x" * 100, "cache_control": {"type": "ephemeral"}}]


def user(text):
    return [{"role": "user", "content": [{"type": "text", "text": text}]}]


def json_transport(body_obj, status=200, headers=None):
    calls = []

    def transport(url, hdrs, body, timeout):
        calls.append(json.loads(body))
        return status, headers or {}, [json.dumps(body_obj).encode()]

    transport.calls = calls
    return transport


class TestPrefixId(unittest.TestCase):
    def test_suffix_changes_do_not_change_the_prefix(self):
        a = {"system": SYSTEM, "messages": user("diff A")}
        b = {"system": SYSTEM, "messages": user("a completely different diff B")}
        self.assertEqual(prefix_id(a), prefix_id(b))

    def test_prefix_change_changes_the_id(self):
        a = {"system": SYSTEM, "messages": user("d")}
        b = {
            "system": [{**SYSTEM[0], "text": SYSTEM[0]["text"] + "!"}],
            "messages": user("d"),
        }
        self.assertNotEqual(prefix_id(a), prefix_id(b))

    def test_tool_order_is_part_of_the_prefix(self):
        t1 = {"name": "a", "input_schema": {}}
        t2 = {"name": "b", "input_schema": {}}
        one = {"tools": [t1, t2], "system": SYSTEM, "messages": user("d")}
        two = {"tools": [t2, t1], "system": SYSTEM, "messages": user("d")}
        self.assertNotEqual(prefix_id(one), prefix_id(two))

    def test_no_breakpoint_means_no_prefix(self):
        self.assertIsNone(prefix_id({"system": "plain", "messages": user("d")}))


class TestNonStreaming(unittest.TestCase):
    def setUp(self):
        self.cfg = ModelConfig(stream=False)
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(self.tmp.name, "run-t")

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, transport):
        return ClaudeClient(
            self.cfg, self.ledger, api_key="test", transport=transport, max_retries=2
        )

    def test_usage_and_tags_land_in_the_ledger(self):
        transport = json_transport(
            {
                "content": [{"type": "text", "text": '{"findings": []}'}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 300,
                    "cache_creation_input_tokens": 5_000,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 700,
                },
            }
        )
        res = self.client(transport).messages(
            Tags("baseline", "pr-1", 1, "review"),
            system=SYSTEM,
            messages=user("diff"),
            output_schema={"type": "object"},
        )
        self.assertEqual(res.json(), {"findings": []})
        self.assertEqual(res.usage.context_volume, 5_300)

        (rec,) = list(self.ledger.records())
        self.assertEqual(
            (rec.agent, rec.pr_id, rec.pr_ordinal, rec.phase), ("baseline", "pr-1", 1, "review")
        )
        self.assertEqual(rec.model, "claude-opus-5")
        self.assertEqual(rec.effort, "xhigh")
        self.assertIsNotNone(rec.prefix_id)

        sent = transport.calls[0]
        self.assertEqual(
            sent["output_config"], {"effort": "xhigh", "format": {"type": "json_schema", "schema": {"type": "object"}}}
        )
        self.assertNotIn("stream", sent)

    def test_truncation_is_flagged_not_swallowed(self):
        transport = json_transport(
            {
                "content": [{"type": "text", "text": "half a fin"}],
                "stop_reason": "max_tokens",
                "usage": {"input_tokens": 10, "output_tokens": 32000},
            }
        )
        res = self.client(transport).messages(
            Tags("memory", "pr-2", 2, "review"), system=SYSTEM, messages=user("d")
        )
        self.assertTrue(res.truncated)
        self.assertTrue(list(self.ledger.records())[0].truncated)

    def test_refusal_raises_after_logging(self):
        transport = json_transport(
            {"content": [], "stop_reason": "refusal", "usage": {"input_tokens": 10}}
        )
        with self.assertRaises(Refusal):
            self.client(transport).messages(
                Tags("memory", "pr-3", 3, "review"), system=SYSTEM, messages=user("d")
            )
        # The call still cost money, so it is still in the ledger.
        self.assertEqual(len(list(self.ledger.records())), 1)

    def test_retries_then_succeeds(self):
        seq = [
            (429, {"retry-after": "0"}, [b'{"error":"slow down"}']),
            (
                200,
                {},
                [
                    json.dumps(
                        {
                            "content": [{"type": "text", "text": "ok"}],
                            "stop_reason": "end_turn",
                            "usage": {"input_tokens": 1, "output_tokens": 1},
                        }
                    ).encode()
                ],
            ),
        ]

        def transport(url, hdrs, body, timeout):
            return seq.pop(0)

        slept = []
        orig = claude_mod.time.sleep
        claude_mod.time.sleep = slept.append
        try:
            res = self.client(transport).messages(
                Tags("baseline", "pr-4", 4, "review"), system=SYSTEM, messages=user("d")
            )
        finally:
            claude_mod.time.sleep = orig
        self.assertEqual(res.text, "ok")
        self.assertEqual(slept, [0.0])

    def test_non_retryable_status_raises(self):
        transport = json_transport({"error": "bad"}, status=400)
        with self.assertRaises(ClaudeError):
            self.client(transport).messages(
                Tags("baseline", "pr-5", 5, "review"), system=SYSTEM, messages=user("d")
            )

    def test_missing_api_key_is_an_error_not_an_estimate(self):
        client = ClaudeClient(
            self.cfg, self.ledger, api_key=None, transport=json_transport({})
        )
        client._api_key = None
        with self.assertRaises(ClaudeError):
            client.messages(
                Tags("baseline", "pr-6", 6, "review"), system=SYSTEM, messages=user("d")
            )


class TestStreaming(unittest.TestCase):
    def test_sse_accumulates_text_and_usage(self):
        events = [
            {
                "type": "message_start",
                "message": {
                    "usage": {
                        "input_tokens": 420,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 38_000,
                        "output_tokens": 1,
                    }
                },
            },
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm"}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": '{"fin'}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": 'dings":[]}'}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 913}},
            {"type": "message_stop"},
        ]
        raw = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
        # Split mid-line to exercise the line buffer.
        chunks = [raw[i : i + 7] for i in range(0, len(raw), 7)]

        def transport(url, hdrs, body, timeout):
            self.assertTrue(json.loads(body)["stream"])
            return 200, {}, chunks

        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(tmp, "run-s")
            client = ClaudeClient(
                ModelConfig(stream=True), ledger, api_key="t", transport=transport
            )
            res = client.messages(
                Tags("memory", "pr-7", 7, "review"), system=SYSTEM, messages=user("d")
            )
        self.assertEqual(res.json(), {"findings": []})
        self.assertEqual(res.thinking, "hmm")
        self.assertEqual(res.stop_reason, "end_turn")
        self.assertEqual(res.usage.output_tokens, 913)
        self.assertEqual(res.usage.cache_read_input_tokens, 38_000)
        self.assertEqual(res.usage.context_volume, 38_420)

    def test_stream_error_event_raises(self):
        raw = b'data: {"type":"error","error":{"type":"overloaded_error"}}\n\n'

        def transport(url, hdrs, body, timeout):
            return 200, {}, [raw]

        client = ClaudeClient(ModelConfig(stream=True), None, api_key="t", transport=transport)
        with self.assertRaises(ClaudeError):
            client.messages(Tags("memory", "pr-8", 8, "review"), system=SYSTEM, messages=user("d"))


class TestCacheAudit(unittest.TestCase):
    def test_repeated_prefix_with_no_read_warns(self):
        responses = [
            {
                "content": [{"type": "text", "text": "1"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5_000},
            },
            {
                "content": [{"type": "text", "text": "2"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5_010, "cache_read_input_tokens": 0},
            },
        ]

        def transport(url, hdrs, body, timeout):
            return 200, {}, [json.dumps(responses.pop(0)).encode()]

        client = ClaudeClient(
            ModelConfig(stream=False), None, api_key="t", transport=transport
        )
        for i in (1, 2):
            client.messages(
                Tags("baseline", f"pr-{i}", i, "review"), system=SYSTEM, messages=user(f"d{i}")
            )
        self.assertEqual(len(client.cache_warnings), 1)
        self.assertIn("not byte-stable", client.cache_warnings[0])


class TestCountTokens(unittest.TestCase):
    def test_count_tokens_hits_the_endpoint(self):
        seen = {}

        def transport(url, hdrs, body, timeout):
            seen["url"] = url
            return 200, {}, [b'{"input_tokens": 1234}']

        client = ClaudeClient(ModelConfig(), None, api_key="t", transport=transport)
        n = client.count_tokens(system="s", messages=user("d"))
        self.assertEqual(n, 1234)
        self.assertTrue(seen["url"].endswith("/v1/messages/count_tokens"))


if __name__ == "__main__":
    unittest.main()
