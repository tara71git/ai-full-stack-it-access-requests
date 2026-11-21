"""T01 Mock provider, exercised with the supplied message exports."""

import json
import socket
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
REQUESTS = ROOT / "materials" / "requests"
MOCKS = ROOT / "tests" / "mock_responses"


class MockProviderTest(unittest.TestCase):
    def test_t01_mock_provider_isolates_a_scripted_transport_failure(self):
        with patch.object(socket, "socket", side_effect=AssertionError("network call")) as network:
            from intake.provider import MockProvider, ProviderError

            provider = MockProvider(
                fixture_dir=MOCKS,
                scripts={
                    "IT-001.json": ("it-001.json",),
                    "IT-002.json": ("it-002-transport-error.json",),
                },
            )
            it_001 = json.loads((REQUESTS / "IT-001.json").read_text(encoding="utf-8"))
            it_002 = json.loads((REQUESTS / "IT-002.json").read_text(encoding="utf-8"))

            with self.assertRaisesRegex(ProviderError, "Transport unavailable"):
                provider.extract(document_id="IT-002.json", message=it_002["message"])
            reply = provider.extract(document_id="IT-001.json", message=it_001["message"])

        saved_envelope = json.loads((MOCKS / "it-001.json").read_text(encoding="utf-8"))
        self.assertEqual(reply.envelope, saved_envelope)
        self.assertEqual(reply.response_source, "mock")
        self.assertEqual(provider.calls, ("IT-002.json", "IT-001.json"))
        for field in ("owner", "next_action", "route"):
            self.assertNotIn(field, reply.envelope)
            self.assertFalse(hasattr(reply, field))
        network.assert_not_called()
