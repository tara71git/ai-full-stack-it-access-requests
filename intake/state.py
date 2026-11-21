"""Durable input, work claims, attempt history, and current review state."""

import hashlib
import json
import shlex
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

from intake.extraction import (
    ExtractionError, POLICY_PATH, PROMPT_BYTES, ROOT, SCHEMA_BYTES, Source, extract,
)
from intake.provider import PROVIDER_ERRORS, ProviderError
from intake.routing import review, route


DEFAULT_MOCKS = ROOT / "tests/mock_responses"


def fixture_revision(directory):
    if not directory.is_dir():
        raise ValueError("mock fixtures directory does not exist")
    entries = [(item.name, sha(item.read_bytes())) for item in sorted(directory.glob("*.json"))]
    return sha(canonical(entries).encode())


DDL = """
CREATE TABLE IF NOT EXISTS sources (
    document_id TEXT NOT NULL, source_sha256 TEXT NOT NULL, raw BLOB NOT NULL,
    PRIMARY KEY (document_id, source_sha256)
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY, document_id TEXT NOT NULL, source_sha256 TEXT NOT NULL,
    settings TEXT NOT NULL, lease_until REAL, claim_token TEXT, outcome TEXT,
    superseded_by INTEGER REFERENCES jobs(id),
    FOREIGN KEY (document_id, source_sha256) REFERENCES sources
);
CREATE INDEX IF NOT EXISTS jobs_document ON jobs(document_id, id);
CREATE INDEX IF NOT EXISTS unfinished ON jobs(lease_until, id) WHERE outcome IS NULL;
CREATE TABLE IF NOT EXISTS current (
    document_id TEXT PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id)
);
CREATE TABLE IF NOT EXISTS runtime (
    name TEXT PRIMARY KEY, value INTEGER NOT NULL
);
"""


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


DEFAULT_FIXTURE_VERSION = fixture_revision(DEFAULT_MOCKS)
POLICY_BYTES = POLICY_PATH.read_bytes() + b"\0" + Path(__file__).with_name("routing.py").read_bytes()


@dataclass(frozen=True)
class Settings:
    provider: str = "mock"
    model: str = "gpt-4.1-mini"
    max_output_tokens: int = 2048
    timeout: float = 60.0
    fixture_dir: str = str(DEFAULT_MOCKS)
    fixture_version: str | None = DEFAULT_FIXTURE_VERSION
    security_environments: tuple[str, ...] = ("production",)
    security_permissions: tuple[str, ...] = ("write", "admin")

    @property
    def response_source(self):
        return "mock" if self.provider == "mock" else f"{self.provider}:{self.model}"

    def describe(self):
        settings = asdict(self)
        settings["security_environments"] = sorted(set(self.security_environments))
        settings["security_permissions"] = sorted(set(self.security_permissions))
        settings["prompt_version"] = sha(PROMPT_BYTES)
        settings["schema_version"] = sha(SCHEMA_BYTES)
        settings["policy_version"] = sha(
            POLICY_BYTES + b"\0" +
            canonical([settings["security_environments"], settings["security_permissions"]]).encode()
        )
        if self.provider != "mock":
            settings.pop("fixture_dir")
            settings.pop("fixture_version")
        return settings


