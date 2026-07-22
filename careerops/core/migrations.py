from collections.abc import Callable
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import uuid


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
            availability_binding_json TEXT NOT NULL DEFAULT '{}',
            availability_binding_sha256 TEXT NOT NULL DEFAULT '',
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
            prompt_sha256 TEXT NOT NULL DEFAULT '',
            input_artifact_ids_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'running', 'completed', 'blocked', 'failed')
            ),
            privacy_mode TEXT NOT NULL CHECK (
                privacy_mode IN (
                    'local_private', 'trusted_private', 'cloud_external', 'verifier_only'
                )
            ),
            provider_binding_json TEXT NOT NULL DEFAULT '{}',
            provider_binding_sha256 TEXT NOT NULL DEFAULT '',
            started_at TEXT,
            claim_expires_at TEXT,
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


def _add_provider_availability_bindings(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TRIGGER IF EXISTS validate_ai_task_authority_immutable")
    provider_columns = _column_names(connection, "ai_provider_configs")
    if "availability_binding_json" not in provider_columns:
        connection.execute(
            """
            ALTER TABLE ai_provider_configs
            ADD COLUMN availability_binding_json TEXT NOT NULL DEFAULT '{}'
            """
        )
    if "availability_binding_sha256" not in provider_columns:
        connection.execute(
            """
            ALTER TABLE ai_provider_configs
            ADD COLUMN availability_binding_sha256 TEXT NOT NULL DEFAULT ''
            """
        )
    task_columns = _column_names(connection, "ai_tasks")
    if "provider_binding_json" not in task_columns:
        connection.execute(
            """
            ALTER TABLE ai_tasks
            ADD COLUMN provider_binding_json TEXT NOT NULL DEFAULT '{}'
            """
        )
    if "provider_binding_sha256" not in task_columns:
        connection.execute(
            """
            ALTER TABLE ai_tasks
            ADD COLUMN provider_binding_sha256 TEXT NOT NULL DEFAULT ''
            """
        )
    if "claim_expires_at" not in task_columns:
        connection.execute("ALTER TABLE ai_tasks ADD COLUMN claim_expires_at TEXT")
    connection.execute(
        """
        UPDATE ai_provider_configs
        SET enabled = 0,
            availability_binding_json = '{}',
            availability_binding_sha256 = ''
        """
    )
    connection.execute(
        """
        UPDATE ai_tasks
        SET provider_binding_json = '{}', provider_binding_sha256 = ''
        """
    )
    connection.execute(
        """
        UPDATE ai_tasks
        SET invocation_started_at = COALESCE(started_at, updated_at, created_at)
        WHERE status = 'running' AND invocation_started_at IS NULL
        """
    )
    connection.execute(
        """
        UPDATE ai_tasks
        SET claim_expires_at = COALESCE(updated_at, started_at, created_at)
        WHERE status = 'running' AND claim_expires_at IS NULL
        """
    )
    text_fields = (
        "provider_kind",
        "model",
        "privacy_mode",
        "endpoint",
        "identity_name",
        "executable",
        "executable_sha256",
        "executable_version",
        "codex_home",
        "reasoning_effort",
        "model_digest",
        "model_metadata_sha256",
        "show_metadata_sha256",
        "attestation_version",
        "runtime_instance_id",
        "runtime_handle",
        "container_image_reference",
        "container_image_id",
        "issuer_instance_id",
        "egress_policy",
        "isolation_evidence_sha256",
        "isolation_evidence_signature",
    )
    boolean_fields = (
        "data_leaves_machine",
        "is_ai",
        "cloud_disabled",
        "egress_enforced",
    )
    field_conditions = "\n                OR ".join(
        [
            f"json_type(NEW.availability_binding_json, '$.{field}') IS NOT 'text'"
            for field in text_fields
        ]
        + [
            "(json_type(NEW.availability_binding_json, "
            f"'$.{field}') IS NULL OR json_type(NEW.availability_binding_json, "
            f"'$.{field}') NOT IN ('true', 'false'))"
            for field in boolean_fields
        ]
    )
    for operation in ("INSERT", "UPDATE"):
        trigger_name = f"validate_provider_binding_sha_{operation.lower()}"
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
        connection.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE {operation} ON ai_provider_configs
            WHEN NEW.enabled = 1
             AND (
                length(NEW.availability_binding_sha256) != 64
                OR lower(NEW.availability_binding_sha256)
                    != NEW.availability_binding_sha256
                OR NEW.availability_binding_sha256 GLOB '*[^0-9a-f]*'
                OR NEW.availability_binding_json = '{{}}'
                OR NOT json_valid(NEW.availability_binding_json)
                OR json_type(NEW.availability_binding_json) != 'object'
                OR (
                    SELECT COUNT(*)
                    FROM json_each(NEW.availability_binding_json)
                ) != 26
                OR length(NEW.availability_binding_json) > 32768
                OR {field_conditions}
                OR json_extract(
                    NEW.availability_binding_json, '$.provider_kind'
                ) IS NOT NEW.provider_kind
                OR json_extract(
                    NEW.availability_binding_json, '$.privacy_mode'
                ) IS NOT NEW.privacy_mode
                OR json_extract(
                    NEW.availability_binding_json, '$.endpoint'
                ) IS NOT NEW.endpoint
                OR json_extract(
                    NEW.availability_binding_json, '$.identity_name'
                ) IS NOT NEW.identity_name
                OR json_extract(
                    NEW.availability_binding_json, '$.model'
                ) IS NOT NEW.model
             )
            BEGIN
                SELECT RAISE(ABORT, 'enabled provider is missing availability binding');
            END
            """
        )


def _add_ai_prompt_and_artifact_authority(connection: sqlite3.Connection) -> None:
    if "prompt_sha256" not in _column_names(connection, "ai_tasks"):
        connection.execute(
            "ALTER TABLE ai_tasks ADD COLUMN prompt_sha256 TEXT NOT NULL DEFAULT ''"
        )

    path_expression = "path COLLATE NOCASE" if os.name == "nt" else "path"
    connection.execute(
        f"""
        UPDATE ai_artifacts
        SET path = path || '.quarantined-' || id || '-' || rowid
        WHERE rowid NOT IN (
            SELECT MIN(rowid)
            FROM ai_artifacts
            GROUP BY {path_expression}
        )
        """
    )
    connection.execute("DROP INDEX IF EXISTS idx_ai_artifacts_unique_path")
    index_expression = "path COLLATE NOCASE" if os.name == "nt" else "path"
    connection.execute(
        "CREATE UNIQUE INDEX idx_ai_artifacts_unique_path "
        f"ON ai_artifacts({index_expression})"
    )

    for operation in ("INSERT", "UPDATE"):
        connection.execute(
            f"DROP TRIGGER IF EXISTS validate_ai_artifact_metadata_{operation.lower()}"
        )
        connection.execute(
            f"""
            CREATE TRIGGER validate_ai_artifact_metadata_{operation.lower()}
            BEFORE {operation} ON ai_artifacts
            WHEN NEW.artifact_type NOT IN (
                'context', 'raw_output', 'validated_output', 'draft_text'
            ) OR NEW.schema_version != '1'
            BEGIN
                SELECT RAISE(ABORT, 'invalid AI artifact metadata');
            END
            """
        )

    connection.execute("DROP TRIGGER IF EXISTS validate_ai_task_authority_insert")
    connection.execute(
        """
        CREATE TRIGGER validate_ai_task_authority_insert
        BEFORE INSERT ON ai_tasks
        WHEN length(NEW.prompt_sha256) != 64
          OR lower(NEW.prompt_sha256) != NEW.prompt_sha256
          OR NEW.prompt_sha256 GLOB '*[^0-9a-f]*'
        BEGIN
            SELECT RAISE(ABORT, 'invalid AI task prompt authority');
        END
        """
    )
    connection.execute("DROP TRIGGER IF EXISTS validate_ai_task_authority_immutable")
    connection.execute(
        """
        CREATE TRIGGER validate_ai_task_authority_immutable
        BEFORE UPDATE ON ai_tasks
        WHEN NEW.provider_config_id != OLD.provider_config_id
          OR NEW.prompt_template_id != OLD.prompt_template_id
          OR NEW.prompt_sha256 != OLD.prompt_sha256
          OR NEW.privacy_mode != OLD.privacy_mode
          OR NEW.provider_binding_json != OLD.provider_binding_json
          OR NEW.provider_binding_sha256 != OLD.provider_binding_sha256
        BEGIN
            SELECT RAISE(ABORT, 'AI task authority is immutable');
        END
        """
    )


def _add_ai_artifact_storage_version(connection: sqlite3.Connection) -> None:
    if "storage_version" not in _column_names(connection, "ai_artifacts"):
        connection.execute(
            """
            ALTER TABLE ai_artifacts
            ADD COLUMN storage_version INTEGER NOT NULL DEFAULT 1
            """
        )
    for operation in ("INSERT", "UPDATE"):
        trigger_name = f"validate_ai_artifact_storage_{operation.lower()}"
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
        connection.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE {operation} ON ai_artifacts
            WHEN NEW.storage_version != 2
            BEGIN
                SELECT RAISE(ABORT, 'invalid AI artifact storage version');
            END
            """
        )


