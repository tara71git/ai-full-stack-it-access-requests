
# Northstar access request intake

The CLI prepares exported access requests for human review. Regional operators can use the same SQLite database on a shared Linux host and inspect completed requests from a separate terminal while intake runs.

## Setup and offline walkthrough

Use Python 3.11 or later with SQLite 3.38 or later. The application uses the Python standard library, so setup requires no package installation. Run these commands from the repository root. The walkthrough uses a fresh database and the bundled mock replies. [Dependency lockfile](requirements.lock)

```bash
intake_dir="$(mktemp -d)"
python3 -m intake --db "$intake_dir/intake.sqlite3" start materials/requests --provider mock
python3 -m intake --db "$intake_dir/intake.sqlite3" queue
python3 -m intake --db "$intake_dir/intake.sqlite3" pending
python3 -m intake --db "$intake_dir/intake.sqlite3" history IT-001.json
python3 -m intake --db "$intake_dir/intake.sqlite3" resume
python3 -m intake --db "$intake_dir/intake.sqlite3" retry IT-001.json
python3 -m intake --db "$intake_dir/intake.sqlite3" refresh --provider mock
python3 -m intake --db "$intake_dir/intake.sqlite3" export --out "$intake_dir/snapshot"
python3 -m intake accuracy --provider mock
```

Run the mock-only test suite with the credential removed from the process environment.

```bash
env -u API_KEY python3 -m unittest discover -s tests -v
```

## Operator commands

- Use `start <directory>` for new or revised exports. Each UTF-8 JSON file represents one message, keyed by its filename. With `--provider mock --fixtures <directory>`, supply saved response envelopes named after the lowercase export filenames. Use the live provider for new wording when no mock reply exists.
- Open a separate terminal and use `queue` for completed attempts and `pending` for queued, running, or resumable work. Match an entry to `history <document_id>` by its attempt ID and source hash. The queue keeps the latest completed result visible while a replacement is pending.
- Use `resume` after interruption. It reads stored input and settings from the database. An abandoned claim expires after the configured timeout plus 30 seconds from its last renewal. If the saved prompt, schema, policy, or fixture version is unavailable, `resume` reports the blocked attempts.
- Run the `retry_command` shown on an error record. Retry uses the latest stored source and saved settings unless you supply overrides. It creates a new attempt, so a retry after a previous success also records a new outcome.
- Use `refresh` with the intended provider and settings when model settings, extraction rules, or policy change. It finds affected stored documents even if the original batch directory is gone. A later batch that omits an earlier file leaves that earlier document current.
- Use `export --out <new-directory>` to publish a consistent snapshot. The parent directory must exist. Keep the extraction JSONL and linked queue JSON together.

The queue includes status, owner, next action and the named manager as a contact. Security Review evaluates risk and forwards its recommendation to the system owner for a decision. Humans make approval decisions after intake.

## OpenAI configuration

The live adapter supports the OpenAI Responses API with strict Structured Outputs. Put the facilitator-provided credential in the `API_KEY` environment variable for the live provider, then point intake at the database shared by your operators.

```bash
python3 -m intake --db ./intake.sqlite3 start materials/requests --provider openai --model gpt-4.1-mini --max-output-tokens 2048 --timeout 60
```

The default mock mode works without a credential. `--model`, `--max-output-tokens`, and `--timeout` configure the request and its socket timeout in seconds. `--security-environment` and `--security-permission` can each be repeated to replace the corresponding policy list for an approved policy revision. The office defaults are production and write/admin. To set a database's shared claim limit at creation, put `--worker-limit N` before the subcommand. The default is four.

Use one local database on one Linux host, with a shared path and OS permissions for authorized operators. Each operator runs the CLI on that host. Keep the deployed prompt, schema, policy, and fixture files stable during a run. Batch figures, interruption assumptions, and verification results are documented in the handover. [Verification](deliverables/verification.md)

## Reproduce the submission snapshot

Use a fresh database and export immediately after the 20 supplied messages finish. This recreates the saved queue, extraction records and redacted request evidence with the same attempt IDs.

```bash
snapshot_dir="$(mktemp -d)"
env -u API_KEY python3 -m intake --db "$snapshot_dir/intake.sqlite3" start materials/requests --provider mock
env -u API_KEY python3 -m intake --db "$snapshot_dir/intake.sqlite3" export --out "$snapshot_dir/snapshot"
```

The snapshot uses the bundled mock responses. The accuracy exercise also includes an authored paraphrase. [Queue](deliverables/queue.json) · [Extractions](deliverables/extractions.jsonl) · [Accuracy report](deliverables/accuracy.md) · [Redacted request](deliverables/request-evidence.json)