class Store:
    def __init__(self, path, *, clock=time.time, worker_limit=None):
        self.path = Path(path)
        self.clock = clock
        with self.connection() as db:
            if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                db.execute("PRAGMA journal_mode=WAL")
            db.executescript(DDL)
            row = db.execute("SELECT value FROM runtime WHERE name='worker_limit'").fetchone()
            if row is None:
                db.execute("INSERT OR IGNORE INTO runtime VALUES ('worker_limit', ?)",
                           (worker_limit or 4,))
                row = db.execute("SELECT value FROM runtime WHERE name='worker_limit'").fetchone()
            self.worker_limit = row["value"]
        if worker_limit is not None and self.worker_limit != worker_limit:
            raise ValueError(f"shared database already uses worker limit {self.worker_limit}")

    @contextmanager
    def connection(self, *, write=False):
        db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        finally:
            db.close()

    def enqueue(self, sources, settings, *, force=False):
        encoded = canonical(settings.describe())
        with self.connection(write=True) as db:
            return self._enqueue(db, sources, encoded, force=force)

    @staticmethod
    def _enqueue(db, sources, encoded, *, force=False):
        ids = []
        for source in sources:
            digest = source.sha256
            latest = db.execute(
                "SELECT * FROM jobs WHERE document_id=? ORDER BY id DESC LIMIT 1",
                (source.document_id,),
            ).fetchone()
            if not force and latest and latest["source_sha256"] == digest and latest["settings"] == encoded:
                if latest["outcome"] is None:
                    ids.append(latest["id"])
                    continue
                if json.loads(latest["outcome"])["error"] is None:
                    continue
            db.execute("INSERT OR IGNORE INTO sources VALUES (?, ?, ?)",
                       (source.document_id, digest, source.raw))
            ids.append(db.execute(
                "INSERT INTO jobs(document_id, source_sha256, settings) VALUES (?, ?, ?)",
                (source.document_id, digest, encoded),
            ).lastrowid)
        return ids

    def refresh(self, settings):
        encoded = canonical(settings.describe())
        with self.connection(write=True) as db:
            rows = db.execute("""
                SELECT j.document_id, s.raw, j.settings
                FROM jobs j JOIN sources s USING (document_id, source_sha256)
                JOIN (SELECT MAX(id) AS latest FROM jobs GROUP BY document_id) u ON j.id=u.latest
                ORDER BY j.document_id
            """).fetchall()
            changed = (Source(row["document_id"], row["raw"]) for row in rows
                       if row["settings"] != encoded)
            ids = self._enqueue(db, changed, encoded)
            db.execute("""
                UPDATE jobs AS prior SET superseded_by=(
                    SELECT MAX(id) FROM jobs newer WHERE newer.document_id=prior.document_id
                )
                WHERE prior.outcome IS NULL AND prior.superseded_by IS NULL AND prior.settings<>?
                  AND (SELECT settings FROM jobs newer WHERE newer.document_id=prior.document_id
                       ORDER BY id DESC LIMIT 1)=?
            """, (encoded, encoded))
            return ids

    def claim(self, settings, *, seconds=90):
        return self._claim(canonical(settings.describe()), seconds)

    def _claim(self, encoded, seconds):
        with self.connection(write=True) as db:
            now = self.clock()
            active = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE outcome IS NULL AND lease_until>?", (now,)
            ).fetchone()[0]
            if active >= self.worker_limit:
                return None
            row = db.execute("""
                SELECT j.id, j.document_id, s.raw
                FROM jobs j JOIN sources s USING (document_id, source_sha256)
                WHERE j.outcome IS NULL AND (j.lease_until IS NULL OR j.lease_until<=?)
                      AND j.settings=? AND j.superseded_by IS NULL
                ORDER BY j.id LIMIT 1
            """, (now, encoded)).fetchone()
            if not row:
                return None
            token = uuid4().hex
            db.execute("UPDATE jobs SET lease_until=?, claim_token=? WHERE id=?",
                       (now + seconds, token, row["id"]))
            return row["id"], token, Source(row["document_id"], row["raw"])

    def finish(self, job_id, token, outcome):
        with self.connection(write=True) as db:
            row = db.execute("""
                UPDATE jobs SET outcome=?, lease_until=NULL, claim_token=NULL
                WHERE id=? AND claim_token=? AND outcome IS NULL AND lease_until>?
                RETURNING document_id, superseded_by
            """, (canonical(outcome), job_id, token, self.clock())).fetchone()
            if row is None:
                return False
            if row["superseded_by"] is None:
                db.execute("""
                    INSERT INTO current VALUES (?, ?)
                    ON CONFLICT(document_id) DO UPDATE SET job_id=excluded.job_id
                    WHERE excluded.job_id>current.job_id
                """, (row["document_id"], job_id))
            return True

    @contextmanager
    def keep_claim(self, job_id, token, settings):
        if settings.provider != "openai":
            yield
            return
        stopped = Event()
        seconds = settings.timeout + 30

        def renew():
            while not stopped.wait(seconds / 3):
                with self.connection(write=True) as db:
                    now = self.clock()
                    saved = db.execute("""
                        UPDATE jobs SET lease_until=? WHERE id=? AND claim_token=?
                        AND outcome IS NULL AND lease_until>?
                    """, (now + seconds, job_id, token, now))
                    if not saved.rowcount:
                        return

        thread = Thread(target=renew, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join()

    def process(self, provider, settings, stopped=None):
        stopped = stopped or Event()
        encoded = canonical(settings.describe())
        while not stopped.is_set():
            claim = self._claim(encoded, settings.timeout + 30)
            if not claim:
                break
            job_id, token, source = claim
            with self.keep_claim(job_id, token, settings):
                self.finish(job_id, token, outcome_for(source, provider, settings))

    def run(self, provider, settings, ids=None):
        if ids == []:
            return
        encoded = canonical(settings.describe())
        stopped = Event()
        with ThreadPoolExecutor(max_workers=self.worker_limit) as workers:
            try:
                while True:
                    results = [workers.submit(self.process, provider, settings, stopped)
                               for _ in range(self.worker_limit)]
                    for result in results:
                        result.result()
                    if not self.has_unfinished(encoded, ids):
                        return
                    time.sleep(0.1)
            finally:
                stopped.set()

    def has_unfinished(self, encoded, ids):
        with self.connection() as db:
            row = db.execute(
                "SELECT 1 FROM jobs WHERE outcome IS NULL AND superseded_by IS NULL AND settings=? "
                "AND (? IS NULL OR id IN (SELECT value FROM json_each(?))) LIMIT 1",
                (encoded, None if ids is None else 1, json.dumps(ids)),
            ).fetchone()
            return row is not None

    def pending(self):
        with self.connection() as db:
            rows = db.execute("""
                SELECT j.id, j.document_id, j.source_sha256, j.settings, j.lease_until,
                       c.job_id AS current_attempt_id
                FROM jobs j LEFT JOIN current c USING (document_id)
                WHERE j.outcome IS NULL AND j.superseded_by IS NULL ORDER BY j.id
            """).fetchall()
        now = self.clock()
        result = []
        for row in rows:
            until = row["lease_until"]
            if until is None:
                state = "queued"
            else:
                state = "running" if until > now else "ready-to-resume"
            result.append({
                "attempt_id": row["id"], "document_id": row["document_id"],
                "source_sha256": row["source_sha256"], "settings": json.loads(row["settings"]),
                "work_state": state, "current_attempt_id": row["current_attempt_id"],
            })
        return result

    def latest_settings(self, document_id):
        with self.connection() as db:
            row = db.execute("""
                SELECT settings FROM jobs WHERE document_id=? ORDER BY id DESC LIMIT 1
            """, (document_id,)).fetchone()
            if row is None:
                raise ValueError("document has no stored source")
            return json.loads(row["settings"])

    def retry(self, document_id, settings):
        encoded = canonical(settings.describe())
        with self.connection(write=True) as db:
            row = db.execute("""
                SELECT s.raw FROM jobs j JOIN sources s USING (document_id, source_sha256)
                WHERE j.document_id=? ORDER BY j.id DESC LIMIT 1
            """, (document_id,)).fetchone()
            if row is None:
                raise ValueError("document has no stored source")
            return self._enqueue(db, [Source(document_id, row["raw"])], encoded, force=True)

    def _record(self, row):
        record = {
            "document_id": row["document_id"], "source_sha256": row["source_sha256"],
            "attempt_id": row["id"], "settings": json.loads(row["settings"]),
            **json.loads(row["outcome"]),
        }
        record["retry_command"] = (
            shlex.join(["python3", "-m", "intake", "--db", str(self.path.resolve()),
                        "retry", "--", record["document_id"]]) if record["error"] else None
        )
        return record

    def current(self):
        with self.connection() as db:
            rows = db.execute("""
                SELECT j.* FROM current c JOIN jobs j ON j.id=c.job_id ORDER BY c.document_id
            """).fetchall()
            return [self._record(row) for row in rows]

    def history(self, document_id):
        with self.connection() as db:
            rows = db.execute("""
                SELECT * FROM jobs WHERE document_id=? AND
                    (outcome IS NOT NULL OR superseded_by IS NOT NULL) ORDER BY id
            """, (document_id,)).fetchall()
            return [self._record(row) if row["outcome"] is not None else {
                "document_id": row["document_id"], "source_sha256": row["source_sha256"],
                "attempt_id": row["id"], "settings": json.loads(row["settings"]),
                "work_state": "superseded", "superseded_by": row["superseded_by"],
            } for row in rows]

    def queue(self):
        return queue_for(self.current())


def queue_for(records):
    return [{key: row[key] for key in (
        "document_id", "source_sha256", "attempt_id", "status", "owner", "next_action",
        "action_description", "manager_contact", "awaiting_human_review", "error",
        "retry_command",
    )} for row in records]


def outcome_for(source, provider, settings):
    try:
        facts, origin = extract(source, provider)
    except (ExtractionError, ProviderError) as exc:
        # Provider detail is untrusted. Only adapter-defined classifications are published.
        reason = (str(exc) if isinstance(exc, ExtractionError)
                  else PROVIDER_ERRORS.get(exc.code, PROVIDER_ERRORS["unavailable"]))
        return {
            **review("error", "IT Service Desk", "retry-extraction"),
            "extraction": None, "error": reason, "response_source": settings.response_source,
        }
    return {
        **route(source, facts, security_environments=settings.security_environments,
                security_permissions=settings.security_permissions),
        "extraction": facts, "error": None, "response_source": origin,
    }