def _upgrade_provider_binding_image_fields(
    raw_json: str, expected_sha256: str
) -> tuple[str, str] | None:
    text_fields = {
        "provider_kind",
        "model",
        "privacy_mode",
        "endpoint",
        "identity_name",
        "executable",
        "executable_sha256",
        "executable_version",
        "codex_home",
        "reasoning_effort",
        "model_digest",
        "model_metadata_sha256",
        "show_metadata_sha256",
        "attestation_version",
        "runtime_instance_id",
        "runtime_handle",
        "issuer_instance_id",
        "egress_policy",
        "isolation_evidence_sha256",
        "isolation_evidence_signature",
    }
    boolean_fields = {
        "data_leaves_machine",
        "is_ai",
        "cloud_disabled",
        "egress_enforced",
    }
    old_fields = text_fields | boolean_fields
    image_fields = {"container_image_reference", "container_image_id"}
    try:
        binding = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(binding, dict) or set(binding) not in (
        old_fields,
        old_fields | image_fields,
    ):
        return None
    if any(not isinstance(binding[field], str) for field in text_fields):
        return None
    if any(type(binding[field]) is not bool for field in boolean_fields):
        return None
    if any(
        field in binding and not isinstance(binding[field], str)
        for field in image_fields
    ):
        return None
    canonical_before = json.dumps(binding, sort_keys=True, separators=(",", ":"))
    if raw_json != canonical_before:
        return None
    if hashlib.sha256(raw_json.encode("utf-8")).hexdigest() != expected_sha256:
        return None
    binding.setdefault("container_image_reference", "")
    binding.setdefault("container_image_id", "")
    canonical_after = json.dumps(binding, sort_keys=True, separators=(",", ":"))
    return (
        canonical_after,
        hashlib.sha256(canonical_after.encode("utf-8")).hexdigest(),
    )


