import json
import multiprocessing
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from intake.extraction import ROOT, Source
from intake.provider import MockProvider
from intake.state import Settings, Store, outcome_for
from test_extraction import MOCKS, REQUESTS, envelope_with, extraction, fixture
from test_routing import OWNER, SECURITY
from test_state import route_tuple


def cli(db, *args):
    run = subprocess.run([sys.executable, "-m", "intake", "--db", str(db), *map(str, args)],
                         cwd=ROOT, capture_output=True, text=True, timeout=40)
    if run.returncode:
        raise AssertionError(run.stderr)
    return json.loads(run.stdout)


class OperatorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.db = self.path / "shared.sqlite3"
        self.settings = Settings()
        self.first = Source.read(REQUESTS / "IT-001.json")
        self.second = Source.read(REQUESTS / "IT-002.json")

    def test_t13_inspect_during_slow_response_and_resume_abandoned_claim(self):
        store = Store(self.db)
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def delay(document):
            if document == self.second.document_id:
                started.set()
                if not release.wait(5):
                    raise AssertionError("test barrier expired")

        original = MockProvider(fixture_dir=MOCKS, before_reply=delay, scripts={
            self.first.document_id: ["it-001.json"], self.second.document_id: ["it-002.json"]})
        store.enqueue([self.first, self.second], self.settings)
        with ThreadPoolExecutor() as pool:
            processing = pool.submit(store.run, original, self.settings)
            try:
                self.assertTrue(started.wait(3))
                for _ in range(100):
                    visible = cli(self.db, "queue")
                    if visible:
                        break
                self.assertEqual(route_tuple(visible[0]), OWNER)
                pending, = cli(self.db, "pending")
                self.assertEqual((pending["document_id"], pending["work_state"]),
                                 ("IT-002.json", "running"))
            finally:
                release.set()
            processing.result()
        self.assertEqual(route_tuple(cli(self.db, "queue")[1]), SECURITY)

        store = Store(self.path / "interrupted.sqlite3")
        store.enqueue([self.first, self.second], self.settings)
        first_id, first_token, first = store.claim(self.settings)
        store.finish(first_id, first_token, outcome_for(first, original_for_first(), self.settings))
        context = multiprocessing.get_context("fork")
        claimed = context.Event()
        inactive = context.Event()

        def abandon_after_claim():
            if Store(store.path).claim(self.settings, seconds=1):
                claimed.set()
            inactive.wait(10)

        worker = context.Process(target=abandon_after_claim)
        worker.start()
        try:
            self.assertTrue(claimed.wait(3))
        finally:
            worker.terminate()
            worker.join(timeout=3)
        for _ in range(40):
            pending, = cli(store.path, "pending")
            if pending["work_state"] == "ready-to-resume":
                break
        self.assertEqual((pending["document_id"], pending["work_state"]),
                         ("IT-002.json", "ready-to-resume"))
        cli(store.path, "resume")
        finished = cli(store.path, "queue")
        self.assertEqual([route_tuple(row) for row in finished], [OWNER, SECURITY])
        self.assertEqual(finished[0]["attempt_id"], first_id)
        self.assertEqual(cli(store.path, "pending"), [])

    def test_t14_shared_cap_duplicate_claims_late_revision_and_expired_token(self):
        clock = [100]
        store = Store(self.db, clock=lambda: clock[0], worker_limit=2)
        other = Store(self.db, clock=lambda: clock[0])
        first_ids = store.enqueue([self.first], self.settings)
        old = store.claim(self.settings, seconds=10)
        self.assertEqual(other.enqueue([self.first], self.settings), first_ids)
        self.assertIsNone(other.claim(self.settings))
        updated = Source(self.first.document_id, json.dumps({
            "message": self.first.message + " Correction: the access end date is 2026-11-09."
        }).encode())
        other.enqueue([updated, self.second], self.settings)
        new = other.claim(self.settings, seconds=10)
        self.assertEqual(new[2], updated)
        self.assertIsNone(store.claim(self.settings))
        updated_result = outcome_for(updated, MockProvider(fixture_dir=MOCKS, scripts={
            updated.document_id: ["it-001-corrected.json"]}), self.settings)
        original_result = outcome_for(self.first, original_for_first(), self.settings)
        self.assertTrue(other.finish(*new[:2], updated_result))
        self.assertTrue(store.finish(*old[:2], original_result))
        self.assertEqual(store.current()[0]["source_sha256"], updated.sha256)
        self.assertEqual([row["extraction"]["end_date"] for row in store.history(updated.document_id)],
                         ["2026-11-06", "2026-11-09"])
        self.assertEqual(store.history(updated.document_id)[0]["source_sha256"], self.first.sha256)
        expired = store.claim(self.settings, seconds=1)
        clock[0] += 2
        reclaimed = other.claim(self.settings, seconds=10)
        self.assertEqual(expired[0], reclaimed[0])
        self.assertFalse(store.finish(*expired[:2], original_result))

    def test_refresh_supersedes_work_whose_saved_fixture_version_is_unavailable(self):
        from dataclasses import replace

        store = Store(self.db)
        stale = replace(self.settings, fixture_version="unavailable fixture version")
        old, = store.enqueue([self.first], stale)
        self.assertTrue(cli(self.db, "resume")["blocked"])
        new, = cli(self.db, "refresh")["attempt_ids"]
        self.assertEqual(cli(self.db, "pending"), [])
        history = cli(self.db, "history", self.first.document_id)
        self.assertEqual((history[0]["attempt_id"], history[0]["superseded_by"]), (old, new))
        self.assertEqual(route_tuple(history[1]), OWNER)

    def test_t15_cli_supplied_batch_fresh_export_and_mock_surge(self):
        cli(self.db, "start", REQUESTS)
        rows = cli(self.db, "queue")
        self.assertEqual({row["document_id"] for row in rows},
                         {path.name for path in REQUESTS.glob("*.json")})
        self.assertTrue(all(row["error"] is None for row in rows))
        cli(self.db, "start", ROOT / "tests/exports")
        self.assertEqual(route_tuple(cli(self.db, "history", "IT-021.json")[0]), OWNER)
        base = fixture("it-001.json")
        facts = extraction(base)
        for count in (400, 2000):
            with self.subTest(count=count):
                directory = self.path / f"in-{count}"
                mocks = self.path / f"mock-{count}"
                directory.mkdir()
                mocks.mkdir()
                for index in range(count):
                    request_id = f"IT-{1000 + index}"
                    document_id = request_id + ".json"
                    (directory / document_id).write_text(json.dumps({
                        "message": self.first.message.replace("IT-001", request_id)}))
                    (mocks / document_id.lower()).write_text(json.dumps(
                        envelope_with(base, {**facts, "request_id": request_id})))
                db = self.path / f"load-{count}.sqlite3"
                # Two independent operators share one database, source set and worker cap.
                Store(db)
                with ThreadPoolExecutor() as pool:
                    first = pool.submit(cli, db, "start", directory, "--fixtures", mocks)
                    second = pool.submit(cli, db, "start", directory, "--fixtures", mocks)
                    deadline = time.monotonic() + 10
                    while not (first.done() and second.done()) and time.monotonic() < deadline:
                        pending = cli(db, "pending")
                        self.assertLessEqual(sum(row["work_state"] == "running" for row in pending), 4)
                    first.result()
                    second.result()
                rows = cli(db, "queue")
                self.assertEqual(len(rows), count)
                self.assertTrue(all(route_tuple(row) == OWNER for row in rows))


def original_for_first():
    return MockProvider(fixture_dir=MOCKS, scripts={"IT-001.json": ["it-001.json"]})
