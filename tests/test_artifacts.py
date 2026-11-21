import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from intake.extraction import ROOT, Source
from intake.live import OpenAIProvider, TransportResponse
from intake.provider import MockProvider
from intake.state import Settings, Store, outcome_for
from test_extraction import MOCKS, REQUESTS, fixture
from test_operations import cli
from test_routing import OWNER
from test_state import route_tuple


class ArtifactTests(unittest.TestCase):
    def test_t20_export_preserves_current_queue_link_and_redacted_request(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            output = Path(directory) / "bundle"
            cli(database, "start", REQUESTS)
            store = Store(database)
            store.retry("IT-002.json", Settings())
            provider = MockProvider(fixture_dir=MOCKS, scripts={
                "IT-002.json": ["rate-limit.json"]})
            store.process(provider, Settings())
            cli(database, "export", "--out", output)
            records = [json.loads(line) for line in (output / "extractions.jsonl").read_text().splitlines()]
            queue = json.loads((output / "queue.json").read_bytes())
            evidence = json.loads((output / "request-evidence.json").read_bytes())
            self.assertEqual(len(records), 20)
            self.assertEqual(len(queue), 20)
            self.assertEqual({row["document_id"] for row in records},
                             {path.name for path in REQUESTS.glob("*.json")})
            for path, row, item in zip(sorted(REQUESTS.glob("*.json")), records, queue, strict=True):
                self.assertEqual(row["source_sha256"], Source.read(path).sha256)
                for field in ("document_id", "attempt_id", "source_sha256", "status", "owner", "next_action"):
                    self.assertEqual(row[field], item[field])
                self.assertEqual(row["response_source"], "mock")
                if path.name == "IT-002.json":
                    self.assertIsNone(row["extraction"])
                    self.assertIn("rate limit", row["error"])
                else:
                    self.assertIsInstance(row["extraction"], dict)
                    self.assertIsNone(row["error"])
            self.assertEqual(evidence["headers"]["Authorization"], "Bearer [REDACTED]")
            self.assertEqual(evidence["body"]["model"], "gpt-4.1-mini")
            self.assertGreaterEqual(len(evidence["response_handling"]), 3)

            live = Settings(provider="openai", model="fixture-alternate-model")
            reply = json.dumps({**fixture("it-001.json"), "model": "fixture-model-snapshot"})
            provider = OpenAIProvider(live, environ={"API_KEY": "synthetic-credential"},
                                      transport=lambda *args: TransportResponse(200, reply.encode()))
            store.retry("IT-001.json", live)
            store.process(provider, live)
            output = output.parent / "live-origin-bundle"
            cli(database, "export", "--out", output)
            record = json.loads((output / "extractions.jsonl").read_text().splitlines()[0])
            evidence = json.loads((output / "request-evidence.json").read_bytes())
            self.assertEqual(record["response_source"], "openai:fixture-model-snapshot")
            self.assertEqual(evidence["body"]["model"], "fixture-alternate-model")
            self.assertNotIn("synthetic-credential", json.dumps(record))

    def test_export_holds_one_snapshot_and_cleans_up_interrupted_publication(self):
        import intake.artifacts as artifacts

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            database = path / "state.sqlite3"
            store = Store(database)
            settings = Settings()
            source = Source.read(REQUESTS / "IT-001.json")
            store.enqueue([source], settings)
            store.process(MockProvider(fixture_dir=MOCKS, scripts={
                source.document_id: ["it-001.json"]}), settings)
            earlier, = store.current()
            output = path / "bundle"
            write_json = artifacts.write_json

            def concurrent_commit(destination, value):
                if destination.name == "queue.json":
                    another = Store(database)
                    another.retry(source.document_id, settings)
                    another.process(MockProvider(fixture_dir=MOCKS, scripts={
                        source.document_id: ["rate-limit.json"]}), settings)
                write_json(destination, value)

            with patch.object(artifacts, "write_json", side_effect=concurrent_commit):
                artifacts.export_snapshot(store, output)
            self.assertGreater(store.current()[0]["attempt_id"], earlier["attempt_id"])
            record = json.loads((output / "extractions.jsonl").read_text())
            queue, = json.loads((output / "queue.json").read_text())
            self.assertEqual(record["attempt_id"], earlier["attempt_id"])
            self.assertEqual(queue["attempt_id"], earlier["attempt_id"])
            self.assertEqual(route_tuple(queue), OWNER)

            failed = path / "failed-bundle"
            with patch.object(artifacts, "write_json", side_effect=OSError("write interrupted")):
                with self.assertRaises(OSError):
                    artifacts.export_snapshot(store, failed)
            self.assertFalse(failed.exists())
            self.assertEqual(list(path.glob(".intake-export-*")), [])
            self.assertEqual(artifacts.export_snapshot(store, failed)["records"], 1)

    def test_t21_paraphrase_reaches_provider_and_keeps_expected_facts(self):
        from intake.accuracy import read_cases

        source = Source.read(ROOT / "tests/exports/IT-021.json")
        original = Source.read(REQUESTS / "IT-001.json")
        self.assertNotEqual(source.message, original.message)
        expected = read_cases()[-1]
        scripts = {"IT-021.json": ["it-021.json"]}
        seen = []

        class Provider(MockProvider):
            def extract(self, **kwargs):
                seen.append(kwargs["message"])
                return super().extract(**kwargs)

        result = outcome_for(source, Provider(fixture_dir=MOCKS, scripts=scripts), Settings())
        self.assertEqual(seen, [source.message])
        self.assertEqual(expected.source, source)
        self.assertEqual(route_tuple(result), OWNER)
        self.assertEqual({field: result["extraction"][field] for field in expected.facts},
                         expected.facts)
        self.assertTrue(all(quote in source.message for quote in result["extraction"]["supporting_quotes"]))

    def test_t22_accuracy_counts_exact_scalar_matches_and_reports_mismatches(self):
        from intake.accuracy import compare, measure, read_cases

        cases = read_cases()
        settings = Settings()
        scripts = {case.source.document_id: [case.source.document_id.lower()] for case in cases}
        observed = measure(cases, MockProvider(fixture_dir=MOCKS, scripts=scripts), settings)
        result = compare(cases, observed, settings)
        self.assertEqual((result["exact_matches"], result["comparisons"]), (252, 252))
        self.assertEqual(result["response_sources"], ["mock"])
        self.assertEqual(result["settings"]["model"], "gpt-4.1-mini")
        first = observed["IT-001.json"]
        wrong_date = {**first, "extraction": {**first["extraction"], "end_date": "2026-11-09"}}
        result = compare(cases, {**observed, "IT-001.json": wrong_date}, settings)
        self.assertEqual(result["exact_matches"], 251)
        discrepancy, = result["mismatches"]
        self.assertEqual((discrepancy["document_id"], discrepancy["field"], discrepancy["expected"],
                          discrepancy["actual"], discrepancy["category"]),
                         ("IT-001.json", "end_date", "2026-11-06", "2026-11-09", "factual-difference"))
        self.assertIn("2026-11-06", discrepancy["source_message"])
        purpose = {**first, "extraction": {**first["extraction"], "business_purpose":
                   "Review the weekly conversion dashboard using the revised event definitions"}}
        result = compare(cases, {**observed, "IT-001.json": purpose}, settings)
        self.assertEqual(result["exact_matches"], 251)
        self.assertEqual(result["mismatches"][0]["category"], "purpose-review")
        self.assertEqual(result["factual_differences"], 0)
        for record in (None, "invalid record", {**first, "error": "failure"}, {**first, "extraction": {}},
                       {**first, "source_sha256": "outdated"}):
            with self.subTest(record=record):
                result = compare(cases, {**observed, "IT-001.json": record}, settings)
                self.assertEqual((result["exact_matches"], result["comparisons"]), (240, 252))
                self.assertEqual(result["failed_records"][0]["document_id"], "IT-001.json")