def _add_provider_container_image_bindings(connection: sqlite3.Connection) -> None:
    provider_bindings = connection.execute(
        """
        SELECT id, availability_binding_json, availability_binding_sha256
        FROM ai_provider_configs
        """
    ).fetchall()
    task_bindings = connection.execute(
        """
        SELECT id, provider_binding_json, provider_binding_sha256
        FROM ai_tasks
        """
    ).fetchall()

    # Rebuild the latest validation triggers and disable capabilities that
    # predate image identity. Valid historical provenance is retained below.
    _add_provider_availability_bindings(connection)
    for provider_id, raw_json, expected_sha256 in provider_bindings:
        upgraded = _upgrade_provider_binding_image_fields(raw_json, expected_sha256)
        if upgraded is not None:
            connection.execute(
                """
                UPDATE ai_provider_configs
                SET availability_binding_json = ?, availability_binding_sha256 = ?
                WHERE id = ?
                """,
                (*upgraded, provider_id),
            )
    for task_id, raw_json, expected_sha256 in task_bindings:
        upgraded = _upgrade_provider_binding_image_fields(raw_json, expected_sha256)
        if upgraded is not None:
            connection.execute(
                """
                UPDATE ai_tasks
                SET provider_binding_json = ?, provider_binding_sha256 = ?
                WHERE id = ?
                """,
                (*upgraded, task_id),
            )
    _add_ai_prompt_and_artifact_authority(connection)


_V17_REPORT_PROVENANCE_FIELDS = frozenset(
    {
        "ai_task_id",
        "context_sha256",
        "output_checksums",
        "privacy_mode",
        "prompt_sha256",
        "prompt_template_id",
        "provider_binding",
        "provider_binding_sha256",
        "provider_config_id",
        "provider_identity_name",
        "provider_kind",
        "provider_model",
    }
)
_V17_PROVIDER_BINDING_FIELDS = (
    "provider_kind",
    "model",
    "privacy_mode",
    "data_leaves_machine",
    "is_ai",
    "endpoint",
    "identity_name",
    "executable",
    "executable_sha256",
    "executable_version",
    "codex_home",
    "reasoning_effort",
    "model_digest",
    "model_metadata_sha256",
    "show_metadata_sha256",
    "attestation_version",
    "runtime_instance_id",
    "runtime_handle",
    "container_image_reference",
    "container_image_id",
    "issuer_instance_id",
    "egress_policy",
    "cloud_disabled",
    "egress_enforced",
    "isolation_evidence_sha256",
    "isolation_evidence_signature",
)
_V17_PROVIDER_BOOLEAN_FIELDS = frozenset(
    {"data_leaves_machine", "is_ai", "cloud_disabled", "egress_enforced"}
)
_V17_AI_ARTIFACT_TYPES = frozenset(
    {"context", "raw_output", "validated_output", "draft_text"}
)
_V17_MAX_AUTHORITY_FILE_BYTES = 16_000_000


def _v17_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _v17_audit_value(value: object) -> object:
    if value is None or type(value) in {bool, int, str}:
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return {
            "sqlite_storage_class": "real",
            "value": str(value),
        }
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "encoding": "hex",
            "sqlite_storage_class": "blob",
            "value": bytes(value).hex(),
        }
    if isinstance(value, dict):
        return {str(key): _v17_audit_value(inner) for key, inner in value.items()}
    if isinstance(value, (list, tuple)):
        return [_v17_audit_value(inner) for inner in value]
    return {
        "python_type": type(value).__name__,
        "value": str(value),
    }


def _v17_is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _v17_contains_reserved_provenance(
    value: object, *, allow_root: bool = False
) -> bool:
    if isinstance(value, dict):
        if "provenance" in value and not allow_root:
            return True
        return any(
            _v17_contains_reserved_provenance(inner)
            for key, inner in value.items()
            if not (allow_root and key == "provenance")
        )
    if isinstance(value, (list, tuple)):
        return any(_v17_contains_reserved_provenance(inner) for inner in value)
    return False


def _v17_provider_binding(raw_json: object, expected_sha256: object) -> dict | None:
    if not isinstance(raw_json, str) or not _v17_is_sha256(expected_sha256):
        return None
    try:
        value = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or set(value) != set(_V17_PROVIDER_BINDING_FIELDS):
        return None
    for field in _V17_PROVIDER_BINDING_FIELDS:
        field_value = value[field]
        if field in _V17_PROVIDER_BOOLEAN_FIELDS:
            if type(field_value) is not bool:
                return None
        elif not isinstance(field_value, str):
            return None
    if _v17_json(value) != raw_json:
        return None
    if hashlib.sha256(raw_json.encode("utf-8")).hexdigest() != expected_sha256:
        return None
    return value


