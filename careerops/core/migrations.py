from collections.abc import Callable
import json
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
            identity_name TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            privacy_mode TEXT NOT NULL CHECK (
                privacy_mode IN (
                    'local_private', 'trusted_private', 'cloud_external', 'verifier_only'
                )
            ),
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
            task_type TEXT NOT NULL CHECK (
                task_type IN (
                    'analyze_jd', 'analyze_fit', 'suggest_resume_edits',
                    'draft_cover_letter', 'review_claims'
                )
            ),
            provider_config_id TEXT NOT NULL REFERENCES ai_provider_configs(id),
            prompt_template_id TEXT NOT NULL,
            input_artifact_ids_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'running', 'completed', 'blocked', 'failed')
            ),
            privacy_mode TEXT NOT NULL CHECK (
                privacy_mode IN (
                    'local_private', 'trusted_private', 'cloud_external', 'verifier_only'
                )
            ),
            started_at TEXT,
            invocation_started_at TEXT,
            completed_at TEXT,
            error_summary TEXT NOT NULL,
            claim_token TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ai_artifacts (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES ai_tasks(id),
            claim_token TEXT NOT NULL,
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
            claim_token TEXT NOT NULL,
            finding_type TEXT NOT NULL,
            severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'blocking')),
            claim_text TEXT NOT NULL,
            evidence_item_id TEXT REFERENCES evidence_items(id),
            recommendation TEXT NOT NULL,
            verifier_state TEXT NOT NULL CHECK (
                verifier_state IN ('pending', 'verified', 'needs_evidence', 'blocked', 'rejected')
            ),
            decision TEXT NOT NULL CHECK (decision IN ('pending', 'accepted', 'rejected')),
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
            verification_state TEXT NOT NULL CHECK (
                verification_state IN (
                    'verified', 'user_stated', 'needs_verification',
                    'rejected', 'private_do_not_publish'
                )
            ),
            privacy_level TEXT NOT NULL CHECK (
                privacy_level IN ('public', 'private', 'sensitive')
            ),
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
        CREATE TABLE IF NOT EXISTS ai_task_input_artifacts (
            task_id TEXT NOT NULL REFERENCES ai_tasks(id),
            artifact_id TEXT NOT NULL REFERENCES ai_artifacts(id),
            created_at TEXT NOT NULL,
            PRIMARY KEY(task_id, artifact_id)
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
        "CREATE INDEX IF NOT EXISTS idx_ai_tasks_provider ON ai_tasks(provider_config_id)",
        "CREATE INDEX IF NOT EXISTS idx_ai_tasks_status ON ai_tasks(status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_ai_artifacts_task ON ai_artifacts(task_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_ai_artifacts_claim ON ai_artifacts(task_id, claim_token)",
        "CREATE INDEX IF NOT EXISTS idx_ai_findings_task ON ai_findings(task_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_ai_findings_claim ON ai_findings(task_id, claim_token)",
        "CREATE INDEX IF NOT EXISTS idx_ai_findings_evidence ON ai_findings(evidence_item_id)",
        "CREATE INDEX IF NOT EXISTS idx_profile_facts_key ON profile_facts(fact_key, created_at)",
        """
        CREATE INDEX IF NOT EXISTS idx_application_evidence_item
        ON application_evidence_links(evidence_item_id, created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_ai_task_input_artifact
        ON ai_task_input_artifacts(artifact_id, task_id)
        """,
    )
    for statement in statements:
        connection.execute(statement)


def _add_ai_task_claim_token(connection: sqlite3.Connection) -> None:
    if "claim_token" not in _column_names(connection, "ai_tasks"):
        connection.execute("ALTER TABLE ai_tasks ADD COLUMN claim_token TEXT")


