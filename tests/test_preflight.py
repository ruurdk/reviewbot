import unittest

from reviewbot.memory import MEMORY_TYPES, AgentMemoryClient
from reviewbot import preflight
from tests.fakes import FakeMemoryService


def client(service):
    return AgentMemoryClient(
        "store-1",
        base_url="https://example.invalid",
        api_key="k",
        namespace="redis-py-run-1",
        transport=service.transport,
    )


class UnregisteredTypes(FakeMemoryService):
    """Reproduces the live failure: create rejects an unregistered type."""

    def __init__(self, registered=()):
        super().__init__()
        self.registered = set(registered)

    def _create(self, payload):
        for rec in payload["memories"]:
            if rec.get("memoryType") not in self.registered:
                return {
                    "created": [],
                    "errors": [
                        {
                            "id": rec["id"],
                            "error": f'memory type "{rec.get("memoryType")}" is not registered on this store',
                        }
                    ],
                }
        return super()._create(payload)


class TestPreflight(unittest.TestCase):
    def test_a_provisioned_store_is_ready(self):
        service = FakeMemoryService()
        result = preflight.run(client(service), timeout=1)
        self.assertTrue(result.ready, result.render())
        self.assertEqual(result.registered_types, list(MEMORY_TYPES))
        self.assertIsNotNone(result.visibility_lag_s)
        # Probe records are cleaned up, so a preflight leaves no residue.
        self.assertEqual(service.records, {})

    def test_unregistered_types_are_named_not_guessed_at(self):
        service = UnregisteredTypes(registered={"repo_convention"})
        result = preflight.run(client(service), timeout=1)
        self.assertFalse(result.ready)
        self.assertEqual(result.registered_types, ["repo_convention"])
        self.assertEqual(result.unregistered_types, ["review_finding", "review_policy"])
        text = result.render()
        self.assertIn("not registered on this store", text)
        self.assertIn("docs/store-provisioning.md", text)

    def test_probe_writes_go_to_an_isolated_namespace(self):
        service = FakeMemoryService()
        preflight.run(client(service), measure_visibility=False)
        namespaces = {
            call[2]["memories"][0]["namespace"]
            for call in service.calls
            if call[0] == "POST" and call[1] == "long-term-memory"
        }
        self.assertEqual(namespaces, {preflight.PROBE_NAMESPACE})

    def test_visibility_timeout_is_a_failure_not_a_pass(self):
        service = FakeMemoryService(visibility_lag=10_000)
        result = preflight.run(client(service), timeout=0.05)
        self.assertFalse(result.ready)
        self.assertIsNone(result.visibility_lag_s)
        self.assertIn("not searchable", result.render())

    def test_an_unhealthy_store_short_circuits(self):
        class Down(FakeMemoryService):
            def transport(self, method, url, headers, body, timeout):
                return 503, {}, b'{"message":"unavailable"}'

        result = preflight.run(client(Down()), timeout=1)
        self.assertFalse(result.ready)
        self.assertEqual(len(result.checks), 1)


if __name__ == "__main__":
    unittest.main()
