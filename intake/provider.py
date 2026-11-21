"""Provider interface and fixture-backed implementation for offline intake."""

import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Callable, Mapping, Protocol, Sequence


class ProviderError(Exception):
    """The provider could not return a response."""


@dataclass(frozen=True)
class ProviderReply:
    envelope: dict[str, object]
    response_source: str


class Provider(Protocol):
    def extract(self, *, document_id: str, message: str) -> ProviderReply:
        """Return the provider's raw response envelope and its source."""
        ...


class MockProvider:
    """Consume each document's fixture script in call order."""

    def __init__(
        self,
        *,
        fixture_dir: Path,
        scripts: Mapping[str, Sequence[str]],
        before_reply: Callable[[str], None] | None = None,
    ) -> None:
        self._fixture_dir = fixture_dir
        self._scripts = {document_id: deque(fixtures) for document_id, fixtures in scripts.items()}
        self._before_reply = before_reply
        self._calls: list[str] = []
        self._lock = Lock()

    @property
    def calls(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._calls)

    def extract(self, *, document_id: str, message: str) -> ProviderReply:
        with self._lock:
            self._calls.append(document_id)
            script = self._scripts.get(document_id)
            if not script:
                raise ProviderError(f"No remaining mock reply for {document_id}")
            fixture = script.popleft()

        # Wait outside the lock so a blocked reply leaves other documents free to run.
        if self._before_reply is not None:
            self._before_reply(document_id)

        try:
            envelope = json.loads((self._fixture_dir / fixture).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ProviderError(f"Unable to read mock reply {fixture} for {document_id}") from error
        if not isinstance(envelope, dict):
            raise ProviderError(f"Mock reply {fixture} must contain a JSON object")
        if "transport_error" in envelope:
            raise ProviderError(str(envelope["transport_error"]))
        return ProviderReply(envelope=envelope, response_source="mock")