def _add_finite_state_guards(connection: sqlite3.Connection) -> None:
    guards = {
        "ai_provider_configs": """
            NEW.enabled NOT IN (0, 1)
            OR NEW.privacy_mode NOT IN (
                'local_private', 'trusted_private', 'cloud_external', 'verifier_only'
            )
        """,
        "ai_tasks": """
            NEW.task_type NOT IN (
                'analyze_jd', 'analyze_fit', 'suggest_resume_edits',
                'draft_cover_letter', 'review_claims'
            )
            OR NEW.status NOT IN ('pending', 'running', 'completed', 'blocked', 'failed')
            OR NEW.privacy_mode NOT IN (
                'local_private', 'trusted_private', 'cloud_external', 'verifier_only'
            )
        """,
        "ai_findings": """
            NEW.severity NOT IN ('info', 'warning', 'blocking')
            OR NEW.verifier_state NOT IN (
                'pending', 'verified', 'needs_evidence', 'blocked', 'rejected'
            )
            OR NEW.decision NOT IN ('pending', 'accepted', 'rejected')
        """,
        "profile_facts": """
            NEW.verification_state NOT IN (
                'verified', 'user_stated', 'needs_verification',
                'rejected', 'private_do_not_publish'
            )
            OR NEW.privacy_level NOT IN ('public', 'private', 'sensitive')
        """,
        "evidence_items": """
            NEW.verification_state NOT IN (
                'verified', 'user_stated', 'needs_verification',
                'rejected', 'private_do_not_publish'
            )
            OR NEW.privacy_level NOT IN ('public', 'private', 'sensitive')
        """,
    }
    for table, invalid_condition in guards.items():
        for operation in ("INSERT", "UPDATE"):
            connection.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS validate_{table}_{operation.lower()}_states
                BEFORE {operation} ON {table}
                WHEN {invalid_condition}
                BEGIN
                    SELECT RAISE(ABORT, 'invalid finite state');
                END
                """
            )


def _fix_ai_foreign_key_indexes(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_tasks_provider ON ai_tasks(provider_config_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_findings_evidence ON ai_findings(evidence_item_id)"
    )
    connection.execute("DROP INDEX IF EXISTS idx_application_versions_application")


def _add_ai_attempt_provenance(connection: sqlite3.Connection) -> None:
    for table in ("ai_artifacts", "ai_findings"):
        if "claim_token" not in _column_names(connection, table):
            connection.execute(f"ALTER TABLE {table} ADD COLUMN claim_token TEXT")
        connection.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{table}_claim
            ON {table}(task_id, claim_token)
            """
        )
        connection.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS validate_{table}_claim_token
            BEFORE INSERT ON {table}
            WHEN NEW.claim_token IS NULL OR NEW.claim_token = ''
            BEGIN
                SELECT RAISE(ABORT, 'missing AI attempt claim token');
            END
            """
        )


def _add_normalized_ai_task_inputs(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_task_input_artifacts (
            task_id TEXT NOT NULL REFERENCES ai_tasks(id),
            artifact_id TEXT NOT NULL REFERENCES ai_artifacts(id),
            created_at TEXT NOT NULL,
            PRIMARY KEY(task_id, artifact_id)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ai_task_input_artifact
        ON ai_task_input_artifacts(artifact_id, task_id)
        """
    )
    tasks = connection.execute(
        "SELECT id, input_artifact_ids_json, created_at FROM ai_tasks"
    ).fetchall()
    for task_id, artifact_ids_json, created_at in tasks:
        try:
            artifact_ids = json.loads(artifact_ids_json)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(artifact_ids, list):
            continue
        for artifact_id in artifact_ids:
            if not isinstance(artifact_id, str):
                continue
            connection.execute(
                """
                INSERT OR IGNORE INTO ai_task_input_artifacts (
                    task_id, artifact_id, created_at
                )
                SELECT target.id, artifact.id, ?
                FROM ai_tasks AS target
                JOIN ai_artifacts AS artifact ON artifact.id = ?
                JOIN ai_tasks AS owner ON owner.id = artifact.task_id
                WHERE target.id = ?
                  AND target.application_id = owner.application_id
                """,
                (created_at, artifact_id, task_id),
            )


