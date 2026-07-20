from collections.abc import Callable
import sqlite3


Migration = Callable[[sqlite3.Connection], None]


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_application_role_type(connection: sqlite3.Connection) -> None:
    if "role_type" not in _column_names(connection, "applications"):
        connection.execute(
            "ALTER TABLE applications ADD COLUMN role_type TEXT NOT NULL DEFAULT ''"
        )


def _add_application_submitted_at(connection: sqlite3.Connection) -> None:
    if "submitted_at" not in _column_names(connection, "applications"):
        connection.execute("ALTER TABLE applications ADD COLUMN submitted_at TEXT")


def _add_ai_evidence_and_version_tables(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS ai_provider_configs (
            id TEXT PRIMARY KEY,
            provider_kind TEXT NOT NULL,
            display_name TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            privacy_mode TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            model TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(provider_kind, endpoint, model)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ai_tasks (
            id TEXT PRIMARY KEY,
            application_id TEXT NOT NULL REFERENCES applications(id),
            task_type TEXT NOT NULL,
            provider_config_id TEXT NOT NULL REFERENCES ai_provider_configs(id),
            prompt_template_id TEXT NOT NULL,
            input_artifact_ids_json TEXT NOT NULL,
            status TEXT NOT NULL,
            privacy_mode TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            error_summary TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ai_artifacts (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES ai_tasks(id),
            artifact_type TEXT NOT NULL,
            path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ai_findings (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES ai_tasks(id),
            finding_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            claim_text TEXT NOT NULL,
            evidence_item_id TEXT REFERENCES evidence_items(id),
            recommendation TEXT NOT NULL,
            verifier_state TEXT NOT NULL,
            decision TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS profile_facts (
            id TEXT PRIMARY KEY,
            fact_key TEXT NOT NULL,
            fact_value TEXT NOT NULL,
            verification_state TEXT NOT NULL,
            privacy_level TEXT NOT NULL,
            notes TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS application_evidence_links (
            application_id TEXT NOT NULL REFERENCES applications(id),
            evidence_item_id TEXT NOT NULL REFERENCES evidence_items(id),
            created_at TEXT NOT NULL,
            PRIMARY KEY(application_id, evidence_item_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS application_versions (
            id TEXT PRIMARY KEY,
            application_id TEXT NOT NULL REFERENCES applications(id),
            version_number INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(application_id, version_number)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_ai_tasks_application ON ai_tasks(application_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_ai_tasks_status ON ai_tasks(status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_ai_artifacts_task ON ai_artifacts(task_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_ai_findings_task ON ai_findings(task_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_profile_facts_key ON profile_facts(fact_key, created_at)",
        """
        CREATE INDEX IF NOT EXISTS idx_application_evidence_item
        ON application_evidence_links(evidence_item_id, created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_application_versions_application
        ON application_versions(application_id, version_number)
        """,
    )
    for statement in statements:
        connection.execute(statement)


MIGRATIONS: tuple[tuple[int, Migration], ...] = (
    (1, _add_application_role_type),
    (2, _add_application_submitted_at),
    (3, _add_ai_evidence_and_version_tables),
)

LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]


def run_migrations(connection: sqlite3.Connection) -> None:
    current_version = connection.execute("PRAGMA user_version").fetchone()[0]
    for version, migration in MIGRATIONS:
        if version <= current_version:
            continue

        connection.execute("BEGIN IMMEDIATE")
        try:
            migration(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

        connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
        current_version = version
