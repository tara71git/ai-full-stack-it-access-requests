"""Compare observed records to the source-derived reference table."""

import re
from dataclasses import dataclass

from intake.extraction import ROOT, SCALAR_FIELDS, ExtractionError, Source, validate_facts
from intake.state import outcome_for


ACCURACY_PATH = ROOT / "deliverables/accuracy.md"
TABLE_FIELDS = ("request_id", "kind", "requester", "team", "system", "environment",
                "permission", "business_purpose", "start_date", "end_date", "manager")


@dataclass(frozen=True)
class Case:
    source: Source
    facts: dict
    evidence: str


def read_cases():
    reference = ACCURACY_PATH.read_text(encoding="utf-8")
    date_match = re.search(r"Each case states request date \*\*(\d{4}-\d{2}-\d{2})\*\*", reference)
    if not date_match:
        raise ValueError("accuracy reference does not specify the shared request date")
    cases = []
    for line in reference.splitlines():
        if not line.startswith("| [IT-"):
            continue
        link, *values, evidence = (cell.strip() for cell in line.split("|")[1:-1])
        match = re.fullmatch(r"\[(IT-\d+\.json)\]\(([^)]+)\)", link)
        if not match or len(values) != len(TABLE_FIELDS):
            raise ValueError("invalid accuracy reference row")
        source = Source.read(ACCURACY_PATH.parent / match[2])
        if source.document_id != match[1]:
            raise ValueError("accuracy reference filename and linked export differ")
        facts = dict(zip(TABLE_FIELDS, (None if value == "—" else value for value in values),
                         strict=True))
        facts["request_date"] = date_match[1]
        cases.append(Case(source, facts, evidence))
    if len(cases) != 21 or len({case.source.document_id for case in cases}) != 21:
        raise ValueError("accuracy reference requires 21 distinct exports")
    return cases


def measure(cases, provider, settings):
    return {case.source.document_id: {
        **outcome_for(case.source, provider, settings), "source_sha256": case.source.sha256,
    } for case in cases}


def record_problem(source, record):
    if record is None:
        return "record missing"
    if not isinstance(record, dict):
        return "record is not an object"
    if record.get("source_sha256") != source.sha256:
        return "record refers to a different source revision"
    if record.get("error"):
        return record["error"]
    try:
        validate_facts(record.get("extraction"), source.message)
    except ExtractionError as exc:
        return str(exc)
    return None


def compare(cases, observations, settings):
    exact_matches = 0
    failures = []
    mismatches = []
    quote_count = 0
    issue_case_count = 0
    response_sources = set()
    for case in cases:
        source = case.source
        record = observations.get(source.document_id)
        if isinstance(record, dict) and isinstance(record.get("response_source"), str):
            response_sources.add(record["response_source"])
        problem = record_problem(source, record)
        if problem:
            failures.append({"document_id": source.document_id, "error": problem})
            continue
        actual = record["extraction"]
        quote_count += len(actual["supporting_quotes"])
        issue_case_count += bool(actual["issues"])
        for field in SCALAR_FIELDS:
            if case.facts[field] == actual[field]:
                exact_matches += 1
                continue
            mismatches.append({
                "document_id": source.document_id, "field": field, "expected": case.facts[field],
                "actual": actual[field], "reference_quote": case.evidence,
                "source_message": source.message,
                "category": "purpose-review" if field == "business_purpose" else "factual-difference",
            })
    return {
        "settings": settings.describe(),
        "response_sources": sorted(response_sources),
        "cases": len(cases),
        "comparisons": len(cases) * len(SCALAR_FIELDS),
        "exact_matches": exact_matches,
        "factual_differences": sum(item["category"] == "factual-difference" for item in mismatches),
        "mismatches": mismatches,
        "failed_records": failures,
        "grounded_quotes": quote_count,
        "records_with_issues": issue_case_count,
    }
