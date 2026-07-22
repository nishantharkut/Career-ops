from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import errno
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
from threading import Lock
import time
from typing import Any, BinaryIO
from uuid import uuid4

from careerops.core.database import connect_database, initialize_database
from careerops.core.migrations import PRE_AI_APPROVAL_AUTHORITY_SCHEMA_VERSION
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


class PersistedJsonSizeError(ValueError):
    """Canonical repository JSON exceeded the storage limit."""


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
# Allow one heartbeat interval and SQLite's full five-second writer wait.
MIN_AI_TASK_CLAIM_LEASE_SECONDS = 15.0
AI_ARTIFACT_TYPES = frozenset(
    {"context", "raw_output", "validated_output", "draft_text"}
)
AI_ARTIFACT_SCHEMA_VERSIONS = frozenset({"1"})
AI_ARTIFACT_STORAGE_VERSION = 2
DIRECTORY_FSYNC_SUPPORTED = hasattr(os, "O_DIRECTORY")
REPORT_AI_AUTHORITY_FIELDS = frozenset(
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
    "container_image_reference",
    "container_image_id",
    "issuer_instance_id",
    "egress_policy",
    "cloud_disabled",
    "egress_enforced",
    "isolation_evidence_sha256",
    "isolation_evidence_signature",
)


@dataclass(frozen=True)
class ApplicationPacketSnapshot:
    application: Application
    job_description: JobDescriptionSnapshot
    selected_resume: ResumeVersion | None
    latest_report: Report | None
    approval: Approval | None
    latest_ai_task: AiTask | None
    evidence: list[EvidenceItem]
    versions: list[ApplicationVersion]
    approval_eligibility: ApprovalEligibility


@dataclass(frozen=True)
class ApprovalEligibility:
    eligible: bool
    reason: str
    ai_task_id: str | None


@dataclass(frozen=True)
class ResumeDraftAuthority:
    application: Application
    task: AiTask
    parent_resume: ResumeVersion
    parent_source: bytes
    context_artifact: AiArtifact
    context_bytes: bytes
    findings: tuple[AiFinding, ...]
    evidence: tuple[EvidenceItem, ...]
    profile_facts: tuple[ProfileFact, ...]
    authority_version_id: str
    authority_snapshot_sha256: str
    task_signature: str
    parent_signature: str
    artifact_signatures: tuple[tuple[str, str], ...]
    finding_signatures: tuple[tuple[str, str], ...]
    evidence_signatures: tuple[tuple[str, str], ...]
    profile_fact_signatures: tuple[tuple[str, str], ...]
    artifact_file_evidence: tuple[tuple[str, _ArtifactFileEvidence], ...]
    parent_file_evidence: _ArtifactFileEvidence


PROVIDER_BINDING_BOOLEAN_FIELDS = frozenset(
    {"data_leaves_machine", "is_ai", "cloud_disabled", "egress_enforced"}
)
UTC_NOW_LOCK = Lock()
LAST_UTC_NOW: datetime | None = None


@dataclass(frozen=True)
class _ArtifactFileEvidence:
    path: Path
    sha256: str
    identity: tuple[int, int, int, int, int]


@dataclass(frozen=True)
class _ArtifactPreflight:
    row_signature: tuple[Any, ...]
    file_evidence: _ArtifactFileEvidence


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


