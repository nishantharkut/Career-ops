from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
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


def finite_value(enum_type: type[Any], value: str, label: str) -> str:
    try:
        return enum_type(value).value
    except ValueError as exc:
        raise ValueError(f"Invalid {label}: {value}") from exc


class CareerOpsRepository:
    def __init__(self, database_path: Path, vault_path: Path):
        self.database_path = Path(database_path)
        self.vault_path = Path(vault_path)

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

    def upsert_ai_provider(
        self,
        *,
        provider_kind: str,
        display_name: str,
        privacy_mode: str,
        endpoint: str,
        model: str,
        enabled: bool,
    ) -> AiProviderConfig:
        privacy_mode = finite_value(PrivacyMode, privacy_mode, "privacy mode")
        provider_id = new_id("aip")
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO ai_provider_configs (
                    id, provider_kind, display_name, enabled, privacy_mode, endpoint,
                    model, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_kind, endpoint, model) DO UPDATE SET
                    display_name = excluded.display_name,
                    enabled = excluded.enabled,
                    privacy_mode = excluded.privacy_mode,
                    updated_at = excluded.updated_at
                """,
                (
                    provider_id,
                    provider_kind,
                    display_name,
                    int(enabled),
                    privacy_mode,
                    endpoint,
                    model,
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
        input_artifact_ids: list[str],
        privacy_mode: str,
    ) -> AiTask:
        task_type = finite_value(AiTaskType, task_type, "task type")
        privacy_mode = finite_value(PrivacyMode, privacy_mode, "privacy mode")
        task_id = new_id("ait")
        now = utc_now()
        with self.connection() as connection:
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
                    prompt_template_id, input_artifact_ids_json, status, privacy_mode,
                    started_at, completed_at, error_summary, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    application_id,
                    task_type,
                    provider_config_id,
                    prompt_template_id,
                    json.dumps(input_artifact_ids),
                    "pending",
                    privacy_mode,
                    None,
                    None,
                    "",
                    now,
                    now,
                ),
            )
            for artifact_id in dict.fromkeys(input_artifact_ids):
                connection.execute(
                    """
                    INSERT INTO ai_task_input_artifacts (
                        task_id, artifact_id, created_at
                    ) VALUES (?, ?, ?)
                    """,
                    (task_id, artifact_id, now),
                )
        return self.get_ai_task(task_id)

    def get_ai_task(self, task_id: str) -> AiTask:
        row = self._one("SELECT * FROM ai_tasks WHERE id = ?", (task_id,))
        return self._ai_task_from_row(row)

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
        return [self._ai_task_from_row(row) for row in rows]

    def claim_ai_task(self, task_id: str) -> str | None:
        now = utc_now()
        claim_token = secrets.token_urlsafe(32)
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE ai_tasks
                SET status = 'running', started_at = ?, completed_at = NULL,
                    error_summary = '', claim_token = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, claim_token, now, task_id),
            )
        return claim_token if cursor.rowcount == 1 else None

    def start_ai_task(self, task_id: str) -> str:
        claim_token = self.claim_ai_task(task_id)
        if claim_token is None:
            raise ValueError("AI task is not pending")
        return claim_token

    def complete_ai_task(self, task_id: str, claim_token: str) -> AiTask:
        before = self.get_ai_task(task_id)
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE ai_tasks
                SET status = 'completed', completed_at = ?, error_summary = '',
                    claim_token = NULL, updated_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                """,
                (now, now, task_id, claim_token),
            )
            if cursor.rowcount != 1:
                raise ValueError("AI task claim is not active")
            self._write_audit(
                connection,
                "ai_task.completed",
                "ai_task",
                task_id,
                before.__dict__,
                {"status": "completed", "completed_at": now},
            )
        return self.get_ai_task(task_id)

    def fail_ai_task(self, task_id: str, claim_token: str, error_summary: str) -> AiTask:
        before = self.get_ai_task(task_id)
        now = utc_now()
        safe_error = provider_error_category(error_summary)
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE ai_tasks
                SET status = 'failed', completed_at = ?, error_summary = ?,
                    claim_token = NULL, updated_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                """,
                (now, safe_error, now, task_id, claim_token),
            )
            if cursor.rowcount != 1:
                raise ValueError("AI task claim is not active")
            self._write_audit(
                connection,
                "ai_task.failed",
                "ai_task",
                task_id,
                before.__dict__,
                {"status": "failed", "completed_at": now, "error_summary": safe_error},
            )
        return self.get_ai_task(task_id)

    def requeue_interrupted_ai_tasks(self) -> int:
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE ai_tasks
                SET status = 'pending', started_at = NULL, completed_at = NULL,
                    error_summary = '', claim_token = NULL, updated_at = ?
                WHERE status = 'running'
                """,
                (now,),
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
        if not SHA256_PATTERN.fullmatch(sha256):
            raise ValueError(
                "Artifact SHA-256 must contain exactly 64 hexadecimal characters"
            )
        try:
            artifact_path = Path(path).resolve(strict=True)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"Artifact file does not exist: {path}") from exc
        vault_root = self.vault_path.resolve(strict=True)
        if not artifact_path.is_relative_to(vault_root):
            raise ValueError("Artifact path must remain within the repository vault")
        if not artifact_path.is_file():
            raise FileNotFoundError(f"Artifact path is not a file: {artifact_path}")
        with artifact_path.open("rb") as artifact_file:
            actual_sha256 = hashlib.file_digest(artifact_file, "sha256").hexdigest()
        normalized_sha256 = sha256.lower()
        if actual_sha256 != normalized_sha256:
            raise ValueError("Artifact checksum does not match file bytes")

        artifact_id = new_id("aia")
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_active_ai_claim(connection, task_id, claim_token)
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
        return self.get_ai_artifact(artifact_id)

    def get_ai_artifact(self, artifact_id: str) -> AiArtifact:
        row = self._one("SELECT * FROM ai_artifacts WHERE id = ?", (artifact_id,))
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
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
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
                    finding_type,
                    severity,
                    claim_text,
                    evidence_item_id,
                    recommendation,
                    verifier_state,
                    decision,
                    json.dumps(payload or {}),
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
    ) -> ApplicationVersion:
        version_id = new_id("appv")
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
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
                    json.dumps(snapshot),
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
                    "snapshot": snapshot,
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
        raw_text_path.write_text(jd_text, encoding="utf-8")
        source_kind = "url" if source_url else "manual"
        jd_sha = sha256_text(jd_text)

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
        resume = self.get_resume_version(resume_version_id)
        now = utc_now()
        with self.connection() as connection:
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
                resume.__dict__,
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
        application = self.get_application(application_id)
        self.get_resume_version(resume_version_id)
        status = (
            ApplicationStatus.NEEDS_RESUME_UPDATE
            if application.approval_id or application.latest_report_id
            else application.status
        )
        now = utc_now()
        before = application.__dict__
        with self.connection() as connection:
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
                before,
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
    ) -> Report:
        report_id = new_id("rep")
        now = utc_now()
        blockers = risks.get("blockers", [])
        warnings = risks.get("warnings", [])
        status = ApplicationStatus.READY_FOR_REVIEW if not blockers else ApplicationStatus.NEEDS_EVIDENCE
        with self.connection() as connection:
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
                    json.dumps(fit),
                    json.dumps(risks),
                    len(blockers),
                    len(warnings),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE applications
                SET latest_report_id = ?, approval_id = NULL, status = ?, updated_at = ?
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

    def approve_application(self, application_id: str, report_id: str, note: str) -> Approval:
        application = self.get_application(application_id)
        report = self.get_report(report_id)
        resume = self.get_resume_version(report.resume_version_id)
        if report.application_id != application.id:
            raise ValueError("Report does not belong to application")
        if report.blocking_issue_count:
            raise ValueError("Application has blocking risks")
        if application.selected_resume_version_id != report.resume_version_id:
            raise ValueError("Selected resume differs from report resume")
        if application.jd_snapshot_id != report.jd_snapshot_id:
            raise ValueError("Selected JD differs from report JD")
        if not resume.pdf_sha256:
            raise ValueError("Selected resume has no PDF checksum")

        approval_id = new_id("apr")
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO approvals (
                    id, application_id, resume_version_id, report_id, pdf_sha256,
                    approved_at, approval_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    application.id,
                    resume.id,
                    report.id,
                    resume.pdf_sha256,
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
                (approval_id, ApplicationStatus.APPROVED.value, now, application.id),
            )
            self._write_audit(
                connection,
                "application.approved",
                "application",
                application.id,
                application.__dict__,
                {"approval_id": approval_id, "report_id": report.id, "pdf_sha256": resume.pdf_sha256},
            )
        return self.get_approval(approval_id)

    def mark_submitted_manual(self, application_id: str, note: str) -> Application:
        application = self.get_application(application_id)
        if application.status != ApplicationStatus.APPROVED:
            raise ValueError("Application must be approved before manual submission is recorded")
        now = utc_now()
        with self.connection() as connection:
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
                application.__dict__,
                {"submission_state": SubmissionState.SUBMITTED_MANUAL.value, "notes": note},
            )
        return self.get_application(application_id)

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
            """,
            (task_id, claim_token),
        ).fetchone()
        if row is None:
            raise ValueError("AI task claim is not active")

    def _write_audit(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        entity_type: str,
        entity_id: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
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
                json.dumps(redact(before or {})),
                json.dumps(redact(after or {})),
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
        return AiProviderConfig(
            id=row["id"],
            provider_kind=row["provider_kind"],
            display_name=row["display_name"],
            enabled=bool(row["enabled"]),
            privacy_mode=PrivacyMode(row["privacy_mode"]),
            endpoint=row["endpoint"],
            model=row["model"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _ai_task_from_row(self, row: sqlite3.Row) -> AiTask:
        return AiTask(
            id=row["id"],
            application_id=row["application_id"],
            task_type=AiTaskType(row["task_type"]),
            provider_config_id=row["provider_config_id"],
            prompt_template_id=row["prompt_template_id"],
            input_artifact_ids=json.loads(row["input_artifact_ids_json"]),
            status=AiTaskStatus(row["status"]),
            privacy_mode=PrivacyMode(row["privacy_mode"]),
            started_at=row["started_at"],
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
