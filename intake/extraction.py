"""Read one export and validate the provider response against the supplied contract."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from intake.provider import Provider


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "snapshot.schema.json"
PROMPT_PATH = Path(__file__).with_name("extraction_prompt.txt")
POLICY_PATH = ROOT / "materials" / "office-policy.md"
SCHEMA_BYTES = SCHEMA_PATH.read_bytes()
SCHEMA = json.loads(SCHEMA_BYTES)
PROMPT_BYTES = PROMPT_PATH.read_bytes()
PROMPT = PROMPT_BYTES.decode("utf-8")
SCALAR_FIELDS = tuple(key for key, rule in SCHEMA["properties"].items()
                      if rule["type"] != "array")
DATE_FIELDS = ("request_date", "start_date", "end_date")
IDENTIFIER = re.compile(r"(?<![a-z0-9])[a-z]+-\d+(?![a-z0-9])", re.IGNORECASE)


class ExtractionError(ValueError):
    """An export or provider reply failed an intake check."""


def strict_json(value):
    def unique_object(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    def reject_constant(value):
        raise ValueError("invalid JSON constant")

    try:
        return json.loads(value, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except RecursionError:
        raise ValueError("JSON nesting exceeds the parser limit") from None


@dataclass(frozen=True)
class Source:
    document_id: str
    raw: bytes

    @classmethod
    def read(cls, path: Path):
        return cls(path.name, path.read_bytes())

    @property
    def sha256(self):
        return hashlib.sha256(self.raw).hexdigest()

    @property
    def message(self):
        try:
            data = strict_json(self.raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise ExtractionError("invalid UTF-8 JSON export") from None
        if not isinstance(data, dict) or not isinstance(data.get("message"), str):
            raise ExtractionError("export requires a message string")
        try:
            data["message"].encode("utf-8")
        except UnicodeError:
            raise ExtractionError("export contains invalid Unicode in message") from None
        return data["message"]


def validate_facts(facts, message):
    if not isinstance(facts, dict) or facts.keys() != set(SCHEMA["required"]):
        raise ExtractionError("extraction does not have the required schema fields")
    for field, rule in SCHEMA["properties"].items():
        value = facts[field]
        if rule["type"] == "array":
            valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
        else:
            valid = (value is None or isinstance(value, str)) and (
                "enum" not in rule or value in rule["enum"])
        if not valid:
            raise ExtractionError(f"invalid extraction field {field}")
    for field in DATE_FIELDS:
        value = facts[field]
        if value is None:
            continue
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError:
            raise ExtractionError(f"invalid YYYY-MM-DD date in {field}") from None
    if any(not quote or quote not in message for quote in facts["supporting_quotes"]):
        raise ExtractionError("supporting quote is absent from the decoded message")
    try:
        json.dumps(facts, ensure_ascii=False).encode("utf-8")
    except UnicodeError:
        raise ExtractionError("invalid Unicode in extraction") from None


def decode_reply(envelope, message):
    if not isinstance(envelope, dict):
        raise ExtractionError("malformed response envelope")
    status = envelope.get("status")
    if status in ("incomplete", "cancelled", "in_progress", "queued"):
        raise ExtractionError("incomplete model response")
    if status == "failed":
        raise ExtractionError("model reported a failed response")
    if status != "completed" or not isinstance(envelope.get("output"), list):
        raise ExtractionError("malformed response envelope")
    texts = []
    for item in envelope["output"]:
        if not isinstance(item, dict):
            raise ExtractionError("malformed response output")
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            raise ExtractionError("unexpected response output type")
        if item.get("status") != "completed":
            raise ExtractionError("incomplete model message")
        if not isinstance(item.get("content"), list):
            raise ExtractionError("malformed response content")
        for part in item["content"]:
            if not isinstance(part, dict):
                raise ExtractionError("malformed response content")
            if part.get("type") == "refusal":
                raise ExtractionError("model refused extraction")
            if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                raise ExtractionError("unexpected response content")
            texts.append(part["text"])
    if len(texts) != 1:
        raise ExtractionError("expected one structured model response")
    try:
        facts = strict_json(texts[0])
    except ValueError:
        raise ExtractionError("malformed extraction JSON") from None
    validate_facts(facts, message)
    return facts


def extract(source: Source, provider: Provider):
    message = source.message
    reply = provider.extract(document_id=source.document_id, message=message)
    facts = decode_reply(reply.envelope, message)
    mismatch = filename_mismatch(source.document_id, facts["request_id"])
    if mismatch and mismatch not in facts["issues"]:
        facts["issues"].append(mismatch)
    return facts, reply.response_source


def filename_mismatch(document_id, stated_id):
    match = IDENTIFIER.search(Path(document_id).stem)
    if match and stated_id and match[0].casefold() != stated_id.casefold():
        return f"request_id mismatch: filename says {match[0]}, message says {stated_id}"
    return None