def database_row_sha256(row: sqlite3.Row) -> str:
    serialized = json.dumps(
        dict(row),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256_text(serialized)


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
        raise PersistedJsonSizeError("Persisted JSON payload exceeded its size limit")
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
        normalized_claim_lease_seconds = float(claim_lease_seconds)
        if (
            not math.isfinite(normalized_claim_lease_seconds)
            or normalized_claim_lease_seconds < MIN_AI_TASK_CLAIM_LEASE_SECONDS
        ):
            raise ValueError(
                "AI task claim lease must be finite and at least 15 seconds"
            )
        self.database_path = Path(database_path)
        self.vault_path = Path(vault_path)
        self.claim_lease_seconds = normalized_claim_lease_seconds

    @property
    def claim_heartbeat_interval(self) -> float:
        return max(0.02, min(10.0, self.claim_lease_seconds / 3))

    def initialize(self) -> None:
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
        initialize_database(
            self.database_path,
            vault_path=self.vault_path,
            target_version=PRE_AI_APPROVAL_AUTHORITY_SCHEMA_VERSION,
        )
        self._upgrade_legacy_ai_artifacts()
        initialize_database(self.database_path, vault_path=self.vault_path)
        self._reconcile_ai_artifact_storage()
        self.requeue_interrupted_ai_tasks()

    @contextmanager
    def _ai_artifact_coordination(self) -> Iterator[None]:
        vault_root = Path(os.path.abspath(self.vault_path))
        lock_path = vault_root / ".ai-artifacts.lock"
        root_stat = vault_root.lstat()
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise ValueError("Repository vault path must be a directory")
        try:
            prior_stat = lock_path.lstat()
        except FileNotFoundError:
            prior_stat = None
        if prior_stat is not None and (
            stat.S_ISLNK(prior_stat.st_mode)
            or not stat.S_ISREG(prior_stat.st_mode)
            or prior_stat.st_nlink != 1
        ):
            raise ValueError("AI artifact coordination path must be a regular file")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOINHERIT", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        acquired = False
        try:
            descriptor_stat = self._validate_ai_artifact_coordination_path(
                lock_path, descriptor, prior_stat
            )
            if descriptor_stat.st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            self._acquire_ai_artifact_file_lock(descriptor)
            acquired = True
            self._validate_ai_artifact_coordination_path(
                lock_path, descriptor, prior_stat
            )
            yield
        finally:
            try:
                if acquired:
                    self._release_ai_artifact_file_lock(descriptor)
            finally:
                os.close(descriptor)

    @staticmethod
    def _validate_ai_artifact_coordination_path(
        lock_path: Path, descriptor: int, prior_stat: os.stat_result | None
    ) -> os.stat_result:
        path_stat = lock_path.lstat()
        descriptor_stat = os.fstat(descriptor)
        if (
            stat.S_ISLNK(path_stat.st_mode)
            or not stat.S_ISREG(path_stat.st_mode)
            or path_stat.st_nlink != 1
            or descriptor_stat.st_nlink != 1
            or (path_stat.st_dev, path_stat.st_ino)
            != (descriptor_stat.st_dev, descriptor_stat.st_ino)
            or (
                prior_stat is not None
                and (prior_stat.st_dev, prior_stat.st_ino)
                != (descriptor_stat.st_dev, descriptor_stat.st_ino)
            )
        ):
            raise ValueError("AI artifact coordination path must be a regular file")
        return descriptor_stat

    @staticmethod
    def _acquire_ai_artifact_file_lock(descriptor: int) -> None:
        if os.name != "nt":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
            return
        import msvcrt

        while True:
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                time.sleep(0.05)

    @staticmethod
    def _release_ai_artifact_file_lock(descriptor: int) -> None:
        if os.name != "nt":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)

    def _upgrade_legacy_ai_artifacts(self) -> None:
        with self._ai_artifact_coordination():
            self._sweep_ai_artifact_storage_coordinated()
            self._upgrade_legacy_ai_artifacts_coordinated()

    def _upgrade_legacy_ai_artifacts_coordinated(self) -> None:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM ai_artifacts ORDER BY created_at ASC, id ASC"
            ).fetchall()
        for row in rows:
            storage_version = row["storage_version"]
            if storage_version == AI_ARTIFACT_STORAGE_VERSION:
                continue
            if storage_version != 1:
                raise ValueError("legacy AI artifact storage version is invalid")
            source_path = Path(row["path"])
            target_path = self._ai_artifact_storage_path(
                row["task_id"], row["id"], row["artifact_type"]
            )
            try:
                source_path, source_sha256 = self._stable_artifact_digest(
                    source_path
                )
                if source_sha256 != row["sha256"]:
                    raise ValueError("stored checksum does not match source bytes")
                if source_path != Path(os.path.realpath(target_path)):
                    self._require_ai_task_source_path(row["task_id"], source_path)
                    evidence = self._publish_ai_artifact_copy(
                        source_path=source_path,
                        artifact_path=target_path,
                        expected_sha256=row["sha256"],
                        allow_existing=True,
                    )
                else:
                    copied_path, identity = self._fast_artifact_identity(
                        source_path
                    )
                    evidence = _ArtifactFileEvidence(
                        copied_path, source_sha256, identity
                    )
                self._require_ai_artifact_path(
                    row["task_id"], row["id"], evidence.path
                )
            except (FileNotFoundError, OSError, ValueError) as exc:
                raise ValueError(
                    f"legacy AI artifact {row['id']} failed integrity validation"
                ) from exc
            self._commit_legacy_ai_artifact_upgrade(
                row["id"], row["path"], evidence.path, row["sha256"]
            )

    def _commit_legacy_ai_artifact_upgrade(
        self,
        artifact_id: str,
        old_path: str,
        artifact_path: Path,
        sha256: str,
    ) -> None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE ai_artifacts
                SET path = ?, storage_version = ?
                WHERE id = ? AND path = ? AND sha256 = ? AND storage_version = 1
                """,
                (
                    str(artifact_path),
                    AI_ARTIFACT_STORAGE_VERSION,
                    artifact_id,
                    old_path,
                    sha256,
                ),
            )
            if cursor.rowcount == 1:
                return
            current = connection.execute(
                "SELECT path, sha256, storage_version FROM ai_artifacts WHERE id = ?",
                (artifact_id,),
            ).fetchone()
            if (
                current is None
                or current["path"] != str(artifact_path)
                or current["sha256"] != sha256
                or current["storage_version"] != AI_ARTIFACT_STORAGE_VERSION
            ):
                raise ValueError("legacy AI artifact authority changed during upgrade")

    def _reconcile_ai_artifact_storage(self) -> None:
        with self._ai_artifact_coordination():
            self._validate_current_ai_artifact_storage()
            self._reconcile_ai_artifact_storage_coordinated()
            self._validate_current_ai_artifact_storage()

    def _reconcile_ai_artifact_storage_coordinated(self) -> None:
        self._sweep_ai_artifact_storage_coordinated()

    def _sweep_ai_artifact_storage_coordinated(self) -> None:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM ai_artifacts ORDER BY created_at ASC, id ASC"
            ).fetchall()
        recorded_files: dict[str, str] = {}
        protected_legacy_directories: set[str] = set()
        for row in rows:
            target = self._ai_artifact_storage_path(
                row["task_id"], row["id"], row["artifact_type"]
            )
            target_directory_key = self._artifact_path_key(target.parent)
            if row["storage_version"] == AI_ARTIFACT_STORAGE_VERSION:
                recorded_files[target_directory_key] = self._artifact_path_key(
                    Path(row["path"])
                )
            elif row["storage_version"] == 1:
                protected_legacy_directories.add(target_directory_key)
            else:
                raise ValueError("AI artifact storage version is invalid")

        artifacts_root = self.vault_path / "ai-artifacts"
        for task_entry in list(artifacts_root.iterdir()):
            if task_entry.is_symlink() or not task_entry.is_dir():
                if task_entry.name.endswith(".tmp"):
                    self._remove_ai_artifact_storage_entry_if_unowned(task_entry)
                    continue
                raise ValueError("AI artifact storage contains an invalid entry")
            for artifact_entry in list(task_entry.iterdir()):
                directory_key = self._artifact_path_key(artifact_entry)
                if directory_key in recorded_files:
                    if artifact_entry.is_symlink() or not artifact_entry.is_dir():
                        raise ValueError(
                            "AI artifact storage contains an invalid entry"
                        )
                    for child in artifact_entry.iterdir():
                        if (
                            self._artifact_path_key(child)
                            == recorded_files[directory_key]
                        ):
                            continue
                        self._remove_ai_artifact_storage_entry_if_unowned(child)
                    continue
                if directory_key in protected_legacy_directories:
                    continue
                self._remove_ai_artifact_storage_entry_if_unowned(artifact_entry)
            self._remove_ai_artifact_storage_entry_if_unowned(
                task_entry, empty_directory_only=True
            )

    def _remove_ai_artifact_storage_entry_if_unowned(
        self, path: Path, *, empty_directory_only: bool = False
    ) -> bool:
        if self._ai_artifact_storage_entry_is_db_protected(path):
            return False
        if empty_directory_only:
            try:
                path.rmdir()
            except OSError:
                return False
            return True
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path)
        return True

    def _ai_artifact_storage_entry_is_db_protected(self, path: Path) -> bool:
        candidate = Path(os.path.abspath(path))
        artifacts_root = Path(os.path.abspath(self.vault_path / "ai-artifacts"))
        try:
            candidate.relative_to(artifacts_root)
        except ValueError as exc:
            raise ValueError(
                "AI artifact cleanup path must remain within owned storage"
            ) from exc
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, task_id, artifact_type, path, storage_version
                FROM ai_artifacts
                """
            ).fetchall()
        candidate_key = self._artifact_path_key(candidate)
        for row in rows:
            if row["storage_version"] == AI_ARTIFACT_STORAGE_VERSION:
                protected_path = Path(row["path"])
            elif row["storage_version"] == 1:
                protected_path = self._ai_artifact_storage_path(
                    row["task_id"], row["id"], row["artifact_type"]
                )
            else:
                raise ValueError("AI artifact storage version is invalid")
            protected = Path(os.path.abspath(protected_path))
            protected_key = self._artifact_path_key(protected)
            if candidate_key == protected_key or candidate_key in {
                self._artifact_path_key(parent) for parent in protected.parents
            }:
                return True
        return False

    def _validate_current_ai_artifact_storage(self) -> None:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM ai_artifacts ORDER BY created_at ASC, id ASC"
            ).fetchall()
        for row in rows:
            if row["storage_version"] == AI_ARTIFACT_STORAGE_VERSION:
                self._artifact_evidence_for_row(row)
            elif row["storage_version"] != 1:
                raise ValueError("AI artifact storage version is invalid")

    @staticmethod
    def _artifact_path_key(path: Path) -> str:
        return os.path.normcase(str(Path(os.path.abspath(path))))

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
            application = connection.execute(
                "SELECT * FROM applications WHERE id = ?", (application_id,)
            ).fetchone()
            if application is None:
                raise KeyError("No application with that ID")
            self._require_pristine_application_packet(application)
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
            task_row = connection.execute(
                "SELECT * FROM ai_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task_row is None:
                raise RuntimeError("Created AI task was not readable")
            task = self._ai_task_from_row(task_row, list(input_artifact_ids))
        return task

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

    def list_pending_ai_task_ids(self, *, limit: int) -> list[str]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("Pending AI task limit must be a positive integer")
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT id FROM ai_tasks
                WHERE status = 'pending'
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [row["id"] for row in rows]

    def claim_ai_task(self, task_id: str) -> str | None:
        claim_token = secrets.token_urlsafe(32)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            task = connection.execute(
                "SELECT * FROM ai_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None or task["status"] != AiTaskStatus.PENDING.value:
                return None
            application = connection.execute(
                "SELECT * FROM applications WHERE id = ?",
                (task["application_id"],),
            ).fetchone()
            if application is None:
                raise ValueError("AI task application is missing")
            if not self._application_packet_is_pristine(application):
                self._cancel_pending_ai_task_rows(connection, [task], now)
                return None
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
                    FROM applications AS application
                    WHERE application.id = ai_tasks.application_id
                      AND application.approval_id IS NULL
                      AND application.submission_state = 'not_submitted'
                      AND application.submitted_at IS NULL
                      AND application.status NOT IN ('approved', 'submitted_manual')
                  )
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
        raise ValueError("AI task success requires the aggregate finalizer")

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
        raise ValueError("AI task blocking requires the aggregate finalizer")

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
        artifact_preflight = self._preflight_ai_artifacts(task_id, claim_token)

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
            if {row["id"] for row in artifact_rows} != set(artifact_preflight):
                raise ValueError("AI task artifact ownership changed during finalization")
            for artifact in artifact_rows:
                preflight = artifact_preflight[artifact["id"]]
                if self._artifact_row_signature(artifact) != preflight.row_signature:
                    raise ValueError(
                        "AI task artifact ownership changed during finalization"
                    )
                # Same-user mutation after this fast check is outside the process
                # threat model; getters and later reads rehash and reject divergence.
                self._validate_artifact_row_fast(
                    artifact, preflight.file_evidence
                )
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
                finding_id, finding_type = self._insert_ai_finding(
                    connection=connection,
                    task_id=task_id,
                    claim_token=claim_token,
                    finding=finding,
                    now=now,
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
                    reviewed_resume = connection.execute(
                        "SELECT * FROM resume_versions WHERE id = ?",
                        (expected_resume_version_id,),
                    ).fetchone()
                    if reviewed_resume is None:
                        raise ValueError("Selected AI review resume is missing")
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
                            ai_task_id, pdf_sha256, fit_json, risks_json,
                            blocking_issue_count, warning_count, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            report_id,
                            application["id"],
                            expected_resume_version_id,
                            expected_jd_snapshot_id,
                            task_id,
                            reviewed_resume["pdf_sha256"],
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
            self._insert_application_version(
                connection=connection,
                application_id=application["id"],
                event_type=f"ai_task.{terminal_status}",
                snapshot=snapshot,
                now=now,
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

    def requeue_expired_ai_task_claim(
        self, task_id: str, claim_token: str
    ) -> bool:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            cursor = connection.execute(
                """
                UPDATE ai_tasks
                SET status = 'pending', started_at = NULL, completed_at = NULL,
                    error_summary = '', claim_token = NULL,
                    claim_expires_at = NULL, updated_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                  AND (claim_expires_at IS NULL OR claim_expires_at <= ?)
                """,
                (now, task_id, claim_token, now),
            )
        return cursor.rowcount == 1

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
        self._require_ai_task_source_path(task_id, Path(os.path.abspath(path)))
        normalized_sha256 = sha256.lower()
        with self._ai_artifact_coordination():
            self._sweep_ai_artifact_storage_coordinated()
            return self._save_ai_artifact_coordinated(
                task_id=task_id,
                claim_token=claim_token,
                artifact_type=artifact_type,
                path=path,
                sha256=normalized_sha256,
                schema_version=schema_version,
            )

    def _save_ai_artifact_coordinated(
        self,
        *,
        task_id: str,
        claim_token: str,
        artifact_type: str,
        path: Path,
        sha256: str,
        schema_version: str,
    ) -> AiArtifact:
        artifact_id = new_id("aia")
        artifact_path, file_evidence = self._materialize_ai_artifact(
            task_id=task_id,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            source_path=path,
            expected_sha256=sha256,
        )
        inserted_artifact: AiArtifact | None = None
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                now = utc_now()
                self._require_active_ai_claim(connection, task_id, claim_token)
                connection.execute(
                    """
                    INSERT INTO ai_artifacts (
                        id, task_id, claim_token, artifact_type, path, sha256,
                        schema_version, storage_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        task_id,
                        claim_token,
                        artifact_type,
                        str(artifact_path),
                        sha256,
                        schema_version,
                        AI_ARTIFACT_STORAGE_VERSION,
                        now,
                    ),
                )
                inserted = connection.execute(
                    "SELECT * FROM ai_artifacts WHERE id = ?", (artifact_id,)
                ).fetchone()
                if inserted is None:
                    raise ValueError("AI artifact row was not persisted")
                self._validate_artifact_row_fast(inserted, file_evidence)
                inserted_artifact = self._ai_artifact_from_row(inserted)
        except sqlite3.IntegrityError as exc:
            self._remove_materialized_ai_artifact(artifact_path)
            if "ai_artifacts.path" in str(exc):
                raise ValueError("AI artifact physical path is already owned") from exc
            raise
        except BaseException:
            self._remove_materialized_ai_artifact(artifact_path)
            raise
        if inserted_artifact is None:
            self._remove_materialized_ai_artifact(artifact_path)
            raise ValueError("AI artifact row was not persisted")
        return inserted_artifact

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
        raise ValueError("AI findings require the aggregate finalizer")

    def _insert_ai_finding(
        self,
        *,
        connection: sqlite3.Connection,
        task_id: str,
        claim_token: str,
        finding: Mapping[str, Any],
        now: str,
    ) -> tuple[str, str]:
        severity = finite_value(
            FindingSeverity, str(finding["severity"]), "severity"
        )
        verifier_state = finite_value(
            VerifierState, str(finding["verifier_state"]), "verifier state"
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
        _safe_payload, payload_json = persisted_json(safe_finding["payload"])
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
        return finding_id, finding_type

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
                """
                SELECT ai_findings.*, applications.approval_id,
                       applications.submission_state, applications.submitted_at,
                       applications.status AS application_status
                FROM ai_findings
                JOIN ai_tasks ON ai_tasks.id = ai_findings.task_id
                JOIN applications ON applications.id = ai_tasks.application_id
                WHERE ai_findings.id = ?
                """,
                (finding_id,),
            ).fetchone()
            if before_row is None:
                raise KeyError(f"No AI finding: {finding_id}")
            if (
                before_row["approval_id"] is not None
                or before_row["submission_state"]
                != SubmissionState.NOT_SUBMITTED.value
                or before_row["submitted_at"] is not None
                or before_row["application_status"]
                in {
                    ApplicationStatus.APPROVED.value,
                    ApplicationStatus.SUBMITTED_MANUAL.value,
                }
            ):
                raise ValueError(
                    "AI task cannot replace approved or submitted application state"
                )
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
            fact_row = connection.execute(
                "SELECT * FROM profile_facts WHERE id = ?", (fact_id,)
            ).fetchone()
            if fact_row is None:
                raise RuntimeError("Created profile fact was not readable")
            fact = self._profile_fact_from_row(fact_row)
        return fact

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
            evidence_row = connection.execute(
                "SELECT * FROM evidence_items WHERE id = ?", (evidence_id,)
            ).fetchone()
            if evidence_row is None:
                raise RuntimeError("Created evidence item was not readable")
            evidence = self._evidence_item_from_row(evidence_row)
        return evidence

    def create_and_link_evidence_item(
        self,
        *,
        application_id: str,
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
        stored_source_path = str(source_path) if source_path else None
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            application = connection.execute(
                "SELECT id FROM applications WHERE id = ?", (application_id,)
            ).fetchone()
            if application is None:
                raise KeyError("No application with that ID")
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
                    stored_source_path,
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
                    "source_path": stored_source_path,
                    "source_url": source_url,
                    "source_sha256": source_sha256,
                    "verification_state": verification_state,
                    "privacy_level": privacy_level,
                    "notes": notes,
                },
            )
            connection.execute(
                """
                INSERT INTO application_evidence_links (
                    application_id, evidence_item_id, created_at
                ) VALUES (?, ?, ?)
                """,
                (application_id, evidence_id, now),
            )
            self._write_audit(
                connection,
                "application.evidence_linked",
                "application",
                application_id,
                None,
                {"evidence_item_id": evidence_id},
            )
            evidence_row = connection.execute(
                "SELECT * FROM evidence_items WHERE id = ?", (evidence_id,)
            ).fetchone()
            if evidence_row is None:
                raise RuntimeError("Created evidence item was not readable")
            evidence = self._evidence_item_from_row(evidence_row)
        return evidence

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
            evidence_row = connection.execute(
                "SELECT * FROM evidence_items WHERE id = ?", (evidence_item_id,)
            ).fetchone()
            if evidence_row is None:
                raise RuntimeError("Linked evidence item was not readable")
            evidence = self._evidence_item_from_row(evidence_row)
        return evidence

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
        if (
            event_type.startswith("ai_task.")
            or ai_task_id is not None
            or claim_token is not None
        ):
            raise ValueError("AI application versions require the aggregate finalizer")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            version_id, _version_number = self._insert_application_version(
                connection=connection,
                application_id=application_id,
                event_type=event_type,
                snapshot=snapshot,
                now=now,
            )
            version_row = connection.execute(
                "SELECT * FROM application_versions WHERE id = ?", (version_id,)
            ).fetchone()
            if version_row is None:
                raise RuntimeError("Created application version was not readable")
            version = self._application_version_from_row(version_row)
        return version

    def _insert_application_version(
        self,
        *,
        connection: sqlite3.Connection,
        application_id: str,
        event_type: str,
        snapshot: Mapping[str, Any],
        now: str,
    ) -> tuple[str, int]:
        safe_snapshot, snapshot_json = persisted_json(dict(snapshot))
        version_id = new_id("appv")
        version_number = connection.execute(
            """
            SELECT COALESCE(MAX(version_number), 0) + 1
            FROM application_versions
            WHERE application_id = ?
            """,
            (application_id,),
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
        return version_id, version_number

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
        raise ValueError("AI JD updates require the aggregate finalizer")

    def update_user_corrected_requirements(
        self,
        application_id: str,
        user_corrected: Mapping[str, Any],
    ) -> JobDescriptionSnapshot:
        safe_user_corrected, _serialized_user_corrected = persisted_json(
            dict(user_corrected)
        )
        if not isinstance(safe_user_corrected, dict):
            raise ValueError("User-corrected requirements must be an object")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            application = connection.execute(
                "SELECT * FROM applications WHERE id = ?", (application_id,)
            ).fetchone()
            if application is None:
                raise KeyError("No application with that ID")
            self._require_pristine_application_packet(application)
            snapshot = connection.execute(
                "SELECT * FROM jd_snapshots WHERE id = ?",
                (application["jd_snapshot_id"],),
            ).fetchone()
            if snapshot is None or snapshot["application_id"] != application_id:
                raise ValueError("Application job description ownership is invalid")
            try:
                existing = json.loads(snapshot["extracted_requirements_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("Stored requirements are invalid") from exc
            if not isinstance(existing, dict):
                raise ValueError("Stored requirements are invalid")
            grouped = any(
                key in existing
                for key in (
                    "raw_ai",
                    "deterministic",
                    "user_corrected",
                    "uncertainty",
                )
            )
            authoritative_groups = {
                "raw_ai": existing.get("raw_ai", {}) if grouped else {},
                "deterministic": (
                    existing.get("deterministic", {}) if grouped else existing
                ),
                "uncertainty": existing.get("uncertainty", {}) if grouped else {},
            }
            if not all(
                isinstance(value, dict) for value in authoritative_groups.values()
            ):
                raise ValueError("Stored requirement groups are invalid")
            updated = {
                "raw_ai": authoritative_groups["raw_ai"],
                "deterministic": authoritative_groups["deterministic"],
                "user_corrected": safe_user_corrected,
                "uncertainty": authoritative_groups["uncertainty"],
            }
            _safe_updated, serialized = persisted_json(updated)
            connection.execute(
                """
                UPDATE jd_snapshots
                SET extracted_requirements_json = ?
                WHERE id = ? AND application_id = ?
                """,
                (serialized, snapshot["id"], application_id),
            )
            invalidated_report_id = application["latest_report_id"]
            if invalidated_report_id is not None:
                status = (
                    ApplicationStatus.NEEDS_RESUME_UPDATE.value
                    if application["selected_resume_version_id"] is not None
                    else ApplicationStatus.DRAFT.value
                )
                connection.execute(
                    """
                    UPDATE applications
                    SET latest_report_id = NULL, approval_id = NULL,
                        status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, now, application_id),
                )
                self._write_audit(
                    connection,
                    "application.report_invalidated_by_user_requirements",
                    "application",
                    application_id,
                    {"latest_report_id": invalidated_report_id},
                    {"latest_report_id": None, "status": status},
                )
            else:
                connection.execute(
                    "UPDATE applications SET updated_at = ? WHERE id = ?",
                    (now, application_id),
                )
            version_id, version_number = self._insert_application_version(
                connection=connection,
                application_id=application_id,
                event_type="requirements.user_corrected_updated",
                snapshot={
                    "invalidated_report_id": invalidated_report_id,
                    "jd_snapshot_id": snapshot["id"],
                    "user_corrected": safe_user_corrected,
                },
                now=now,
            )
            self._write_audit(
                connection,
                "jd_snapshot.user_requirements_updated",
                "jd_snapshot",
                snapshot["id"],
                {
                    "requirements_sha256": sha256_text(
                        snapshot["extracted_requirements_json"]
                    )
                },
                {
                    "application_version_id": version_id,
                    "requirements_sha256": sha256_text(serialized),
                    "version_number": version_number,
                },
            )
            updated_snapshot_row = connection.execute(
                "SELECT * FROM jd_snapshots WHERE id = ?",
                (application["jd_snapshot_id"],),
            ).fetchone()
            if updated_snapshot_row is None:
                raise RuntimeError("Updated job description was not readable")
            updated_snapshot = self._jd_snapshot_from_row(updated_snapshot_row)
        return updated_snapshot

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

    def get_resume_draft_authority(
        self,
        *,
        application_id: str,
        task_id: str,
        finding_ids: Sequence[str],
    ) -> ResumeDraftAuthority:
        if not finding_ids or len(set(finding_ids)) != len(finding_ids):
            raise ValueError("Resume draft finding IDs must be nonempty and unique")

        with self.connection() as connection:
            connection.execute("BEGIN")
            application_row = connection.execute(
                "SELECT * FROM applications WHERE id = ?", (application_id,)
            ).fetchone()
            if application_row is None:
                raise KeyError("No application with that ID")
            task_row = connection.execute(
                "SELECT * FROM ai_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task_row is None:
                raise KeyError("No AI task with that ID")
            if task_row["application_id"] != application_id:
                raise ValueError("AI task does not belong to this application")
            if (
                task_row["status"] != AiTaskStatus.COMPLETED.value
                or task_row["task_type"]
                != AiTaskType.SUGGEST_RESUME_EDITS.value
            ):
                raise ValueError("AI task cannot authorize a resume draft")

            parent_id = application_row["selected_resume_version_id"]
            if parent_id is None:
                raise ValueError("Application has no selected parent resume")
            parent_row = connection.execute(
                "SELECT * FROM resume_versions WHERE id = ?", (parent_id,)
            ).fetchone()
            if parent_row is None:
                raise ValueError("Selected parent resume is missing")

            authority_matches: list[tuple[sqlite3.Row, dict[str, Any]]] = []
            version_rows = connection.execute(
                """
                SELECT * FROM application_versions
                WHERE application_id = ? AND event_type = 'ai_task.completed'
                ORDER BY version_number ASC
                """,
                (application_id,),
            ).fetchall()
            for version_row in version_rows:
                try:
                    snapshot = json.loads(version_row["snapshot_json"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError("AI task authority snapshot is invalid") from exc
                if not isinstance(snapshot, dict):
                    raise ValueError("AI task authority snapshot is invalid")
                task_snapshot = snapshot.get("ai_task")
                if (
                    isinstance(task_snapshot, dict)
                    and task_snapshot.get("id") == task_id
                ):
                    authority_matches.append((version_row, snapshot))
            if len(authority_matches) != 1:
                raise ValueError("AI task completed authority is ambiguous")
            authority_version, authority_snapshot = authority_matches[0]
            task_snapshot = authority_snapshot.get("ai_task")
            if (
                not isinstance(task_snapshot, dict)
                or task_snapshot.get("status") != AiTaskStatus.COMPLETED.value
                or task_snapshot.get("task_type")
                != AiTaskType.SUGGEST_RESUME_EDITS.value
            ):
                raise ValueError("AI task completed authority is invalid")

            artifact_manifest = authority_snapshot.get("artifacts")
            if not isinstance(artifact_manifest, list):
                raise ValueError("AI task artifact authority is invalid")
            manifest_by_id: dict[str, dict[str, str]] = {}
            manifest_by_type: dict[str, dict[str, str]] = {}
            for item in artifact_manifest:
                if (
                    not isinstance(item, dict)
                    or set(item) != {"artifact_type", "id", "sha256"}
                    or type(item.get("artifact_type")) is not str
                    or type(item.get("id")) is not str
                    or type(item.get("sha256")) is not str
                    or item["id"] in manifest_by_id
                    or item["artifact_type"] in manifest_by_type
                ):
                    raise ValueError("AI task artifact authority is invalid")
                manifest_by_id[item["id"]] = item
                manifest_by_type[item["artifact_type"]] = item
            if set(manifest_by_type) != {
                "context",
                "raw_output",
                "validated_output",
            }:
                raise ValueError("AI task artifact authority is incomplete")

            placeholders = ", ".join("?" for _ in manifest_by_id)
            artifact_rows = connection.execute(
                f"SELECT * FROM ai_artifacts WHERE id IN ({placeholders})",
                tuple(manifest_by_id),
            ).fetchall()
            if len(artifact_rows) != len(manifest_by_id):
                raise ValueError("AI task artifact authority is missing")
            artifact_by_id = {row["id"]: row for row in artifact_rows}
            context_row = artifact_by_id[manifest_by_type["context"]["id"]]
            attempt_token = context_row["claim_token"]
            if type(attempt_token) is not str or not attempt_token:
                raise ValueError("AI task completed attempt is invalid")
            artifact_file_evidence: dict[str, _ArtifactFileEvidence] = {}
            for artifact_id, manifest in manifest_by_id.items():
                artifact_row = artifact_by_id[artifact_id]
                if (
                    artifact_row["task_id"] != task_id
                    or artifact_row["claim_token"] != attempt_token
                    or artifact_row["artifact_type"] != manifest["artifact_type"]
                    or artifact_row["sha256"] != manifest["sha256"]
                ):
                    raise ValueError("AI task artifact authority changed")
                artifact_file_evidence[artifact_id] = (
                    self._artifact_evidence_for_row(artifact_row)
                )
            attempt_artifact_rows = connection.execute(
                """
                SELECT * FROM ai_artifacts
                WHERE task_id = ? AND claim_token = ?
                ORDER BY created_at ASC, id ASC
                """,
                (task_id, attempt_token),
            ).fetchall()
            if {row["id"] for row in attempt_artifact_rows} != set(manifest_by_id):
                raise ValueError("AI task completed attempt artifacts are ambiguous")

            provenance = authority_snapshot.get("provenance")
            if (
                not isinstance(provenance, dict)
                or provenance.get("ai_task_id") != task_id
                or provenance.get("context_sha256") != context_row["sha256"]
            ):
                raise ValueError("AI task context provenance is invalid")

            snapshot_finding_ids = authority_snapshot.get("finding_ids")
            side_effect = authority_snapshot.get("side_effect")
            suggestion_ids = (
                side_effect.get("suggestion_finding_ids")
                if isinstance(side_effect, dict)
                else None
            )
            if (
                not isinstance(snapshot_finding_ids, list)
                or not isinstance(suggestion_ids, list)
                or any(type(value) is not str for value in snapshot_finding_ids)
                or any(type(value) is not str for value in suggestion_ids)
                or len(set(snapshot_finding_ids)) != len(snapshot_finding_ids)
                or snapshot_finding_ids != suggestion_ids
            ):
                raise ValueError("AI task finding authority is invalid")

            attempt_finding_rows = connection.execute(
                """
                SELECT * FROM ai_findings
                WHERE task_id = ? AND claim_token = ?
                ORDER BY created_at ASC, id ASC
                """,
                (task_id, attempt_token),
            ).fetchall()
            if {row["id"] for row in attempt_finding_rows} != set(
                snapshot_finding_ids
            ):
                raise ValueError("AI task completed attempt findings are ambiguous")
            attempt_findings = {row["id"]: row for row in attempt_finding_rows}
            selected_finding_rows: list[sqlite3.Row] = []
            for finding_id in finding_ids:
                finding_row = connection.execute(
                    "SELECT * FROM ai_findings WHERE id = ?", (finding_id,)
                ).fetchone()
                if finding_row is None:
                    raise KeyError("No AI finding with that ID")
                if finding_id not in attempt_findings:
                    raise ValueError("AI finding is not authorized by this task")
                selected_finding_rows.append(finding_row)

            evidence_rows = connection.execute(
                """
                SELECT evidence_items.*
                FROM application_evidence_links
                JOIN evidence_items
                  ON evidence_items.id = application_evidence_links.evidence_item_id
                WHERE application_evidence_links.application_id = ?
                ORDER BY application_evidence_links.created_at ASC,
                         evidence_items.id ASC
                """,
                (application_id,),
            ).fetchall()
            profile_fact_rows = connection.execute(
                "SELECT * FROM profile_facts ORDER BY created_at ASC, id ASC"
            ).fetchall()

            context_buffer = BytesIO()
            context_evidence = self._stable_artifact_read(
                Path(context_row["path"]), context_buffer
            )
            if (
                context_evidence.path != Path(context_row["path"])
                or context_evidence.sha256 != context_row["sha256"]
            ):
                raise ValueError("AI task context checksum is invalid")
            artifact_file_evidence[context_row["id"]] = context_evidence

            parent_buffer = BytesIO()
            parent_evidence = self._stable_artifact_read(
                Path(parent_row["tex_path"]), parent_buffer
            )
            vault_root = Path(os.path.realpath(self.vault_path))
            try:
                parent_relative = parent_evidence.path.relative_to(vault_root)
            except ValueError as exc:
                raise ValueError("Parent resume source is not repository-owned") from exc
            if (
                not parent_relative.parts
                or parent_relative.parts[0] != "resumes"
                or parent_evidence.path != Path(parent_row["tex_path"])
                or not SHA256_PATTERN.fullmatch(parent_row["tex_sha256"])
                or parent_row["tex_sha256"].lower() != parent_row["tex_sha256"]
                or parent_evidence.sha256 != parent_row["tex_sha256"]
            ):
                raise ValueError("Parent resume source checksum is invalid")

            application = self._application_from_row(application_row)
            task = self._ai_task_from_row(
                task_row, self._ai_task_input_artifact_ids(connection, task_id)
            )
            parent = self._resume_from_row(parent_row)
            context_artifact = self._ai_artifact_from_row(context_row)
            selected_findings = tuple(
                self._ai_finding_from_row(row) for row in selected_finding_rows
            )
            evidence = tuple(
                self._evidence_item_from_row(row) for row in evidence_rows
            )
            profile_facts = tuple(
                self._profile_fact_from_row(row) for row in profile_fact_rows
            )
            authority = ResumeDraftAuthority(
                application=application,
                task=task,
                parent_resume=parent,
                parent_source=parent_buffer.getvalue(),
                context_artifact=context_artifact,
                context_bytes=context_buffer.getvalue(),
                findings=selected_findings,
                evidence=evidence,
                profile_facts=profile_facts,
                authority_version_id=authority_version["id"],
                authority_snapshot_sha256=sha256_text(
                    authority_version["snapshot_json"]
                ),
                task_signature=database_row_sha256(task_row),
                parent_signature=database_row_sha256(parent_row),
                artifact_signatures=tuple(
                    sorted(
                        (row["id"], database_row_sha256(row))
                        for row in artifact_rows
                    )
                ),
                finding_signatures=tuple(
                    sorted(
                        (row["id"], database_row_sha256(row))
                        for row in attempt_finding_rows
                    )
                ),
                evidence_signatures=tuple(
                    sorted(
                        (row["id"], database_row_sha256(row))
                        for row in evidence_rows
                    )
                ),
                profile_fact_signatures=tuple(
                    sorted(
                        (row["id"], database_row_sha256(row))
                        for row in profile_fact_rows
                    )
                ),
                artifact_file_evidence=tuple(
                    sorted(artifact_file_evidence.items())
                ),
                parent_file_evidence=parent_evidence,
            )
        return authority

    def create_and_select_resume_draft(
        self,
        *,
        application_id: str,
        resume_id: str,
        name: str,
        tex_path: Path,
        tex_sha256: str,
        authority: ResumeDraftAuthority,
        referenced_evidence_ids: Sequence[str],
    ) -> ResumeVersion:
        if (
            type(name) is not str
            or not name
            or name != name.strip()
            or len(name) > 300
        ):
            raise ValueError("Resume draft name is invalid")
        if not re.fullmatch(r"res_[0-9a-f]{32}", resume_id):
            raise ValueError("Resume draft ID is invalid")
        if len(set(referenced_evidence_ids)) != len(referenced_evidence_ids):
            raise ValueError("Referenced evidence IDs must be unique")
        expected_evidence_signatures = dict(authority.evidence_signatures)
        if not set(referenced_evidence_ids).issubset(expected_evidence_signatures):
            raise ValueError("Referenced evidence is not authorized")

        child_evidence = self._stable_artifact_read(tex_path)
        expected_child_path = Path(
            os.path.realpath(
                self.vault_path / "resumes" / resume_id / "source.tex"
            )
        )
        if (
            child_evidence.path != expected_child_path
            or not SHA256_PATTERN.fullmatch(tex_sha256)
            or tex_sha256.lower() != tex_sha256
            or child_evidence.sha256 != tex_sha256
        ):
            raise ValueError("Resume draft source is not repository-owned")

        expected_artifact_signatures = dict(authority.artifact_signatures)
        expected_artifact_file_evidence = dict(
            authority.artifact_file_evidence
        )
        if set(expected_artifact_file_evidence) != set(
            expected_artifact_signatures
        ):
            raise ValueError("AI task artifact preflight is incomplete")
        expected_finding_signatures = dict(authority.finding_signatures)
        expected_profile_signatures = dict(authority.profile_fact_signatures)
        expected_parent_evidence = authority.parent_file_evidence
        expected_parent_source_sha256 = hashlib.sha256(
            authority.parent_source
        ).hexdigest()
        inserted_resume: ResumeVersion | None = None
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            application = connection.execute(
                "SELECT * FROM applications WHERE id = ?", (application_id,)
            ).fetchone()
            if application is None:
                raise KeyError("No application with that ID")
            if (
                authority.application.id != application_id
                or application["approval_id"] is not None
                or application["status"]
                in {
                    ApplicationStatus.APPROVED.value,
                    ApplicationStatus.SUBMITTED_MANUAL.value,
                }
                or application["submission_state"]
                != SubmissionState.NOT_SUBMITTED.value
                or application["submitted_at"] is not None
            ):
                raise ValueError(
                    "Cannot create a resume draft for an approved or submitted application"
                )
            if (
                application["selected_resume_version_id"]
                != authority.parent_resume.id
            ):
                raise ValueError("Selected resume changed since the AI task")

            task_row = connection.execute(
                "SELECT * FROM ai_tasks WHERE id = ?", (authority.task.id,)
            ).fetchone()
            if (
                task_row is None
                or task_row["application_id"] != application_id
                or task_row["status"] != AiTaskStatus.COMPLETED.value
                or task_row["task_type"]
                != AiTaskType.SUGGEST_RESUME_EDITS.value
                or database_row_sha256(task_row) != authority.task_signature
            ):
                raise ValueError("AI task authority changed before draft creation")

            authority_matches: list[tuple[sqlite3.Row, dict[str, Any]]] = []
            version_rows = connection.execute(
                """
                SELECT * FROM application_versions
                WHERE application_id = ? AND event_type = 'ai_task.completed'
                ORDER BY version_number ASC
                """,
                (application_id,),
            ).fetchall()
            for version_row in version_rows:
                try:
                    snapshot = json.loads(version_row["snapshot_json"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError("AI task authority snapshot is invalid") from exc
                if not isinstance(snapshot, dict):
                    raise ValueError("AI task authority snapshot is invalid")
                task_snapshot = snapshot.get("ai_task")
                if (
                    isinstance(task_snapshot, dict)
                    and task_snapshot.get("id") == authority.task.id
                ):
                    authority_matches.append((version_row, snapshot))
            if len(authority_matches) != 1:
                raise ValueError("AI task completed authority is ambiguous")
            authority_version, authority_snapshot = authority_matches[0]
            task_snapshot = authority_snapshot.get("ai_task")
            if (
                authority_version["id"] != authority.authority_version_id
                or sha256_text(authority_version["snapshot_json"])
                != authority.authority_snapshot_sha256
                or not isinstance(task_snapshot, dict)
                or task_snapshot.get("status") != AiTaskStatus.COMPLETED.value
                or task_snapshot.get("task_type")
                != AiTaskType.SUGGEST_RESUME_EDITS.value
            ):
                raise ValueError("AI task completed authority changed")

            artifact_rows = connection.execute(
                "SELECT * FROM ai_artifacts WHERE task_id = ? AND claim_token = ?",
                (authority.task.id, authority.context_artifact.claim_token),
            ).fetchall()
            if {
                row["id"]: database_row_sha256(row) for row in artifact_rows
            } != expected_artifact_signatures:
                raise ValueError("AI task artifact authority changed")
            for artifact_row in artifact_rows:
                self._validate_artifact_row_fast(
                    artifact_row,
                    expected_artifact_file_evidence[artifact_row["id"]],
                )

            finding_rows = connection.execute(
                """
                SELECT * FROM ai_findings
                WHERE task_id = ? AND claim_token = ?
                """,
                (authority.task.id, authority.context_artifact.claim_token),
            ).fetchall()
            if {
                row["id"]: database_row_sha256(row) for row in finding_rows
            } != expected_finding_signatures:
                raise ValueError("AI finding authority changed")

            artifact_manifest = authority_snapshot.get("artifacts")
            manifest_by_id: dict[str, dict[str, str]] = {}
            manifest_by_type: dict[str, dict[str, str]] = {}
            if not isinstance(artifact_manifest, list):
                raise ValueError("AI task artifact authority changed")
            for item in artifact_manifest:
                if (
                    not isinstance(item, dict)
                    or set(item) != {"artifact_type", "id", "sha256"}
                    or type(item.get("artifact_type")) is not str
                    or type(item.get("id")) is not str
                    or type(item.get("sha256")) is not str
                    or item["id"] in manifest_by_id
                    or item["artifact_type"] in manifest_by_type
                ):
                    raise ValueError("AI task artifact authority changed")
                manifest_by_id[item["id"]] = item
                manifest_by_type[item["artifact_type"]] = item
            expected_manifest = {
                row["id"]: {
                    "artifact_type": row["artifact_type"],
                    "id": row["id"],
                    "sha256": row["sha256"],
                }
                for row in artifact_rows
            }
            if (
                manifest_by_id != expected_manifest
                or set(manifest_by_type)
                != {"context", "raw_output", "validated_output"}
            ):
                raise ValueError("AI task artifact authority changed")

            snapshot_finding_ids = authority_snapshot.get("finding_ids")
            side_effect = authority_snapshot.get("side_effect")
            suggestion_ids = (
                side_effect.get("suggestion_finding_ids")
                if isinstance(side_effect, dict)
                else None
            )
            expected_finding_ids = set(expected_finding_signatures)
            if (
                not isinstance(snapshot_finding_ids, list)
                or not isinstance(suggestion_ids, list)
                or any(type(value) is not str for value in snapshot_finding_ids)
                or any(type(value) is not str for value in suggestion_ids)
                or len(set(snapshot_finding_ids)) != len(snapshot_finding_ids)
                or snapshot_finding_ids != suggestion_ids
                or set(snapshot_finding_ids) != expected_finding_ids
            ):
                raise ValueError("AI task finding authority changed")
            provenance = authority_snapshot.get("provenance")
            context_manifest = manifest_by_type["context"]
            if (
                not isinstance(provenance, dict)
                or provenance.get("ai_task_id") != authority.task.id
                or provenance.get("context_sha256") != context_manifest["sha256"]
                or context_manifest["id"] != authority.context_artifact.id
                or context_manifest["sha256"] != authority.context_artifact.sha256
            ):
                raise ValueError("AI task context authority changed")
            evidence_rows = connection.execute(
                """
                SELECT evidence_items.*
                FROM application_evidence_links
                JOIN evidence_items
                  ON evidence_items.id = application_evidence_links.evidence_item_id
                WHERE application_evidence_links.application_id = ?
                """,
                (application_id,),
            ).fetchall()
            if {
                row["id"]: database_row_sha256(row) for row in evidence_rows
            } != expected_evidence_signatures:
                raise ValueError("Application evidence authority changed")
            for evidence_id in referenced_evidence_ids:
                evidence_row = next(
                    (row for row in evidence_rows if row["id"] == evidence_id),
                    None,
                )
                if (
                    evidence_row is None
                    or evidence_row["verification_state"]
                    != VerificationState.VERIFIED.value
                    or evidence_row["privacy_level"] != PrivacyLevel.PUBLIC.value
                ):
                    raise ValueError("Referenced evidence is not publishable")

            profile_rows = connection.execute(
                "SELECT * FROM profile_facts ORDER BY created_at ASC, id ASC"
            ).fetchall()
            if {
                row["id"]: database_row_sha256(row) for row in profile_rows
            } != expected_profile_signatures:
                raise ValueError("Profile fact authority changed")

            parent_row = connection.execute(
                "SELECT * FROM resume_versions WHERE id = ?",
                (authority.parent_resume.id,),
            ).fetchone()
            if (
                parent_row is None
                or database_row_sha256(parent_row) != authority.parent_signature
            ):
                raise ValueError("Parent resume authority changed")
            try:
                parent_path, parent_identity = self._fast_artifact_identity(
                    Path(parent_row["tex_path"])
                )
            except (FileNotFoundError, OSError, ValueError) as exc:
                raise ValueError("Parent resume source changed") from exc
            if (
                parent_path != expected_parent_evidence.path
                or parent_identity != expected_parent_evidence.identity
                or parent_row["tex_sha256"] != expected_parent_evidence.sha256
                or expected_parent_evidence.sha256
                != expected_parent_source_sha256
            ):
                raise ValueError("Parent resume source changed")

            try:
                committed_child_path, committed_child_identity = (
                    self._fast_artifact_identity(tex_path)
                )
            except (FileNotFoundError, OSError, ValueError) as exc:
                raise ValueError(
                    "Resume draft source changed before commit"
                ) from exc
            if (
                committed_child_path != child_evidence.path
                or committed_child_identity != child_evidence.identity
            ):
                raise ValueError("Resume draft source changed before commit")

            connection.execute(
                """
                INSERT INTO resume_versions (
                    id, name, role_family, tex_path, tex_sha256, pdf_path,
                    pdf_sha256, parent_resume_version_id, render_status,
                    parse_status, page_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, 'not_rendered',
                          'not_parsed', NULL, ?, ?)
                """,
                (
                    resume_id,
                    name,
                    parent_row["role_family"],
                    str(child_evidence.path),
                    child_evidence.sha256,
                    parent_row["id"],
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
                {
                    "name": name,
                    "parent_resume_version_id": parent_row["id"],
                    "role_family": parent_row["role_family"],
                    "task_id": authority.task.id,
                    "tex_sha256": child_evidence.sha256,
                },
            )
            invalidated_report_id = application["latest_report_id"]
            invalidated_approval_id = application["approval_id"]
            status = (
                ApplicationStatus.NEEDS_RESUME_UPDATE
                if invalidated_report_id is not None
                else ApplicationStatus(application["status"])
            )
            connection.execute(
                """
                UPDATE applications
                SET selected_resume_version_id = ?, latest_report_id = NULL,
                    approval_id = NULL, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (resume_id, status.value, now, application_id),
            )
            self._write_audit(
                connection,
                "application.resume_selected",
                "application",
                application_id,
                dict(application),
                {
                    "approval_id": None,
                    "latest_report_id": None,
                    "selected_resume_version_id": resume_id,
                    "status": status.value,
                },
            )
            self._insert_application_version(
                connection=connection,
                application_id=application_id,
                event_type="resume_draft.created",
                snapshot={
                    "context_artifact_id": authority.context_artifact.id,
                    "context_sha256": authority.context_artifact.sha256,
                    "finding_ids": [finding.id for finding in authority.findings],
                    "invalidated_approval_id": invalidated_approval_id,
                    "invalidated_report_id": invalidated_report_id,
                    "parent_resume_version_id": parent_row["id"],
                    "resume_version_id": resume_id,
                    "task_id": authority.task.id,
                    "tex_sha256": child_evidence.sha256,
                },
                now=now,
            )
            inserted_row = connection.execute(
                "SELECT * FROM resume_versions WHERE id = ?", (resume_id,)
            ).fetchone()
            if inserted_row is None:
                raise RuntimeError("Created resume draft was not readable")
            inserted_resume = self._resume_from_row(inserted_row)
        if inserted_resume is None:
            raise RuntimeError("Created resume draft was not returned")
        return inserted_resume

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

    def create_and_select_resume_version(
        self,
        *,
        application_id: str,
        name: str,
        role_family: str,
        tex_path: Path,
        tex_sha256: str,
    ) -> ResumeVersion:
        canonical_tex_path, actual_sha256 = self._stable_artifact_digest(tex_path)
        try:
            relative_path = canonical_tex_path.relative_to(
                Path(os.path.realpath(self.vault_path))
            )
        except ValueError as exc:
            raise ValueError("Resume source must remain within the repository vault") from exc
        if not relative_path.parts or relative_path.parts[0] != "resumes":
            raise ValueError("Resume source must be repository-owned")
        if not SHA256_PATTERN.fullmatch(tex_sha256) or actual_sha256 != tex_sha256.lower():
            raise ValueError("Resume source checksum did not match")

        resume_id = new_id("res")
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            application = connection.execute(
                "SELECT * FROM applications WHERE id = ?", (application_id,)
            ).fetchone()
            if application is None:
                raise KeyError("No application with that ID")
            if (
                application["approval_id"] is not None
                or application["status"] == ApplicationStatus.APPROVED.value
                or application["submission_state"]
                != SubmissionState.NOT_SUBMITTED.value
                or application["submitted_at"] is not None
            ):
                raise ValueError(
                    "Cannot upload a resume to an approved or submitted application"
                )
            connection.execute(
                """
                INSERT INTO resume_versions (
                    id, name, role_family, tex_path, tex_sha256, pdf_path, pdf_sha256,
                    parent_resume_version_id, render_status, parse_status, page_count,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, 'not_rendered',
                          'not_parsed', NULL, ?, ?)
                """,
                (
                    resume_id,
                    name,
                    role_family,
                    str(canonical_tex_path),
                    actual_sha256,
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
                {
                    "name": name,
                    "role_family": role_family,
                    "tex_sha256": actual_sha256,
                },
            )
            status = (
                ApplicationStatus.NEEDS_RESUME_UPDATE
                if application["latest_report_id"] is not None
                else ApplicationStatus(application["status"])
            )
            connection.execute(
                """
                UPDATE applications
                SET selected_resume_version_id = ?, latest_report_id = NULL,
                    status = ?, updated_at = ?
                WHERE id = ?
                """,
                (resume_id, status.value, now, application_id),
            )
            self._write_audit(
                connection,
                "application.resume_selected",
                "application",
                application_id,
                dict(application),
                {"selected_resume_version_id": resume_id, "status": status.value},
            )
            resume_row = connection.execute(
                "SELECT * FROM resume_versions WHERE id = ?", (resume_id,)
            ).fetchone()
            if resume_row is None:
                raise RuntimeError("Created resume version was not readable")
            resume = self._resume_from_row(resume_row)
        return resume

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
        if ai_task_id is not None or claim_token is not None:
            raise ValueError("AI reports require the aggregate finalizer")
        self._reject_reserved_report_provenance(fit, risks)
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
            resume = connection.execute(
                "SELECT * FROM resume_versions WHERE id = ?", (resume_version_id,)
            ).fetchone()
            if resume is None:
                raise KeyError("No resume version with that ID")
            connection.execute(
                """
                INSERT INTO reports (
                    id, application_id, resume_version_id, jd_snapshot_id,
                    ai_task_id, pdf_sha256, fit_json, risks_json,
                    blocking_issue_count, warning_count, created_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    application_id,
                    resume_version_id,
                    jd_snapshot_id,
                    resume["pdf_sha256"],
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
            self._prepare_ai_tasks_for_read_only_transition(
                connection, application_id, now
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
            self._prepare_ai_tasks_for_read_only_transition(
                connection, application_id, now
            )
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
    def _application_packet_is_pristine(application: sqlite3.Row) -> bool:
        return not (
            application["approval_id"] is not None
            or application["submission_state"]
            != SubmissionState.NOT_SUBMITTED.value
            or application["submitted_at"] is not None
            or application["status"]
            in {
                ApplicationStatus.APPROVED.value,
                ApplicationStatus.SUBMITTED_MANUAL.value,
            }
        )

    def _prepare_ai_tasks_for_read_only_transition(
        self,
        connection: sqlite3.Connection,
        application_id: str,
        now: str,
    ) -> None:
        running = connection.execute(
            """
            SELECT id FROM ai_tasks
            WHERE application_id = ? AND status = 'running'
            LIMIT 1
            """,
            (application_id,),
        ).fetchone()
        if running is not None:
            raise ValueError("Application has a running AI task")
        pending = connection.execute(
            """
            SELECT * FROM ai_tasks
            WHERE application_id = ? AND status = 'pending'
            ORDER BY created_at ASC, id ASC
            """,
            (application_id,),
        ).fetchall()
        self._cancel_pending_ai_task_rows(connection, pending, now)

    def _cancel_pending_ai_task_rows(
        self,
        connection: sqlite3.Connection,
        tasks: Sequence[sqlite3.Row],
        now: str,
    ) -> None:
        for task in tasks:
            cursor = connection.execute(
                """
                UPDATE ai_tasks
                SET status = 'failed', completed_at = ?, error_summary = 'cancelled',
                    claim_token = NULL, claim_expires_at = NULL, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, now, task["id"]),
            )
            if cursor.rowcount != 1:
                raise ValueError("AI task is not pending")
            self._write_audit(
                connection,
                "ai_task.failed",
                "ai_task",
                task["id"],
                dict(task),
                {
                    "status": "failed",
                    "completed_at": now,
                    "error_summary": "cancelled",
                },
            )

    @classmethod
    def _require_pristine_application_packet(cls, application: sqlite3.Row) -> None:
        if not cls._application_packet_is_pristine(application):
            raise ValueError(
                "AI task cannot replace approved or submitted application state"
            )

    @staticmethod
    def _reserved_report_fields(value: Mapping[str, Any]) -> set[str]:
        reserved = set(value).intersection(REPORT_AI_AUTHORITY_FIELDS)
        if "provenance" in value:
            reserved.add("provenance")
        return reserved

    @classmethod
    def _contains_reserved_provenance(
        cls, value: Any, *, allow_root: bool = False
    ) -> bool:
        if isinstance(value, Mapping):
            if "provenance" in value and not allow_root:
                return True
            return any(
                cls._contains_reserved_provenance(inner)
                for key, inner in value.items()
                if not (allow_root and key == "provenance")
            )
        if isinstance(value, (list, tuple)):
            return any(cls._contains_reserved_provenance(inner) for inner in value)
        return False

    @classmethod
    def _reject_reserved_report_provenance(
        cls, fit: Mapping[str, Any], risks: Mapping[str, Any]
    ) -> None:
        if (
            cls._reserved_report_fields(fit)
            or cls._reserved_report_fields(risks)
            or cls._contains_reserved_provenance(fit)
            or cls._contains_reserved_provenance(risks)
        ):
            raise ValueError("Report contains reserved AI provenance fields")

    def _require_report_provenance_authority(
        self, connection: sqlite3.Connection, report: sqlite3.Row
    ) -> None:
        try:
            fit = json.loads(report["fit_json"])
            risks = json.loads(report["risks_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Report payload is invalid") from exc
        if not isinstance(fit, dict) or not isinstance(risks, dict):
            raise ValueError("Report payload is invalid")
        direct_fields = (
            set(fit).intersection(REPORT_AI_AUTHORITY_FIELDS)
            | set(risks).intersection(REPORT_AI_AUTHORITY_FIELDS)
        )
        if direct_fields:
            raise ValueError("Report contains reserved AI provenance fields")
        if self._contains_reserved_provenance(
            fit, allow_root=True
        ) or self._contains_reserved_provenance(risks, allow_root=True):
            raise ValueError("Report contains reserved AI provenance fields")
        fit_provenance = fit.get("provenance")
        risks_provenance = risks.get("provenance")
        if fit_provenance is None and risks_provenance is None:
            raise ValueError(
                "AI review is required for the current application report"
            )
        if (
            not isinstance(fit_provenance, dict)
            or fit_provenance != risks_provenance
        ):
            raise ValueError("Report contains untrusted reserved AI provenance")
        task_id = report["ai_task_id"]
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("Report contains untrusted reserved AI provenance")
        if set(fit_provenance) != REPORT_AI_AUTHORITY_FIELDS:
            raise ValueError("Report contains untrusted reserved AI provenance")
        if fit_provenance.get("ai_task_id") != task_id:
            raise ValueError("AI review report task binding does not match")

        task = connection.execute(
            "SELECT * FROM ai_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if (
            task is None
            or task["application_id"] != report["application_id"]
            or task["task_type"] != AiTaskType.ANALYZE_FIT.value
            or task["status"] != AiTaskStatus.COMPLETED.value
            or not task["completed_at"]
        ):
            raise ValueError("AI review task is not current and completed")
        provider = connection.execute(
            "SELECT * FROM ai_provider_configs WHERE id = ?",
            (task["provider_config_id"],),
        ).fetchone()
        if provider is None or not provider["enabled"]:
            raise ValueError("AI review provider authority is unavailable")
        try:
            task_binding, task_binding_json, task_binding_sha256 = (
                canonical_provider_binding(json.loads(task["provider_binding_json"]))
            )
            provider_binding, provider_binding_json, provider_binding_sha256 = (
                canonical_provider_binding(
                    json.loads(provider["availability_binding_json"])
                )
            )
        except (TypeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("AI review provider authority is invalid") from exc
        if (
            task_binding_json != task["provider_binding_json"]
            or task_binding_sha256 != task["provider_binding_sha256"]
            or provider_binding_json != provider["availability_binding_json"]
            or provider_binding_sha256
            != provider["availability_binding_sha256"]
            or task_binding != provider_binding
            or task_binding_sha256 != provider_binding_sha256
            or task["provider_config_id"] != provider["id"]
            or task["privacy_mode"] != provider["privacy_mode"]
        ):
            raise ValueError("AI review provider binding is stale")
        if task_binding["is_ai"] is not True:
            raise ValueError("AI review must use an AI provider")

        versions = connection.execute(
            """
            SELECT snapshot_json
            FROM application_versions
            WHERE application_id = ? AND event_type = 'ai_task.completed'
            ORDER BY version_number DESC
            """,
            (report["application_id"],),
        ).fetchall()
        authority_snapshots: list[dict[str, Any]] = []
        for version in versions:
            try:
                snapshot = json.loads(version["snapshot_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                isinstance(snapshot, dict)
                and isinstance(snapshot.get("ai_task"), dict)
                and snapshot["ai_task"].get("id") == task_id
                and snapshot.get("provenance") == fit_provenance
                and isinstance(snapshot.get("side_effect"), dict)
                and snapshot["side_effect"].get("report_id") == report["id"]
            ):
                authority_snapshots.append(snapshot)
        if len(authority_snapshots) != 1:
            raise ValueError("AI review report authority is missing or ambiguous")
        authority = authority_snapshots[0]
        task_snapshot = authority["ai_task"]
        try:
            declared_input_ids = json.loads(task["input_artifact_ids_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("AI review task input authority is invalid") from exc
        normalized_input_ids = self._ai_task_input_artifact_ids(connection, task_id)
        if (
            declared_input_ids != normalized_input_ids
            or task_snapshot.get("input_artifact_ids") != normalized_input_ids
            or task_snapshot.get("provider_config_id") != provider["id"]
            or task_snapshot.get("status") != AiTaskStatus.COMPLETED.value
            or task_snapshot.get("task_type") != AiTaskType.ANALYZE_FIT.value
        ):
            raise ValueError("AI review task authority is invalid")

        manifest = authority.get("artifacts")
        if not isinstance(manifest, list):
            raise ValueError("AI review artifact authority is invalid")
        manifest_by_id: dict[str, dict[str, str]] = {}
        manifest_by_type: dict[str, dict[str, str]] = {}
        for item in manifest:
            if (
                not isinstance(item, dict)
                or set(item) != {"artifact_type", "id", "sha256"}
                or any(type(item.get(field)) is not str for field in item)
                or item["id"] in manifest_by_id
                or item["artifact_type"] in manifest_by_type
            ):
                raise ValueError("AI review artifact authority is invalid")
            manifest_by_id[item["id"]] = item
            manifest_by_type[item["artifact_type"]] = item
        if set(manifest_by_type) != {
            "context",
            "raw_output",
            "validated_output",
        }:
            raise ValueError("AI review artifact authority is incomplete")
        placeholders = ", ".join("?" for _ in manifest_by_id)
        artifact_rows = connection.execute(
            f"SELECT * FROM ai_artifacts WHERE id IN ({placeholders})",
            tuple(manifest_by_id),
        ).fetchall()
        if len(artifact_rows) != len(manifest_by_id):
            raise ValueError("AI review artifact authority is missing")
        artifact_by_id = {row["id"]: row for row in artifact_rows}
        context_row = artifact_by_id[manifest_by_type["context"]["id"]]
        claim_token = context_row["claim_token"]
        if not isinstance(claim_token, str) or not claim_token:
            raise ValueError("AI review attempt authority is invalid")
        for artifact_id, manifest_item in manifest_by_id.items():
            artifact = artifact_by_id[artifact_id]
            if (
                artifact["task_id"] != task_id
                or artifact["claim_token"] != claim_token
                or artifact["artifact_type"] != manifest_item["artifact_type"]
                or artifact["sha256"] != manifest_item["sha256"]
            ):
                raise ValueError("AI review artifact authority changed")
            self._validate_artifact_row(artifact)
        attempt_artifacts = connection.execute(
            "SELECT id FROM ai_artifacts WHERE task_id = ? AND claim_token = ?",
            (task_id, claim_token),
        ).fetchall()
        if {row["id"] for row in attempt_artifacts} != set(manifest_by_id):
            raise ValueError("AI review attempt artifact authority is ambiguous")

        context_buffer = BytesIO()
        context_evidence = self._stable_artifact_read(
            Path(context_row["path"]), context_buffer
        )
        if context_evidence.sha256 != context_row["sha256"]:
            raise ValueError("AI review context checksum changed")
        try:
            context = json.loads(context_buffer.getvalue())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("AI review context is invalid") from exc
        if (
            not isinstance(context, dict)
            or context.get("task_type") != AiTaskType.ANALYZE_FIT.value
            or not isinstance(context.get("job_description"), dict)
            or context["job_description"].get("snapshot_id")
            != report["jd_snapshot_id"]
            or not isinstance(context.get("selected_resume"), dict)
            or context["selected_resume"].get("version_id")
            != report["resume_version_id"]
        ):
            raise ValueError("AI review context does not match the report")

        finding_ids = authority.get("finding_ids")
        if (
            not isinstance(finding_ids, list)
            or any(type(finding_id) is not str for finding_id in finding_ids)
            or len(set(finding_ids)) != len(finding_ids)
        ):
            raise ValueError("AI review finding authority is invalid")
        finding_rows = connection.execute(
            "SELECT * FROM ai_findings WHERE task_id = ? AND claim_token = ?",
            (task_id, claim_token),
        ).fetchall()
        if {row["id"] for row in finding_rows} != set(finding_ids):
            raise ValueError("AI review finding authority changed")
        if any(
            row["severity"] == FindingSeverity.BLOCKING.value
            or row["verifier_state"]
            in {
                VerifierState.BLOCKED.value,
                VerifierState.NEEDS_EVIDENCE.value,
                VerifierState.REJECTED.value,
            }
            for row in finding_rows
        ):
            raise ValueError("AI review has verifier blockers")

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
            "provider_binding_sha256": task_binding_sha256,
            "provider_config_id": provider["id"],
            "provider_identity_name": provider["identity_name"],
            "provider_kind": provider["provider_kind"],
            "provider_model": provider["model"],
        }
        if fit_provenance != expected_provenance:
            raise ValueError("AI review provenance no longer matches repository authority")

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
        self._require_report_provenance_authority(connection, report)
        self._require_report_risk_counts(report)
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
        if report["pdf_sha256"] != resume["pdf_sha256"]:
            raise ValueError("AI review does not match the current resume PDF")
        self._verify_resume_source_row(resume)
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
        self._require_report_provenance_authority(connection, report)
        self._require_report_risk_counts(report)
        if report["blocking_issue_count"]:
            raise ValueError("Approved application report has blocking risks")
        if report["pdf_sha256"] != resume["pdf_sha256"]:
            raise ValueError("AI review does not match the current resume PDF")
        self._verify_resume_source_row(resume)
        self._verify_resume_pdf_row(resume)
        return application

    @staticmethod
    def _require_report_risk_counts(report: sqlite3.Row) -> None:
        try:
            risks = json.loads(report["risks_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Report risks are invalid") from exc
        blockers = risks.get("blockers") if isinstance(risks, dict) else None
        warnings = risks.get("warnings") if isinstance(risks, dict) else None
        if (
            not isinstance(blockers, list)
            or not isinstance(warnings, list)
            or len(blockers) != report["blocking_issue_count"]
            or len(warnings) != report["warning_count"]
        ):
            raise ValueError("Report risk counts are invalid")

    def _verify_resume_source_row(self, resume: sqlite3.Row) -> None:
        source_path = resume["tex_path"]
        expected_sha256 = resume["tex_sha256"]
        if (
            not source_path
            or not expected_sha256
            or not SHA256_PATTERN.fullmatch(expected_sha256)
        ):
            raise ValueError("Selected resume source integrity verification failed")
        try:
            _path, actual_sha256 = self._stable_artifact_digest(Path(source_path))
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise ValueError(
                "Selected resume source integrity verification failed"
            ) from exc
        if actual_sha256 != expected_sha256.lower():
            raise ValueError("Selected resume source integrity verification failed")

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

    def get_application_packet_snapshot(
        self, application_id: str
    ) -> ApplicationPacketSnapshot:
        with self.connection() as connection:
            connection.execute("BEGIN")
            application_row = connection.execute(
                "SELECT * FROM applications WHERE id = ?", (application_id,)
            ).fetchone()
            if application_row is None:
                raise KeyError("No application with that ID")
            application = self._application_from_row(application_row)

            jd_row = connection.execute(
                "SELECT * FROM jd_snapshots WHERE id = ?",
                (application.jd_snapshot_id,),
            ).fetchone()
            if jd_row is None or jd_row["application_id"] != application.id:
                raise ValueError("Application job description ownership is invalid")
            try:
                requirements = json.loads(jd_row["extracted_requirements_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("Stored requirements are invalid") from exc
            job_description = self._jd_snapshot_from_row(
                jd_row, parsed_requirements=requirements
            )

            selected_resume = None
            resume_row = None
            if application.selected_resume_version_id is not None:
                resume_row = connection.execute(
                    "SELECT * FROM resume_versions WHERE id = ?",
                    (application.selected_resume_version_id,),
                ).fetchone()
                if resume_row is None:
                    raise ValueError("Selected resume is missing")
                selected_resume = self._resume_from_row(resume_row)

            latest_report = None
            report_row = None
            if application.latest_report_id is not None:
                report_row = connection.execute(
                    "SELECT * FROM reports WHERE id = ?",
                    (application.latest_report_id,),
                ).fetchone()
                if report_row is None:
                    raise ValueError("Latest report is missing")
                latest_report = Report(
                    id=report_row["id"],
                    application_id=report_row["application_id"],
                    resume_version_id=report_row["resume_version_id"],
                    jd_snapshot_id=report_row["jd_snapshot_id"],
                    ai_task_id=report_row["ai_task_id"],
                    pdf_sha256=report_row["pdf_sha256"],
                    fit=json.loads(report_row["fit_json"]),
                    risks=json.loads(report_row["risks_json"]),
                    blocking_issue_count=report_row["blocking_issue_count"],
                    warning_count=report_row["warning_count"],
                    created_at=report_row["created_at"],
                )
                if (
                    latest_report.application_id != application.id
                    or latest_report.resume_version_id
                    != application.selected_resume_version_id
                    or latest_report.jd_snapshot_id != application.jd_snapshot_id
                ):
                    raise ValueError("Latest report ownership is invalid")

            approval = None
            if application.approval_id is not None:
                approval_row = connection.execute(
                    "SELECT * FROM approvals WHERE id = ?", (application.approval_id,)
                ).fetchone()
                if approval_row is None:
                    raise ValueError("Approval is missing")
                approval = Approval(
                    id=approval_row["id"],
                    application_id=approval_row["application_id"],
                    resume_version_id=approval_row["resume_version_id"],
                    report_id=approval_row["report_id"],
                    pdf_sha256=approval_row["pdf_sha256"],
                    approved_at=approval_row["approved_at"],
                    approval_note=approval_row["approval_note"],
                )
                if (
                    approval.application_id != application.id
                    or approval.resume_version_id
                    != application.selected_resume_version_id
                    or approval.report_id != application.latest_report_id
                    or selected_resume is None
                    or approval.pdf_sha256 != selected_resume.pdf_sha256
                ):
                    raise ValueError("Approval ownership is invalid")

            task_row = connection.execute(
                """
                SELECT * FROM ai_tasks
                WHERE application_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (application.id,),
            ).fetchone()
            latest_ai_task = None
            if task_row is not None:
                artifact_rows = connection.execute(
                    """
                    SELECT links.artifact_id, source_tasks.application_id
                    FROM ai_task_input_artifacts AS links
                    LEFT JOIN ai_artifacts AS artifacts
                      ON artifacts.id = links.artifact_id
                    LEFT JOIN ai_tasks AS source_tasks
                      ON source_tasks.id = artifacts.task_id
                    WHERE links.task_id = ?
                    ORDER BY links.ordinal ASC
                    """,
                    (task_row["id"],),
                ).fetchall()
                if any(
                    row["application_id"] != application.id for row in artifact_rows
                ):
                    raise ValueError("AI task input ownership is invalid")
                linked_artifact_ids = [row["artifact_id"] for row in artifact_rows]
                try:
                    declared_artifact_ids = json.loads(
                        task_row["input_artifact_ids_json"]
                    )
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError("AI task input manifest is invalid") from exc
                if declared_artifact_ids != linked_artifact_ids:
                    raise ValueError("AI task input manifest is invalid")
                latest_ai_task = self._ai_task_from_row(
                    task_row, linked_artifact_ids
                )

            evidence_rows = connection.execute(
                """
                SELECT evidence_items.*
                FROM application_evidence_links
                JOIN evidence_items
                  ON evidence_items.id = application_evidence_links.evidence_item_id
                WHERE application_evidence_links.application_id = ?
                ORDER BY application_evidence_links.created_at ASC,
                         evidence_items.id ASC
                """,
                (application.id,),
            ).fetchall()
            version_rows = connection.execute(
                """
                SELECT * FROM application_versions
                WHERE application_id = ?
                ORDER BY version_number ASC
                """,
                (application.id,),
            ).fetchall()
            packet = ApplicationPacketSnapshot(
                application=application,
                job_description=job_description,
                selected_resume=selected_resume,
                latest_report=latest_report,
                approval=approval,
                latest_ai_task=latest_ai_task,
                evidence=[
                    self._evidence_item_from_row(row) for row in evidence_rows
                ],
                versions=[
                    self._application_version_from_row(row) for row in version_rows
                ],
                approval_eligibility=self._approval_eligibility(
                    connection, application_row, report_row, resume_row
                ),
            )
        return packet

    def _approval_eligibility(
        self,
        connection: sqlite3.Connection,
        application: sqlite3.Row,
        report: sqlite3.Row | None,
        resume: sqlite3.Row | None,
    ) -> ApprovalEligibility:
        task_id = report["ai_task_id"] if report is not None else None
        if application["approval_id"] is not None:
            return ApprovalEligibility(False, "Packet is already approved.", task_id)
        default_reason = (
            "Run AI review for the current JD and resume before approval."
        )
        if report is None or resume is None:
            return ApprovalEligibility(False, default_reason, task_id)
        try:
            self._require_report_provenance_authority(connection, report)
            self._require_report_risk_counts(report)
            if report["blocking_issue_count"]:
                return ApprovalEligibility(
                    False,
                    "Resolve report blockers and run AI review again before approval.",
                    task_id,
                )
            if (
                application["latest_report_id"] != report["id"]
                or application["selected_resume_version_id"]
                != report["resume_version_id"]
                or application["jd_snapshot_id"] != report["jd_snapshot_id"]
                or report["pdf_sha256"] != resume["pdf_sha256"]
            ):
                raise ValueError("AI review packet binding is stale")
            self._verify_resume_source_row(resume)
            self._verify_resume_pdf_row(resume)
        except (KeyError, OSError, TypeError, ValueError):
            if isinstance(task_id, str) and task_id:
                provider_row = connection.execute(
                    """
                    SELECT config.availability_binding_json
                    FROM ai_tasks AS task
                    JOIN ai_provider_configs AS config
                      ON config.id = task.provider_config_id
                    WHERE task.id = ?
                    """,
                    (task_id,),
                ).fetchone()
                if provider_row is not None:
                    try:
                        binding = json.loads(
                            provider_row["availability_binding_json"]
                        )
                    except (TypeError, json.JSONDecodeError):
                        binding = None
                    if isinstance(binding, dict) and binding.get("is_ai") is False:
                        return ApprovalEligibility(
                            False,
                            "Verifier-only results are Not AI. Run AI review with an AI provider.",
                            task_id,
                        )
            return ApprovalEligibility(False, default_reason, task_id)
        return ApprovalEligibility(True, "AI review is current and complete.", task_id)

    def list_applications(self) -> list[Application]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM applications ORDER BY created_at DESC").fetchall()
        return [self._application_from_row(row) for row in rows]

    def get_jd_snapshot(self, snapshot_id: str) -> JobDescriptionSnapshot:
        row = self._one("SELECT * FROM jd_snapshots WHERE id = ?", (snapshot_id,))
        return self._jd_snapshot_from_row(row)

    @staticmethod
    def _jd_snapshot_from_row(
        row: sqlite3.Row,
        *,
        parsed_requirements: Any | None = None,
    ) -> JobDescriptionSnapshot:
        requirements = (
            json.loads(row["extracted_requirements_json"])
            if parsed_requirements is None
            else parsed_requirements
        )
        return JobDescriptionSnapshot(
            id=row["id"],
            application_id=row["application_id"],
            source_url=row["source_url"],
            raw_text_path=Path(row["raw_text_path"]),
            raw_text_sha256=row["raw_text_sha256"],
            captured_at=row["captured_at"],
            extracted_requirements_json=requirements,
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
            ai_task_id=row["ai_task_id"],
            pdf_sha256=row["pdf_sha256"],
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
        evidence = self._stable_artifact_read(path)
        return evidence.path, evidence.sha256

    def _stable_artifact_read(
        self, path: Path, destination: BinaryIO | None = None
    ) -> _ArtifactFileEvidence:
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
                if destination is not None:
                    destination.write(chunk)
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
        return _ArtifactFileEvidence(canonical_path, digest.hexdigest(), identity)

    def _fast_artifact_identity(
        self, path: Path
    ) -> tuple[Path, tuple[int, int, int, int, int]]:
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
            path_stat = current.lstat()
        except FileNotFoundError as exc:
            raise FileNotFoundError("Repository vault does not exist") from exc
        if stat.S_ISLNK(path_stat.st_mode):
            raise ValueError("Artifact path must be a regular vault file")
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
        identity = (
            path_stat.st_dev,
            path_stat.st_ino,
            path_stat.st_size,
            path_stat.st_mtime_ns,
            path_stat.st_nlink,
        )
        return canonical_path, identity

    def _materialize_ai_artifact(
        self,
        *,
        task_id: str,
        artifact_id: str,
        artifact_type: str,
        source_path: Path,
        expected_sha256: str,
    ) -> tuple[Path, _ArtifactFileEvidence]:
        source_candidate = Path(os.path.abspath(source_path))
        self._require_ai_task_source_path(task_id, source_candidate)
        artifact_path = self._ai_artifact_storage_path(
            task_id, artifact_id, artifact_type
        )
        evidence = self._publish_ai_artifact_copy(
            source_path=source_path,
            artifact_path=artifact_path,
            expected_sha256=expected_sha256,
            allow_existing=False,
        )
        self._require_ai_task_source_path(task_id, source_candidate)
        self._require_ai_artifact_path(task_id, artifact_id, evidence.path)
        return evidence.path, evidence

    def _ai_artifact_storage_path(
        self, task_id: str, artifact_id: str, artifact_type: str
    ) -> Path:
        suffix = ".txt" if artifact_type == "draft_text" else ".json"
        return (
            self.vault_path
            / "ai-artifacts"
            / task_id
            / artifact_id
            / f"{artifact_type}{suffix}"
        )

    def _publish_ai_artifact_copy(
        self,
        *,
        source_path: Path,
        artifact_path: Path,
        expected_sha256: str,
        allow_existing: bool,
    ) -> _ArtifactFileEvidence:
        artifacts_root = self.vault_path / "ai-artifacts"
        task_root = artifact_path.parent.parent
        artifact_root = artifact_path.parent
        artifacts_root.mkdir(exist_ok=True)
        artifacts_root_stat = artifacts_root.lstat()
        if stat.S_ISLNK(artifacts_root_stat.st_mode) or not stat.S_ISDIR(
            artifacts_root_stat.st_mode
        ):
            raise ValueError("AI artifact repository path must be a directory")
        task_root.mkdir(exist_ok=True)
        task_root_stat = task_root.lstat()
        if stat.S_ISLNK(task_root_stat.st_mode) or not stat.S_ISDIR(
            task_root_stat.st_mode
        ):
            raise ValueError("AI artifact repository path must be a directory")
        artifact_root.mkdir(exist_ok=allow_existing)
        artifact_root_stat = artifact_root.lstat()
        if stat.S_ISLNK(artifact_root_stat.st_mode) or not stat.S_ISDIR(
            artifact_root_stat.st_mode
        ):
            raise ValueError("AI artifact repository path must be a directory")
        temporary_path = artifact_root / f".{artifact_path.stem}-{uuid4().hex}.tmp"
        published = False
        try:
            if artifact_path.exists():
                if not allow_existing:
                    raise FileExistsError("AI artifact target already exists")
            else:
                with temporary_path.open("xb") as destination:
                    source_evidence = self._stable_artifact_read(
                        source_path, destination
                    )
                    if source_evidence.sha256 != expected_sha256:
                        raise ValueError(
                            "Artifact checksum does not match file bytes"
                        )
                    destination.flush()
                    os.fsync(destination.fileno())
                os.replace(temporary_path, artifact_path)
                published = True
            copied_path, copied_sha256 = self._stable_artifact_digest(
                artifact_path
            )
            copied_path, copied_identity = self._fast_artifact_identity(
                copied_path
            )
            if copied_sha256 != expected_sha256:
                raise ValueError(
                    "Artifact copy checksum does not match source bytes"
                )
            self._fsync_directory(artifact_root)
            self._fsync_directory(task_root)
            self._fsync_directory(artifacts_root)
            evidence = _ArtifactFileEvidence(
                copied_path, copied_sha256, copied_identity
            )
            return evidence
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            if published or not allow_existing:
                artifact_path.unlink(missing_ok=True)
                try:
                    artifact_root.rmdir()
                except OSError:
                    pass
            raise

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if not DIRECTORY_FSYNC_SUPPORTED:
            return
        descriptor = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _remove_materialized_ai_artifact(self, artifact_path: Path) -> None:
        try:
            relative = artifact_path.relative_to(
                Path(os.path.abspath(self.vault_path))
            )
        except ValueError:
            return
        if len(relative.parts) != 4 or relative.parts[0] != "ai-artifacts":
            return
        artifact_path.unlink(missing_ok=True)
        try:
            artifact_path.parent.rmdir()
        except OSError:
            pass

    @staticmethod
    def _artifact_row_signature(row: sqlite3.Row) -> tuple[Any, ...]:
        return tuple(
            row[field]
            for field in (
                "id",
                "task_id",
                "claim_token",
                "artifact_type",
                "path",
                "sha256",
                "schema_version",
                "storage_version",
                "created_at",
            )
        )

    def _preflight_ai_artifacts(
        self, task_id: str, claim_token: str
    ) -> dict[str, _ArtifactPreflight]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM ai_artifacts
                WHERE task_id = ? AND claim_token = ?
                ORDER BY created_at ASC, id ASC
                """,
                (task_id, claim_token),
            ).fetchall()
        preflight: dict[str, _ArtifactPreflight] = {}
        for row in rows:
            evidence = self._artifact_evidence_for_row(row)
            preflight[row["id"]] = _ArtifactPreflight(
                self._artifact_row_signature(row), evidence
            )
        return preflight

    def _artifact_evidence_for_row(
        self, row: sqlite3.Row
    ) -> _ArtifactFileEvidence:
        if row["storage_version"] != AI_ARTIFACT_STORAGE_VERSION:
            raise ValueError("AI artifact storage version is invalid")
        self._validate_ai_artifact_metadata(
            row["artifact_type"], row["schema_version"]
        )
        try:
            artifact_path, actual_sha256 = self._stable_artifact_digest(
                Path(row["path"])
            )
            artifact_path, identity = self._fast_artifact_identity(artifact_path)
            self._require_ai_artifact_path(
                row["task_id"], row["id"], artifact_path
            )
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise ValueError("AI artifact integrity verification failed") from exc
        if str(artifact_path) != row["path"] or actual_sha256 != row["sha256"]:
            raise ValueError("AI artifact integrity verification failed")
        return _ArtifactFileEvidence(artifact_path, actual_sha256, identity)

    def _validate_artifact_row(self, row: sqlite3.Row) -> None:
        self._artifact_evidence_for_row(row)

    def _validate_artifact_row_fast(
        self, row: sqlite3.Row, evidence: _ArtifactFileEvidence
    ) -> None:
        try:
            self._validate_ai_artifact_metadata(
                row["artifact_type"], row["schema_version"]
            )
            artifact_path, identity = self._fast_artifact_identity(Path(row["path"]))
            self._require_ai_artifact_path(
                row["task_id"], row["id"], artifact_path
            )
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise ValueError("AI artifact integrity verification failed") from exc
        if (
            str(artifact_path) != row["path"]
            or artifact_path != evidence.path
            or row["sha256"] != evidence.sha256
            or identity != evidence.identity
        ):
            raise ValueError("AI artifact integrity verification failed")

    @staticmethod
    def _validate_ai_artifact_metadata(
        artifact_type: str, schema_version: str
    ) -> None:
        if (
            artifact_type not in AI_ARTIFACT_TYPES
            or schema_version not in AI_ARTIFACT_SCHEMA_VERSIONS
        ):
            raise ValueError("AI artifact metadata was invalid")

    def _require_ai_task_source_path(
        self, task_id: str, artifact_path: Path
    ) -> None:
        vault_root = Path(os.path.abspath(self.vault_path))
        try:
            relative = artifact_path.relative_to(vault_root)
        except ValueError as exc:
            raise ValueError(
                "AI artifact source path must be under its vault task attempt"
            ) from exc
        parts = relative.parts
        if (
            len(parts) < 4
            or parts[0] != "ai-tasks"
            or parts[1] != task_id
            or not parts[2].startswith("attempt-")
            or len(parts[2]) == len("attempt-")
        ):
            raise ValueError(
                "AI artifact source path must be under its vault task attempt"
            )

    def _require_ai_artifact_path(
        self, task_id: str, artifact_id: str, artifact_path: Path
    ) -> None:
        vault_root = Path(os.path.abspath(self.vault_path))
        try:
            relative = artifact_path.relative_to(vault_root)
        except ValueError as exc:
            raise ValueError("AI artifact path must be repository-owned") from exc
        parts = relative.parts
        if (
            len(parts) != 4
            or parts[0] != "ai-artifacts"
            or parts[1] != task_id
            or parts[2] != artifact_id
            or not parts[3]
        ):
            raise ValueError("AI artifact path must be repository-owned")

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
