from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from careerops.core.migrations import run_migrations


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS applications (
    id TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    role_title TEXT NOT NULL,
    location TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    jd_snapshot_id TEXT NOT NULL,
    status TEXT NOT NULL,
    selected_resume_version_id TEXT,
    latest_report_id TEXT,
    approval_id TEXT,
    submission_state TEXT NOT NULL,
    deadline TEXT,
    notes TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jd_snapshots (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    raw_text_path TEXT NOT NULL,
    raw_text_sha256 TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    extracted_requirements_json TEXT NOT NULL,
    parser_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resume_versions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role_family TEXT NOT NULL,
    tex_path TEXT NOT NULL,
    tex_sha256 TEXT NOT NULL,
    pdf_path TEXT,
    pdf_sha256 TEXT,
    parent_resume_version_id TEXT,
    render_status TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    page_count INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_items (
    id TEXT PRIMARY KEY,
    claim_text TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    source_path TEXT,
    source_url TEXT,
    source_sha256 TEXT,
    verification_state TEXT NOT NULL CHECK (
        verification_state IN (
            'verified', 'user_stated', 'needs_verification',
            'rejected', 'private_do_not_publish'
        )
    ),
    privacy_level TEXT NOT NULL CHECK (privacy_level IN ('public', 'private', 'sensitive')),
    notes TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    resume_version_id TEXT NOT NULL,
    jd_snapshot_id TEXT NOT NULL,
    fit_json TEXT NOT NULL,
    risks_json TEXT NOT NULL,
    blocking_issue_count INTEGER NOT NULL,
    warning_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    resume_version_id TEXT NOT NULL,
    report_id TEXT NOT NULL,
    pdf_sha256 TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    approval_note TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


@contextmanager
def connect_database(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
    finally:
        connection.close()


def initialize_database(path: Path) -> None:
    with connect_database(path) as connection:
        connection.executescript(SCHEMA)
        run_migrations(connection)
