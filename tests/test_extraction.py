import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from intake.provider import MockProvider

ROOT = Path(__file__).resolve().parents[1]
REQUESTS = ROOT / "materials/requests"
MOCKS = ROOT / "tests/mock_responses"


def fixture(name):
    return json.loads((MOCKS / name).read_text())


def extraction(envelope):
    return json.loads(envelope["output"][0]["content"][0]["text"])


def envelope_with(envelope, facts):
    envelope["output"][0]["content"][0]["text"] = json.dumps(facts)
    return envelope


class ExtractionTests(unittest.TestCase):
    def test_t02_decoded_200_word_export_original_bytes_filename_and_message_boundary(self):
        from intake.extraction import Source, extract
        from intake.provider import ProviderReply

        head = 'Lucía Vega from Analytics, IT-101 dated 2026-10-22. I need new read access to Atlas in staging from 2026-10-23 through 2026-11-06 to check the weekly conversion dashboard.'
        tail = 'My manager is "Priya Shah".'
        remaining = 200 - len(head.split()) - len(tail.split())
        filler = ("The sample records are ready. " * (remaining // 5)).split()
        filler += "Sample records are loaded.".split()[:remaining % 5]
        message = " ".join([head, *filler, tail])
        self.assertEqual(len(message.split()), 200)
        raw = json.dumps({"subject": "Production admin request", "exported_at": "2026-11-20",
                          "message": message}).encode()
        calls = []
        case = extraction(fixture("it-001.json"))
        case.update(request_id="IT-101", requester="Lucía Vega", supporting_quotes=[tail])

        class Provider:
            def extract(self, **kwargs):
                calls.append(kwargs)
                return ProviderReply(envelope_with(fixture("it-001.json"), case), "mock")

        with tempfile.TemporaryDirectory() as dirname:
            path = Path(dirname) / "IT-101.json"
            path.write_bytes(raw)
            source = Source.read(path)
            facts, origin = extract(source, Provider())
        self.assertEqual(source.document_id, "IT-101.json")
        self.assertEqual(source.sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(calls, [{"document_id": "IT-101.json", "message": message}])
        self.assertEqual((facts["requester"], facts["request_date"], facts["manager"], origin),
                         ("Lucía Vega", "2026-10-22", "Priya Shah", "mock"))
        self.assertEqual(facts["supporting_quotes"], [tail])
        duplicate = Source("copy-IT-101.json", raw)
        self.assertEqual(duplicate.sha256, source.sha256)
        self.assertNotEqual(duplicate.document_id, source.document_id)
        self.assertEqual(extract(duplicate, Provider())[0]["request_id"], "IT-101")
        from intake.state import Settings, Store

        with tempfile.TemporaryDirectory() as dirname:
            store = Store(Path(dirname) / "intake.sqlite3")
            settings = Settings()
            store.enqueue([source, duplicate], settings)
            store.process(Provider(), settings)
            self.assertEqual({row["document_id"]: row["extraction"]["request_id"]
                              for row in store.current()},
                             {"IT-101.json": "IT-101", "copy-IT-101.json": "IT-101"})

    def test_t03_schema_completion_dates_and_exact_quotes(self):
        from intake.extraction import ExtractionError, Source, decode_reply

        message = Source.read(REQUESTS / "IT-001.json").message
        baseline = extraction(fixture("it-001.json"))
        self.assertEqual(decode_reply(fixture("it-001.json"), message), baseline)
        variants = [[], {**baseline, "extra": None}]
        variants.extend({k: v for k, v in baseline.items() if k != field} for field in baseline)
        for field in baseline.keys() - {"issues", "supporting_quotes"}:
            variants.append({**baseline, field: 42})
            self.assertEqual(decode_reply(envelope_with(fixture("it-001.json"), {
                **baseline, field: None}), message)[field], None)
        for field, allowed in {"kind": ["new", "change", "withdrawal", "other"],
                               "environment": ["staging", "production"],
                               "permission": ["read", "write", "admin"]}.items():
            variants.append({**baseline, field: "unexpected"})
            for value in allowed:
                self.assertEqual(decode_reply(envelope_with(fixture("it-001.json"), {
                    **baseline, field: value}), message)[field], value)
        for field in ("issues", "supporting_quotes"):
            variants.extend([{**baseline, field: "text"}, {**baseline, field: [1]}])
        variants += [{**baseline, "start_date": "2026-02-30"},
                     {**baseline, "end_date": "20261106"},
                     {**baseline, "supporting_quotes": ["an invented source quote"]}]
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(ExtractionError):
                decode_reply(envelope_with(fixture("it-001.json"), variant), message)
        with self.assertRaisesRegex(ExtractionError, "incomplete"):
            decode_reply({**fixture("it-001.json"), "status": "incomplete"}, message)

    def test_t04_explicit_correction_relative_dates_requested_permission_and_filename_mismatch(self):
        from intake.extraction import Source, extract

        expected = {
            7: {"kind": "change", "permission": "write"},
            8: {"environment": "staging"},
            11: {"start_date": None, "end_date": None},
            15: {"start_date": "2026-10-29", "end_date": "2026-10-31"},
            16: {"request_id": "IT-061"},
            19: {"kind": "change", "permission": "read"},
        }
        for number, wanted in expected.items():
            document_id = f"IT-{number:03}.json"
            with self.subTest(document_id=document_id):
                source = Source.read(REQUESTS / document_id)
                provider = MockProvider(fixture_dir=MOCKS, scripts={
                    document_id: [document_id.lower()]})
                actual, _ = extract(source, provider)
                self.assertEqual({field: actual[field] for field in wanted}, wanted)
                if number == 11:
                    for field in ("start_date", "end_date"):
                        self.assertTrue(any(field in issue and "relative" in issue
                                            for issue in actual["issues"]))
                if number == 16:
                    self.assertTrue(any("IT-061" in issue and "IT-016" in issue
                                        for issue in actual["issues"]))
