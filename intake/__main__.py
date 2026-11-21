"""Command-line interface for access request operators."""

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

from intake.accuracy import compare, measure, read_cases
from intake.artifacts import export_snapshot, public_settings
from intake.extraction import Source
from intake.provider import MockProvider
from intake.state import Settings, Store, canonical, fixture_revision


def positive_integer(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return result


def positive_float(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("expected a finite positive number")
    return result


def parser_for_cli():
    parser = argparse.ArgumentParser(prog="python3 -m intake")
    parser.add_argument("--db", type=Path, default=Path("intake.sqlite3"))
    parser.add_argument("--worker-limit", type=positive_integer,
                        help="set the shared worker cap when creating a database")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("queue", "pending", "resume"):
        commands.add_parser(command)
    commands.add_parser("history").add_argument("document_id")
    commands.add_parser("export").add_argument("--out", type=Path, required=True)
    add_settings_options(commands.add_parser("accuracy"))
    for command in ("start", "refresh", "retry"):
        sub = commands.add_parser(command)
        if command == "start":
            sub.add_argument("batch", type=Path)
        if command == "retry":
            sub.add_argument("document_id")
        add_settings_options(sub)
    return parser


def add_settings_options(parser):
    parser.add_argument("--provider", choices=("mock", "openai"), default=argparse.SUPPRESS)
    parser.add_argument("--model", default=argparse.SUPPRESS)
    parser.add_argument("--max-output-tokens", type=positive_integer, default=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=positive_float, default=argparse.SUPPRESS)
    parser.add_argument("--fixtures", dest="fixture_dir", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--security-environment", action="append", choices=("staging", "production"),
                        dest="security_environments", default=argparse.SUPPRESS,
                        help="replace the security-review environment list")
    parser.add_argument("--security-permission", action="append", choices=("read", "write", "admin"),
                        dest="security_permissions", default=argparse.SUPPRESS,
                        help="replace the security-review permission list")


def cli_settings(args, saved=None):
    values = {field: getattr(args, field, (saved or {}).get(field, getattr(Settings, field)))
              for field in Settings.__dataclass_fields__}
    directory = Path(values["fixture_dir"]).resolve()
    values["fixture_dir"] = str(directory)
    values["fixture_version"] = fixture_revision(directory) if values["provider"] == "mock" else None
    for field in ("security_environments", "security_permissions"):
        values[field] = tuple(values[field])
    return Settings(**values)


def provider_for(settings):
    if settings.provider == "mock":
        return MockProvider(fixture_dir=Path(settings.fixture_dir), scripts={},
                            use_document_fixtures=True)
    from intake.live import OpenAIProvider

    return OpenAIProvider(settings)


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def settings_for_resume(saved):
    fields = Settings.__dataclass_fields__
    settings = Settings(**{field: saved[field] for field in fields if field in saved})
    if settings.provider == "mock" and (
        fixture_revision(Path(settings.fixture_dir)) != settings.fixture_version
    ):
        raise ValueError("saved mock fixtures changed")
    if canonical(settings.describe()) != canonical(saved):
        raise ValueError("saved processing versions differ from the installed versions")
    return settings


def resume(store):
    groups = {}
    for row in store.pending():
        groups.setdefault(canonical(row["settings"]), []).append(row["attempt_id"])
    blocked = []
    for encoded, ids in groups.items():
        try:
            settings = settings_for_resume(json.loads(encoded))
            provider = provider_for(settings)
        except (OSError, ValueError) as exc:
            blocked.append({"attempt_ids": ids, "reason": str(exc), "action": "run refresh with the intended settings"})
            continue
        store.run(provider, settings, ids)
    return {"pending": store.pending(), "blocked": blocked}


def main(argv=None):
    parser = parser_for_cli()
    args = parser.parse_args(argv)
    try:
        if args.command == "accuracy":
            settings = cli_settings(args)
            cases = read_cases()
            report = compare(cases, measure(cases, provider_for(settings), settings), settings)
            report["settings"] = public_settings(report["settings"])
            emit(report)
            return 0
        store = Store(args.db, worker_limit=args.worker_limit)
        if args.command == "queue":
            emit(store.queue())
        elif args.command == "pending":
            emit(store.pending())
        elif args.command == "history":
            emit(store.history(args.document_id))
        elif args.command == "resume":
            emit(resume(store))
        elif args.command == "export":
            emit(export_snapshot(store, args.out))
        else:
            saved = store.latest_settings(args.document_id) if args.command == "retry" else None
            settings = cli_settings(args, saved)
            provider = provider_for(settings)
            if args.command == "start":
                paths = sorted(args.batch.glob("*.json"))
                if not paths:
                    raise ValueError("intake directory contains no JSON exports")
                ids = store.enqueue([Source.read(path) for path in paths], settings)
            elif args.command == "retry":
                ids = store.retry(args.document_id, settings)
            else:
                ids = store.refresh(settings)
            store.run(provider, settings, ids)
            emit({"attempt_ids": ids})
    except KeyboardInterrupt:
        parser.exit(130, "intake interrupted; inspect pending work and run resume\n")
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(2, f"intake: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