def _canonicalize_ai_task_inputs(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TRIGGER IF EXISTS validate_ai_task_input_application_insert")
    connection.execute("DROP TRIGGER IF EXISTS validate_ai_task_input_application_update")
    connection.execute("DROP TABLE IF EXISTS ai_task_input_artifacts_v9")
    connection.execute(
        """
        CREATE TABLE ai_task_input_artifacts_v9 (
            task_id TEXT NOT NULL REFERENCES ai_tasks(id),
            artifact_id TEXT NOT NULL REFERENCES ai_artifacts(id),
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            created_at TEXT NOT NULL,
            UNIQUE(task_id, ordinal),
            UNIQUE(task_id, artifact_id)
        )
        """
    )

    artifact_applications = {
        artifact_id: application_id
        for artifact_id, application_id in connection.execute(
            """
            SELECT artifact.id, owner.application_id
            FROM ai_artifacts AS artifact
            JOIN ai_tasks AS owner ON owner.id = artifact.task_id
            """
        ).fetchall()
    }
    tasks = connection.execute(
        """
        SELECT id, application_id, input_artifact_ids_json, created_at
        FROM ai_tasks
        """
    ).fetchall()
    for task_id, application_id, artifact_ids_json, created_at in tasks:
        try:
            legacy_artifact_ids = json.loads(artifact_ids_json)
        except (TypeError, json.JSONDecodeError):
            legacy_artifact_ids = []
        if not isinstance(legacy_artifact_ids, list):
            legacy_artifact_ids = []

        canonical_artifact_ids: list[str] = []
        seen_artifact_ids: set[str] = set()
        for artifact_id in legacy_artifact_ids:
            if not isinstance(artifact_id, str) or artifact_id in seen_artifact_ids:
                continue
            seen_artifact_ids.add(artifact_id)
            if artifact_applications.get(artifact_id) != application_id:
                continue
            connection.execute(
                """
                INSERT INTO ai_task_input_artifacts_v9 (
                    task_id, artifact_id, ordinal, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (task_id, artifact_id, len(canonical_artifact_ids), created_at),
            )
            canonical_artifact_ids.append(artifact_id)

        connection.execute(
            "UPDATE ai_tasks SET input_artifact_ids_json = ? WHERE id = ?",
            (json.dumps(canonical_artifact_ids), task_id),
        )

    connection.execute("DROP TABLE IF EXISTS ai_task_input_artifacts")
    connection.execute(
        "ALTER TABLE ai_task_input_artifacts_v9 RENAME TO ai_task_input_artifacts"
    )
    connection.execute(
        """
        CREATE INDEX idx_ai_task_input_artifact
        ON ai_task_input_artifacts(artifact_id, task_id)
        """
    )
    for operation in ("INSERT", "UPDATE"):
        connection.execute(
            f"""
            CREATE TRIGGER validate_ai_task_input_application_{operation.lower()}
            BEFORE {operation} ON ai_task_input_artifacts
            WHEN NOT EXISTS (
                SELECT 1
                FROM ai_tasks AS target
                JOIN ai_artifacts AS artifact ON artifact.id = NEW.artifact_id
                JOIN ai_tasks AS owner ON owner.id = artifact.task_id
                WHERE target.id = NEW.task_id
                  AND target.application_id = owner.application_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'input artifact application mismatch');
            END
            """
        )


def _add_ai_invocation_fence(connection: sqlite3.Connection) -> None:
    if "invocation_started_at" not in _column_names(connection, "ai_tasks"):
        connection.execute("ALTER TABLE ai_tasks ADD COLUMN invocation_started_at TEXT")
    connection.execute(
        """
        UPDATE ai_tasks
        SET invocation_started_at = COALESCE(started_at, updated_at, created_at)
        WHERE status = 'running' AND invocation_started_at IS NULL
        """
    )


def _add_provider_identity(connection: sqlite3.Connection) -> None:
    for trigger_name in (
        "validate_codex_identity_insert",
        "validate_codex_identity_update",
        "validate_provider_identity_insert",
        "validate_provider_identity_update",
        "validate_provider_binding_immutable",
    ):
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
    if "identity_name" not in _column_names(connection, "ai_provider_configs"):
        connection.execute(
            """
            ALTER TABLE ai_provider_configs
            ADD COLUMN identity_name TEXT NOT NULL DEFAULT ''
            """
        )
    connection.execute(
        """
        UPDATE ai_provider_configs
        SET identity_name = ''
        WHERE provider_kind != 'codex'
        """
    )
    connection.execute(
        """
        UPDATE ai_provider_configs
        SET identity_name = 'codex2'
        WHERE provider_kind = 'codex'
          AND endpoint = 'codex-identity://codex2'
        """
    )
    connection.execute(
        """
        UPDATE ai_provider_configs
        SET identity_name = 'codex'
        WHERE provider_kind = 'codex'
          AND endpoint = 'codex-identity://codex'
        """
    )
    connection.execute(
        """
        UPDATE ai_provider_configs
        SET enabled = 0, identity_name = ''
        WHERE provider_kind = 'codex'
          AND endpoint NOT IN (
              'codex-identity://codex2', 'codex-identity://codex'
          )
        """
    )
    for operation in ("INSERT", "UPDATE"):
        connection.execute(
            f"""
            CREATE TRIGGER validate_provider_identity_{operation.lower()}
            BEFORE {operation} ON ai_provider_configs
            WHEN (
                NEW.provider_kind != 'codex' AND NEW.identity_name != ''
            ) OR (
                NEW.provider_kind = 'codex'
                AND NOT (
                    (
                        NEW.identity_name = 'codex2'
                        AND NEW.endpoint = 'codex-identity://codex2'
                    )
                    OR (
                        NEW.identity_name = 'codex'
                        AND NEW.endpoint = 'codex-identity://codex'
                    )
                    OR (
                        NEW.enabled = 0
                        AND NEW.identity_name = ''
                        AND NEW.endpoint NOT IN (
                            'codex-identity://codex2',
                            'codex-identity://codex'
                        )
                    )
                )
            )
            BEGIN
                SELECT RAISE(ABORT, 'invalid provider identity binding');
            END
            """
        )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS validate_provider_binding_immutable
        BEFORE UPDATE ON ai_provider_configs
        WHEN NEW.provider_kind != OLD.provider_kind
          OR NEW.endpoint != OLD.endpoint
          OR NEW.model != OLD.model
          OR NEW.identity_name != OLD.identity_name
        BEGIN
            SELECT RAISE(ABORT, 'provider configuration binding is immutable');
        END
        """
    )


def _repair_ai_orchestration_migrations(connection: sqlite3.Connection) -> None:
    _add_ai_invocation_fence(connection)
    _add_provider_identity(connection)


MIGRATIONS: tuple[tuple[int, Migration], ...] = (
    (1, _add_application_role_type),
    (2, _add_application_submitted_at),
    (3, _add_ai_evidence_and_version_tables),
    (4, _add_ai_task_claim_token),
    (5, _add_finite_state_guards),
    (6, _fix_ai_foreign_key_indexes),
    (7, _add_ai_attempt_provenance),
    (8, _add_normalized_ai_task_inputs),
    (9, _canonicalize_ai_task_inputs),
    (10, _add_ai_invocation_fence),
    (11, _add_provider_identity),
    (12, _repair_ai_orchestration_migrations),
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