def _v17_read_authority_file(raw_path: object) -> tuple[bytes, str] | None:
    if not isinstance(raw_path, str) or not raw_path:
        return None
    path = Path(raw_path)
    try:
        canonical_path = Path(os.path.realpath(path))
        if not path.is_absolute() or str(canonical_path) != raw_path:
            return None
        path_stat = canonical_path.lstat()
        if (
            stat.S_ISLNK(path_stat.st_mode)
            or not stat.S_ISREG(path_stat.st_mode)
            or path_stat.st_nlink != 1
            or path_stat.st_size > _V17_MAX_AUTHORITY_FILE_BYTES
        ):
            return None
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(canonical_path, flags)
        try:
            opened = os.fstat(descriptor)
            identity = (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_nlink,
            )
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_size > _V17_MAX_AUTHORITY_FILE_BYTES
            ):
                return None
            chunks: list[bytes] = []
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _V17_MAX_AUTHORITY_FILE_BYTES:
                    return None
                chunks.append(chunk)
                digest.update(chunk)
            after_read = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after_path = canonical_path.lstat()
        if identity != (
            after_read.st_dev,
            after_read.st_ino,
            after_read.st_size,
            after_read.st_mtime_ns,
            after_read.st_nlink,
        ) or identity != (
            after_path.st_dev,
            after_path.st_ino,
            after_path.st_size,
            after_path.st_mtime_ns,
            after_path.st_nlink,
        ):
            return None
    except (OSError, ValueError):
        return None
    return b"".join(chunks), digest.hexdigest()


