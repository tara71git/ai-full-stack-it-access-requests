# Verification and operator handover

## Reproduction

Run from the repository root with Python 3.11+ and SQLite 3.38+. The documented mock walkthrough creates a fresh database. The test suite uses temporary directories, saved replies, and an injected transport. [README](../README.md)

| Command | Assertions and intended comparisons |
| --- | --- |
| `env -u API_KEY python3 -m unittest discover -s tests -v` | Compare extracted fields and routes to facts read from the supplied messages and policy. Assert schema shape, completion state, source-byte hashes, decoded quotes, precedence, and all review routes. Inspect the named test results in the saved run. |
| `env -u API_KEY python3 -m unittest discover -s tests -p test_operations.py -v` | Observe a completed result while a second call is held. Reclaim an abandoned job from durable state, preserve the earlier result, enforce the shared cap, and check late or expired claims. Run overlapping mock CLI batches of 400 and 2,000 exports and compare terminal counts and owner-review routes. |
| `env -u API_KEY python3 -m unittest discover -s tests -p test_failures.py -v` | Check the latest error and older valid history, retry from stored input, and verify refusal, interruption, malformed JSON, provider outages, rate limits, credential loading and redaction. Compare the simulated HTTP request shape to the configured model, schema, prompt and decoded message. |
| `env -u API_KEY python3 -m intake --db "$snapshot_dir/intake.sqlite3" export --out "$snapshot_dir/snapshot"` | First run the README’s submission snapshot intake command. Compare filenames and independently computed SHA-256 hashes against the JSONL. Match queue and extraction entries on document ID, hash, attempt ID and route. Compare the three generated files with the saved deliverables. |
| `python3 -m intake accuracy --provider mock` | Compare 21 messages across 12 scalar fields. Check fixture provenance, denominator, error handling, exact quote validation, and issue counts. Follow the procedure to assess mismatched business-purpose wording. |

## Expected operating behavior

- Northstar ordinarily receives 200–400 messages per day and may see about 2,000 in a surge. The CLI makes one model call per new attempt and writes each outcome as it finishes. Four shared claim slots by default allow other requests to progress when one provider call is slow. The cap limits simultaneous work. The provider controls available request rate and latency.
- Operators share a SQLite WAL database on local disk on one Linux host. They run the CLI on that host, for example through SSH. Source bytes are stored before processing. Transactions coalesce identical pending submissions and publish outcome plus current pointer together. Generation order keeps an older completion in history when a newer completion is already current. Use a local filesystem and stable host clock. Keep the SQLite files and directory permissions limited to authorized users.
- During an interruption, committed results remain visible. Ctrl-C stops the local command from claiming additional work and allows in-flight calls to finish. After abrupt process termination, an abandoned claim becomes resumable after its last lease expires. The next worker can repeat an uncommitted provider call. A stale claim token cannot publish once its lease expires. Live calls renew their claims while their process is active. The configured HTTP timeout applies to socket inactivity, and provider request duration can vary.
- Rate limits and outages become visible per-document errors with a retry command. Operators decide when to retry. Completed requests remain available while other failures await retry. Changed settings, prompts, schema, policy versions, and fixture contents can invalidate reuse. A refresh under installed versions supersedes incompatible pending attempts while preserving history.
- If every claim slot is occupied, additional work waits. The app processes local batches in one CLI host and keeps revision history in one database. Disk retention and backups are operator managed. Review outcomes are human decisions. The override check combines model-identified evidence with common instruction markers, and novel phrasing depends on model interpretation.
- The mock load check exercises filesystem reads, CLI behavior, claims and database writes. Live provider throughput, sustained rate limits, latency, and extraction accuracy remain unmeasured. Recovery verification terminates a subprocess with an uncommitted claim and demonstrates stale-token fencing with a controlled clock. The tests use no facilitator credential.

## Observed during this build

- The starter mock provider already passed. The new extraction tests initially failed because the extraction module was missing. Routing tests failed for the missing policy module, and attempt tests failed for the missing state module. Their tests passed after each implementation.
- Shared-operator tests initially found missing CLI, shared claims, and worker execution. The passing checks exercised the documented CLI, a delayed response, recovery and overlapping mock batches. A later retry regression check caught loss of the saved mock settings and verified that subsequent retries inherit them.
- Failure tests initially found generic provider errors and a missing live adapter. The fake-transport verification passed after request construction and fixed error classifications were implemented. It used a synthetic credential supplied through an injected environment mapping.
- Artifact and accuracy tests initially found missing export and comparison entry points. The handover test initially found the starter README had no executable walkthrough. The saved suite records the final automated observations. [Saved test run](../planning/test-run.txt)
- Export tests verify snapshot consistency across a concurrent database commit and cleanup after an interrupted write. A source-to-queue check confirmed the original-byte SHA-256 hashes, schema shape and exact quotes for all 20 records. [Queue](queue.json) · [Extractions](extractions.jsonl)
- Further checks reproduced three edge cases before repair. A blank withdrawal identifier reached confirmation, deeply nested JSON interrupted the worker, and a non-object accuracy record interrupted scoring. The final tests verify that these produce clarification, a per-document error, and a zero-score record respectively.

