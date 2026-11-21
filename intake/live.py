"""OpenAI Responses transport and request construction."""

import http.client
import json
import os
import re
from dataclasses import dataclass

from intake.extraction import PROMPT, SCHEMA, strict_json
from intake.provider import PROVIDER_ERRORS, ProviderError, ProviderReply


HOST = "api.openai.com"
PATH = "/v1/responses"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MODEL_ID = re.compile(r"[A-Za-z0-9_.:-]+")


@dataclass(frozen=True)
class TransportResponse:
    status: int
    body: bytes


def request_body(settings, message):
    return {
        "model": settings.model,
        "store": False,
        "max_output_tokens": settings.max_output_tokens,
        "instructions": PROMPT,
        "input": [{"role": "user", "content": json.dumps({"message": message}, ensure_ascii=False)}],
        "text": {"format": {"type": "json_schema", "name": "access_request",
                            "strict": True, "schema": SCHEMA}},
    }


def request_headers(key):
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def send(headers, body, timeout):
    connection = http.client.HTTPSConnection(HOST, timeout=timeout)
    try:
        connection.request("POST", PATH, body=json.dumps(body).encode(), headers=headers)
        with connection.getresponse() as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1) if response.status == 200 else b""
            return TransportResponse(response.status, payload)
    finally:
        connection.close()


def failure(code):
    return ProviderError(PROVIDER_ERRORS[code], code=code)


class OpenAIProvider:
    def __init__(self, settings, *, environ=None, transport=send):
        environ = os.environ if environ is None else environ
        self._key = environ.get("API_KEY", "").strip()
        if not self._key:
            raise ValueError("set API_KEY before running the OpenAI provider")
        if not self._key.isascii() or any(character.isspace() for character in self._key):
            raise ValueError("API_KEY has an invalid format")
        if not MODEL_ID.fullmatch(settings.model):
            raise ValueError("invalid OpenAI model identifier")
        self.settings = settings
        self.transport = transport

    def extract(self, *, document_id, message):
        try:
            response = self.transport(request_headers(self._key),
                                      request_body(self.settings, message), self.settings.timeout)
        except TimeoutError:
            raise failure("timeout") from None
        except (OSError, http.client.HTTPException):
            raise failure("unavailable") from None
        status = response.status
        if status == 429:
            raise failure("rate_limit")
        if status in (401, 403):
            raise failure("credentials")
        if status >= 500:
            raise failure("unavailable")
        if status != 200:
            raise failure("configuration")
        if len(response.body) > MAX_RESPONSE_BYTES:
            raise failure("malformed")
        try:
            envelope = strict_json(response.body.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise failure("malformed") from None
        if not isinstance(envelope, dict) or self._key in json.dumps(envelope, ensure_ascii=False):
            raise failure("malformed")
        actual_model = envelope.get("model")
        if not isinstance(actual_model, str) or not MODEL_ID.fullmatch(actual_model):
            actual_model = self.settings.model
        return ProviderReply(envelope, f"openai:{actual_model}")


def request_evidence(settings):
    return {
        "endpoint": f"https://{HOST}{PATH}",
        "method": "POST",
        "headers": request_headers("[REDACTED]"),
        "body": request_body(settings, "[REDACTED example message]"),
        "timeout_seconds": settings.timeout,
        "processing_settings": settings.describe(),
        "response_handling": [
            "Classify HTTP rate limits, authentication failures, timeouts and outages with fixed messages.",
            "Require a completed response and completed assistant output. Record refusal or interruption as an error.",
            "Parse the single structured JSON output and validate schema, date format and exact source quotes.",
            "Apply the ordered office policy and persist the attempt with its response provenance.",
        ],
    }
