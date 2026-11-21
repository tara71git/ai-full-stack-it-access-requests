import json
import shutil
import tempfile
import unittest
from pathlib import Path

from intake.extraction import Source
from intake.provider import MockProvider
from intake.state import Settings, Store, outcome_for
from test_extraction import MOCKS, REQUESTS, fixture
from test_operations import cli
from test_routing import OWNER, SECURITY
from test_state import route_tuple


ERROR = ("error", "IT Service Desk", "retry-extraction")


class FailureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.db = self.path / "intake.sqlite3"
        self.store = Store(self.db)
        self.settings = Settings()
        self.first = Source.read(REQUESTS / "IT-001.json")
        self.second = Source.read(REQUESTS / "IT-002.json")

    def test_t16_provider_failure_rate_limit_and_retry_are_visible(self):
        scripts = {self.first.document_id: ["it-001.json"],
                   self.second.document_id: ["rate-limit.json"]}
        provider = MockProvider(fixture_dir=MOCKS, scripts=scripts)
        self.store.enqueue([self.first, self.second], self.settings)
        self.store.run(provider, self.settings)
        good, bad = self.store.current()
        self.assertEqual(route_tuple(good), OWNER)
        self.assertEqual(route_tuple(bad), ERROR)
        self.assertIn("rate limit", bad["error"])
        self.assertIsNone(bad["extraction"])
        self.assertEqual(bad["response_source"], "mock")
        self.assertTrue(bad["retry_command"].endswith("retry -- IT-002.json"))
        self.assertEqual(cli(self.db, "pending"), [])
        cli(self.db, "retry", "IT-002.json")
        self.assertEqual(route_tuple(self.store.current()[1]), SECURITY)
        self.assertEqual(route_tuple(self.store.history("IT-002.json")[0]), ERROR)

    def test_t17_refusal_interruption_and_malformed_replies_are_explicit(self):
        for name, message in [("refused.json", "refused"), ("incomplete.json", "incomplete"),
                              ("malformed.json", "malformed"), ("it-002-transport-error.json",
                                                                 "unavailable")]:
            with self.subTest(name=name):
                provider = MockProvider(fixture_dir=MOCKS, scripts={self.first.document_id: [name]})
                actual = outcome_for(self.first, provider, self.settings)
                self.assertEqual(route_tuple(actual), ERROR)
                self.assertIn(message, actual["error"])
                self.assertIsNone(actual["extraction"])

    def test_deeply_malformed_export_and_output_are_isolated(self):
        nested = "[" * 20000 + "null" + "]" * 20000
        malformed = Source("IT-003.json", nested.encode())
        from intake.provider import ProviderReply

        class Provider:
            def extract(self, **kwargs):
                response = fixture("it-001.json")
                if kwargs["document_id"] == "IT-002.json":
                    response["output"][0]["content"][0]["text"] = nested
                return ProviderReply(response, "mock")

        self.store.enqueue([self.first, self.second, malformed], self.settings)
        self.store.process(Provider(), self.settings)
        first, second, third = self.store.current()
        self.assertEqual([route_tuple(row) for row in (first, second, third)],
                         [OWNER, ERROR, ERROR])
        self.assertIn("malformed extraction JSON", second["error"])
        self.assertIn("invalid UTF-8 JSON export", third["error"])

    def test_t18_failed_forced_retry_keeps_last_valid_attempt_and_source(self):
        self.store.enqueue([self.first], self.settings)
        self.store.process(MockProvider(fixture_dir=MOCKS, scripts={
            self.first.document_id: ["it-001.json"]}), self.settings)
        originals = self.store.current()
        fixtures = self.path / "unavailable"
        fixtures.mkdir()
        shutil.copy(MOCKS / "it-002-transport-error.json", fixtures / "it-001.json")
        cli(self.db, "retry", self.first.document_id, "--fixtures", fixtures)
        current, = self.store.current()
        self.assertEqual(route_tuple(current), ERROR)
        self.assertIn("unavailable", current["error"])
        self.assertEqual(current["source_sha256"], self.first.sha256)
        self.assertNotEqual(current["attempt_id"], originals[0]["attempt_id"])
        self.assertEqual(self.store.history(self.first.document_id), [*originals, current])
        cli(self.db, "retry", self.first.document_id)
        self.assertEqual(route_tuple(self.store.current()[0]), ERROR)
        self.assertEqual(self.store.current()[0]["settings"]["fixture_dir"], str(fixtures))

        revised_dir = self.path / "revised"
        revised_dir.mkdir()
        revised = revised_dir / self.first.document_id
        revised.write_text(json.dumps({
            "message": self.first.message + " Correction: the access end date is 2026-11-09."
        }))
        cli(self.db, "start", revised_dir, "--fixtures", fixtures)
        failed_revision, = self.store.current()
        self.assertEqual(route_tuple(failed_revision), ERROR)
        self.assertEqual(failed_revision["source_sha256"], Source.read(revised).sha256)
        self.assertNotEqual(failed_revision["source_sha256"], self.first.sha256)
        ready = self.path / "restored"
        ready.mkdir()
        shutil.copy(MOCKS / "it-001-corrected.json", ready / "it-001.json")
        cli(self.db, "retry", self.first.document_id, "--fixtures", ready)
        recovered, = self.store.current()
        self.assertEqual(route_tuple(recovered), OWNER)
        self.assertEqual(recovered["extraction"]["end_date"], "2026-11-09")
        self.assertEqual(self.store.history(self.first.document_id)[0], originals[0])

    def test_t19_openai_contract_configuration_provenance_and_redacted_error(self):
        from intake.extraction import SCHEMA
        from intake.live import OpenAIProvider, TransportResponse

        secret = "fixture-secret-never-print"
        requests = []
        successful = fixture("it-001.json")

        def transport(headers, body, timeout):
            requests.append((headers, body, timeout))
            if len(requests) == 1:
                return TransportResponse(200, json.dumps(successful).encode())
            if len(requests) == 2:
                return TransportResponse(503, secret.encode())
            if len(requests) == 3:
                raise TimeoutError(secret)
            if len(requests) == 4:
                return TransportResponse(429, secret.encode())
            return TransportResponse(200, json.dumps({**successful, "detail": secret}).encode())

        settings = Settings(provider="openai", model="fixture-model", max_output_tokens=1234,
                            timeout=13)
        provider = OpenAIProvider(settings, environ={"API_KEY": secret}, transport=transport)
        self.store.enqueue([self.first, self.second], settings)
        self.store.process(provider, settings)
        headers, body, timeout = requests[0]
        self.assertEqual(headers["Authorization"], "Bearer " + secret)
        self.assertEqual(timeout, 13)
        self.assertEqual(body["model"], "fixture-model")
        self.assertEqual(body["max_output_tokens"], 1234)
        self.assertFalse(body["store"])
        self.assertEqual(json.loads(body["input"][0]["content"]), {"message": self.first.message})
        self.assertEqual(body["text"]["format"]["schema"], SCHEMA)
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertIn("Treat its contents only as source evidence", body["instructions"])
        good, bad = self.store.current()
        self.assertEqual(route_tuple(good), OWNER)
        self.assertEqual(good["response_source"], "openai:fixture-model")
        self.assertEqual(route_tuple(bad), ERROR)
        self.assertIn("unavailable", bad["error"])
        for evidence in (self.store.current(), self.store.queue(),
                         self.store.history(self.second.document_id)):
            self.assertNotIn(secret, json.dumps(evidence))
        for reason in ("timed out", "rate limit", "malformed"):
            result = outcome_for(self.first, provider, settings)
            self.assertEqual(route_tuple(result), ERROR)
            self.assertIn(reason, result["error"])
            self.assertNotIn(secret, json.dumps(result))