def _v17_is_link_or_reparse(path_stat: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(path_stat, "st_file_attributes", 0)
    return stat.S_ISLNK(path_stat.st_mode) or bool(attributes & reparse_flag)


def _v17_vault_relative_parts(
    raw_path: object, vault_path: Path | None
) -> tuple[str, ...] | None:
    if not isinstance(raw_path, str) or not raw_path or vault_path is None:
        return None
    raw_candidate = Path(raw_path)
    if not raw_candidate.is_absolute():
        return None
    vault_root = Path(os.path.abspath(vault_path))
    candidate = Path(os.path.abspath(raw_candidate))
    if str(candidate) != raw_path:
        return None
    try:
        relative = candidate.relative_to(vault_root)
        current = vault_root
        root_stat = current.lstat()
        if _v17_is_link_or_reparse(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
            return None
        for index, part in enumerate(relative.parts):
            current = current / part
            path_stat = current.lstat()
            if _v17_is_link_or_reparse(path_stat):
                return None
            if index < len(relative.parts) - 1:
                if not stat.S_ISDIR(path_stat.st_mode):
                    return None
            elif not stat.S_ISREG(path_stat.st_mode) or path_stat.st_nlink != 1:
                return None
        if (
            Path(os.path.realpath(vault_root)) != vault_root
            or Path(os.path.realpath(candidate)) != candidate
        ):
            return None
    except (FileNotFoundError, OSError, ValueError):
        return None
    return relative.parts


def _v17_artifact_path_matches(
    raw_path: object,
    task_id: str,
    artifact_id: str,
    vault_path: Path | None,
) -> bool:
    parts = _v17_vault_relative_parts(raw_path, vault_path)
    return parts is not None and len(parts) == 4 and tuple(
        os.path.normcase(part) for part in parts[:3]
    ) == tuple(
        os.path.normcase(part)
        for part in ("ai-artifacts", task_id, artifact_id)
    ) and bool(parts[3])


def _v17_resume_path_matches(raw_path: object, vault_path: Path | None) -> bool:
    parts = _v17_vault_relative_parts(raw_path, vault_path)
    return (
        parts is not None
        and len(parts) >= 2
        and os.path.normcase(parts[0]) == os.path.normcase("resumes")
        and bool(parts[-1])
    )


def _v17_report_authority(
    connection: sqlite3.Connection,
    application: sqlite3.Row,
    report: sqlite3.Row,
    resume: sqlite3.Row,
    vault_path: Path | None,
) -> tuple[str, str] | None:
    try:
        if (
            application["latest_report_id"] != report["id"]
            or application["id"] != report["application_id"]
            or application["selected_resume_version_id"] != report["resume_version_id"]
            or application["jd_snapshot_id"] != report["jd_snapshot_id"]
            or resume["id"] != report["resume_version_id"]
            or not _v17_is_sha256(resume["pdf_sha256"])
            or not _v17_is_sha256(resume["tex_sha256"])
        ):
            return None

        if (
            not _v17_resume_path_matches(resume["pdf_path"], vault_path)
            or not _v17_resume_path_matches(resume["tex_path"], vault_path)
        ):
            return None
        pdf_file = _v17_read_authority_file(resume["pdf_path"])
        source_file = _v17_read_authority_file(resume["tex_path"])
        if (
            pdf_file is None
            or pdf_file[1] != resume["pdf_sha256"]
            or source_file is None
            or source_file[1] != resume["tex_sha256"]
        ):
            return None

        fit = json.loads(report["fit_json"])
        risks = json.loads(report["risks_json"])
        if not isinstance(fit, dict) or not isinstance(risks, dict):
            return None
        if (
            set(fit).intersection(_V17_REPORT_PROVENANCE_FIELDS)
            or set(risks).intersection(_V17_REPORT_PROVENANCE_FIELDS)
            or _v17_contains_reserved_provenance(fit, allow_root=True)
            or _v17_contains_reserved_provenance(risks, allow_root=True)
        ):
            return None
        fit_provenance = fit.get("provenance")
        if (
            not isinstance(fit_provenance, dict)
            or set(fit_provenance) != _V17_REPORT_PROVENANCE_FIELDS
            or risks.get("provenance") != fit_provenance
        ):
            return None
        blockers = risks.get("blockers")
        warnings = risks.get("warnings")
        if (
            not isinstance(blockers, list)
            or not isinstance(warnings, list)
            or blockers
            or len(blockers) != report["blocking_issue_count"]
            or len(warnings) != report["warning_count"]
        ):
            return None

        authority_snapshots: list[dict] = []
        versions = connection.execute(
            """
            SELECT snapshot_json FROM application_versions
            WHERE application_id = ? AND event_type = 'ai_task.completed'
            """,
            (application["id"],),
        ).fetchall()
        for version in versions:
            try:
                snapshot = json.loads(version["snapshot_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                isinstance(snapshot, dict)
                and isinstance(snapshot.get("side_effect"), dict)
                and snapshot["side_effect"].get("report_id") == report["id"]
            ):
                authority_snapshots.append(snapshot)
        if len(authority_snapshots) != 1:
            return None
        authority = authority_snapshots[0]
        task_snapshot = authority.get("ai_task")
        if not isinstance(task_snapshot, dict):
            return None
        task_id = task_snapshot.get("id")
        if not isinstance(task_id, str) or fit_provenance.get("ai_task_id") != task_id:
            return None
        if authority.get("provenance") != fit_provenance:
            return None

        task = connection.execute(
            "SELECT * FROM ai_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if (
            task is None
            or task["application_id"] != application["id"]
            or task["task_type"] != "analyze_fit"
            or task["status"] != "completed"
            or not task["completed_at"]
            or not _v17_is_sha256(task["prompt_sha256"])
        ):
            return None
        provider = connection.execute(
            "SELECT * FROM ai_provider_configs WHERE id = ?",
            (task["provider_config_id"],),
        ).fetchone()
        if provider is None or provider["enabled"] != 1:
            return None
        task_binding = _v17_provider_binding(
            task["provider_binding_json"], task["provider_binding_sha256"]
        )
        provider_binding = _v17_provider_binding(
            provider["availability_binding_json"],
            provider["availability_binding_sha256"],
        )
        if (
            task_binding is None
            or provider_binding is None
            or task_binding != provider_binding
            or task["provider_binding_sha256"]
            != provider["availability_binding_sha256"]
            or task_binding["is_ai"] is not True
            or task["privacy_mode"] != provider["privacy_mode"]
            or task_binding["provider_kind"] != provider["provider_kind"]
            or task_binding["model"] != provider["model"]
            or task_binding["endpoint"] != provider["endpoint"]
            or task_binding["identity_name"] != provider["identity_name"]
        ):
            return None

        normalized_input_ids = [
            row["artifact_id"]
            for row in connection.execute(
                """
                SELECT artifact_id FROM ai_task_input_artifacts
                WHERE task_id = ? ORDER BY ordinal ASC
                """,
                (task_id,),
            ).fetchall()
        ]
        declared_input_ids = json.loads(task["input_artifact_ids_json"])
        if (
            declared_input_ids != normalized_input_ids
            or task_snapshot.get("input_artifact_ids") != normalized_input_ids
            or task_snapshot.get("provider_config_id") != provider["id"]
            or task_snapshot.get("status") != "completed"
            or task_snapshot.get("task_type") != "analyze_fit"
        ):
            return None

        manifest = authority.get("artifacts")
        if not isinstance(manifest, list):
            return None
        manifest_by_id: dict[str, dict] = {}
        manifest_by_type: dict[str, dict] = {}
        for item in manifest:
            if (
                not isinstance(item, dict)
                or set(item) != {"artifact_type", "id", "sha256"}
                or any(type(item.get(field)) is not str for field in item)
                or not _v17_is_sha256(item["sha256"])
                or item["id"] in manifest_by_id
                or item["artifact_type"] in manifest_by_type
            ):
                return None
            manifest_by_id[item["id"]] = item
            manifest_by_type[item["artifact_type"]] = item
        if set(manifest_by_type) != {"context", "raw_output", "validated_output"}:
            return None
        placeholders = ", ".join("?" for _ in manifest_by_id)
        artifact_rows = connection.execute(
            f"SELECT * FROM ai_artifacts WHERE id IN ({placeholders})",
            tuple(manifest_by_id),
        ).fetchall()
        if len(artifact_rows) != len(manifest_by_id):
            return None
        artifacts = {row["id"]: row for row in artifact_rows}
        context_row = artifacts[manifest_by_type["context"]["id"]]
        claim_token = context_row["claim_token"]
        if not isinstance(claim_token, str) or not claim_token:
            return None
        artifact_contents: dict[str, bytes] = {}
        for artifact_id, item in manifest_by_id.items():
            artifact = artifacts[artifact_id]
            if (
                artifact["task_id"] != task_id
                or artifact["claim_token"] != claim_token
                or artifact["artifact_type"] != item["artifact_type"]
                or artifact["sha256"] != item["sha256"]
                or artifact["schema_version"] != "1"
                or artifact["storage_version"] != 2
                or not _v17_artifact_path_matches(
                    artifact["path"], task_id, artifact_id, vault_path
                )
            ):
                return None
            file_contents = _v17_read_authority_file(artifact["path"])
            if file_contents is None or file_contents[1] != artifact["sha256"]:
                return None
            artifact_contents[artifact["artifact_type"]] = file_contents[0]
        attempt_artifact_ids = {
            row["id"]
            for row in connection.execute(
                "SELECT id FROM ai_artifacts WHERE task_id = ? AND claim_token = ?",
                (task_id, claim_token),
            ).fetchall()
        }
        if attempt_artifact_ids != set(manifest_by_id):
            return None

        context = json.loads(artifact_contents["context"])
        if (
            not isinstance(context, dict)
            or context.get("task_type") != "analyze_fit"
            or not isinstance(context.get("job_description"), dict)
            or context["job_description"].get("snapshot_id")
            != report["jd_snapshot_id"]
            or not isinstance(context.get("selected_resume"), dict)
            or context["selected_resume"].get("version_id")
            != report["resume_version_id"]
        ):
            return None

        finding_ids = authority.get("finding_ids")
        if (
            not isinstance(finding_ids, list)
            or any(type(finding_id) is not str for finding_id in finding_ids)
            or len(set(finding_ids)) != len(finding_ids)
        ):
            return None
        finding_rows = connection.execute(
            "SELECT * FROM ai_findings WHERE task_id = ? AND claim_token = ?",
            (task_id, claim_token),
        ).fetchall()
        if {row["id"] for row in finding_rows} != set(finding_ids):
            return None
        if any(
            row["severity"] == "blocking"
            or row["verifier_state"]
            in {"blocked", "needs_evidence", "rejected"}
            for row in finding_rows
        ):
            return None

        expected_provenance = {
            "ai_task_id": task_id,
            "context_sha256": manifest_by_type["context"]["sha256"],
            "output_checksums": {
                "raw_output": manifest_by_type["raw_output"]["sha256"],
                "validated_output": manifest_by_type["validated_output"]["sha256"],
            },
            "privacy_mode": task["privacy_mode"],
            "prompt_sha256": task["prompt_sha256"],
            "prompt_template_id": task["prompt_template_id"],
            "provider_binding": task_binding,
            "provider_binding_sha256": task["provider_binding_sha256"],
            "provider_config_id": provider["id"],
            "provider_identity_name": provider["identity_name"],
            "provider_kind": provider["provider_kind"],
            "provider_model": provider["model"],
        }
        if fit_provenance != expected_provenance:
            return None
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return task_id, resume["pdf_sha256"]


def _v17_discard_unsafe_approval_artifacts(
    connection: sqlite3.Connection, vault_path: Path | None
) -> None:
    approved_artifacts = connection.execute(
        """
        SELECT artifact.*, task.application_id
        FROM ai_artifacts AS artifact
        JOIN ai_tasks AS task ON task.id = artifact.task_id
        JOIN applications AS application ON application.id = task.application_id
        WHERE application.approval_id IS NOT NULL
        ORDER BY task.application_id, artifact.created_at, artifact.id
        """
    ).fetchall()

    unsafe_by_application: dict[str, list[tuple[sqlite3.Row, list[str]]]] = {}
    for artifact in approved_artifacts:
        if artifact["storage_version"] == 1:
            raise ValueError(
                "legacy AI artifacts must be upgraded before v17 migration"
            )
        rejection_reasons: list[str] = []
        if artifact["storage_version"] != 2:
            rejection_reasons.append("storage_version")
        if artifact["artifact_type"] not in _V17_AI_ARTIFACT_TYPES:
            rejection_reasons.append("artifact_type")
        if artifact["schema_version"] != "1":
            rejection_reasons.append("schema_version")
        if not _v17_is_sha256(artifact["sha256"]):
            rejection_reasons.append("sha256")
        metadata_is_valid = not rejection_reasons
        path_is_owned = _v17_artifact_path_matches(
            artifact["path"], artifact["task_id"], artifact["id"], vault_path
        )
        if not path_is_owned:
            rejection_reasons.append("repository_owned_path")
        file_contents = (
            _v17_read_authority_file(artifact["path"])
            if metadata_is_valid and path_is_owned
            else None
        )
        if metadata_is_valid and path_is_owned:
            if file_contents is None:
                rejection_reasons.append("stable_file")
            elif file_contents[1] != artifact["sha256"]:
                rejection_reasons.append("checksum")
        if rejection_reasons:
            unsafe_by_application.setdefault(artifact["application_id"], []).append(
                (artifact, rejection_reasons)
            )

    reason = "AI artifacts were not provably repository-owned during v17 migration"
    for application_id, unsafe_artifacts in unsafe_by_application.items():
        artifacts = [artifact for artifact, _reasons in unsafe_artifacts]
        artifact_ids = [artifact["id"] for artifact in artifacts]
        placeholders = ", ".join("?" for _ in artifact_ids)
        removed_links = connection.execute(
            f"""
            SELECT task_id, artifact_id, ordinal, created_at
            FROM ai_task_input_artifacts
            WHERE artifact_id IN ({placeholders})
            ORDER BY task_id, ordinal, artifact_id
            """,
            artifact_ids,
        ).fetchall()
        dependent_task_ids = sorted({row["task_id"] for row in removed_links})
        manifest_before: dict[str, object] = {}
        for task_id in dependent_task_ids:
            raw_manifest = connection.execute(
                "SELECT input_artifact_ids_json FROM ai_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()[0]
            try:
                manifest_before[task_id] = json.loads(raw_manifest)
            except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
                manifest_before[task_id] = raw_manifest
        connection.execute(
            f"DELETE FROM ai_task_input_artifacts WHERE artifact_id IN ({placeholders})",
            artifact_ids,
        )
        updated_task_manifests: list[dict[str, object]] = []
        for task_id in dependent_task_ids:
            remaining_ids = [
                row["artifact_id"]
                for row in connection.execute(
                    """
                    SELECT artifact_id FROM ai_task_input_artifacts
                    WHERE task_id = ? ORDER BY ordinal ASC
                    """,
                    (task_id,),
                ).fetchall()
            ]
            connection.execute(
                "UPDATE ai_tasks SET input_artifact_ids_json = ? WHERE id = ?",
                (_v17_json(remaining_ids), task_id),
            )
            updated_task_manifests.append(
                {
                    "after": remaining_ids,
                    "before": manifest_before[task_id],
                    "task_id": task_id,
                }
            )
        connection.execute(
            f"DELETE FROM ai_artifacts WHERE id IN ({placeholders})", artifact_ids
        )
        now = datetime.now(UTC).isoformat()
        connection.execute(
            """
            INSERT INTO audit_events (
                id, event_type, entity_type, entity_id,
                before_json, after_json, created_at
            ) VALUES (?, ?, 'application', ?, ?, ?, ?)
            """,
            (
                f"aud_{uuid.uuid4().hex}",
                "ai_artifacts.discarded_by_ai_authority_migration",
                application_id,
                _v17_json(
                    _v17_audit_value(
                        {
                            "artifacts": [
                                {
                                    "artifact_type": artifact["artifact_type"],
                                    "claim_token": artifact["claim_token"],
                                    "created_at": artifact["created_at"],
                                    "id": artifact["id"],
                                    "path": artifact["path"],
                                    "rejection_reasons": rejection_reasons,
                                    "schema_version": artifact["schema_version"],
                                    "sha256": artifact["sha256"],
                                    "storage_version": artifact["storage_version"],
                                    "task_id": artifact["task_id"],
                                }
                                for artifact, rejection_reasons in unsafe_artifacts
                            ],
                            "removed_input_links": [
                                dict(row) for row in removed_links
                            ],
                        }
                    )
                ),
                _v17_json(
                    _v17_audit_value(
                        {
                            "artifact_ids": artifact_ids,
                            "reason": reason,
                            "updated_task_manifests": updated_task_manifests,
                        }
                    )
                ),
                now,
            ),
        )


def _v17_recover_unprovable_approvals(
    connection: sqlite3.Connection, vault_path: Path | None
) -> None:
    reason = "AI approval authority could not be proven during v17 migration"
    applications = connection.execute(
        """
        SELECT * FROM applications WHERE approval_id IS NOT NULL
        """
    ).fetchall()
    for application in applications:
        report = connection.execute(
            "SELECT * FROM reports WHERE id = ?", (application["latest_report_id"],)
        ).fetchone()
        resume = connection.execute(
            "SELECT * FROM resume_versions WHERE id = ?",
            (application["selected_resume_version_id"],),
        ).fetchone()
        approval = connection.execute(
            "SELECT * FROM approvals WHERE id = ?", (application["approval_id"],)
        ).fetchone()
        authority = (
            _v17_report_authority(
                connection, application, report, resume, vault_path
            )
            if report is not None and resume is not None
            else None
        )
        is_submitted = (
            application["submission_state"] != "not_submitted"
            or application["submitted_at"] is not None
            or application["status"] == "submitted_manual"
        )
        preserve = (
            authority is not None
            and report["ai_task_id"] == authority[0]
            and report["pdf_sha256"] == authority[1]
            and approval is not None
            and approval["application_id"] == application["id"]
            and approval["report_id"] == report["id"]
            and approval["resume_version_id"] == resume["id"]
            and approval["pdf_sha256"] == resume["pdf_sha256"]
            and (application["status"] == "approved" or is_submitted)
        )
        if preserve:
            continue
        now = datetime.now(UTC).isoformat()
        if is_submitted:
            existing = connection.execute(
                """
                SELECT 1 FROM audit_events
                WHERE event_type = ? AND entity_type = 'application'
                  AND entity_id = ?
                LIMIT 1
                """,
                (
                    "application.submitted_authority_unprovable_by_ai_migration",
                    application["id"],
                ),
            ).fetchone()
            if existing is None:
                historical_before = {
                    "approval_id": application["approval_id"],
                    "latest_report_id": application["latest_report_id"],
                    "status": application["status"],
                    "submission_state": application["submission_state"],
                }
                historical_after = {**historical_before, "reason": reason}
                connection.execute(
                    """
                    INSERT INTO audit_events (
                        id, event_type, entity_type, entity_id,
                        before_json, after_json, created_at
                    ) VALUES (?, ?, 'application', ?, ?, ?, ?)
                    """,
                    (
                        f"aud_{uuid.uuid4().hex}",
                        "application.submitted_authority_unprovable_by_ai_migration",
                        application["id"],
                        _v17_json(_v17_audit_value(historical_before)),
                        _v17_json(_v17_audit_value(historical_after)),
                        now,
                    ),
                )
            continue
        before = {
            "approval_id": application["approval_id"],
            "latest_report_id": application["latest_report_id"],
            "status": application["status"],
        }
        after = {
            "approval_id": None,
            "latest_report_id": None,
            "reason": reason,
            "status": "ready_for_review",
        }
        connection.execute(
            """
            UPDATE applications
            SET approval_id = NULL, latest_report_id = NULL,
                status = 'ready_for_review', updated_at = ?
            WHERE id = ?
            """,
            (now, application["id"]),
        )
        connection.execute(
            """
            INSERT INTO audit_events (
                id, event_type, entity_type, entity_id,
                before_json, after_json, created_at
            ) VALUES (?, ?, 'application', ?, ?, ?, ?)
            """,
            (
                f"aud_{uuid.uuid4().hex}",
                "application.approval_invalidated_by_ai_authority_migration",
                application["id"],
                _v17_json(_v17_audit_value(before)),
                _v17_json(_v17_audit_value(after)),
                now,
            ),
        )


def _add_report_ai_approval_authority(
    connection: sqlite3.Connection, vault_path: Path | None = None
) -> None:
    has_approval = connection.execute(
        "SELECT 1 FROM applications WHERE approval_id IS NOT NULL LIMIT 1"
    ).fetchone()
    if has_approval is not None and vault_path is None:
        raise ValueError("v17 migration requires the configured repository vault")
    report_columns = _column_names(connection, "reports")
    if "ai_task_id" not in report_columns:
        connection.execute(
            "ALTER TABLE reports ADD COLUMN ai_task_id TEXT REFERENCES ai_tasks(id)"
        )
    if "pdf_sha256" not in report_columns:
        connection.execute("ALTER TABLE reports ADD COLUMN pdf_sha256 TEXT")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_reports_ai_task ON reports(ai_task_id)"
    )
    previous_row_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        _v17_discard_unsafe_approval_artifacts(connection, vault_path)
        applications = connection.execute(
            """
            SELECT * FROM applications
            WHERE latest_report_id IS NOT NULL
            """
        ).fetchall()
        for application in applications:
            report = connection.execute(
                """
                SELECT * FROM reports
                WHERE id = ? AND ai_task_id IS NULL AND pdf_sha256 IS NULL
                """,
                (application["latest_report_id"],),
            ).fetchone()
            resume = connection.execute(
                "SELECT * FROM resume_versions WHERE id = ?",
                (application["selected_resume_version_id"],),
            ).fetchone()
            if report is None or resume is None:
                continue
            authority = _v17_report_authority(
                connection, application, report, resume, vault_path
            )
            if authority is None:
                continue
            connection.execute(
                """
                UPDATE reports SET ai_task_id = ?, pdf_sha256 = ?
                WHERE id = ? AND ai_task_id IS NULL AND pdf_sha256 IS NULL
                """,
                (*authority, report["id"]),
            )
        _v17_recover_unprovable_approvals(connection, vault_path)
    finally:
        connection.row_factory = previous_row_factory


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
    (13, _add_provider_availability_bindings),
    (14, _add_ai_prompt_and_artifact_authority),
    (15, _add_ai_artifact_storage_version),
    (16, _add_provider_container_image_bindings),
    (17, _add_report_ai_approval_authority),
)

LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]
PRE_AI_APPROVAL_AUTHORITY_SCHEMA_VERSION = 16


def run_migrations(
    connection: sqlite3.Connection,
    *,
    vault_path: Path | None = None,
    target_version: int | None = None,
) -> None:
    maximum_version = (
        LATEST_SCHEMA_VERSION if target_version is None else target_version
    )
    if maximum_version < 0 or maximum_version > LATEST_SCHEMA_VERSION:
        raise ValueError("migration target version is invalid")
    current_version = connection.execute("PRAGMA user_version").fetchone()[0]
    for version, migration in MIGRATIONS:
        if version <= current_version or version > maximum_version:
            continue

        connection.execute("BEGIN IMMEDIATE")
        try:
            if version == 17:
                _add_report_ai_approval_authority(connection, vault_path)
            else:
                migration(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

        connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
        current_version = version
