"""Publish a consistent, portable review snapshot."""

import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from intake.live import request_evidence
from intake.state import DEFAULT_MOCKS, Settings, canonical, queue_for


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def public_settings(settings):
    visible = dict(settings)
    if "fixture_dir" in visible:
        directory = Path(visible["fixture_dir"])
        visible["fixture_dir"] = (
            "tests/mock_responses" if directory == DEFAULT_MOCKS else directory.name
        )
    return visible


def export_snapshot(store, output):
    output = output.resolve()
    if output.exists():
        raise ValueError("export directory already exists; choose a new directory")
    records = store.current()
    profiles = {}
    for record in records:
        record["settings"] = public_settings(record["settings"])
        profiles[canonical(record["settings"])] = record["settings"]
    sample = next((profile for profile in profiles.values() if profile["provider"] == "openai"),
                  next(iter(profiles.values()), {}))
    base = Settings(**{field: sample[field] for field in Settings.__dataclass_fields__
                       if field in sample})
    evidence = request_evidence(replace(base, provider="openai"))
    evidence["example_kind"] = "credential-redacted configuration example"
    evidence["snapshot_profiles"] = list(profiles.values())
    with TemporaryDirectory(prefix=".intake-export-", dir=output.parent) as staging:
        bundle = Path(staging) / "bundle"
        bundle.mkdir()
        with (bundle / "extractions.jsonl").open("w", encoding="utf-8") as stream:
            for record in records:
                stream.write(canonical(record) + "\n")
        write_json(bundle / "queue.json", queue_for(records))
        write_json(bundle / "request-evidence.json", evidence)
        if output.exists():
            raise ValueError("export directory already exists; choose a new directory")
        bundle.rename(output)
    return {"output": str(output), "records": len(records)}
