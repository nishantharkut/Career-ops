from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from threading import Lock
from typing import Any
from uuid import uuid4

from careerops.core.database import connect_database, initialize_database
from careerops.core.models import (
    AiArtifact,
    AiFinding,
    AiProviderConfig,
    AiTask,
    Application,
    ApplicationVersion,
    Approval,
    AuditEvent,
    EvidenceItem,
    JobDescriptionSnapshot,
    ProfileFact,
    Report,
    ResumeVersion,
)
from careerops.core.status import (
    AiTaskStatus,
    AiTaskType,
    ApplicationStatus,
    FindingDecision,
    FindingSeverity,
    PrivacyLevel,
    PrivacyMode,
    SubmissionState,
    VerificationState,
    VerifierState,
)


SECRET_PATTERNS = (
    (
        re.compile(
            r'''(?is)(?P<prefix>["']?(?:password|secret|token|api[_-]?key|authorization)["']?\s*[:=]\s*)'''
            r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\r\n,;}]+)'''
        ),
        r"\g<prefix>[REDACTED]",
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
)
SECRET_KEY_PATTERN = re.compile(r"(?i)(password|secret|token|api[_-]?key|authorization)")
PROVIDER_ERROR_CATEGORIES = frozenset(
    {
        "provider_unavailable",
        "provider_timeout",
        "provider_error",
        "invalid_output",
        "verification_failed",
        "cancelled",
    }
)
SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")
CODEX_IDENTITIES = frozenset({"codex2", "codex"})
MAX_PERSISTED_JSON_BYTES = 256_000
MAX_PROVIDER_BINDING_BYTES = 32_768
MAX_AI_ARTIFACT_BYTES = 16_000_000
AI_ARTIFACT_TYPES = frozenset(
    {"context", "raw_output", "validated_output", "draft_text"}
)
AI_ARTIFACT_SCHEMA_VERSIONS = frozenset({"1"})
PROVIDER_BINDING_FIELDS = (
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
    "issuer_instance_id",
    "egress_policy",
    "cloud_disabled",
    "egress_enforced",
    "isolation_evidence_sha256",
    "isolation_evidence_signature",
)
PROVIDER_BINDING_BOOLEAN_FIELDS = frozenset(
    {"data_leaves_machine", "is_ai", "cloud_disabled", "egress_enforced"}
)
UTC_NOW_LOCK = Lock()
LAST_UTC_NOW: datetime | None = None


def utc_now() -> str:
    global LAST_UTC_NOW

    now = datetime.now(UTC)
    with UTC_NOW_LOCK:
        if LAST_UTC_NOW is not None and now <= LAST_UTC_NOW:
            now = LAST_UTC_NOW + timedelta(microseconds=1)
        LAST_UTC_NOW = now
    return now.isoformat(timespec="microseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, inner in value.items():
            if not isinstance(key, str):
                raise TypeError(f"Unsupported audit key type: {type(key).__name__}")
            normalized[key] = (
                "[REDACTED]" if SECRET_KEY_PATTERN.search(key) else redact(inner)
            )
        return normalized
    if isinstance(value, (list, tuple)):
        return [redact(inner) for inner in value]
    if isinstance(value, str):
        redacted = value
        for pattern, replacement in SECRET_PATTERNS:
            redacted = pattern.sub(replacement, redacted)
        return redacted
    if isinstance(value, Path):
        return redact(str(value))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise TypeError(f"Unsupported audit value type: {type(value).__name__}")


def provider_error_category(value: str) -> str:
    normalized = value.strip().lower()
    return normalized if normalized in PROVIDER_ERROR_CATEGORIES else "provider_error"


def persisted_json(value: Any) -> tuple[Any, str]:
    try:
        safe_value = redact(value)
    except TypeError:
        raise
    except Exception:
        raise ValueError("Persisted payload was not valid JSON") from None
    try:
        serialized = json.dumps(
            safe_value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except Exception:
        raise ValueError("Persisted payload was not valid JSON") from None
    if len(serialized.encode("utf-8")) > MAX_PERSISTED_JSON_BYTES:
        raise ValueError("Persisted JSON payload exceeded its size limit")
    return safe_value, serialized


def without_provenance(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: without_provenance(inner)
            for key, inner in value.items()
            if key != "provenance"
        }
    if isinstance(value, (list, tuple)):
        return [without_provenance(inner) for inner in value]
    return value


def canonical_provider_binding(
    value: Mapping[str, Any],
) -> tuple[dict[str, Any], str, str]:
    if set(value) != set(PROVIDER_BINDING_FIELDS):
        raise ValueError("Provider availability binding fields did not match")
    normalized: dict[str, Any] = {}
    for field in PROVIDER_BINDING_FIELDS:
        field_value = value[field]
        if field in PROVIDER_BINDING_BOOLEAN_FIELDS:
            if not isinstance(field_value, bool):
                raise ValueError(f"Provider binding {field} must be a boolean")
        elif not isinstance(field_value, str):
            raise ValueError(f"Provider binding {field} must be a string")
        normalized[field] = field_value
    safe_value, serialized = persisted_json(normalized)
    if len(serialized.encode("utf-8")) > MAX_PROVIDER_BINDING_BYTES:
        raise ValueError("Provider availability binding exceeded its size limit")
    binding = dict(safe_value)
    return binding, serialized, sha256_text(serialized)


def finite_value(enum_type: type[Any], value: str, label: str) -> str:
    try:
        return enum_type(value).value
    except ValueError as exc:
        raise ValueError(f"Invalid {label}: {value}") from exc


class CareerOpsRepository:
    def __init__(
        self,
        database_path: Path,
        vault_path: Path,
        *,
        claim_lease_seconds: float = 30.0,
    ):
        if claim_lease_seconds <= 0:
            raise ValueError("AI task claim lease must be positive")
        self.database_path = Path(database_path)
        self.vault_path = Path(vault_path)
        self.claim_lease_seconds = float(claim_lease_seconds)

    @property
    def claim_heartbeat_interval(self) -> float:
        return max(0.02, min(10.0, self.claim_lease_seconds / 3))

    def initialize(self) -> None:
        initialize_database(self.database_path)
        for directory in (
            "applications",
            "resumes",
            "evidence",
            "reports",
            "exports",
            "ai-tasks",
            "ai-artifacts",
            "application-versions",
        ):
            (self.vault_path / directory).mkdir(parents=True, exist_ok=True)
        self.requeue_interrupted_ai_tasks()

    def upsert_ai_provider(
        self,
        *,
        provider_kind: str,
        display_name: str,
        privacy_mode: str,
        endpoint: str,
        identity_name: str = "",
        model: str,
        enabled: bool,
        availability_binding: Mapping[str, Any],
    ) -> AiProviderConfig:
        privacy_mode = finite_value(PrivacyMode, privacy_mode, "privacy mode")
        if provider_kind == "codex":
            if identity_name not in CODEX_IDENTITIES:
                raise ValueError("Codex identity name must be codex2 or codex")
            expected_endpoint = f"codex-identity://{identity_name}"
            if endpoint != expected_endpoint:
                raise ValueError("Codex endpoint does not match its identity name")
        elif identity_name:
            raise ValueError("Identity names are only supported for Codex providers")
        binding, binding_json, binding_sha256 = canonical_provider_binding(
            availability_binding
        )
        if (
            binding["provider_kind"] != provider_kind
            or binding["model"] != model
            or binding["privacy_mode"] != privacy_mode
            or binding["endpoint"] != endpoint
            or binding["identity_name"] != identity_name
        ):
            raise ValueError("Provider availability binding did not match configuration")
        provider_id = new_id("aip")
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO ai_provider_configs (
                    id, provider_kind, display_name, identity_name, enabled,
                    privacy_mode, endpoint, model, availability_binding_json,
                    availability_binding_sha256, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_kind, endpoint, model) DO UPDATE SET
                    display_name = excluded.display_name,
                    identity_name = excluded.identity_name,
                    enabled = excluded.enabled,
                    privacy_mode = excluded.privacy_mode,
                    availability_binding_json = excluded.availability_binding_json,
                    availability_binding_sha256 = excluded.availability_binding_sha256,
                    updated_at = excluded.updated_at
                """,
                (
                    provider_id,
                    provider_kind,
                    display_name,
                    identity_name,
                    int(enabled),
                    privacy_mode,
                    endpoint,
                    model,
                    binding_json,
                    binding_sha256,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM ai_provider_configs
                WHERE provider_kind = ? AND endpoint = ? AND model = ?
                """,
                (provider_kind, endpoint, model),
            ).fetchone()
        if row is None:
            raise RuntimeError("AI provider upsert did not return a row")
        return self._ai_provider_from_row(row)

    def upsert_ai_provider_config(self, **values: Any) -> AiProviderConfig:
        return self.upsert_ai_provider(**values)

    def get_ai_provider(self, provider_config_id: str) -> AiProviderConfig:
        row = self._one("SELECT * FROM ai_provider_configs WHERE id = ?", (provider_config_id,))
        return self._ai_provider_from_row(row)

    def get_ai_provider_config(self, provider_config_id: str) -> AiProviderConfig:
        return self.get_ai_provider(provider_config_id)

    def list_ai_providers(self) -> list[AiProviderConfig]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM ai_provider_configs ORDER BY created_at ASC, id ASC"
            ).fetchall()
        return [self._ai_provider_from_row(row) for row in rows]

    def list_ai_provider_configs(self) -> list[AiProviderConfig]:
        return self.list_ai_providers()

    def create_ai_task(
        self,
        *,
        application_id: str,
        task_type: str,
        provider_config_id: str,
        prompt_template_id: str,
        prompt_sha256: str,
        input_artifact_ids: list[str],
        privacy_mode: str,
        expected_provider_binding: Mapping[str, Any],
        expected_provider_binding_sha256: str,
    ) -> AiTask:
        task_type = finite_value(AiTaskType, task_type, "task type")
        privacy_mode = finite_value(PrivacyMode, privacy_mode, "privacy mode")
        if (
            not SHA256_PATTERN.fullmatch(prompt_sha256)
            or prompt_sha256.lower() != prompt_sha256
        ):
            raise ValueError("AI prompt SHA-256 was invalid")
        expected_binding, expected_binding_json, expected_binding_sha = (
            canonical_provider_binding(expected_provider_binding)
        )
        if expected_binding_sha != expected_provider_binding_sha256:
            raise ValueError("Expected AI provider binding checksum was invalid")
        seen_artifact_ids: set[str] = set()
        for artifact_id in input_artifact_ids:
            if not isinstance(artifact_id, str):
                raise ValueError("Input artifact IDs must be strings")
            if artifact_id in seen_artifact_ids:
                raise ValueError(f"duplicate input artifact ID: {artifact_id}")
            seen_artifact_ids.add(artifact_id)
        task_id = new_id("ait")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            provider = connection.execute(
                "SELECT * FROM ai_provider_configs WHERE id = ?",
                (provider_config_id,),
            ).fetchone()
            if provider is None:
                raise KeyError("No AI provider configuration with that ID")
            if not provider["enabled"]:
                raise ValueError("AI provider configuration is disabled")
            if provider["privacy_mode"] != privacy_mode:
                raise ValueError("AI task privacy mode does not match its provider")
            if (
                not provider["availability_binding_sha256"]
                or provider["availability_binding_json"] == "{}"
            ):
                raise ValueError("AI provider configuration is not durably bound")
            binding, binding_json, binding_sha256 = canonical_provider_binding(
                json.loads(provider["availability_binding_json"])
            )
            if (
                binding_json != provider["availability_binding_json"]
                or binding_sha256 != provider["availability_binding_sha256"]
            ):
                raise ValueError("AI provider configuration binding is invalid")
            if (
                binding != expected_binding
                or binding_json != expected_binding_json
                or binding_sha256 != expected_binding_sha
            ):
                raise ValueError("AI provider configuration changed during enqueue")
            if input_artifact_ids:
                placeholders = ", ".join("?" for _ in input_artifact_ids)
                rows = connection.execute(
                    f"""
                    SELECT artifact.id, owner.application_id
                    FROM ai_artifacts AS artifact
                    JOIN ai_tasks AS owner ON owner.id = artifact.task_id
                    WHERE artifact.id IN ({placeholders})
                    """,
                    tuple(input_artifact_ids),
                ).fetchall()
                found_ids = {row["id"] for row in rows}
                missing_ids = [
                    artifact_id
                    for artifact_id in input_artifact_ids
                    if artifact_id not in found_ids
                ]
                if missing_ids:
                    raise ValueError(
                        f"Unknown input artifact IDs: {', '.join(missing_ids)}"
                    )
                foreign_ids = [
                    row["id"]
                    for row in rows
                    if row["application_id"] != application_id
                ]
                if foreign_ids:
                    raise ValueError(
                        "Input artifacts must belong to the same application"
                    )
            connection.execute(
                """
                INSERT INTO ai_tasks (
                    id, application_id, task_type, provider_config_id,
                    prompt_template_id, prompt_sha256, input_artifact_ids_json,
                    status, privacy_mode,
                    provider_binding_json, provider_binding_sha256, started_at,
                    completed_at, error_summary, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    application_id,
                    task_type,
                    provider_config_id,
                    prompt_template_id,
                    prompt_sha256,
                    json.dumps(input_artifact_ids),
                    "pending",
                    privacy_mode,
                    binding_json,
                    binding_sha256,
                    None,
                    None,
                    "",
                    now,
                    now,
                ),
            )
            for ordinal, artifact_id in enumerate(input_artifact_ids):
                connection.execute(
                    """
                    INSERT INTO ai_task_input_artifacts (
                        task_id, artifact_id, ordinal, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (task_id, artifact_id, ordinal, now),
                )
        return self.get_ai_task(task_id)

    def get_ai_task(self, task_id: str) -> AiTask:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM ai_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError("No AI task with that ID")
            input_artifact_ids = self._ai_task_input_artifact_ids(
                connection, task_id
            )
        return self._ai_task_from_row(row, input_artifact_ids)

    def list_ai_tasks(
        self,
        application_id: str | None = None,
        *,
        status: str | None = None,
    ) -> list[AiTask]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if application_id is not None:
            clauses.append("application_id = ?")
            parameters.append(application_id)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM ai_tasks{where} ORDER BY created_at ASC, id ASC",
                tuple(parameters),
            ).fetchall()
            input_artifact_ids = {
                row["id"]: self._ai_task_input_artifact_ids(connection, row["id"])
                for row in rows
            }
        return [
            self._ai_task_from_row(row, input_artifact_ids[row["id"]])
            for row in rows
        ]

    def claim_ai_task(self, task_id: str) -> str | None:
        claim_token = secrets.token_urlsafe(32)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            expires_at = (
                datetime.fromisoformat(now)
                + timedelta(seconds=self.claim_lease_seconds)
            ).isoformat(timespec="microseconds")
            cursor = connection.execute(
                """
                UPDATE ai_tasks
                SET status = 'running', started_at = ?, completed_at = NULL,
                    error_summary = '', claim_token = ?, claim_expires_at = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, claim_token, expires_at, now, task_id),
            )
        return claim_token if cursor.rowcount == 1 else None

    def start_ai_task(self, task_id: str) -> str:
        claim_token = self.claim_ai_task(task_id)
        if claim_token is None:
            raise ValueError("AI task is not pending")
        return claim_token

    def begin_ai_task_invocation(self, task_id: str, claim_token: str) -> bool:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            cursor = connection.execute(
                """
                UPDATE ai_tasks
                SET invocation_started_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                  AND invocation_started_at IS NULL
                  AND claim_expires_at > ?
                  AND length(prompt_sha256) = 64
                  AND lower(prompt_sha256) = prompt_sha256
                  AND prompt_sha256 NOT GLOB '*[^0-9a-f]*'
                  AND provider_binding_sha256 != ''
                  AND EXISTS (
                    SELECT 1
                    FROM ai_provider_configs AS config
                    WHERE config.id = ai_tasks.provider_config_id
                      AND config.enabled = 1
                      AND config.privacy_mode = ai_tasks.privacy_mode
                      AND config.availability_binding_json
                          = ai_tasks.provider_binding_json
                      AND config.availability_binding_sha256
                          = ai_tasks.provider_binding_sha256
                  )
                """,
                (now, now, task_id, claim_token, now),
            )
            if cursor.rowcount == 1:
                self._write_audit(
                    connection,
                    "ai_task.invocation_started",
                    "ai_task",
                    task_id,
                    {"invocation_started_at": None},
                    {"invocation_started_at": now},
                )
                return True
        return False

    def renew_ai_task_claim(self, task_id: str, claim_token: str) -> bool:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            expires_at = (
                datetime.fromisoformat(now)
                + timedelta(seconds=self.claim_lease_seconds)
            ).isoformat(timespec="microseconds")
            cursor = connection.execute(
                """
                UPDATE ai_tasks
                SET claim_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                  AND claim_expires_at > ?
                """,
                (expires_at, now, task_id, claim_token, now),
            )
        return cursor.rowcount == 1

    def complete_ai_task(self, task_id: str, claim_token: str) -> AiTask:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            before = connection.execute(
                "SELECT * FROM ai_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if before is None:
                raise KeyError("No AI task with that ID")
            cursor = connection.execute(
                """
                UPDATE ai_tasks
                SET status = 'completed', completed_at = ?, error_summary = '',
                    claim_token = NULL, claim_expires_at = NULL, updated_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                  AND invocation_started_at IS NOT NULL
                  AND claim_expires_at > ?
                """,
                (now, now, task_id, claim_token, now),
            )
            if cursor.rowcount != 1:
                raise ValueError("AI task claim is not active")
            self._write_audit(
                connection,
                "ai_task.completed",
                "ai_task",
                task_id,
                dict(before),
                {"status": "completed", "completed_at": now},
            )
        return self.get_ai_task(task_id)

    def fail_ai_task(self, task_id: str, claim_token: str, error_summary: str) -> AiTask:
        safe_error = provider_error_category(error_summary)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            before = connection.execute(
                "SELECT * FROM ai_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if before is None:
                raise KeyError("No AI task with that ID")
            cursor = connection.execute(
                """
                UPDATE ai_tasks
                SET status = 'failed', completed_at = ?, error_summary = ?,
                    claim_token = NULL, claim_expires_at = NULL, updated_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                  AND claim_expires_at > ?
                """,
                (now, safe_error, now, task_id, claim_token, now),
            )
            if cursor.rowcount != 1:
                raise ValueError("AI task claim is not active")
            self._write_audit(
                connection,
                "ai_task.failed",
                "ai_task",
                task_id,
                dict(before),
                {"status": "failed", "completed_at": now, "error_summary": safe_error},
            )
        return self.get_ai_task(task_id)

    def block_ai_task(
        self, task_id: str, claim_token: str, error_summary: str
    ) -> AiTask:
        safe_error = provider_error_category(error_summary)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            before = connection.execute(
                "SELECT * FROM ai_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if before is None:
                raise KeyError("No AI task with that ID")
            cursor = connection.execute(
                """
                UPDATE ai_tasks
                SET status = 'blocked', completed_at = ?, error_summary = ?,
                    claim_token = NULL, claim_expires_at = NULL, updated_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                  AND invocation_started_at IS NOT NULL
                  AND claim_expires_at > ?
                """,
                (now, safe_error, now, task_id, claim_token, now),
            )
            if cursor.rowcount != 1:
                raise ValueError("AI task claim is not active")
            self._write_audit(
                connection,
                "ai_task.blocked",
                "ai_task",
                task_id,
                dict(before),
                {
                    "status": "blocked",
                    "completed_at": now,
                    "error_summary": safe_error,
                },
            )
        return self.get_ai_task(task_id)

    def finalize_ai_task_attempt(
        self,
        *,
        task_id: str,
        claim_token: str,
        terminal_status: str,
        error_summary: str,
        expected_jd_snapshot_id: str,
        expected_resume_version_id: str | None,
        findings: Sequence[Mapping[str, Any]],
        success_payload: Mapping[str, Any] | None = None,
    ) -> AiTask:
        if terminal_status not in {
            AiTaskStatus.COMPLETED.value,
            AiTaskStatus.BLOCKED.value,
        }:
            raise ValueError("AI task final status must be completed or blocked")
        safe_error = (
            ""
            if terminal_status == AiTaskStatus.COMPLETED.value
            else provider_error_category(error_summary)
        )
        safe_success, _ = persisted_json(
            without_provenance(dict(success_payload or {}))
        )

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            task = connection.execute(
                "SELECT * FROM ai_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if (
                task is None
                or task["status"] != AiTaskStatus.RUNNING.value
                or task["claim_token"] != claim_token
                or task["invocation_started_at"] is None
                or task["claim_expires_at"] is None
                or task["claim_expires_at"] <= now
            ):
                raise ValueError("AI task claim is not active")
            provider = connection.execute(
                "SELECT * FROM ai_provider_configs WHERE id = ?",
                (task["provider_config_id"],),
            ).fetchone()
            if provider is None or not provider["enabled"]:
                raise ValueError("AI task provider authority is unavailable")
            if (
                not SHA256_PATTERN.fullmatch(task["prompt_sha256"])
                or task["prompt_sha256"].lower() != task["prompt_sha256"]
            ):
                raise ValueError("AI task prompt authority is invalid")
            task_binding, task_binding_json, task_binding_sha256 = (
                canonical_provider_binding(json.loads(task["provider_binding_json"]))
            )
            provider_binding, provider_binding_json, provider_binding_sha256 = (
                canonical_provider_binding(
                    json.loads(provider["availability_binding_json"])
                )
            )
            if (
                task_binding_json != task["provider_binding_json"]
                or task_binding_sha256 != task["provider_binding_sha256"]
                or provider_binding_json != provider["availability_binding_json"]
                or provider_binding_sha256
                != provider["availability_binding_sha256"]
                or task_binding != provider_binding
                or task_binding_sha256 != provider_binding_sha256
                or task["privacy_mode"] != provider["privacy_mode"]
            ):
                raise ValueError("AI task provider authority changed")
            artifact_rows = connection.execute(
                """
                SELECT * FROM ai_artifacts
                WHERE task_id = ? AND claim_token = ?
                ORDER BY created_at ASC, id ASC
                """,
                (task_id, claim_token),
            ).fetchall()
            for artifact in artifact_rows:
                self._validate_artifact_row(artifact)
            safe_artifacts = [
                {
                    "artifact_type": artifact["artifact_type"],
                    "id": artifact["id"],
                    "sha256": artifact["sha256"],
                }
                for artifact in artifact_rows
            ]
            checksums = {
                artifact["artifact_type"]: artifact["sha256"]
                for artifact in artifact_rows
            }
            if len(checksums) != len(artifact_rows):
                raise ValueError("AI task artifact types must be unique per attempt")
            required_artifacts = {"context", "raw_output", "validated_output"}
            if (
                terminal_status == AiTaskStatus.COMPLETED.value
                and task["task_type"] == AiTaskType.DRAFT_COVER_LETTER.value
            ):
                required_artifacts.add("draft_text")
            if (
                terminal_status == AiTaskStatus.BLOCKED.value
                and safe_error == "invalid_output"
            ):
                allowed_artifact_sets = (
                    {"context"},
                    {"context", "raw_output"},
                )
            else:
                allowed_artifact_sets = (required_artifacts,)
            if set(checksums) not in allowed_artifact_sets:
                raise ValueError("AI task required artifacts were incomplete")
            expected_outputs = {
                key: checksums[key]
                for key in ("raw_output", "validated_output")
                if key in checksums
            }
            safe_provenance, _ = persisted_json(
                {
                    "ai_task_id": task["id"],
                    "context_sha256": checksums["context"],
                    "output_checksums": expected_outputs,
                    "privacy_mode": task["privacy_mode"],
                    "prompt_sha256": task["prompt_sha256"],
                    "prompt_template_id": task["prompt_template_id"],
                    "provider_binding": task_binding,
                    "provider_binding_sha256": task_binding_sha256,
                    "provider_config_id": provider["id"],
                    "provider_identity_name": provider["identity_name"],
                    "provider_kind": provider["provider_kind"],
                    "provider_model": provider["model"],
                }
            )
            application = connection.execute(
                "SELECT * FROM applications WHERE id = ?",
                (task["application_id"],),
            ).fetchone()
            if application is None:
                raise KeyError("No application with that ID")
            if application["jd_snapshot_id"] != expected_jd_snapshot_id:
                raise ValueError("Selected job description changed during AI task")
            if (
                expected_resume_version_id is not None
                and application["selected_resume_version_id"]
                != expected_resume_version_id
            ):
                raise ValueError("Selected resume changed during AI task")

            finding_ids: list[str] = []
            finding_types: dict[str, list[str]] = {}
            for finding in findings:
                severity = finite_value(
                    FindingSeverity, str(finding["severity"]), "severity"
                )
                verifier_state = finite_value(
                    VerifierState,
                    str(finding["verifier_state"]),
                    "verifier state",
                )
                decision = finite_value(
                    FindingDecision,
                    str(finding.get("decision", FindingDecision.PENDING.value)),
                    "decision",
                )
                if decision != FindingDecision.PENDING.value:
                    raise ValueError("AI findings must start with a pending decision")
                safe_finding, _ = persisted_json(
                    {
                        "claim_text": str(finding["claim_text"]),
                        "finding_type": str(finding["finding_type"]),
                        "payload": dict(finding.get("payload", {})),
                        "recommendation": str(finding["recommendation"]),
                    }
                )
                _safe_payload, payload_json = persisted_json(
                    safe_finding["payload"]
                )
                finding_id = new_id("aif")
                finding_type = safe_finding["finding_type"]
                connection.execute(
                    """
                    INSERT INTO ai_findings (
                        id, task_id, claim_token, finding_type, severity, claim_text,
                        evidence_item_id, recommendation, verifier_state, decision,
                        payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        finding_id,
                        task_id,
                        claim_token,
                        finding_type,
                        severity,
                        safe_finding["claim_text"],
                        finding.get("evidence_item_id"),
                        safe_finding["recommendation"],
                        verifier_state,
                        decision,
                        payload_json,
                        now,
                        now,
                    ),
                )
                finding_ids.append(finding_id)
                finding_types.setdefault(finding_type, []).append(finding_id)

            side_effect: dict[str, Any] = {}
            if terminal_status == AiTaskStatus.COMPLETED.value:
                if task["task_type"] == AiTaskType.ANALYZE_JD.value:
                    self._require_pristine_application_packet(application)
                    snapshot = connection.execute(
                        "SELECT * FROM jd_snapshots WHERE id = ?",
                        (expected_jd_snapshot_id,),
                    ).fetchone()
                    if (
                        snapshot is None
                        or snapshot["application_id"] != application["id"]
                    ):
                        raise ValueError("AI task cannot update this JD snapshot")
                    existing = json.loads(snapshot["extracted_requirements_json"])
                    grouped = any(
                        key in existing
                        for key in (
                            "raw_ai",
                            "deterministic",
                            "user_corrected",
                            "uncertainty",
                        )
                    )
                    raw_ai = dict(safe_success["raw_ai"])
                    uncertainty = dict(safe_success["uncertainty"])
                    raw_ai["provenance"] = safe_provenance
                    uncertainty["provenance"] = safe_provenance
                    updated = {
                        "raw_ai": raw_ai,
                        "deterministic": (
                            existing.get("deterministic", {}) if grouped else existing
                        ),
                        "user_corrected": existing.get("user_corrected", {}),
                        "uncertainty": uncertainty,
                    }
                    _safe_updated, serialized = persisted_json(updated)
                    connection.execute(
                        """
                        UPDATE jd_snapshots
                        SET extracted_requirements_json = ?
                        WHERE id = ?
                        """,
                        (serialized, expected_jd_snapshot_id),
                    )
                    invalidated_report_id = application["latest_report_id"]
                    if invalidated_report_id is not None:
                        invalidated_status = (
                            ApplicationStatus.NEEDS_RESUME_UPDATE.value
                            if application["selected_resume_version_id"] is not None
                            else ApplicationStatus.DRAFT.value
                        )
                        connection.execute(
                            """
                            UPDATE applications
                            SET latest_report_id = NULL, status = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (invalidated_status, now, application["id"]),
                        )
                        self._write_audit(
                            connection,
                            "application.report_invalidated_by_jd_analysis",
                            "application",
                            application["id"],
                            {"latest_report_id": invalidated_report_id},
                            {
                                "ai_task_id": task_id,
                                "latest_report_id": None,
                                "status": invalidated_status,
                            },
                        )
                    self._write_audit(
                        connection,
                        "jd_snapshot.ai_requirements_updated",
                        "jd_snapshot",
                        expected_jd_snapshot_id,
                        {
                            "requirements_sha256": sha256_text(
                                snapshot["extracted_requirements_json"]
                            )
                        },
                        {
                            "ai_task_id": task_id,
                            "requirements_sha256": sha256_text(serialized),
                            "updated_at": now,
                        },
                    )
                    side_effect = {
                        "invalidated_report_id": invalidated_report_id,
                        "jd_snapshot_id": expected_jd_snapshot_id,
                    }
                elif task["task_type"] == AiTaskType.ANALYZE_FIT.value:
                    if expected_resume_version_id is None:
                        raise ValueError("A selected resume is required for this AI task")
                    self._require_pristine_application_packet(application)
                    fit_values = dict(safe_success["fit"])
                    risk_values = dict(safe_success["risks"])
                    fit_values["provenance"] = safe_provenance
                    risk_values["provenance"] = safe_provenance
                    fit, fit_json = persisted_json(fit_values)
                    risks, risks_json = persisted_json(risk_values)
                    blockers = risks.get("blockers", [])
                    warnings = risks.get("warnings", [])
                    if not isinstance(blockers, list) or not isinstance(warnings, list):
                        raise ValueError("AI report risk groups must be lists")
                    report_id = new_id("rep")
                    application_status = (
                        ApplicationStatus.READY_FOR_REVIEW
                        if not blockers
                        else ApplicationStatus.NEEDS_EVIDENCE
                    )
                    connection.execute(
                        """
                        INSERT INTO reports (
                            id, application_id, resume_version_id, jd_snapshot_id,
                            fit_json, risks_json, blocking_issue_count, warning_count,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            report_id,
                            application["id"],
                            expected_resume_version_id,
                            expected_jd_snapshot_id,
                            fit_json,
                            risks_json,
                            len(blockers),
                            len(warnings),
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE applications
                        SET latest_report_id = ?, status = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            report_id,
                            application_status.value,
                            now,
                            application["id"],
                        ),
                    )
                    self._write_audit(
                        connection,
                        "report.created",
                        "report",
                        report_id,
                        None,
                        {
                            "application_id": application["id"],
                            "blocking_issue_count": len(blockers),
                            "warning_count": len(warnings),
                        },
                    )
                    side_effect = {"report_id": report_id}
                elif task["task_type"] == AiTaskType.SUGGEST_RESUME_EDITS.value:
                    side_effect = {
                        "suggestion_finding_ids": finding_types.get(
                            "resume_edit_suggestion", []
                        )
                    }
                elif task["task_type"] == AiTaskType.REVIEW_CLAIMS.value:
                    side_effect = {
                        "decision_finding_ids": finding_types.get(
                            "claim_review_decision", []
                        )
                    }
                elif task["task_type"] == AiTaskType.DRAFT_COVER_LETTER.value:
                    draft_artifacts = [
                        artifact
                        for artifact in artifact_rows
                        if artifact["artifact_type"] == "draft_text"
                    ]
                    if len(draft_artifacts) != 1:
                        raise ValueError("Cover letter task draft artifact was invalid")
                    side_effect = {
                        "draft_text_artifact_id": draft_artifacts[0]["id"],
                        "draft_text_sha256": draft_artifacts[0]["sha256"],
                    }

            snapshot = {
                "ai_task": {
                    "id": task_id,
                    "input_artifact_ids": self._ai_task_input_artifact_ids(
                        connection, task_id
                    ),
                    "provider_config_id": task["provider_config_id"],
                    "status": terminal_status,
                    "task_type": task["task_type"],
                },
                "artifacts": safe_artifacts,
                "finding_ids": finding_ids,
                "provenance": safe_provenance,
                "side_effect": side_effect,
            }
            safe_snapshot, snapshot_json = persisted_json(snapshot)
            version_id = new_id("appv")
            version_number = connection.execute(
                """
                SELECT COALESCE(MAX(version_number), 0) + 1
                FROM application_versions
                WHERE application_id = ?
                """,
                (application["id"],),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO application_versions (
                    id, application_id, version_number, event_type,
                    snapshot_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    application["id"],
                    version_number,
                    f"ai_task.{terminal_status}",
                    snapshot_json,
                    now,
                ),
            )
            self._write_audit(
                connection,
                "application.version_recorded",
                "application",
                application["id"],
                None,
                {
                    "application_version_id": version_id,
                    "version_number": version_number,
                    "event_type": f"ai_task.{terminal_status}",
                    "snapshot": safe_snapshot,
                },
            )
            cursor = connection.execute(
                """
                UPDATE ai_tasks
                SET status = ?, completed_at = ?, error_summary = ?,
                    claim_token = NULL, claim_expires_at = NULL, updated_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                  AND invocation_started_at IS NOT NULL
                  AND claim_expires_at > ?
                """,
                (
                    terminal_status,
                    now,
                    safe_error,
                    now,
                    task_id,
                    claim_token,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("AI task claim is not active")
            self._write_audit(
                connection,
                f"ai_task.{terminal_status}",
                "ai_task",
                task_id,
                {
                    "status": task["status"],
                    "started_at": task["started_at"],
                },
                {
                    "status": terminal_status,
                    "completed_at": now,
                    "error_summary": safe_error,
                },
            )
        return self.get_ai_task(task_id)

    def requeue_interrupted_ai_tasks(self) -> int:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            cursor = connection.execute(
                """
                UPDATE ai_tasks
                SET status = 'pending', started_at = NULL, completed_at = NULL,
                    error_summary = '', claim_token = NULL,
                    claim_expires_at = NULL, updated_at = ?
                WHERE status = 'running'
                  AND (claim_expires_at IS NULL OR claim_expires_at <= ?)
                """,
                (now, now),
            )
        return cursor.rowcount

    def save_ai_artifact(
        self,
        task_id: str,
        claim_token: str,
        artifact_type: str,
        path: Path,
        sha256: str,
        schema_version: str,
    ) -> AiArtifact:
        self._validate_ai_artifact_metadata(artifact_type, schema_version)
        if not SHA256_PATTERN.fullmatch(sha256):
            raise ValueError(
                "Artifact SHA-256 must contain exactly 64 hexadecimal characters"
            )
        normalized_sha256 = sha256.lower()
        artifact_path, actual_sha256 = self._stable_artifact_digest(path)
        self._require_ai_task_artifact_path(task_id, artifact_path)
        if actual_sha256 != normalized_sha256:
            raise ValueError("Artifact checksum does not match file bytes")

        artifact_id = new_id("aia")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            self._require_active_ai_claim(connection, task_id, claim_token)
            try:
                connection.execute(
                    """
                    INSERT INTO ai_artifacts (
                        id, task_id, claim_token, artifact_type, path, sha256,
                        schema_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        task_id,
                        claim_token,
                        artifact_type,
                        str(artifact_path),
                        normalized_sha256,
                        schema_version,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "ai_artifacts.path" in str(exc):
                    raise ValueError(
                        "AI artifact physical path is already owned"
                    ) from exc
                raise
        return self.get_ai_artifact(artifact_id)

    def get_ai_artifact(self, artifact_id: str) -> AiArtifact:
        row = self._one("SELECT * FROM ai_artifacts WHERE id = ?", (artifact_id,))
        self._validate_artifact_row(row)
        return self._ai_artifact_from_row(row)

    def list_ai_artifacts(self, task_id: str) -> list[AiArtifact]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM ai_artifacts
                WHERE task_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (task_id,),
            ).fetchall()
        for row in rows:
            self._validate_artifact_row(row)
        return [self._ai_artifact_from_row(row) for row in rows]

    def save_ai_finding(
        self,
        *,
        task_id: str,
        claim_token: str,
        finding_type: str,
        severity: str,
        claim_text: str,
        evidence_item_id: str | None,
        recommendation: str,
        verifier_state: str,
        decision: str = "pending",
        payload: dict[str, Any] | None = None,
    ) -> AiFinding:
        severity = finite_value(FindingSeverity, severity, "severity")
        verifier_state = finite_value(VerifierState, verifier_state, "verifier state")
        decision = finite_value(FindingDecision, decision, "decision")
        if decision != FindingDecision.PENDING.value:
            raise ValueError("AI findings must start with a pending decision")
        finding_id = new_id("aif")
        safe_finding, _ = persisted_json(
            {
                "claim_text": claim_text,
                "finding_type": finding_type,
                "recommendation": recommendation,
                "payload": payload or {},
            }
        )
        _safe_payload, payload_json = persisted_json(safe_finding["payload"])
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            self._require_active_ai_claim(connection, task_id, claim_token)
            connection.execute(
                """
                INSERT INTO ai_findings (
                    id, task_id, claim_token, finding_type, severity, claim_text,
                    evidence_item_id, recommendation, verifier_state, decision,
                    payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    finding_id,
                    task_id,
                    claim_token,
                    safe_finding["finding_type"],
                    severity,
                    safe_finding["claim_text"],
                    evidence_item_id,
                    safe_finding["recommendation"],
                    verifier_state,
                    decision,
                    payload_json,
                    now,
                    now,
                ),
            )
        return self.get_ai_finding(finding_id)

    def get_ai_finding(self, finding_id: str) -> AiFinding:
        row = self._one("SELECT * FROM ai_findings WHERE id = ?", (finding_id,))
        return self._ai_finding_from_row(row)

    def list_ai_findings(self, task_id: str) -> list[AiFinding]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM ai_findings
                WHERE task_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (task_id,),
            ).fetchall()
        return [self._ai_finding_from_row(row) for row in rows]

    def update_ai_finding_decision(
        self,
        finding_id: str,
        decision: str,
        *,
        expected_decision: str,
    ) -> AiFinding:
        decision = finite_value(FindingDecision, decision, "decision")
        expected_decision = finite_value(
            FindingDecision, expected_decision, "expected decision"
        )
        if decision == FindingDecision.PENDING.value:
            raise ValueError("Invalid decision transition to pending")
        if expected_decision != FindingDecision.PENDING.value:
            raise ValueError("Invalid decision transition from a terminal decision")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            before_row = connection.execute(
                "SELECT * FROM ai_findings WHERE id = ?", (finding_id,)
            ).fetchone()
            if before_row is None:
                raise KeyError(f"No AI finding: {finding_id}")
            before = self._ai_finding_from_row(before_row)
            if before.decision != expected_decision:
                raise ValueError("AI finding decision no longer matches expected decision")
            if (
                decision == FindingDecision.ACCEPTED.value
                and before.verifier_state != VerifierState.VERIFIED
            ):
                raise ValueError("AI finding verifier state does not permit acceptance")

            now = utc_now()
            cursor = connection.execute(
                """
                UPDATE ai_findings
                SET decision = ?, updated_at = ?
                WHERE id = ? AND decision = ?
                """,
                (decision, now, finding_id, expected_decision),
            )
            if cursor.rowcount != 1:
                raise ValueError("AI finding decision no longer matches expected decision")
            after_row = connection.execute(
                "SELECT * FROM ai_findings WHERE id = ?", (finding_id,)
            ).fetchone()
            if after_row is None:
                raise RuntimeError("AI finding disappeared during decision update")
            after = self._ai_finding_from_row(after_row)
            self._write_audit(
                connection,
                "ai_finding.decision_updated",
                "ai_finding",
                finding_id,
                before.__dict__,
                after.__dict__,
            )
        return after

    def create_profile_fact(
        self,
        *,
        fact_key: str,
        fact_value: str,
        verification_state: str,
        privacy_level: str,
        notes: str = "",
    ) -> ProfileFact:
        verification_state = finite_value(
            VerificationState, verification_state, "verification state"
        )
        privacy_level = finite_value(PrivacyLevel, privacy_level, "privacy level")
        fact_id = new_id("fact")
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO profile_facts (
                    id, fact_key, fact_value, verification_state, privacy_level,
                    notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fact_id,
                    fact_key,
                    fact_value,
                    verification_state,
                    privacy_level,
                    notes,
                    now,
                    now,
                ),
            )
            self._write_audit(
                connection,
                "profile_fact.created",
                "profile_fact",
                fact_id,
                None,
                {
                    "fact_key": fact_key,
                    "fact_value": fact_value,
                    "verification_state": verification_state,
                    "privacy_level": privacy_level,
                    "notes": notes,
                },
            )
        return self.get_profile_fact(fact_id)

    def get_profile_fact(self, fact_id: str) -> ProfileFact:
        row = self._one("SELECT * FROM profile_facts WHERE id = ?", (fact_id,))
        return self._profile_fact_from_row(row)

    def list_profile_facts(self) -> list[ProfileFact]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM profile_facts ORDER BY created_at ASC, id ASC"
            ).fetchall()
        return [self._profile_fact_from_row(row) for row in rows]

    def create_evidence_item(
        self,
        *,
        claim_text: str,
        evidence_type: str,
        verification_state: str,
        privacy_level: str,
        source_path: Path | None = None,
        source_url: str | None = None,
        source_sha256: str | None = None,
        notes: str = "",
    ) -> EvidenceItem:
        verification_state = finite_value(
            VerificationState, verification_state, "verification state"
        )
        privacy_level = finite_value(PrivacyLevel, privacy_level, "privacy level")
        evidence_id = new_id("evd")
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO evidence_items (
                    id, claim_text, evidence_type, source_path, source_url,
                    source_sha256, verification_state, privacy_level, notes,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    claim_text,
                    evidence_type,
                    str(source_path) if source_path else None,
                    source_url,
                    source_sha256,
                    verification_state,
                    privacy_level,
                    notes,
                    now,
                    now,
                ),
            )
            self._write_audit(
                connection,
                "evidence_item.created",
                "evidence_item",
                evidence_id,
                None,
                {
                    "claim_text": claim_text,
                    "evidence_type": evidence_type,
                    "source_path": str(source_path) if source_path else None,
                    "source_url": source_url,
                    "source_sha256": source_sha256,
                    "verification_state": verification_state,
                    "privacy_level": privacy_level,
                    "notes": notes,
                },
            )
        return self.get_evidence_item(evidence_id)

    def get_evidence_item(self, evidence_item_id: str) -> EvidenceItem:
        row = self._one("SELECT * FROM evidence_items WHERE id = ?", (evidence_item_id,))
        return self._evidence_item_from_row(row)

    def list_evidence_items(self) -> list[EvidenceItem]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM evidence_items ORDER BY created_at ASC, id ASC"
            ).fetchall()
        return [self._evidence_item_from_row(row) for row in rows]

    def link_application_evidence(
        self, application_id: str, evidence_item_id: str
    ) -> EvidenceItem:
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO application_evidence_links (
                    application_id, evidence_item_id, created_at
                ) VALUES (?, ?, ?)
                """,
                (application_id, evidence_item_id, now),
            )
            if cursor.rowcount == 1:
                self._write_audit(
                    connection,
                    "application.evidence_linked",
                    "application",
                    application_id,
                    None,
                    {"evidence_item_id": evidence_item_id},
                )
        return self.get_evidence_item(evidence_item_id)

    def list_application_evidence(self, application_id: str) -> list[EvidenceItem]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT evidence_items.*
                FROM application_evidence_links
                JOIN evidence_items
                  ON evidence_items.id = application_evidence_links.evidence_item_id
                WHERE application_evidence_links.application_id = ?
                ORDER BY application_evidence_links.created_at ASC, evidence_items.id ASC
                """,
                (application_id,),
            ).fetchall()
        return [self._evidence_item_from_row(row) for row in rows]

    def record_application_version(
        self,
        application_id: str,
        event_type: str,
        snapshot: dict[str, Any],
        *,
        ai_task_id: str | None = None,
        claim_token: str | None = None,
    ) -> ApplicationVersion:
        if (ai_task_id is None) != (claim_token is None):
            raise ValueError("AI task ID and claim token must be provided together")
        safe_snapshot, snapshot_json = persisted_json(snapshot)
        version_id = new_id("appv")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            if ai_task_id is not None and claim_token is not None:
                self._require_active_ai_claim(connection, ai_task_id, claim_token)
                owner = connection.execute(
                    "SELECT application_id FROM ai_tasks WHERE id = ?",
                    (ai_task_id,),
                ).fetchone()
                if owner is None or owner["application_id"] != application_id:
                    raise ValueError("AI task does not belong to the application")
            row = connection.execute(
                """
                SELECT COALESCE(MAX(version_number), 0) + 1
                FROM application_versions
                WHERE application_id = ?
                """,
                (application_id,),
            ).fetchone()
            version_number = row[0]
            connection.execute(
                """
                INSERT INTO application_versions (
                    id, application_id, version_number, event_type, snapshot_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    application_id,
                    version_number,
                    event_type,
                    snapshot_json,
                    now,
                ),
            )
            self._write_audit(
                connection,
                "application.version_recorded",
                "application",
                application_id,
                None,
                {
                    "application_version_id": version_id,
                    "version_number": version_number,
                    "event_type": event_type,
                    "snapshot": safe_snapshot,
                },
            )
        return self.get_application_version(version_id)

    def get_application_version(self, version_id: str) -> ApplicationVersion:
        row = self._one("SELECT * FROM application_versions WHERE id = ?", (version_id,))
        return self._application_version_from_row(row)

    def list_application_versions(self, application_id: str) -> list[ApplicationVersion]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM application_versions
                WHERE application_id = ?
                ORDER BY version_number ASC
                """,
                (application_id,),
            ).fetchall()
        return [self._application_version_from_row(row) for row in rows]

    def update_jd_requirements_from_ai(
        self,
        *,
        snapshot_id: str,
        task_id: str,
        claim_token: str,
        raw_ai: dict[str, Any],
        uncertainty: dict[str, Any],
    ) -> JobDescriptionSnapshot:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            self._require_active_ai_claim(connection, task_id, claim_token)
            task = connection.execute(
                "SELECT application_id, task_type FROM ai_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            snapshot = connection.execute(
                "SELECT * FROM jd_snapshots WHERE id = ?",
                (snapshot_id,),
            ).fetchone()
            if snapshot is None:
                raise KeyError("No JD snapshot with that ID")
            if (
                task is None
                or task["application_id"] != snapshot["application_id"]
                or task["task_type"] != "analyze_jd"
            ):
                raise ValueError("AI task cannot update this JD snapshot")
            application = connection.execute(
                "SELECT * FROM applications WHERE id = ?",
                (snapshot["application_id"],),
            ).fetchone()
            if (
                application is None
                or application["jd_snapshot_id"] != snapshot_id
            ):
                raise ValueError("Selected job description changed")
            self._require_pristine_application_packet(application)

            existing = json.loads(snapshot["extracted_requirements_json"])
            grouped = any(
                key in existing
                for key in (
                    "raw_ai",
                    "deterministic",
                    "user_corrected",
                    "uncertainty",
                )
            )
            updated = {
                "raw_ai": raw_ai,
                "deterministic": (
                    existing.get("deterministic", {}) if grouped else existing
                ),
                "user_corrected": existing.get("user_corrected", {}),
                "uncertainty": uncertainty,
            }
            _safe_updated, serialized = persisted_json(updated)
            connection.execute(
                """
                UPDATE jd_snapshots
                SET extracted_requirements_json = ?
                WHERE id = ?
                """,
                (serialized, snapshot_id),
            )
            invalidated_report_id = application["latest_report_id"]
            if invalidated_report_id is not None:
                invalidated_status = (
                    ApplicationStatus.NEEDS_RESUME_UPDATE.value
                    if application["selected_resume_version_id"] is not None
                    else ApplicationStatus.DRAFT.value
                )
                connection.execute(
                    """
                    UPDATE applications
                    SET latest_report_id = NULL, status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (invalidated_status, now, application["id"]),
                )
                self._write_audit(
                    connection,
                    "application.report_invalidated_by_jd_analysis",
                    "application",
                    application["id"],
                    {"latest_report_id": invalidated_report_id},
                    {
                        "ai_task_id": task_id,
                        "latest_report_id": None,
                        "status": invalidated_status,
                    },
                )
            self._write_audit(
                connection,
                "jd_snapshot.ai_requirements_updated",
                "jd_snapshot",
                snapshot_id,
                {
                    "requirements_sha256": sha256_text(
                        snapshot["extracted_requirements_json"]
                    )
                },
                {
                    "ai_task_id": task_id,
                    "requirements_sha256": sha256_text(serialized),
                    "updated_at": now,
                },
            )
        return self.get_jd_snapshot(snapshot_id)

    def create_application(
        self,
        *,
        company: str,
        role_title: str,
        location: str,
        source_url: str,
        jd_text: str,
        role_type: str = "",
        deadline: str | None = None,
        notes: str = "",
    ) -> Application:
        application_id = new_id("app")
        snapshot_id = new_id("jd")
        now = utc_now()
        snapshot_dir = self.vault_path / "applications" / application_id
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        raw_text_path = snapshot_dir / "jd.txt"
        jd_bytes = jd_text.encode("utf-8")
        raw_text_path.write_bytes(jd_bytes)
        source_kind = "url" if source_url else "manual"
        jd_sha = hashlib.sha256(jd_bytes).hexdigest()

        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO jd_snapshots (
                    id, application_id, source_url, raw_text_path, raw_text_sha256,
                    captured_at, extracted_requirements_json, parser_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    application_id,
                    source_url,
                    str(raw_text_path),
                    jd_sha,
                    now,
                    json.dumps({}),
                    "unparsed",
                ),
            )
            connection.execute(
                """
                INSERT INTO applications (
                    id, company, role_title, location, source_url, source_kind,
                    jd_snapshot_id, status, selected_resume_version_id,
                    latest_report_id, approval_id, submission_state, deadline,
                    notes, created_at, updated_at, role_type, submitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    application_id,
                    company,
                    role_title,
                    location,
                    source_url,
                    source_kind,
                    snapshot_id,
                    ApplicationStatus.DRAFT.value,
                    None,
                    None,
                    None,
                    SubmissionState.NOT_SUBMITTED.value,
                    deadline,
                    notes,
                    now,
                    now,
                    role_type,
                    None,
                ),
            )
            self._write_audit(
                connection,
                "application.created",
                "application",
                application_id,
                None,
                {"company": company, "role_title": role_title, "jd_snapshot_id": snapshot_id},
            )
        return self.get_application(application_id)

    def create_resume_version(
        self,
        *,
        name: str,
        role_family: str,
        tex_path: Path,
        tex_sha256: str,
        pdf_path: Path | None = None,
        pdf_sha256: str | None = None,
        parent_resume_version_id: str | None = None,
        render_status: str = "not_rendered",
        parse_status: str = "not_parsed",
        page_count: int | None = None,
    ) -> ResumeVersion:
        resume_id = new_id("res")
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO resume_versions (
                    id, name, role_family, tex_path, tex_sha256, pdf_path, pdf_sha256,
                    parent_resume_version_id, render_status, parse_status, page_count,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resume_id,
                    name,
                    role_family,
                    str(tex_path),
                    tex_sha256,
                    str(pdf_path) if pdf_path else None,
                    pdf_sha256,
                    parent_resume_version_id,
                    render_status,
                    parse_status,
                    page_count,
                    now,
                    now,
                ),
            )
            self._write_audit(
                connection,
                "resume.created",
                "resume_version",
                resume_id,
                None,
                {"name": name, "role_family": role_family, "tex_sha256": tex_sha256},
            )
        return self.get_resume_version(resume_id)

    def update_resume_render(
        self,
        resume_version_id: str,
        *,
        pdf_path: Path | None,
        pdf_sha256: str | None,
        render_status: str,
        parse_status: str,
        page_count: int | None,
    ) -> ResumeVersion:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            resume = connection.execute(
                "SELECT * FROM resume_versions WHERE id = ?",
                (resume_version_id,),
            ).fetchone()
            if resume is None:
                raise KeyError("No resume version with that ID")
            protected = connection.execute(
                """
                SELECT 1 FROM applications
                WHERE selected_resume_version_id = ?
                  AND (
                    approval_id IS NOT NULL
                    OR submission_state != ?
                    OR submitted_at IS NOT NULL
                  )
                LIMIT 1
                """,
                (resume_version_id, SubmissionState.NOT_SUBMITTED.value),
            ).fetchone()
            if protected is not None:
                raise ValueError(
                    "Cannot change a resume selected by an approved or submitted application"
                )
            connection.execute(
                """
                UPDATE resume_versions
                SET pdf_path = ?, pdf_sha256 = ?, render_status = ?, parse_status = ?,
                    page_count = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    str(pdf_path) if pdf_path else None,
                    pdf_sha256,
                    render_status,
                    parse_status,
                    page_count,
                    now,
                    resume_version_id,
                ),
            )
            self._write_audit(
                connection,
                "resume.render_updated",
                "resume_version",
                resume_version_id,
                dict(resume),
                {
                    "pdf_path": str(pdf_path) if pdf_path else None,
                    "pdf_sha256": pdf_sha256,
                    "render_status": render_status,
                    "parse_status": parse_status,
                    "page_count": page_count,
                },
            )
        return self.get_resume_version(resume_version_id)

    def select_resume(self, application_id: str, resume_version_id: str) -> Application:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            application = connection.execute(
                "SELECT * FROM applications WHERE id = ?", (application_id,)
            ).fetchone()
            resume = connection.execute(
                "SELECT id FROM resume_versions WHERE id = ?", (resume_version_id,)
            ).fetchone()
            if application is None:
                raise KeyError("No application with that ID")
            if resume is None:
                raise KeyError("No resume version with that ID")
            if (
                application["submission_state"]
                != SubmissionState.NOT_SUBMITTED.value
                or application["submitted_at"] is not None
            ):
                raise ValueError(
                    "Cannot change the selected resume after it was submitted"
                )
            if application["selected_resume_version_id"] == resume_version_id:
                return self._application_from_row(application)
            status = (
                ApplicationStatus.NEEDS_RESUME_UPDATE
                if application["approval_id"] or application["latest_report_id"]
                else ApplicationStatus(application["status"])
            )
            connection.execute(
                """
                UPDATE applications
                SET selected_resume_version_id = ?, latest_report_id = NULL,
                    approval_id = NULL, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (resume_version_id, status.value, now, application_id),
            )
            self._write_audit(
                connection,
                "application.resume_selected",
                "application",
                application_id,
                dict(application),
                {"selected_resume_version_id": resume_version_id, "status": status.value},
            )
        return self.get_application(application_id)

    def save_report(
        self,
        *,
        application_id: str,
        resume_version_id: str,
        jd_snapshot_id: str,
        fit: dict[str, Any],
        risks: dict[str, Any],
        ai_task_id: str | None = None,
        claim_token: str | None = None,
    ) -> Report:
        if (ai_task_id is None) != (claim_token is None):
            raise ValueError("AI task ID and claim token must be provided together")
        report_id = new_id("rep")
        blockers = risks.get("blockers", [])
        warnings = risks.get("warnings", [])
        if not isinstance(blockers, list) or not isinstance(warnings, list):
            raise ValueError("Report risk groups must be lists")
        _safe_fit, fit_json = persisted_json(fit)
        _safe_risks, risks_json = persisted_json(risks)
        status = (
            ApplicationStatus.READY_FOR_REVIEW
            if not blockers
            else ApplicationStatus.NEEDS_EVIDENCE
        )
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            application = connection.execute(
                "SELECT * FROM applications WHERE id = ?", (application_id,)
            ).fetchone()
            if application is None:
                raise KeyError("No application with that ID")
            self._require_pristine_application_packet(application)
            if (
                application["selected_resume_version_id"] != resume_version_id
                or application["jd_snapshot_id"] != jd_snapshot_id
            ):
                raise ValueError("Report does not match the selected application packet")
            if ai_task_id is not None and claim_token is not None:
                self._require_active_ai_claim(connection, ai_task_id, claim_token)
                owner = connection.execute(
                    "SELECT application_id, task_type FROM ai_tasks WHERE id = ?",
                    (ai_task_id,),
                ).fetchone()
                if (
                    owner is None
                    or owner["application_id"] != application_id
                    or owner["task_type"] != "analyze_fit"
                    or application["selected_resume_version_id"] != resume_version_id
                    or application["jd_snapshot_id"] != jd_snapshot_id
                ):
                    raise ValueError("AI task cannot create this report")
            connection.execute(
                """
                INSERT INTO reports (
                    id, application_id, resume_version_id, jd_snapshot_id, fit_json,
                    risks_json, blocking_issue_count, warning_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    application_id,
                    resume_version_id,
                    jd_snapshot_id,
                    fit_json,
                    risks_json,
                    len(blockers),
                    len(warnings),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE applications
                SET latest_report_id = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (report_id, status.value, now, application_id),
            )
            self._write_audit(
                connection,
                "report.created",
                "report",
                report_id,
                None,
                {
                    "application_id": application_id,
                    "blocking_issue_count": len(blockers),
                    "warning_count": len(warnings),
                },
            )
        return self.get_report(report_id)

    def approve_application(
        self, application_id: str, report_id: str, note: str
    ) -> Approval:
        approval_id = new_id("apr")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            application, report, resume = self._approval_packet_rows(
                connection, application_id, report_id
            )
            connection.execute(
                """
                INSERT INTO approvals (
                    id, application_id, resume_version_id, report_id, pdf_sha256,
                    approved_at, approval_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    application["id"],
                    resume["id"],
                    report["id"],
                    resume["pdf_sha256"],
                    now,
                    note,
                ),
            )
            connection.execute(
                """
                UPDATE applications
                SET approval_id = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    approval_id,
                    ApplicationStatus.APPROVED.value,
                    now,
                    application["id"],
                ),
            )
            self._write_audit(
                connection,
                "application.approved",
                "application",
                application["id"],
                dict(application),
                {
                    "approval_id": approval_id,
                    "report_id": report["id"],
                    "pdf_sha256": resume["pdf_sha256"],
                },
            )
        return self.get_approval(approval_id)

    def mark_submitted_manual(self, application_id: str, note: str) -> Application:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            application = self._submission_packet_row(connection, application_id)
            connection.execute(
                """
                UPDATE applications
                SET status = ?, submission_state = ?, notes = ?, submitted_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    ApplicationStatus.SUBMITTED_MANUAL.value,
                    SubmissionState.SUBMITTED_MANUAL.value,
                    note,
                    now,
                    now,
                    application_id,
                ),
            )
            self._write_audit(
                connection,
                "application.submitted_manual",
                "application",
                application_id,
                dict(application),
                {"submission_state": SubmissionState.SUBMITTED_MANUAL.value, "notes": note},
            )
        return self.get_application(application_id)

    @staticmethod
    def _require_pristine_application_packet(application: sqlite3.Row) -> None:
        if (
            application["approval_id"] is not None
            or application["submission_state"]
            != SubmissionState.NOT_SUBMITTED.value
            or application["submitted_at"] is not None
        ):
            raise ValueError(
                "AI task cannot replace approved or submitted application state"
            )

    def _approval_packet_rows(
        self,
        connection: sqlite3.Connection,
        application_id: str,
        report_id: str,
    ) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
        application = connection.execute(
            "SELECT * FROM applications WHERE id = ?", (application_id,)
        ).fetchone()
        report = connection.execute(
            "SELECT * FROM reports WHERE id = ?", (report_id,)
        ).fetchone()
        if application is None or report is None:
            raise KeyError("Application approval packet was incomplete")
        resume = connection.execute(
            "SELECT * FROM resume_versions WHERE id = ?",
            (report["resume_version_id"],),
        ).fetchone()
        if resume is None:
            raise KeyError("Application approval resume was missing")
        if report["application_id"] != application["id"]:
            raise ValueError("Report does not belong to application")
        if report["blocking_issue_count"]:
            raise ValueError("Application has blocking risks")
        if application["latest_report_id"] != report["id"]:
            raise ValueError("Report is not the current application report")
        if application["selected_resume_version_id"] != report["resume_version_id"]:
            raise ValueError("Selected resume differs from report resume")
        if application["jd_snapshot_id"] != report["jd_snapshot_id"]:
            raise ValueError("Selected JD differs from report JD")
        if not resume["pdf_sha256"]:
            raise ValueError("Selected resume has no PDF checksum")
        self._verify_resume_pdf_row(resume)
        if (
            application["approval_id"] is not None
            or application["submission_state"] != SubmissionState.NOT_SUBMITTED.value
            or application["submitted_at"] is not None
        ):
            raise ValueError("Application is already approved or submitted")
        return application, report, resume

    def _submission_packet_row(
        self, connection: sqlite3.Connection, application_id: str
    ) -> sqlite3.Row:
        application = connection.execute(
            "SELECT * FROM applications WHERE id = ?", (application_id,)
        ).fetchone()
        if application is None:
            raise KeyError("No application with that ID")
        if (
            application["status"] != ApplicationStatus.APPROVED.value
            or application["approval_id"] is None
        ):
            raise ValueError(
                "Application must be approved before manual submission is recorded"
            )
        approval = connection.execute(
            "SELECT * FROM approvals WHERE id = ?", (application["approval_id"],)
        ).fetchone()
        report = connection.execute(
            "SELECT * FROM reports WHERE id = ?", (application["latest_report_id"],)
        ).fetchone()
        resume = connection.execute(
            "SELECT * FROM resume_versions WHERE id = ?",
            (application["selected_resume_version_id"],),
        ).fetchone()
        if (
            approval is None
            or report is None
            or resume is None
            or approval["application_id"] != application["id"]
            or approval["report_id"] != report["id"]
            or approval["resume_version_id"] != resume["id"]
            or approval["pdf_sha256"] != resume["pdf_sha256"]
            or report["application_id"] != application["id"]
            or report["resume_version_id"] != resume["id"]
            or report["jd_snapshot_id"] != application["jd_snapshot_id"]
        ):
            raise ValueError("Approved application packet changed before submission")
        self._verify_resume_pdf_row(resume)
        return application

    def _verify_resume_pdf_row(self, resume: sqlite3.Row) -> None:
        pdf_path = resume["pdf_path"]
        expected_sha256 = resume["pdf_sha256"]
        if (
            not pdf_path
            or not expected_sha256
            or not SHA256_PATTERN.fullmatch(expected_sha256)
        ):
            raise ValueError("Selected resume PDF integrity verification failed")
        try:
            _path, actual_sha256 = self._stable_artifact_digest(Path(pdf_path))
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise ValueError(
                "Selected resume PDF integrity verification failed"
            ) from exc
        if actual_sha256 != expected_sha256.lower():
            raise ValueError("Selected resume PDF integrity verification failed")

    def get_application(self, application_id: str) -> Application:
        row = self._one("SELECT * FROM applications WHERE id = ?", (application_id,))
        return self._application_from_row(row)

    def list_applications(self) -> list[Application]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM applications ORDER BY created_at DESC").fetchall()
        return [self._application_from_row(row) for row in rows]

    def get_jd_snapshot(self, snapshot_id: str) -> JobDescriptionSnapshot:
        row = self._one("SELECT * FROM jd_snapshots WHERE id = ?", (snapshot_id,))
        return JobDescriptionSnapshot(
            id=row["id"],
            application_id=row["application_id"],
            source_url=row["source_url"],
            raw_text_path=Path(row["raw_text_path"]),
            raw_text_sha256=row["raw_text_sha256"],
            captured_at=row["captured_at"],
            extracted_requirements_json=json.loads(row["extracted_requirements_json"]),
            parser_version=row["parser_version"],
        )

    def get_resume_version(self, resume_version_id: str) -> ResumeVersion:
        row = self._one("SELECT * FROM resume_versions WHERE id = ?", (resume_version_id,))
        return self._resume_from_row(row)

    def list_resume_versions(self) -> list[ResumeVersion]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM resume_versions ORDER BY created_at DESC").fetchall()
        return [self._resume_from_row(row) for row in rows]

    def get_report(self, report_id: str) -> Report:
        row = self._one("SELECT * FROM reports WHERE id = ?", (report_id,))
        return Report(
            id=row["id"],
            application_id=row["application_id"],
            resume_version_id=row["resume_version_id"],
            jd_snapshot_id=row["jd_snapshot_id"],
            fit=json.loads(row["fit_json"]),
            risks=json.loads(row["risks_json"]),
            blocking_issue_count=row["blocking_issue_count"],
            warning_count=row["warning_count"],
            created_at=row["created_at"],
        )

    def get_approval(self, approval_id: str) -> Approval:
        row = self._one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        return Approval(
            id=row["id"],
            application_id=row["application_id"],
            resume_version_id=row["resume_version_id"],
            report_id=row["report_id"],
            pdf_sha256=row["pdf_sha256"],
            approved_at=row["approved_at"],
            approval_note=row["approval_note"],
        )

    def list_audit_events(self) -> list[AuditEvent]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY rowid ASC"
            ).fetchall()
        return [
            AuditEvent(
                id=row["id"],
                event_type=row["event_type"],
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                before_json=json.loads(row["before_json"]),
                after_json=json.loads(row["after_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with connect_database(self.database_path) as connection:
            yield connection

    def _one(self, query: str, parameters: tuple[Any, ...]) -> sqlite3.Row:
        with self.connection() as connection:
            row = connection.execute(query, parameters).fetchone()
        if row is None:
            raise KeyError(f"No row for query: {query}")
        return row

    def _stable_artifact_digest(self, path: Path) -> tuple[Path, str]:
        vault_root = Path(os.path.abspath(self.vault_path))
        artifact_path = Path(os.path.abspath(path))
        try:
            relative = artifact_path.relative_to(vault_root)
        except ValueError as exc:
            raise ValueError(
                "Artifact path must remain within the repository vault"
            ) from exc
        current = vault_root
        try:
            root_stat = current.lstat()
        except FileNotFoundError as exc:
            raise FileNotFoundError("Repository vault does not exist") from exc
        if stat.S_ISLNK(root_stat.st_mode):
            raise ValueError("Artifact path must be a regular vault file")
        path_stat = root_stat
        for part in relative.parts:
            current = current / part
            try:
                path_stat = current.lstat()
            except FileNotFoundError as exc:
                raise FileNotFoundError(
                    f"Artifact file does not exist: {artifact_path}"
                ) from exc
            if stat.S_ISLNK(path_stat.st_mode):
                raise ValueError("Artifact path must be a regular vault file")
        if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_nlink != 1:
            raise ValueError("Artifact path must be a single-link regular file")
        if path_stat.st_size > MAX_AI_ARTIFACT_BYTES:
            raise ValueError("Artifact file exceeded its size limit")
        canonical_path = Path(os.path.realpath(artifact_path))

        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
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
            expected_identity = (
                path_stat.st_dev,
                path_stat.st_ino,
                path_stat.st_size,
                path_stat.st_nlink,
            )
            opened_identity = (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_nlink,
            )
            if opened_identity != expected_identity or not stat.S_ISREG(
                opened.st_mode
            ):
                raise ValueError("Artifact file identity changed while opening")
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_AI_ARTIFACT_BYTES:
                    raise ValueError("Artifact file exceeded its size limit")
                digest.update(chunk)
            after_read = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        try:
            after_path = canonical_path.lstat()
        except FileNotFoundError as exc:
            raise ValueError("Artifact file identity changed while hashing") from exc
        final_identity = (
            after_read.st_dev,
            after_read.st_ino,
            after_read.st_size,
            after_read.st_mtime_ns,
            after_read.st_nlink,
        )
        final_path_identity = (
            after_path.st_dev,
            after_path.st_ino,
            after_path.st_size,
            after_path.st_mtime_ns,
            after_path.st_nlink,
        )
        if final_identity != identity or final_path_identity != identity:
            raise ValueError("Artifact file identity changed while hashing")
        return canonical_path, digest.hexdigest()

    def _validate_artifact_row(self, row: sqlite3.Row) -> None:
        self._validate_ai_artifact_metadata(
            row["artifact_type"], row["schema_version"]
        )
        try:
            artifact_path, actual_sha256 = self._stable_artifact_digest(
                Path(row["path"])
            )
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise ValueError("AI artifact integrity verification failed") from exc
        if str(artifact_path) != row["path"] or actual_sha256 != row["sha256"]:
            raise ValueError("AI artifact integrity verification failed")
        self._require_ai_task_artifact_path(row["task_id"], artifact_path)

    @staticmethod
    def _validate_ai_artifact_metadata(
        artifact_type: str, schema_version: str
    ) -> None:
        if (
            artifact_type not in AI_ARTIFACT_TYPES
            or schema_version not in AI_ARTIFACT_SCHEMA_VERSIONS
        ):
            raise ValueError("AI artifact metadata was invalid")

    def _require_ai_task_artifact_path(
        self, task_id: str, artifact_path: Path
    ) -> None:
        vault_root = Path(os.path.abspath(self.vault_path))
        try:
            relative = artifact_path.relative_to(vault_root)
        except ValueError as exc:
            raise ValueError("AI artifact path must be under its task attempt") from exc
        parts = relative.parts
        if (
            len(parts) < 4
            or parts[0] != "ai-tasks"
            or parts[1] != task_id
            or not parts[2].startswith("attempt-")
            or len(parts[2]) == len("attempt-")
        ):
            raise ValueError("AI artifact path must be under its task attempt")

    def _require_active_ai_claim(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        claim_token: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT 1 FROM ai_tasks
            WHERE id = ? AND status = 'running' AND claim_token = ?
              AND claim_expires_at > ?
            """,
            (task_id, claim_token, utc_now()),
        ).fetchone()
        if row is None:
            raise ValueError("AI task claim is not active")

    def _ai_task_input_artifact_ids(
        self,
        connection: sqlite3.Connection,
        task_id: str,
    ) -> list[str]:
        return [
            row["artifact_id"]
            for row in connection.execute(
                """
                SELECT artifact_id
                FROM ai_task_input_artifacts
                WHERE task_id = ?
                ORDER BY ordinal ASC
                """,
                (task_id,),
            ).fetchall()
        ]

    def _write_audit(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        entity_type: str,
        entity_id: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        _safe_before, before_json = persisted_json(before or {})
        _safe_after, after_json = persisted_json(after or {})
        connection.execute(
            """
            INSERT INTO audit_events (
                id, event_type, entity_type, entity_id, before_json, after_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("aud"),
                event_type,
                entity_type,
                entity_id,
                before_json,
                after_json,
                utc_now(),
            ),
        )

    def _application_from_row(self, row: sqlite3.Row) -> Application:
        return Application(
            id=row["id"],
            company=row["company"],
            role_title=row["role_title"],
            location=row["location"],
            source_url=row["source_url"],
            source_kind=row["source_kind"],
            jd_snapshot_id=row["jd_snapshot_id"],
            status=ApplicationStatus(row["status"]),
            selected_resume_version_id=row["selected_resume_version_id"],
            latest_report_id=row["latest_report_id"],
            approval_id=row["approval_id"],
            submission_state=SubmissionState(row["submission_state"]),
            deadline=row["deadline"],
            notes=row["notes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            role_type=row["role_type"],
            submitted_at=row["submitted_at"],
        )

    def _resume_from_row(self, row: sqlite3.Row) -> ResumeVersion:
        return ResumeVersion(
            id=row["id"],
            name=row["name"],
            role_family=row["role_family"],
            tex_path=Path(row["tex_path"]),
            tex_sha256=row["tex_sha256"],
            pdf_path=Path(row["pdf_path"]) if row["pdf_path"] else None,
            pdf_sha256=row["pdf_sha256"],
            parent_resume_version_id=row["parent_resume_version_id"],
            render_status=row["render_status"],
            parse_status=row["parse_status"],
            page_count=row["page_count"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _ai_provider_from_row(self, row: sqlite3.Row) -> AiProviderConfig:
        binding_json = row["availability_binding_json"]
        binding_sha256 = row["availability_binding_sha256"]
        binding: dict[str, Any] = {}
        if binding_json != "{}" or binding_sha256:
            binding, canonical_json, actual_sha256 = canonical_provider_binding(
                json.loads(binding_json)
            )
            if canonical_json != binding_json or actual_sha256 != binding_sha256:
                raise ValueError("AI provider configuration binding is invalid")
        return AiProviderConfig(
            id=row["id"],
            provider_kind=row["provider_kind"],
            display_name=row["display_name"],
            identity_name=row["identity_name"],
            enabled=bool(row["enabled"]),
            privacy_mode=PrivacyMode(row["privacy_mode"]),
            endpoint=row["endpoint"],
            model=row["model"],
            availability_binding=binding,
            availability_binding_sha256=binding_sha256,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _ai_task_from_row(
        self,
        row: sqlite3.Row,
        input_artifact_ids: list[str],
    ) -> AiTask:
        binding_json = row["provider_binding_json"]
        binding_sha256 = row["provider_binding_sha256"]
        binding: dict[str, Any] = {}
        if binding_json != "{}" or binding_sha256:
            binding, canonical_json, actual_sha256 = canonical_provider_binding(
                json.loads(binding_json)
            )
            if canonical_json != binding_json or actual_sha256 != binding_sha256:
                raise ValueError("AI task provider binding is invalid")
        return AiTask(
            id=row["id"],
            application_id=row["application_id"],
            task_type=AiTaskType(row["task_type"]),
            provider_config_id=row["provider_config_id"],
            prompt_template_id=row["prompt_template_id"],
            prompt_sha256=row["prompt_sha256"],
            input_artifact_ids=input_artifact_ids,
            status=AiTaskStatus(row["status"]),
            privacy_mode=PrivacyMode(row["privacy_mode"]),
            provider_binding=binding,
            provider_binding_sha256=binding_sha256,
            started_at=row["started_at"],
            claim_expires_at=row["claim_expires_at"],
            invocation_started_at=row["invocation_started_at"],
            completed_at=row["completed_at"],
            error_summary=row["error_summary"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _ai_artifact_from_row(self, row: sqlite3.Row) -> AiArtifact:
        return AiArtifact(
            id=row["id"],
            task_id=row["task_id"],
            claim_token=row["claim_token"],
            artifact_type=row["artifact_type"],
            path=Path(row["path"]),
            sha256=row["sha256"],
            schema_version=row["schema_version"],
            created_at=row["created_at"],
        )

    def _ai_finding_from_row(self, row: sqlite3.Row) -> AiFinding:
        return AiFinding(
            id=row["id"],
            task_id=row["task_id"],
            claim_token=row["claim_token"],
            finding_type=row["finding_type"],
            severity=FindingSeverity(row["severity"]),
            claim_text=row["claim_text"],
            evidence_item_id=row["evidence_item_id"],
            recommendation=row["recommendation"],
            verifier_state=VerifierState(row["verifier_state"]),
            decision=FindingDecision(row["decision"]),
            payload=json.loads(row["payload_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _profile_fact_from_row(self, row: sqlite3.Row) -> ProfileFact:
        return ProfileFact(
            id=row["id"],
            fact_key=row["fact_key"],
            fact_value=row["fact_value"],
            verification_state=VerificationState(row["verification_state"]),
            privacy_level=PrivacyLevel(row["privacy_level"]),
            notes=row["notes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _evidence_item_from_row(self, row: sqlite3.Row) -> EvidenceItem:
        return EvidenceItem(
            id=row["id"],
            claim_text=row["claim_text"],
            evidence_type=row["evidence_type"],
            source_path=Path(row["source_path"]) if row["source_path"] else None,
            source_url=row["source_url"],
            source_sha256=row["source_sha256"],
            verification_state=VerificationState(row["verification_state"]),
            privacy_level=PrivacyLevel(row["privacy_level"]),
            notes=row["notes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _application_version_from_row(self, row: sqlite3.Row) -> ApplicationVersion:
        return ApplicationVersion(
            id=row["id"],
            application_id=row["application_id"],
            version_number=row["version_number"],
            event_type=row["event_type"],
            snapshot=json.loads(row["snapshot_json"]),
            created_at=row["created_at"],
        )
