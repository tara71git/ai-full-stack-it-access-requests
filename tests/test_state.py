import json
import tempfile
import unittest
from pathlib import Path

from intake.provider import MockProvider
from test_extraction import MOCKS, REQUESTS
from test_routing import OWNER, SECURITY


def route_tuple(record):
    return tuple(record[key] for key in ("status", "owner", "next_action"))


class StateTests(unittest.TestCase):
    def setUp(self):
        from intake.extraction import Source
        from intake.state import Settings, Store

        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.database = Path(work.name) / "intake.sqlite3"
        self.store = Store(self.database)
        self.settings = Settings()
        self.first = Source.read(REQUESTS / "IT-001.json")
        self.second = Source.read(REQUESTS / "IT-002.json")
        corrected = json.dumps({"message": self.first.message +
                               " Correction: the access end date is 2026-11-09."}).encode()
        self.corrected = Source(self.first.document_id, corrected)

    def process(self, sources, scripts, settings=None):
        provider = MockProvider(fixture_dir=MOCKS, scripts=scripts)
        self.store.enqueue(sources, settings or self.settings)
        self.store.process(provider, settings or self.settings)
        return provider.calls

    def test_t10_unchanged_rerun_reuses_success_and_one_current_entry(self):
        self.assertEqual(self.process([self.first], {"IT-001.json": ["it-001.json"]}),
                         ("IT-001.json",))
        original, = self.store.current()
        self.assertEqual(self.process([self.first], {}), ())
        self.assertEqual(self.store.current(), [original])
        self.assertEqual(self.store.history("IT-001.json"), [original])

    def test_t11_changed_source_replaces_current_and_history_keeps_hash_and_facts(self):
        self.process([self.first], {"IT-001.json": ["it-001.json"]})
        self.process([self.corrected], {"IT-001.json": ["it-001-corrected.json"]})
        current, = self.store.current()
        earlier, latest = self.store.history("IT-001.json")
        self.assertEqual(current, latest)
        self.assertEqual(route_tuple(current), OWNER)
        self.assertEqual(current["source_sha256"], self.corrected.sha256)
        self.assertEqual(current["extraction"]["end_date"], "2026-11-09")
        self.assertEqual(earlier["source_sha256"], self.first.sha256)
        self.assertEqual(earlier["extraction"]["end_date"], "2026-11-06")
        queue, = self.store.queue()
        for field in ("document_id", "source_sha256", "attempt_id"):
            self.assertEqual(queue[field], current[field])

    def test_t12_omission_model_refresh_and_policy_refresh_are_identifiable(self):
        from dataclasses import replace

        self.process([self.first, self.second], {
            "IT-001.json": ["it-001.json"], "IT-002.json": ["it-002.json"]})
        changed = replace(self.settings, model="another-model")
        self.process([self.first], {"IT-001.json": ["it-001.json"]}, changed)
        first, second = self.store.current()
        self.assertEqual(route_tuple(second), SECURITY)
        self.assertEqual(len(self.store.history(second["document_id"])), 1)
        self.assertEqual(first["settings"]["model"], "another-model")
        new_policy = replace(changed, security_environments=("production", "staging"))
        self.store.refresh(new_policy)
        provider = MockProvider(fixture_dir=MOCKS, scripts={
            "IT-001.json": ["it-001.json"], "IT-002.json": ["it-002.json"]})
        self.store.process(provider, new_policy)
        earlier, model_refresh, policy_refresh = self.store.history("IT-001.json")
        self.assertEqual({row["source_sha256"] for row in (earlier, model_refresh, policy_refresh)},
                         {self.first.sha256})
        self.assertEqual(earlier["settings"]["policy_version"],
                         model_refresh["settings"]["policy_version"])
        self.assertNotEqual(model_refresh["settings"]["policy_version"],
                            policy_refresh["settings"]["policy_version"])
        self.assertEqual(route_tuple(policy_refresh), SECURITY)
        self.assertEqual(route_tuple(earlier), OWNER)