The live contract uses the Responses API request format. The redacted example records its endpoint, model, request schema and handling. [OpenAI Responses API reference](https://developers.openai.com/api/reference/resources/responses/methods/create) · [Generated example](request-evidence.json)

## Review queue check on the 20 supplied exports

A fresh intake run used the bundled mock responses, the default office policy and an unset `API_KEY`. Each observed status, owner and next action matched the first applicable policy rule based on its source message. All 20 exports completed with zero errors and zero pending jobs. The fresh snapshot was byte-for-byte identical to the saved deliverables. [Office policy](../materials/office-policy.md) · [Saved queue](queue.json)

| Export | Status | Owner | Next action | Source evidence and rule |
| --- | --- | --- | --- | --- |
| [IT-001](../materials/requests/IT-001.json) | `review` | System Owner | `review-request` | Rule 5. Complete staging/read request for Atlas. |
| [IT-002](../materials/requests/IT-002.json) | `review` | Security Review | `review-risk-then-owner` | Rule 4. Production write access to Ledger. |
| [IT-003](../materials/requests/IT-003.json) | `needs-information` | IT Service Desk | `clarify` | Rule 3. “The same access Sam has” leaves the environment and permission unspecified. The message gives no explicit access dates. |
| [IT-004](../materials/requests/IT-004.json) | `needs-information` | IT Service Desk | `clarify` | Rule 3. The message names staging and production without an explicit correction. |
| [IT-005](../materials/requests/IT-005.json) | `review` | IT Service Desk | `confirm-withdrawal` | Rule 2. The withdrawal states request ID IT-005 and date 2026-10-22. |
| [IT-006](../materials/requests/IT-006.json) | `review` | IT Service Desk | `inspect-message` | Rule 1. The embedded “SYSTEM OVERRIDE” attempts to bypass approval. |
| [IT-007](../materials/requests/IT-007.json) | `review` | Security Review | `review-risk-then-owner` | Rule 4. The request changes staging access from read to write. |
| [IT-008](../materials/requests/IT-008.json) | `review` | System Owner | `review-request` | Rule 5. The labelled correction makes the environment staging. The permission is read. |
| [IT-009](../materials/requests/IT-009.json) | `needs-information` | IT Service Desk | `clarify` | Rule 3. The message leaves the manager unspecified. |
| [IT-010](../materials/requests/IT-010.json) | `needs-information` | IT Service Desk | `clarify` | Rule 3. The production/read request has no fixed end date. |
| [IT-011](../materials/requests/IT-011.json) | `needs-information` | IT Service Desk | `clarify` | Rule 3. “Next Monday - Friday” leaves both access dates unresolved. |
| [IT-012](../materials/requests/IT-012.json) | `review` | Security Review | `review-risk-then-owner` | Rule 4. The request is for admin access in staging. |
| [IT-013](../materials/requests/IT-013.json) | `review` | Security Review | `review-risk-then-owner` | Rule 4. The request is for read access in production. |
| [IT-014](../materials/requests/IT-014.json) | `needs-information` | IT Service Desk | `clarify` | Rule 3. The start date of 2026-11-05 follows the end date of 2026-11-02. |
| [IT-015](../materials/requests/IT-015.json) | `review` | System Owner | `review-request` | Rule 5. The staging/read request is complete with the corrected dates of 2026-10-29 through 2026-10-31. |
| [IT-016](../materials/requests/IT-016.json) | `needs-information` | IT Service Desk | `clarify` | Rule 3. The filename says IT-016 and the message states IT-061. |
| [IT-017](../materials/requests/IT-017.json) | `needs-information` | IT Service Desk | `clarify` | Rule 3. The message asks where to find the access request form, so its kind is `other`. |
| [IT-018](../materials/requests/IT-018.json) | `review` | System Owner | `review-request` | Rule 5. Complete staging/read request for Atlas. |
| [IT-019](../materials/requests/IT-019.json) | `review` | Security Review | `review-risk-then-owner` | Rule 4. The requested permission changes to read and the environment is production. |
| [IT-020](../materials/requests/IT-020.json) | `needs-information` | IT Service Desk | `clarify` | Rule 3. The message names Priya Shah and Elena Park as manager without a correction. |
