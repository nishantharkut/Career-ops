from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
import gc
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier, Event
import time
from types import SimpleNamespace
import warnings

import pytest

from careerops.core.database import connect_database, initialize_database
from careerops.core.migrations import LATEST_SCHEMA_VERSION, MIGRATIONS
from careerops.core.repository import CareerOpsRepository
from careerops.ai.context import load_prompt_template


def make_repo(tmp_path: Path) -> CareerOpsRepository:
    repo = CareerOpsRepository(tmp_path / "careerops.db", tmp_path / "vault")
    repo.initialize()
    return repo


def test_repository_rejects_claim_lease_below_sqlite_contention_floor(
    tmp_path: Path,
):
    with pytest.raises(ValueError, match="at least 15 seconds"):
        CareerOpsRepository(
            tmp_path / "careerops.db",
            tmp_path / "vault",
            claim_lease_seconds=14.999,
        )

    repository = CareerOpsRepository(
        tmp_path / "careerops.db",
        tmp_path / "vault",
        claim_lease_seconds=15,
    )

    assert repository.claim_lease_seconds == 15


@pytest.mark.parametrize(
    "claim_lease_seconds", [float("nan"), float("inf"), float("-inf")]
)
def test_repository_rejects_non_finite_claim_lease(
    tmp_path: Path, claim_lease_seconds: float
):
    with pytest.raises(ValueError, match="finite and at least 15 seconds"):
        CareerOpsRepository(
            tmp_path / "careerops.db",
            tmp_path / "vault",
            claim_lease_seconds=claim_lease_seconds,
        )


def create_legacy_ai_database(database_path: Path, version: int) -> None:
    fixture_dir = Path(__file__).parent / "fixtures"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.executescript(
            (fixture_dir / "v0_schema.sql").read_text(encoding="utf-8")
        )
        connection.executescript(
            (fixture_dir / f"ai_schema_v{version}.sql").read_text(encoding="utf-8")
        )
        connection.commit()


def promote_to_f32e429_v11(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        ALTER TABLE ai_provider_configs
        ADD COLUMN identity_name TEXT NOT NULL DEFAULT '';

        CREATE TRIGGER validate_codex_identity_insert
        BEFORE INSERT ON ai_provider_configs
        WHEN NEW.provider_kind = 'codex'
         AND (
            NEW.identity_name NOT IN ('codex2', 'codex')
            OR NEW.endpoint != ('codex-identity://' || NEW.identity_name)
         )
        BEGIN
            SELECT RAISE(ABORT, 'invalid Codex identity binding');
        END;

        CREATE TRIGGER validate_codex_identity_update
        BEFORE UPDATE ON ai_provider_configs
        WHEN NEW.provider_kind = 'codex'
         AND (
            NEW.identity_name NOT IN ('codex2', 'codex')
            OR NEW.endpoint != ('codex-identity://' || NEW.identity_name)
         )
        BEGIN
            SELECT RAISE(ABORT, 'invalid Codex identity binding');
        END;

        CREATE TRIGGER validate_provider_binding_immutable
        BEFORE UPDATE ON ai_provider_configs
        WHEN NEW.provider_kind != OLD.provider_kind
          OR NEW.endpoint != OLD.endpoint
          OR NEW.model != OLD.model
          OR NEW.identity_name != OLD.identity_name
        BEGIN
            SELECT RAISE(ABORT, 'provider configuration binding is immutable');
        END;

        PRAGMA user_version = 11;
        """
    )


def create_v14_legacy_artifact_database(
    database_path: Path,
    vault_path: Path,
    *,
    source_bytes: bytes = b"legacy context",
    stored_sha256: str | None = None,
    create_source: bool = True,
) -> tuple[str, str, Path]:
    create_legacy_ai_database(database_path, 12)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        for version, migration in MIGRATIONS:
            if version in {13, 14}:
                migration(connection)
        connection.execute("PRAGMA user_version = 14")
        provider_id = "aip_v14_artifact"
        task_id = "ait_v14_artifact"
        artifact_id = "aia_v14_artifact"
        connection.execute(
            """
            INSERT INTO ai_provider_configs (
                id, provider_kind, display_name, identity_name, enabled,
                privacy_mode, endpoint, model, availability_binding_json,
                availability_binding_sha256, created_at, updated_at
            ) VALUES (?, 'mock', 'Legacy artifact provider', '', 0,
                      'local_private', '', 'fixture-v1', '{}', '', ?, ?)
            """,
            (
                provider_id,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO ai_tasks (
                id, application_id, task_type, provider_config_id,
                prompt_template_id, prompt_sha256, input_artifact_ids_json,
                status, privacy_mode, provider_binding_json,
                provider_binding_sha256, started_at, claim_expires_at,
                invocation_started_at, completed_at, error_summary,
                claim_token, created_at, updated_at
            ) VALUES (?, 'app_v0', 'analyze_jd', ?, 'analyze_jd_v1', ?, '[]',
                      'completed', 'local_private', '{}', '', ?, NULL, ?, ?, '',
                      'legacy-claim', ?, ?)
            """,
            (
                task_id,
                provider_id,
                "a" * 64,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:01+00:00",
                "2026-01-01T00:00:02+00:00",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:02+00:00",
            ),
        )
        source_path = (
            vault_path
            / "ai-tasks"
            / task_id
            / "attempt-legacy"
            / "context.json"
        )
        if create_source:
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_bytes(source_bytes)
        connection.execute(
            """
            INSERT INTO ai_artifacts (
                id, task_id, claim_token, artifact_type, path, sha256,
                schema_version, created_at
            ) VALUES (?, ?, 'legacy-claim', 'context', ?, ?, '1', ?)
            """,
            (
                artifact_id,
                task_id,
                str(source_path.resolve()),
                stored_sha256 or hashlib.sha256(source_bytes).hexdigest(),
                "2026-01-01T00:00:01+00:00",
            ),
        )
        connection.commit()
    return task_id, artifact_id, source_path.resolve()


@pytest.mark.parametrize("version", [9, 10, 11, 12])
def test_complete_historical_ai_schema_fixture_upgrades_and_remains_writable(
    tmp_path: Path, version: int
):
    database_path = tmp_path / f"careerops-v{version}.db"
    create_legacy_ai_database(database_path, version)
    repo = CareerOpsRepository(database_path, tmp_path / f"vault-v{version}")
    repo.initialize()

    application = create_application(repo)
    fact = repo.create_profile_fact(
        fact_key="skills.python",
        fact_value="Python",
        verification_state="verified",
        privacy_level="public",
    )
    evidence = repo.create_evidence_item(
        claim_text="Built a Python service",
        evidence_type="project",
        source_path=None,
        source_url="",
        source_sha256=None,
        verification_state="verified",
        privacy_level="public",
    )
    repo.link_application_evidence(application.id, evidence.id)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)

    assert repo.get_profile_fact(fact.id) == fact
    assert repo.list_application_evidence(application.id) == [evidence]
    assert repo.get_ai_task(task.id).prompt_sha256 == load_prompt_template(
        "analyze_fit"
    ).sha256


def create_application(repo: CareerOpsRepository):
    return repo.create_application(
        company="Example Corp",
        role_title="Software Engineering Intern",
        location="Remote",
        source_url="https://example.com/jobs/1",
        jd_text="Build reliable Python services.",
    )


def provider_binding(
    *,
    provider_kind: str = "mock",
    model: str = "fixture-v1",
    privacy_mode: str = "local_private",
    endpoint: str = "",
    identity_name: str = "",
    **overrides,
):
    values = {
        "provider_kind": provider_kind,
        "model": model,
        "privacy_mode": privacy_mode,
        "data_leaves_machine": privacy_mode == "cloud_external",
        "is_ai": True,
        "endpoint": endpoint,
        "identity_name": identity_name,
        "executable": "",
        "executable_sha256": "",
        "executable_version": "",
        "codex_home": "",
        "reasoning_effort": "",
        "model_digest": "",
        "model_metadata_sha256": "",
        "show_metadata_sha256": "",
        "attestation_version": "",
        "runtime_instance_id": "",
        "runtime_handle": "",
        "container_image_reference": "",
        "container_image_id": "",
        "issuer_instance_id": "",
        "egress_policy": "",
        "cloud_disabled": False,
        "egress_enforced": False,
        "isolation_evidence_sha256": "",
        "isolation_evidence_signature": "",
    }
    values.update(overrides)
    return values


def create_provider(repo: CareerOpsRepository):
    return repo.upsert_ai_provider(
        provider_kind="mock",
        display_name="CI mock",
        privacy_mode="local_private",
        endpoint="",
        model="fixture-v1",
        enabled=True,
        availability_binding=provider_binding(),
    )


def create_task(
    repo: CareerOpsRepository,
    application_id: str,
    provider_id: str,
    input_artifact_ids: list[str] | None = None,
):
    return create_bound_task(
        repo,
        application_id,
        provider_id,
        "analyze_fit",
        input_artifact_ids=input_artifact_ids,
    )


def create_bound_task(
    repo: CareerOpsRepository,
    application_id: str,
    provider_id: str,
    task_type: str,
    *,
    input_artifact_ids: list[str] | None = None,
):
    provider = repo.get_ai_provider(provider_id)
    prompt = load_prompt_template(task_type)
    return repo.create_ai_task(
        application_id=application_id,
        task_type=task_type,
        provider_config_id=provider_id,
        privacy_mode="local_private",
        prompt_template_id=prompt.id,
        prompt_sha256=prompt.sha256,
        input_artifact_ids=input_artifact_ids or [],
        expected_provider_binding=provider.availability_binding,
        expected_provider_binding_sha256=provider.availability_binding_sha256,
    )


def write_attempt_artifact(
    repo: CareerOpsRepository,
    task_id: str,
    claim_token: str,
    artifact_type: str,
    content: bytes,
    *,
    schema_version: str = "1",
):
    attempt = repo.vault_path / "ai-tasks" / task_id / "attempt-test"
    attempt.mkdir(parents=True, exist_ok=True)
    path = attempt / f"{artifact_type}.json"
    path.write_bytes(content)
    return repo.save_ai_artifact(
        task_id,
        claim_token,
        artifact_type,
        path,
        hashlib.sha256(content).hexdigest(),
        schema_version,
    )


def attempt_file(
    repo: CareerOpsRepository,
    task_id: str,
    name: str,
    *,
    attempt: str = "attempt-fixture",
) -> Path:
    directory = repo.vault_path / "ai-tasks" / task_id / attempt
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name


def claim_task(repo: CareerOpsRepository, task_id: str) -> str:
    claim_token = repo.claim_ai_task(task_id)
    assert isinstance(claim_token, str)
    return claim_token


def make_legacy_application_pristine_for_claim(
    repo: CareerOpsRepository, application_id: str = "app_v0"
) -> None:
    with repo.connection() as connection:
        connection.execute(
            """
            UPDATE applications
            SET approval_id = NULL, status = 'draft',
                submission_state = 'not_submitted', submitted_at = NULL
            WHERE id = ?
            """,
            (application_id,),
        )


def expire_claim(repo: CareerOpsRepository, task_id: str) -> None:
    with repo.connection() as connection:
        connection.execute(
            "UPDATE ai_tasks SET claim_expires_at = ? WHERE id = ?",
            ("1970-01-01T00:00:00+00:00", task_id),
        )


def finalize_findings(
    repo: CareerOpsRepository,
    application_id: str,
    provider_id: str,
    findings: list[dict[str, Any]],
):
    application = repo.get_application(application_id)
    task = create_bound_task(
        repo, application_id, provider_id, "suggest_resume_edits"
    )
    claim = claim_task(repo, task.id)
    assert repo.begin_ai_task_invocation(task.id, claim) is True
    for artifact_type in ("context", "raw_output", "validated_output"):
        write_attempt_artifact(
            repo,
            task.id,
            claim,
            artifact_type,
            artifact_type.encode("ascii"),
        )
    terminal = repo.finalize_ai_task_attempt(
        task_id=task.id,
        claim_token=claim,
        terminal_status="completed",
        error_summary="",
        expected_jd_snapshot_id=application.jd_snapshot_id,
        expected_resume_version_id=None,
        findings=findings,
    )
    return task, terminal


def create_aggregate_finding(
    repo: CareerOpsRepository,
    application_id: str,
    provider_id: str,
    **finding: Any,
):
    task, _terminal = finalize_findings(
        repo, application_id, provider_id, [finding]
    )
    findings = repo.list_ai_findings(task.id)
    assert len(findings) == 1
    return task, findings[0]


def test_completed_finalization_rejects_empty_artifact_set(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    resume = repo.create_resume_version(
        name="Current resume",
        role_family="software",
        tex_path=tmp_path / "resume.tex",
        tex_sha256="a" * 64,
    )
    repo.select_resume(application.id, resume.id)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    assert repo.begin_ai_task_invocation(task.id, claim) is True

    with pytest.raises(ValueError, match="required artifacts"):
        repo.finalize_ai_task_attempt(
            task_id=task.id,
            claim_token=claim,
            terminal_status="completed",
            error_summary="",
            expected_jd_snapshot_id=application.jd_snapshot_id,
            expected_resume_version_id=resume.id,
            findings=[],
            success_payload={
                "fit": {"score": 80, "provenance": {"ai_task_id": "forged"}},
                "risks": {"blockers": [], "warnings": []},
            },
        )

    assert repo.get_ai_task(task.id).status == "running"
    assert repo.get_application(application.id).latest_report_id is None


def test_finalization_rejects_caller_supplied_authority_provenance(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    resume = repo.create_resume_version(
        name="Current resume",
        role_family="software",
        tex_path=tmp_path / "resume.tex",
        tex_sha256="a" * 64,
    )
    repo.select_resume(application.id, resume.id)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    assert repo.begin_ai_task_invocation(task.id, claim) is True
    artifacts = [
        write_attempt_artifact(repo, task.id, claim, kind, kind.encode("ascii"))
        for kind in ("context", "raw_output", "validated_output")
    ]

    with pytest.raises(TypeError):
        repo.finalize_ai_task_attempt(
            task_id=task.id,
            claim_token=claim,
            terminal_status="completed",
            error_summary="",
            expected_jd_snapshot_id=application.jd_snapshot_id,
            expected_resume_version_id=resume.id,
            artifacts=[artifact.__dict__ for artifact in artifacts],
            provenance={"ai_task_id": "forged", "provider_kind": "forged"},
            findings=[],
            success_payload={
                "fit": {"score": 80},
                "risks": {"blockers": [], "warnings": []},
            },
        )

    assert repo.get_ai_task(task.id).status == "running"
    assert repo.get_application(application.id).latest_report_id is None


def test_finalization_builds_provenance_from_repository_rows(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    resume_dir = repo.vault_path / "resumes" / "ai-report"
    resume_dir.mkdir(parents=True)
    pdf_path = resume_dir / "resume.pdf"
    pdf_bytes = b"%PDF-1.4\nrepository-owned provenance"
    pdf_path.write_bytes(pdf_bytes)
    resume = repo.create_resume_version(
        name="Current resume",
        role_family="software",
        tex_path=resume_dir / "resume.tex",
        tex_sha256="a" * 64,
        pdf_path=pdf_path,
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
    )
    repo.select_resume(application.id, resume.id)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    assert repo.begin_ai_task_invocation(task.id, claim) is True
    artifacts = {
        kind: write_attempt_artifact(
            repo, task.id, claim, kind, kind.encode("ascii")
        )
        for kind in ("context", "raw_output", "validated_output")
    }

    completed = repo.finalize_ai_task_attempt(
        task_id=task.id,
        claim_token=claim,
        terminal_status="completed",
        error_summary="",
        expected_jd_snapshot_id=application.jd_snapshot_id,
        expected_resume_version_id=resume.id,
        findings=[],
        success_payload={
            "fit": {
                "score": 80,
                "provenance": {"provider_kind": "forged"},
                "details": {"provenance": {"ai_task_id": "forged"}},
            },
            "risks": {"blockers": [], "warnings": []},
        },
    )

    report = repo.get_report(repo.get_application(application.id).latest_report_id)
    expected_provenance = {
        "ai_task_id": task.id,
        "context_sha256": artifacts["context"].sha256,
        "output_checksums": {
            "raw_output": artifacts["raw_output"].sha256,
            "validated_output": artifacts["validated_output"].sha256,
        },
        "privacy_mode": "local_private",
        "prompt_sha256": task.prompt_sha256,
        "prompt_template_id": task.prompt_template_id,
        "provider_binding": provider.availability_binding,
        "provider_binding_sha256": provider.availability_binding_sha256,
        "provider_config_id": provider.id,
        "provider_identity_name": "",
        "provider_kind": "mock",
        "provider_model": "fixture-v1",
    }
    assert completed.status == "completed"
    assert report.fit["provenance"] == expected_provenance
    assert "provenance" not in report.fit["details"]
    version = repo.list_application_versions(application.id)[-1]
    assert version.snapshot["provenance"] == expected_provenance
    approval = repo.approve_application(application.id, report.id, "Reviewed")
    assert approval.report_id == report.id


def test_verification_failed_block_requires_all_output_artifacts(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    assert repo.begin_ai_task_invocation(task.id, claim) is True
    write_attempt_artifact(repo, task.id, claim, "context", b"context")
    write_attempt_artifact(repo, task.id, claim, "raw_output", b"raw")

    with pytest.raises(ValueError, match="required artifacts"):
        repo.finalize_ai_task_attempt(
            task_id=task.id,
            claim_token=claim,
            terminal_status="blocked",
            error_summary="verification_failed",
            expected_jd_snapshot_id=application.jd_snapshot_id,
            expected_resume_version_id=None,
            findings=[],
        )


def test_schema_invalid_block_allows_context_when_raw_bytes_are_unavailable(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    assert repo.begin_ai_task_invocation(task.id, claim) is True
    write_attempt_artifact(repo, task.id, claim, "context", b"context")

    blocked = repo.finalize_ai_task_attempt(
        task_id=task.id,
        claim_token=claim,
        terminal_status="blocked",
        error_summary="invalid_output",
        expected_jd_snapshot_id=application.jd_snapshot_id,
        expected_resume_version_id=None,
        findings=[],
    )

    assert blocked.status == "blocked"
    assert repo.list_application_versions(application.id)[-1].event_type == (
        "ai_task.blocked"
    )


def test_cover_letter_completion_requires_draft_artifact(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    resume = repo.create_resume_version(
        name="Current resume",
        role_family="software",
        tex_path=tmp_path / "resume.tex",
        tex_sha256="a" * 64,
    )
    repo.select_resume(application.id, resume.id)
    provider = create_provider(repo)
    task = create_bound_task(
        repo, application.id, provider.id, "draft_cover_letter"
    )
    claim = claim_task(repo, task.id)
    assert repo.begin_ai_task_invocation(task.id, claim) is True
    for artifact_type in ("context", "raw_output", "validated_output"):
        write_attempt_artifact(
            repo,
            task.id,
            claim,
            artifact_type,
            artifact_type.encode("ascii"),
        )

    with pytest.raises(ValueError, match="required artifacts"):
        repo.finalize_ai_task_attempt(
            task_id=task.id,
            claim_token=claim,
            terminal_status="completed",
            error_summary="",
            expected_jd_snapshot_id=application.jd_snapshot_id,
            expected_resume_version_id=resume.id,
            findings=[],
        )


def test_lock_delayed_finalization_rejects_expired_claim(tmp_path: Path):
    repo = CareerOpsRepository(
        tmp_path / "careerops.db",
        tmp_path / "vault",
        claim_lease_seconds=15,
    )
    repo.initialize()
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_bound_task(repo, application.id, provider.id, "analyze_jd")
    claim = claim_task(repo, task.id)
    assert repo.begin_ai_task_invocation(task.id, claim) is True
    write_attempt_artifact(repo, task.id, claim, "context", b"context")
    finalization_started = Event()

    def finalize():
        finalization_started.set()
        return repo.finalize_ai_task_attempt(
            task_id=task.id,
            claim_token=claim,
            terminal_status="blocked",
            error_summary="invalid_output",
            expected_jd_snapshot_id=application.jd_snapshot_id,
            expected_resume_version_id=None,
            findings=[],
        )

    with repo.connection() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        blocker.execute("BEGIN IMMEDIATE")
        future = pool.submit(finalize)
        assert finalization_started.wait(2)
        blocker.execute(
            "UPDATE ai_tasks SET claim_expires_at = ? WHERE id = ?",
            ("1970-01-01T00:00:00+00:00", task.id),
        )
        blocker.commit()
        with pytest.raises(ValueError, match="claim"):
            future.result(timeout=2)

    assert repo.get_ai_task(task.id).status == "running"
    assert repo.list_application_versions(application.id) == []


def test_ai_artifact_requires_task_attempt_source_path(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    first_claim = claim_task(repo, task.id)
    escaped = repo.vault_path / "ai-artifacts" / "context.json"
    escaped.parent.mkdir(parents=True, exist_ok=True)
    escaped.write_bytes(b"context")

    with pytest.raises(ValueError, match="task attempt"):
        repo.save_ai_artifact(
            task.id,
            first_claim,
            "context",
            escaped,
            hashlib.sha256(b"context").hexdigest(),
            "1",
        )

    escaped.unlink()
    artifact = write_attempt_artifact(
        repo, task.id, first_claim, "context", b"context"
    )
    expire_claim(repo, task.id)
    assert repo.requeue_interrupted_ai_tasks() == 1
    second_claim = claim_task(repo, task.id)
    alias_path = artifact.path.with_name(artifact.path.name.upper())
    if not alias_path.exists():
        alias_path = artifact.path

    with pytest.raises(ValueError, match="task attempt"):
        repo.save_ai_artifact(
            task.id,
            second_claim,
            "context",
            alias_path,
            artifact.sha256,
            "1",
        )


def test_ai_artifact_insert_race_rolls_back_database_row_and_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    source = attempt_file(repo, task.id, "insert-race.json")
    original_bytes = b"original"
    source.write_bytes(original_bytes)
    original_require_claim = repo._require_active_ai_claim

    def tamper_materialized_copy(connection, task_id, claim_token):
        original_require_claim(connection, task_id, claim_token)
        copies = list(
            (repo.vault_path / "ai-artifacts" / task.id).glob("*/context.json")
        )
        assert len(copies) == 1
        copies[0].write_bytes(b"tampered-after-materialization")

    monkeypatch.setattr(
        repo, "_require_active_ai_claim", tamper_materialized_copy
    )

    with pytest.raises(ValueError, match="integrity"):
        repo.save_ai_artifact(
            task.id,
            claim,
            "context",
            source,
            hashlib.sha256(original_bytes).hexdigest(),
            "1",
        )

    with repo.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM ai_artifacts WHERE task_id = ?",
            (task.id,),
        ).fetchone()[0] == 0
    artifact_root = repo.vault_path / "ai-artifacts" / task.id
    assert not artifact_root.exists() or list(artifact_root.rglob("*")) == []


def test_ai_artifact_save_does_not_use_post_commit_getter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    path = attempt_file(repo, task.id, "raced.json")
    original_bytes = b"original"
    path.write_bytes(original_bytes)

    def reject_post_commit_getter(_artifact_id: str) -> None:
        raise AssertionError("save must not validate through a post-commit getter")

    monkeypatch.setattr(repo, "get_ai_artifact", reject_post_commit_getter)

    artifact = repo.save_ai_artifact(
        task.id,
        claim,
        "context",
        path,
        hashlib.sha256(original_bytes).hexdigest(),
        "1",
    )

    with repo.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM ai_artifacts WHERE task_id = ?",
            (task.id,),
        ).fetchone()[0] == 1
    assert artifact.path.read_bytes() == original_bytes


def test_ai_artifact_is_repository_owned_copy_not_request_source(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    source = attempt_file(repo, task.id, "source.json")
    source.write_bytes(b"sealed request bytes")

    artifact = repo.save_ai_artifact(
        task.id,
        claim,
        "context",
        source,
        hashlib.sha256(source.read_bytes()).hexdigest(),
        "1",
    )

    relative = artifact.path.relative_to(repo.vault_path)
    assert relative.parts[:3] == ("ai-artifacts", task.id, artifact.id)
    assert artifact.path != source.resolve()
    assert artifact.path.read_bytes() == b"sealed request bytes"
    source.write_bytes(b"request file later changed")
    assert repo.get_ai_artifact(artifact.id) == artifact


def test_ai_artifact_repeated_source_gets_unique_physical_copies(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    source = attempt_file(repo, task.id, "shared-source.json")
    source.write_bytes(b"same source")
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()

    first = repo.save_ai_artifact(
        task.id, claim, "context", source, source_sha256, "1"
    )
    second = repo.save_ai_artifact(
        task.id, claim, "context", source, source_sha256, "1"
    )

    assert first.path != second.path
    assert first.path.read_bytes() == second.path.read_bytes() == b"same source"


def test_ai_artifact_source_replacement_during_read_leaves_no_row_or_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from careerops.core import repository as repository_module

    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    source = attempt_file(repo, task.id, "replace-race.json")
    original_bytes = b"a" * (128 * 1024)
    source.write_bytes(original_bytes)
    original_read = repository_module.os.read
    replaced = False

    def replace_after_first_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        content = original_read(descriptor, size)
        if content and not replaced:
            replaced = True
            source.write_bytes(b"replacement")
        return content

    monkeypatch.setattr(repository_module.os, "read", replace_after_first_read)

    with pytest.raises(ValueError, match="identity changed"):
        repo.save_ai_artifact(
            task.id,
            claim,
            "context",
            source,
            hashlib.sha256(original_bytes).hexdigest(),
            "1",
        )

    with repo.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM ai_artifacts WHERE task_id = ?", (task.id,)
        ).fetchone()[0] == 0
    artifact_root = repo.vault_path / "ai-artifacts" / task.id
    assert not artifact_root.exists() or list(artifact_root.rglob("*")) == []


def test_ai_artifact_db_failure_cleans_only_new_repository_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from careerops.core import repository as repository_module

    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    source = attempt_file(repo, task.id, "db-failure.json")
    source_bytes = b"preserve source"
    source.write_bytes(source_bytes)
    materialized_paths: list[Path] = []
    original_replace = repository_module.os.replace

    def record_materialization(source_path, destination_path) -> None:
        materialized_paths.append(Path(destination_path))
        original_replace(source_path, destination_path)

    def fail_claim_check(_connection, _task_id, _claim_token) -> None:
        raise ValueError("simulated claim loss")

    monkeypatch.setattr(repository_module.os, "replace", record_materialization)
    monkeypatch.setattr(repo, "_require_active_ai_claim", fail_claim_check)

    with pytest.raises(ValueError, match="claim loss"):
        repo.save_ai_artifact(
            task.id,
            claim,
            "context",
            source,
            hashlib.sha256(source_bytes).hexdigest(),
            "1",
        )

    assert len(materialized_paths) == 1
    assert not materialized_paths[0].exists()
    assert not materialized_paths[0].parent.exists()
    assert source.read_bytes() == source_bytes
    with repo.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM ai_artifacts WHERE task_id = ?", (task.id,)
        ).fetchone()[0] == 0


def test_v14_legacy_artifact_is_materialized_idempotently_without_source_deletion(
    tmp_path: Path,
):
    database_path = tmp_path / "careerops.db"
    vault_path = tmp_path / "vault"
    task_id, artifact_id, source_path = create_v14_legacy_artifact_database(
        database_path, vault_path
    )
    repo = CareerOpsRepository(database_path, vault_path)

    repo.initialize()
    artifact = repo.get_ai_artifact(artifact_id)

    assert LATEST_SCHEMA_VERSION >= 15
    assert artifact.task_id == task_id
    assert artifact.path != source_path
    assert artifact.path.relative_to(vault_path).parts[:3] == (
        "ai-artifacts",
        task_id,
        artifact_id,
    )
    assert artifact.path.read_bytes() == source_path.read_bytes() == b"legacy context"
    assert source_path.exists()
    with repo.connection() as connection:
        row = connection.execute(
            "SELECT path, storage_version FROM ai_artifacts WHERE id = ?",
            (artifact_id,),
        ).fetchone()
        assert row["path"] == str(artifact.path)
        assert row["storage_version"] == 2
        with pytest.raises(sqlite3.IntegrityError, match="storage version"):
            connection.execute(
                "UPDATE ai_artifacts SET storage_version = 3 WHERE id = ?",
                (artifact_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="storage version"):
            connection.execute(
                "UPDATE ai_artifacts SET storage_version = 1 WHERE id = ?",
                (artifact_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="storage version"):
            connection.execute(
                """
                INSERT INTO ai_artifacts (
                    id, task_id, claim_token, artifact_type, path, sha256,
                    schema_version, storage_version, created_at
                ) VALUES (?, ?, 'forged', 'context', ?, ?, '1', 1, ?)
                """,
                (
                    "aia_new_legacy_forgery",
                    task_id,
                    str(vault_path / "ai-tasks" / task_id / "attempt-forged.json"),
                    "f" * 64,
                    "2026-01-02T00:00:00+00:00",
                ),
            )

    repo.initialize()

    assert repo.get_ai_artifact(artifact_id) == artifact
    assert source_path.exists()


@pytest.mark.parametrize(
    ("create_source", "stored_sha256"),
    [(False, None), (True, "0" * 64)],
)
def test_v14_missing_or_corrupt_legacy_artifact_fails_closed(
    tmp_path: Path, create_source: bool, stored_sha256: str | None
):
    database_path = tmp_path / "careerops.db"
    vault_path = tmp_path / "vault"
    _task_id, artifact_id, source_path = create_v14_legacy_artifact_database(
        database_path,
        vault_path,
        create_source=create_source,
        stored_sha256=stored_sha256,
    )
    repo = CareerOpsRepository(database_path, vault_path)

    with pytest.raises(ValueError, match="legacy AI artifact"):
        repo.initialize()

    with connect_database(database_path) as connection:
        row = connection.execute(
            "SELECT path, storage_version FROM ai_artifacts WHERE id = ?",
            (artifact_id,),
        ).fetchone()
        assert row["path"] == str(source_path)
        assert row["storage_version"] == 1


def test_v14_upgrade_recovers_crash_after_copy_before_row_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class SimulatedCrash(BaseException):
        pass

    database_path = tmp_path / "careerops.db"
    vault_path = tmp_path / "vault"
    task_id, artifact_id, source_path = create_v14_legacy_artifact_database(
        database_path, vault_path
    )
    crashing_repo = CareerOpsRepository(database_path, vault_path)

    def crash_before_row_update(*_args, **_kwargs) -> None:
        raise SimulatedCrash

    monkeypatch.setattr(
        crashing_repo,
        "_commit_legacy_ai_artifact_upgrade",
        crash_before_row_update,
        raising=False,
    )

    with pytest.raises(SimulatedCrash):
        crashing_repo.initialize()

    copied_path = (
        vault_path
        / "ai-artifacts"
        / task_id
        / artifact_id
        / "context.json"
    )
    assert copied_path.read_bytes() == source_path.read_bytes()
    with connect_database(database_path) as connection:
        row = connection.execute(
            "SELECT path, storage_version FROM ai_artifacts WHERE id = ?",
            (artifact_id,),
        ).fetchone()
        assert row["path"] == str(source_path)
        assert row["storage_version"] == 1

    recovered_repo = CareerOpsRepository(database_path, vault_path)
    recovered_repo.initialize()

    assert recovered_repo.get_ai_artifact(artifact_id).path == copied_path.resolve()
    assert source_path.exists()


def test_v14_upgrade_recovers_crash_after_row_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class SimulatedCrash(BaseException):
        pass

    database_path = tmp_path / "careerops.db"
    vault_path = tmp_path / "vault"
    _task_id, artifact_id, source_path = create_v14_legacy_artifact_database(
        database_path, vault_path
    )
    crashing_repo = CareerOpsRepository(database_path, vault_path)

    def crash_before_orphan_reconciliation() -> None:
        raise SimulatedCrash

    monkeypatch.setattr(
        crashing_repo,
        "_reconcile_ai_artifact_storage",
        crash_before_orphan_reconciliation,
        raising=False,
    )

    with pytest.raises(SimulatedCrash):
        crashing_repo.initialize()

    with connect_database(database_path) as connection:
        row = connection.execute(
            "SELECT path, storage_version FROM ai_artifacts WHERE id = ?",
            (artifact_id,),
        ).fetchone()
        assert row["path"] != str(source_path)
        assert row["storage_version"] == 2

    recovered_repo = CareerOpsRepository(database_path, vault_path)
    recovered_repo.initialize()

    assert recovered_repo.get_ai_artifact(artifact_id).path == Path(row["path"])
    assert source_path.exists()


def test_initialize_removes_crash_orphan_published_before_artifact_insert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class SimulatedCrash(BaseException):
        pass

    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    source = attempt_file(repo, task.id, "crash-orphan.json")
    source.write_bytes(b"orphan")

    original_connection = repo.connection
    connection_calls = 0

    @contextmanager
    def crash_before_insert():
        nonlocal connection_calls
        connection_calls += 1
        if connection_calls == 2:
            raise SimulatedCrash
        with original_connection() as connection:
            yield connection

    monkeypatch.setattr(repo, "connection", crash_before_insert)
    monkeypatch.setattr(
        repo, "_remove_materialized_ai_artifact", lambda _path: None
    )

    with pytest.raises(SimulatedCrash):
        repo.save_ai_artifact(
            task.id,
            claim,
            "context",
            source,
            hashlib.sha256(b"orphan").hexdigest(),
            "1",
        )

    artifact_root = repo.vault_path / "ai-artifacts" / task.id
    assert list(artifact_root.glob("*/context.json"))
    restarted = CareerOpsRepository(repo.database_path, repo.vault_path)
    restarted.initialize()

    assert not artifact_root.exists() or list(artifact_root.rglob("*")) == []
    with restarted.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM ai_artifacts WHERE task_id = ?", (task.id,)
        ).fetchone()[0] == 0
    restarted.initialize()
    assert source.exists()


def test_initialize_preserves_recorded_artifacts_and_cleans_temps_and_orphans(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    source = attempt_file(repo, task.id, "recorded.json")
    source.write_bytes(b"recorded")
    artifact = repo.save_ai_artifact(
        task.id,
        claim,
        "context",
        source,
        hashlib.sha256(b"recorded").hexdigest(),
        "1",
    )
    tracked_temp = artifact.path.parent / ".interrupted.tmp"
    tracked_temp.write_bytes(b"temporary")
    untracked_copy = artifact.path.parent / "untracked-copy.json"
    untracked_copy.write_bytes(b"untracked")
    orphan_dir = (
        repo.vault_path / "ai-artifacts" / task.id / "aia_untracked_orphan"
    )
    orphan_dir.mkdir()
    (orphan_dir / ".copy.tmp").write_bytes(b"orphan")

    restarted = CareerOpsRepository(repo.database_path, repo.vault_path)
    restarted.initialize()
    restarted.initialize()

    assert restarted.get_ai_artifact(artifact.id) == artifact
    assert artifact.path.read_bytes() == b"recorded"
    assert not tracked_temp.exists()
    assert not untracked_copy.exists()
    assert not orphan_dir.exists()


def test_initialize_never_deletes_a_tampered_database_recorded_artifact(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    source = attempt_file(repo, task.id, "recorded-tamper.json")
    source.write_bytes(b"recorded")
    artifact = repo.save_ai_artifact(
        task.id,
        claim,
        "context",
        source,
        hashlib.sha256(b"recorded").hexdigest(),
        "1",
    )
    artifact.path.write_bytes(b"tampered")

    restarted = CareerOpsRepository(repo.database_path, repo.vault_path)
    with pytest.raises(ValueError, match="integrity"):
        restarted.initialize()

    assert artifact.path.read_bytes() == b"tampered"
    with restarted.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM ai_artifacts WHERE id = ?", (artifact.id,)
        ).fetchone()[0] == 1


def test_reconciliation_rechecks_database_before_deleting_post_snapshot_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    seed_source = attempt_file(repo, task.id, "seed.json")
    seed_source.write_bytes(b"seed")
    seed = repo.save_ai_artifact(
        task.id,
        claim,
        "context",
        seed_source,
        hashlib.sha256(b"seed").hexdigest(),
        "1",
    )
    late_artifact_id = "aia_committed_after_snapshot"
    late_path = (
        repo.vault_path
        / "ai-artifacts"
        / task.id
        / late_artifact_id
        / "raw_output.json"
    )
    late_path.parent.mkdir()
    late_path.write_bytes(b"committed")
    snapshot_taken = Event()
    continue_reconciliation = Event()
    reconciling = CareerOpsRepository(repo.database_path, repo.vault_path)
    committing = CareerOpsRepository(repo.database_path, repo.vault_path)
    original_path_key = reconciling._artifact_path_key
    paused = False

    def pause_after_sweep_snapshot(path: Path):
        nonlocal paused
        key = original_path_key(path)
        if not paused and Path(path) == seed.path.parent:
            paused = True
            snapshot_taken.set()
            assert continue_reconciliation.wait(10)
        return key

    monkeypatch.setattr(
        reconciling, "_artifact_path_key", pause_after_sweep_snapshot
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(reconciling._reconcile_ai_artifact_storage)
        assert snapshot_taken.wait(10)
        with committing.connection() as connection:
            connection.execute(
                """
                INSERT INTO ai_artifacts (
                    id, task_id, claim_token, artifact_type, path, sha256,
                    schema_version, storage_version, created_at
                ) VALUES (?, ?, ?, 'raw_output', ?, ?, '1', 2, ?)
                """,
                (
                    late_artifact_id,
                    task.id,
                    claim,
                    str(late_path.resolve()),
                    hashlib.sha256(b"committed").hexdigest(),
                    "2026-07-21T00:00:00+00:00",
                ),
            )
        continue_reconciliation.set()
        future.result(timeout=10)

    assert committing.get_ai_artifact(late_artifact_id).path == late_path.resolve()
    assert late_path.read_bytes() == b"committed"


def test_reconciliation_waits_for_published_artifact_insert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    source = attempt_file(repo, task.id, "coordinated.json")
    source.write_bytes(b"coordinated")
    publishing = CareerOpsRepository(repo.database_path, repo.vault_path)
    reconciling = CareerOpsRepository(repo.database_path, repo.vault_path)
    published = Event()
    allow_insert = Event()
    reconciliation_attempted = Event()
    reconciliation_acquired = Event()
    original_materialize = publishing._materialize_ai_artifact
    original_coordination = reconciling._ai_artifact_coordination

    def pause_after_publication(**kwargs):
        result = original_materialize(**kwargs)
        published.set()
        assert allow_insert.wait(10)
        return result

    @contextmanager
    def observe_reconciliation_coordination():
        reconciliation_attempted.set()
        with original_coordination():
            reconciliation_acquired.set()
            yield

    monkeypatch.setattr(
        publishing, "_materialize_ai_artifact", pause_after_publication
    )
    monkeypatch.setattr(
        reconciling,
        "_ai_artifact_coordination",
        observe_reconciliation_coordination,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        publish_future = executor.submit(
            publishing.save_ai_artifact,
            task.id,
            claim,
            "context",
            source,
            hashlib.sha256(b"coordinated").hexdigest(),
            "1",
        )
        assert published.wait(10)
        reconcile_future = executor.submit(
            reconciling._reconcile_ai_artifact_storage
        )
        assert reconciliation_attempted.wait(10)
        assert not reconciliation_acquired.is_set()
        allow_insert.set()
        artifact = publish_future.result(timeout=10)
        reconcile_future.result(timeout=10)

    assert reconciliation_acquired.is_set()
    assert publishing.get_ai_artifact(artifact.id) == artifact


def test_reconciliation_waits_for_legacy_copy_database_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    database_path = tmp_path / "careerops.db"
    vault_path = tmp_path / "vault"
    task_id, artifact_id, source_path = create_v14_legacy_artifact_database(
        database_path, vault_path
    )
    initialize_database(database_path)
    (vault_path / "ai-artifacts").mkdir(parents=True, exist_ok=True)
    interrupted_target = (
        vault_path
        / "ai-artifacts"
        / task_id
        / artifact_id
        / "context.json"
    )
    interrupted_target.parent.mkdir(parents=True)
    interrupted_target.write_bytes(source_path.read_bytes())
    unrelated_orphan = (
        vault_path
        / "ai-artifacts"
        / task_id
        / "aia_unrelated_orphan"
        / "context.json"
    )
    unrelated_orphan.parent.mkdir()
    unrelated_orphan.write_bytes(b"orphan")
    upgrading = CareerOpsRepository(database_path, vault_path)
    reconciling = CareerOpsRepository(database_path, vault_path)
    copied = Event()
    allow_update = Event()
    reconciliation_attempted = Event()
    reconciliation_acquired = Event()
    original_commit = upgrading._commit_legacy_ai_artifact_upgrade
    original_coordination = reconciling._ai_artifact_coordination

    def pause_before_update(*args, **kwargs):
        copied.set()
        assert allow_update.wait(10)
        return original_commit(*args, **kwargs)

    @contextmanager
    def observe_reconciliation_coordination():
        reconciliation_attempted.set()
        with original_coordination():
            reconciliation_acquired.set()
            yield

    monkeypatch.setattr(
        upgrading, "_commit_legacy_ai_artifact_upgrade", pause_before_update
    )
    monkeypatch.setattr(
        reconciling,
        "_ai_artifact_coordination",
        observe_reconciliation_coordination,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        upgrade_future = executor.submit(upgrading._upgrade_legacy_ai_artifacts)
        assert copied.wait(10)
        reconcile_future = executor.submit(
            reconciling._reconcile_ai_artifact_storage
        )
        assert reconciliation_attempted.wait(10)
        assert not reconciliation_acquired.is_set()
        allow_update.set()
        upgrade_future.result(timeout=10)
        reconcile_future.result(timeout=10)

    artifact = upgrading.get_ai_artifact(artifact_id)
    assert artifact.path == interrupted_target.resolve()
    assert artifact.path.read_bytes() == source_path.read_bytes()
    assert source_path.exists()
    assert not unrelated_orphan.exists()


def test_process_termination_releases_artifact_coordination_and_orphan_converges(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    script = """
import os
from pathlib import Path
import sys
from careerops.core.repository import CareerOpsRepository

repository = CareerOpsRepository(Path(sys.argv[1]), Path(sys.argv[2]))
orphan = Path(sys.argv[2]) / 'ai-artifacts' / 'ait_crashed' / 'aia_crashed'
with repository._ai_artifact_coordination():
    orphan.mkdir(parents=True)
    (orphan / 'context.json').write_bytes(b'orphan')
    os._exit(0)
"""

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(repo.database_path),
            str(repo.vault_path),
        ],
        cwd=Path(__file__).parent.parent,
        check=False,
        timeout=15,
    )

    assert result.returncode == 0
    orphan_task_root = repo.vault_path / "ai-artifacts" / "ait_crashed"
    assert orphan_task_root.exists()
    restarted = CareerOpsRepository(repo.database_path, repo.vault_path)
    restarted.initialize()
    restarted.initialize()
    assert not orphan_task_root.exists()
    assert (repo.vault_path / ".ai-artifacts.lock").is_file()


def test_next_save_cleans_terminated_publishers_orphan_without_initialize(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    crashed_source = attempt_file(repo, task.id, "crashed-publication.json")
    crashed_source.write_bytes(b"crashed publication")
    script = """
import os
from pathlib import Path
import sys
from careerops.core.repository import CareerOpsRepository

repository = CareerOpsRepository(Path(sys.argv[1]), Path(sys.argv[2]))
original_materialize = repository._materialize_ai_artifact

def terminate_after_publication(**kwargs):
    original_materialize(**kwargs)
    os._exit(0)

repository._materialize_ai_artifact = terminate_after_publication
repository.save_ai_artifact(
    sys.argv[3],
    sys.argv[4],
    'context',
    Path(sys.argv[5]),
    sys.argv[6],
    '1',
)
"""

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(repo.database_path),
            str(repo.vault_path),
            task.id,
            claim,
            str(crashed_source),
            hashlib.sha256(b"crashed publication").hexdigest(),
        ],
        cwd=Path(__file__).parent.parent,
        check=False,
        timeout=15,
    )

    assert result.returncode == 0
    crashed_paths = list(
        (repo.vault_path / "ai-artifacts" / task.id).glob("*/context.json")
    )
    assert len(crashed_paths) == 1
    live_source = attempt_file(repo, task.id, "live-publication.json")
    live_source.write_bytes(b"live publication")

    artifact = repo.save_ai_artifact(
        task.id,
        claim,
        "raw_output",
        live_source,
        hashlib.sha256(b"live publication").hexdigest(),
        "1",
    )

    assert not crashed_paths[0].exists()
    assert artifact.path.read_bytes() == b"live publication"
    assert repo.list_ai_artifacts(task.id) == [artifact]


def test_prepublication_sweep_is_lightweight_and_preserves_recorded_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    first_source = attempt_file(repo, task.id, "recorded-before-sweep.json")
    first_source.write_bytes(b"recorded")
    recorded = repo.save_ai_artifact(
        task.id,
        claim,
        "context",
        first_source,
        hashlib.sha256(b"recorded").hexdigest(),
        "1",
    )
    orphan = (
        repo.vault_path
        / "ai-artifacts"
        / task.id
        / "aia_prepublication_orphan"
        / "context.json"
    )
    orphan.parent.mkdir()
    orphan.write_bytes(b"orphan")
    second_source = attempt_file(repo, task.id, "new-after-sweep.json")
    second_source.write_bytes(b"new")
    original_digest = repo._stable_artifact_digest

    def reject_recorded_artifact_rehash(path: Path):
        if Path(os.path.abspath(path)) == recorded.path:
            raise AssertionError("pre-publication sweep rehashed a valid artifact")
        return original_digest(path)

    monkeypatch.setattr(
        repo, "_stable_artifact_digest", reject_recorded_artifact_rehash
    )

    second = repo.save_ai_artifact(
        task.id,
        claim,
        "raw_output",
        second_source,
        hashlib.sha256(b"new").hexdigest(),
        "1",
    )

    assert not orphan.exists()
    assert recorded.path.read_bytes() == b"recorded"
    assert second.path.read_bytes() == b"new"


def test_legacy_upgrade_sweeps_orphans_but_preserves_reserved_target(
    tmp_path: Path,
):
    database_path = tmp_path / "careerops.db"
    vault_path = tmp_path / "vault"
    task_id, artifact_id, source_path = create_v14_legacy_artifact_database(
        database_path, vault_path
    )
    initialize_database(database_path)
    reserved_target = (
        vault_path
        / "ai-artifacts"
        / task_id
        / artifact_id
        / "context.json"
    )
    reserved_target.parent.mkdir(parents=True)
    reserved_target.write_bytes(source_path.read_bytes())
    orphan = (
        vault_path
        / "ai-artifacts"
        / task_id
        / "aia_legacy_orphan"
        / "context.json"
    )
    orphan.parent.mkdir()
    orphan.write_bytes(b"orphan")
    repo = CareerOpsRepository(database_path, vault_path)

    repo._upgrade_legacy_ai_artifacts()

    assert repo.get_ai_artifact(artifact_id).path == reserved_target.resolve()
    assert reserved_target.read_bytes() == source_path.read_bytes()
    assert not orphan.exists()


def test_process_save_and_initialize_serialize_before_artifact_insert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    source = attempt_file(repo, task.id, "process-publication.json")
    source.write_bytes(b"process publication")
    script = """
from pathlib import Path
import sys
from careerops.core.repository import CareerOpsRepository

repository = CareerOpsRepository(Path(sys.argv[1]), Path(sys.argv[2]))
original_materialize = repository._materialize_ai_artifact

def pause_after_publication(**kwargs):
    result = original_materialize(**kwargs)
    print('PUBLISHED', flush=True)
    if sys.stdin.readline().strip() != 'CONTINUE':
        raise RuntimeError('publisher was not released')
    return result

repository._materialize_ai_artifact = pause_after_publication
repository.save_ai_artifact(
    sys.argv[3],
    sys.argv[4],
    'context',
    Path(sys.argv[5]),
    sys.argv[6],
    '1',
)
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(repo.database_path),
            str(repo.vault_path),
            task.id,
            claim,
            str(source),
            hashlib.sha256(b"process publication").hexdigest(),
        ],
        cwd=Path(__file__).parent.parent,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    initializing = CareerOpsRepository(repo.database_path, repo.vault_path)
    coordination_attempted = Event()
    coordination_acquired = Event()
    original_coordination = initializing._ai_artifact_coordination

    @contextmanager
    def observe_initialize_coordination():
        coordination_attempted.set()
        with original_coordination():
            coordination_acquired.set()
            yield

    monkeypatch.setattr(
        initializing, "_ai_artifact_coordination", observe_initialize_coordination
    )

    try:
        assert process.stdout is not None
        with ThreadPoolExecutor(max_workers=2) as executor:
            published_line = executor.submit(process.stdout.readline).result(timeout=15)
            assert published_line.strip() == "PUBLISHED"
            initialize_future = executor.submit(initializing.initialize)
            assert coordination_attempted.wait(10)
            assert not coordination_acquired.is_set()
            assert process.stdin is not None
            process.stdin.write("CONTINUE\n")
            process.stdin.flush()
            initialize_future.result(timeout=15)
        stdout, stderr = process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=15)

    assert process.returncode == 0, (stdout, stderr)
    assert coordination_acquired.is_set()
    artifacts = repo.list_ai_artifacts(task.id)
    assert len(artifacts) == 1
    assert artifacts[0].path.read_bytes() == b"process publication"


def test_artifact_coordination_revalidates_lock_identity_after_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    lock_path = repo.vault_path / ".ai-artifacts.lock"
    acquired = Event()
    yielded = False
    original_acquire = repo._acquire_ai_artifact_file_lock
    original_lstat = Path.lstat

    def observe_acquisition(descriptor: int) -> None:
        original_acquire(descriptor)
        acquired.set()

    def replace_identity_after_acquisition(path: Path):
        result = original_lstat(path)
        if acquired.is_set() and path == lock_path:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_nlink=result.st_nlink,
                st_dev=result.st_dev,
                st_ino=result.st_ino + 1,
                st_size=result.st_size,
            )
        return result

    monkeypatch.setattr(repo, "_acquire_ai_artifact_file_lock", observe_acquisition)
    monkeypatch.setattr(Path, "lstat", replace_identity_after_acquisition)

    with pytest.raises(ValueError, match="coordination path"):
        with repo._ai_artifact_coordination():
            yielded = True

    assert not yielded


def test_artifact_publication_fsyncs_containing_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    source = attempt_file(repo, task.id, "durable.json")
    source.write_bytes(b"durable")
    fsynced_directories: list[Path] = []
    monkeypatch.setattr(
        repo,
        "_fsync_directory",
        lambda path: fsynced_directories.append(Path(path)),
        raising=False,
    )

    artifact = repo.save_ai_artifact(
        task.id,
        claim,
        "context",
        source,
        hashlib.sha256(b"durable").hexdigest(),
        "1",
    )

    assert artifact.path.parent in fsynced_directories
    assert artifact.path.parent.parent in fsynced_directories
    assert repo.vault_path / "ai-artifacts" in fsynced_directories


def test_artifact_directory_fsync_failure_rolls_back_copy_and_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    source = attempt_file(repo, task.id, "fsync-failure.json")
    source.write_bytes(b"durable")

    def fail_directory_fsync(_path: Path) -> None:
        raise OSError("simulated directory fsync failure")

    monkeypatch.setattr(
        repo, "_fsync_directory", fail_directory_fsync, raising=False
    )

    with pytest.raises(OSError, match="directory fsync failure"):
        repo.save_ai_artifact(
            task.id,
            claim,
            "context",
            source,
            hashlib.sha256(b"durable").hexdigest(),
            "1",
        )

    with repo.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM ai_artifacts WHERE task_id = ?", (task.id,)
        ).fetchone()[0] == 0
    artifact_root = repo.vault_path / "ai-artifacts" / task.id
    assert not artifact_root.exists() or list(artifact_root.rglob("*")) == []


def test_directory_fsync_is_explicitly_skipped_when_platform_lacks_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from careerops.core import repository as repository_module

    repo = CareerOpsRepository(tmp_path / "careerops.db", tmp_path / "vault")
    monkeypatch.setattr(repository_module, "DIRECTORY_FSYNC_SUPPORTED", False)

    def unexpected_open(*_args, **_kwargs):
        raise AssertionError("unsupported directory fsync must not open a directory")

    monkeypatch.setattr(repository_module.os, "open", unexpected_open)

    repo._fsync_directory(tmp_path)


def test_directory_fsync_uses_open_fsync_and_close_when_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from careerops.core import repository as repository_module

    repo = CareerOpsRepository(tmp_path / "careerops.db", tmp_path / "vault")
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(repository_module, "DIRECTORY_FSYNC_SUPPORTED", True)
    monkeypatch.setattr(
        repository_module.os,
        "open",
        lambda path, flags: calls.append(("open", (Path(path), flags))) or 73,
    )
    monkeypatch.setattr(
        repository_module.os,
        "fsync",
        lambda descriptor: calls.append(("fsync", descriptor)),
    )
    monkeypatch.setattr(
        repository_module.os,
        "close",
        lambda descriptor: calls.append(("close", descriptor)),
    )

    repo._fsync_directory(tmp_path)

    assert calls[0][0] == "open"
    assert calls[0][1][0] == tmp_path
    assert calls[1:] == [("fsync", 73), ("close", 73)]


@pytest.mark.parametrize(
    ("artifact_type", "schema_version"),
    [
        ("authorization=secret", "1"),
        ("context", "secret-token-value"),
        ("x" * 65, "1"),
        ("context", "x" * 33),
    ],
)
def test_ai_artifact_metadata_is_strictly_bounded(
    tmp_path: Path,
    artifact_type: str,
    schema_version: str,
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    attempt = repo.vault_path / "ai-tasks" / task.id / "attempt-metadata"
    attempt.mkdir(parents=True)
    path = attempt / "value.json"
    path.write_bytes(b"{}")

    with pytest.raises(ValueError, match="artifact metadata"):
        repo.save_ai_artifact(
            task.id,
            claim,
            artifact_type,
            path,
            hashlib.sha256(b"{}").hexdigest(),
            schema_version,
        )


def test_ai_artifact_metadata_triggers_reject_direct_sql(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)

    with repo.connection() as connection:
        for artifact_id, artifact_type, schema_version in (
            ("aia_invalid_type", "authorization=secret", "1"),
            ("aia_invalid_schema", "context", "secret-token-value"),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="artifact metadata"):
                connection.execute(
                    """
                    INSERT INTO ai_artifacts (
                        id, task_id, claim_token, artifact_type, path, sha256,
                        schema_version, storage_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 2, ?)
                    """,
                    (
                        artifact_id,
                        task.id,
                        claim,
                        artifact_type,
                        str(attempt_file(repo, task.id, f"{artifact_id}.json")),
                        "a" * 64,
                        schema_version,
                        "2026-01-01T00:00:00+00:00",
                    ),
                )


def test_initialize_recovers_only_expired_and_null_lease_claims(tmp_path: Path):
    repo = CareerOpsRepository(
        tmp_path / "careerops.db",
        tmp_path / "vault",
        claim_lease_seconds=60,
    )
    repo.initialize()
    application = create_application(repo)
    provider = create_provider(repo)
    live_task = create_task(repo, application.id, provider.id)
    expired_task = create_task(repo, application.id, provider.id)
    null_lease_task = create_task(repo, application.id, provider.id)
    claim_task(repo, live_task.id)
    claim_task(repo, expired_task.id)
    claim_task(repo, null_lease_task.id)
    with repo.connection() as connection:
        connection.execute(
            "UPDATE ai_tasks SET claim_expires_at = ? WHERE id = ?",
            ("1970-01-01T00:00:00+00:00", expired_task.id),
        )
        connection.execute(
            "UPDATE ai_tasks SET claim_expires_at = NULL WHERE id = ?",
            (null_lease_task.id,),
        )

    restarted = CareerOpsRepository(
        repo.database_path,
        repo.vault_path,
        claim_lease_seconds=60,
    )
    restarted.initialize()

    assert restarted.get_ai_task(live_task.id).status == "running"
    assert restarted.get_ai_task(expired_task.id).status == "pending"
    assert restarted.get_ai_task(null_lease_task.id).status == "pending"


def test_claim_lease_starts_after_waiting_for_write_lock(tmp_path: Path):
    repo = CareerOpsRepository(
        tmp_path / "careerops.db",
        tmp_path / "vault",
        claim_lease_seconds=15,
    )
    repo.initialize()
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim_started = Event()

    def claim_after_signal():
        claim_started.set()
        return repo.claim_ai_task(task.id)

    with repo.connection() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        blocker.execute("BEGIN IMMEDIATE")
        future = pool.submit(claim_after_signal)
        assert claim_started.wait(2)
        release_floor = datetime.now(UTC)
        blocker.commit()
        claim = future.result(timeout=2)

    assert isinstance(claim, str)
    claimed = repo.get_ai_task(task.id)
    assert claimed.claim_expires_at is not None
    assert datetime.fromisoformat(claimed.claim_expires_at) > release_floor


def test_lock_delayed_renewal_and_invocation_cannot_revive_expired_claim(
    tmp_path: Path,
):
    for operation in ("renew", "invoke"):
        operation_root = tmp_path / operation
        repo = CareerOpsRepository(
            operation_root / "careerops.db",
            operation_root / "vault",
            claim_lease_seconds=15,
        )
        repo.initialize()
        application = create_application(repo)
        provider = create_provider(repo)
        task = create_task(repo, application.id, provider.id)
        claim = claim_task(repo, task.id)
        callback = (
            repo.renew_ai_task_claim
            if operation == "renew"
            else repo.begin_ai_task_invocation
        )
        operation_started = Event()

        def run_operation():
            operation_started.set()
            return callback(task.id, claim)

        with repo.connection() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute("BEGIN IMMEDIATE")
            future = pool.submit(run_operation)
            assert operation_started.wait(2)
            blocker.execute(
                "UPDATE ai_tasks SET claim_expires_at = ? WHERE id = ?",
                ("1970-01-01T00:00:00+00:00", task.id),
            )
            blocker.commit()
            assert future.result(timeout=2) is False


def test_lock_delayed_failure_rejects_expired_claim(tmp_path: Path):
    repo = CareerOpsRepository(
        tmp_path / "careerops.db",
        tmp_path / "vault",
        claim_lease_seconds=15,
    )
    repo.initialize()
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    transition_started = Event()

    def run_transition():
        transition_started.set()
        return repo.fail_ai_task(task.id, claim, "provider_error")

    with repo.connection() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        blocker.execute("BEGIN IMMEDIATE")
        future = pool.submit(run_transition)
        assert transition_started.wait(2)
        blocker.execute(
            "UPDATE ai_tasks SET claim_expires_at = ? WHERE id = ?",
            ("1970-01-01T00:00:00+00:00", task.id),
        )
        blocker.commit()
        with pytest.raises(ValueError, match="claim"):
            future.result(timeout=2)

    assert repo.get_ai_task(task.id).status == "running"


def test_lock_delayed_recovery_observes_expiry_after_lock_wait(tmp_path: Path):
    repo = CareerOpsRepository(
        tmp_path / "careerops.db",
        tmp_path / "vault",
        claim_lease_seconds=15,
    )
    repo.initialize()
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim_task(repo, task.id)
    recovery_started = Event()

    def recover_after_signal():
        recovery_started.set()
        return repo.requeue_interrupted_ai_tasks()

    with repo.connection() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        blocker.execute("BEGIN IMMEDIATE")
        future = pool.submit(recover_after_signal)
        assert recovery_started.wait(2)
        blocker.execute(
            "UPDATE ai_tasks SET claim_expires_at = ? WHERE id = ?",
            ("1970-01-01T00:00:00+00:00", task.id),
        )
        blocker.commit()
        assert future.result(timeout=2) == 1

    assert repo.get_ai_task(task.id).status == "pending"


def prepare_approvable_application(repo: CareerOpsRepository, tmp_path: Path):
    application = create_application(repo)
    source_dir = repo.vault_path / "resumes" / "approved"
    source_dir.mkdir(parents=True, exist_ok=True)
    tex_path = source_dir / "approved.tex"
    pdf_path = source_dir / "approved.pdf"
    tex_bytes = b"approved resume"
    pdf_bytes = b"%PDF-1.4\napproved"
    tex_path.write_bytes(tex_bytes)
    pdf_path.write_bytes(pdf_bytes)
    resume = repo.create_resume_version(
        name="Approved resume",
        role_family="software",
        tex_path=tex_path,
        tex_sha256=hashlib.sha256(tex_bytes).hexdigest(),
        pdf_path=pdf_path,
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
    )
    repo.select_resume(application.id, resume.id)
    report = repo.save_report(
        application_id=application.id,
        resume_version_id=resume.id,
        jd_snapshot_id=application.jd_snapshot_id,
        fit={"score": 90},
        risks={"blockers": [], "warnings": []},
    )
    return application, resume, report


def create_approved_application(repo: CareerOpsRepository, tmp_path: Path):
    application, resume, report = prepare_approvable_application(repo, tmp_path)
    approval = repo.approve_application(application.id, report.id, "Reviewed")
    return application, resume, report, approval


@pytest.mark.parametrize("submitted", [False, True], ids=["approved", "submitted"])
def test_create_ai_task_rejects_read_only_application_transactionally(
    tmp_path: Path, submitted: bool
):
    repo = make_repo(tmp_path)
    application, _resume, report = prepare_approvable_application(repo, tmp_path)
    provider = create_provider(repo)
    repo.approve_application(application.id, report.id, "Reviewed")
    if submitted:
        repo.mark_submitted_manual(application.id, "Submitted")

    with pytest.raises(ValueError, match="approved or submitted"):
        create_bound_task(repo, application.id, provider.id, "analyze_jd")

    assert repo.list_ai_tasks(application.id) == []


def test_approval_rejects_claimed_task_and_leaves_invocation_path_coherent(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    application, _resume, report = prepare_approvable_application(repo, tmp_path)
    provider = create_provider(repo)
    task = create_bound_task(repo, application.id, provider.id, "analyze_jd")
    claim = claim_task(repo, task.id)

    with pytest.raises(ValueError, match="running AI task"):
        repo.approve_application(application.id, report.id, "Reviewed")

    unchanged = repo.get_application(application.id)
    assert unchanged.approval_id is None
    assert unchanged.status == "ready_for_review"
    assert repo.get_ai_task(task.id).status == "running"
    assert repo.begin_ai_task_invocation(task.id, claim) is True
    repo.fail_ai_task(task.id, claim, "cancelled")


def test_begin_invocation_rejects_non_pristine_application_in_same_update(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_bound_task(repo, application.id, provider.id, "analyze_jd")
    claim = claim_task(repo, task.id)
    with repo.connection() as connection:
        connection.execute(
            """
            UPDATE applications
            SET status = 'submitted_manual', submission_state = 'submitted_manual',
                submitted_at = ?
            WHERE id = ?
            """,
            ("2026-07-22T00:00:00+00:00", application.id),
        )

    assert repo.begin_ai_task_invocation(task.id, claim) is False
    assert repo.get_ai_task(task.id).invocation_started_at is None


def test_approval_commit_wins_race_with_task_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application, _resume, report = prepare_approvable_application(repo, tmp_path)
    provider = create_provider(repo)
    task = create_bound_task(repo, application.id, provider.id, "analyze_jd")
    approval_repo = CareerOpsRepository(repo.database_path, repo.vault_path)
    claim_repo = CareerOpsRepository(repo.database_path, repo.vault_path)
    approval_updated = Event()
    release_approval = Event()
    claim_started = Event()
    original_write_audit = approval_repo._write_audit

    def pause_approval_audit(
        connection, event_type, entity_type, entity_id, before, after
    ):
        original_write_audit(
            connection, event_type, entity_type, entity_id, before, after
        )
        if event_type == "application.approved":
            approval_updated.set()
            assert release_approval.wait(2)

    monkeypatch.setattr(approval_repo, "_write_audit", pause_approval_audit)

    def claim():
        claim_started.set()
        return claim_repo.claim_ai_task(task.id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        approval_future = executor.submit(
            approval_repo.approve_application,
            application.id,
            report.id,
            "Reviewed",
        )
        assert approval_updated.wait(2)
        claim_future = executor.submit(claim)
        assert claim_started.wait(2)
        assert not claim_future.done()
        release_approval.set()
        approval_future.result(timeout=2)
        assert claim_future.result(timeout=2) is None

    cancelled = repo.get_ai_task(task.id)
    assert cancelled.status == "failed"
    assert cancelled.error_summary == "cancelled"
    assert repo.get_application(application.id).approval_id is not None


def test_task_claim_commit_wins_race_with_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application, _resume, report = prepare_approvable_application(repo, tmp_path)
    provider = create_provider(repo)
    task = create_bound_task(repo, application.id, provider.id, "analyze_jd")
    claim_repo = CareerOpsRepository(repo.database_path, repo.vault_path)
    approval_repo = CareerOpsRepository(repo.database_path, repo.vault_path)
    claim_updated = Event()
    release_claim = Event()
    approval_started = Event()
    original_connection = claim_repo.connection

    @contextmanager
    def pause_claim_commit():
        with original_connection() as connection:
            yield connection
            status = connection.execute(
                "SELECT status FROM ai_tasks WHERE id = ?", (task.id,)
            ).fetchone()["status"]
            if status == "running":
                claim_updated.set()
                assert release_claim.wait(2)

    monkeypatch.setattr(claim_repo, "connection", pause_claim_commit)

    def approve():
        approval_started.set()
        return approval_repo.approve_application(
            application.id, report.id, "Reviewed"
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claim_future = executor.submit(claim_repo.claim_ai_task, task.id)
        assert claim_updated.wait(2)
        approval_future = executor.submit(approve)
        assert approval_started.wait(2)
        assert not approval_future.done()
        release_claim.set()
        claim_token = claim_future.result(timeout=2)
        assert isinstance(claim_token, str)
        with pytest.raises(ValueError, match="Application has a running AI task"):
            approval_future.result(timeout=2)

    unchanged = repo.get_application(application.id)
    assert unchanged.approval_id is None
    assert unchanged.status == "ready_for_review"
    assert repo.get_ai_task(task.id).status == "running"


def test_invocation_wins_before_approval_and_application_stays_unapproved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application, _resume, report = prepare_approvable_application(repo, tmp_path)
    provider = create_provider(repo)
    task = create_bound_task(repo, application.id, provider.id, "analyze_jd")
    claim = claim_task(repo, task.id)
    invocation_repo = CareerOpsRepository(repo.database_path, repo.vault_path)
    approval_repo = CareerOpsRepository(repo.database_path, repo.vault_path)
    invocation_updated = Event()
    release_invocation = Event()
    approval_started = Event()
    original_write_audit = invocation_repo._write_audit

    def pause_invocation_audit(
        connection, event_type, entity_type, entity_id, before, after
    ):
        original_write_audit(
            connection, event_type, entity_type, entity_id, before, after
        )
        if event_type == "ai_task.invocation_started":
            invocation_updated.set()
            assert release_invocation.wait(2)

    monkeypatch.setattr(invocation_repo, "_write_audit", pause_invocation_audit)

    def approve():
        approval_started.set()
        return approval_repo.approve_application(
            application.id, report.id, "Reviewed"
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        invocation_future = executor.submit(
            invocation_repo.begin_ai_task_invocation, task.id, claim
        )
        assert invocation_updated.wait(2)
        approval_future = executor.submit(approve)
        assert approval_started.wait(2)
        release_invocation.set()
        assert invocation_future.result(timeout=2) is True
        with pytest.raises(ValueError, match="running AI task"):
            approval_future.result(timeout=2)

    assert repo.get_application(application.id).approval_id is None
    assert repo.get_ai_task(task.id).invocation_started_at is not None
    repo.fail_ai_task(task.id, claim, "cancelled")


def test_approval_cancels_pending_tasks_with_audit_and_preserves_terminal_tasks(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    application, _resume, report = prepare_approvable_application(repo, tmp_path)
    provider = create_provider(repo)
    pending = create_bound_task(repo, application.id, provider.id, "analyze_jd")
    terminal = create_bound_task(repo, application.id, provider.id, "analyze_jd")
    terminal_claim = claim_task(repo, terminal.id)
    terminal_before = repo.fail_ai_task(terminal.id, terminal_claim, "provider_error")

    repo.approve_application(application.id, report.id, "Reviewed")

    cancelled = repo.get_ai_task(pending.id)
    assert cancelled.status == "failed"
    assert cancelled.started_at is None
    assert cancelled.completed_at is not None
    assert cancelled.error_summary == "cancelled"
    assert repo.get_ai_task(terminal.id) == terminal_before
    cancellation_events = [
        event
        for event in repo.list_audit_events()
        if event.entity_id == pending.id and event.event_type == "ai_task.failed"
    ]
    assert len(cancellation_events) == 1
    assert cancellation_events[0].before_json["status"] == "pending"
    assert cancellation_events[0].after_json["error_summary"] == "cancelled"


def test_submission_defensively_cancels_pending_tasks_with_audit(tmp_path: Path):
    repo = make_repo(tmp_path)
    application, _resume, report = prepare_approvable_application(repo, tmp_path)
    provider = create_provider(repo)
    task = create_bound_task(repo, application.id, provider.id, "analyze_jd")
    repo.approve_application(application.id, report.id, "Reviewed")
    with repo.connection() as connection:
        connection.execute(
            """
            UPDATE ai_tasks
            SET status = 'pending', started_at = NULL, completed_at = NULL,
                error_summary = '', claim_token = NULL, claim_expires_at = NULL,
                invocation_started_at = NULL
            WHERE id = ?
            """,
            (task.id,),
        )
    prior_events = len(
        [
            event
            for event in repo.list_audit_events()
            if event.entity_id == task.id and event.event_type == "ai_task.failed"
        ]
    )

    repo.mark_submitted_manual(application.id, "Submitted")

    cancelled = repo.get_ai_task(task.id)
    assert cancelled.status == "failed"
    assert cancelled.error_summary == "cancelled"
    cancellation_events = [
        event
        for event in repo.list_audit_events()
        if event.entity_id == task.id and event.event_type == "ai_task.failed"
    ]
    assert len(cancellation_events) == prior_events + 1
    assert cancellation_events[-1].before_json["status"] == "pending"
    assert repo.get_application(application.id).submission_state == "submitted_manual"


@pytest.mark.parametrize(
    ("fit", "risks"),
    [
        (
            {"score": 90, "provenance": {"ai_task_id": "forged"}},
            {"blockers": [], "warnings": []},
        ),
        (
            {"score": 90},
            {
                "blockers": [],
                "warnings": [],
                "provenance": {"provider_kind": "forged"},
            },
        ),
        ({"score": 90, "ai_task_id": "forged"}, {"blockers": [], "warnings": []}),
    ],
)
def test_non_ai_report_rejects_reserved_ai_provenance(
    tmp_path: Path, fit: dict[str, Any], risks: dict[str, Any]
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    resume = repo.create_resume_version(
        name="Current resume",
        role_family="software",
        tex_path=tmp_path / "resume.tex",
        tex_sha256="a" * 64,
    )
    repo.select_resume(application.id, resume.id)

    with pytest.raises(ValueError, match="reserved AI provenance"):
        repo.save_report(
            application_id=application.id,
            resume_version_id=resume.id,
            jd_snapshot_id=application.jd_snapshot_id,
            fit=fit,
            risks=risks,
        )

    assert repo.get_application(application.id).latest_report_id is None


@pytest.mark.parametrize(
    ("fit", "risks"),
    [
        (
            {"score": 90, "details": {"provenance": {"ai_task_id": "forged"}}},
            {"blockers": [], "warnings": []},
        ),
        (
            {"score": 90},
            {
                "blockers": [],
                "warnings": [{"notes": [{"provenance": "forged"}]}],
            },
        ),
    ],
)
def test_non_ai_report_recursively_rejects_reserved_provenance(
    tmp_path: Path, fit: dict[str, Any], risks: dict[str, Any]
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    resume = repo.create_resume_version(
        name="Current resume",
        role_family="software",
        tex_path=tmp_path / "resume.tex",
        tex_sha256="a" * 64,
    )
    repo.select_resume(application.id, resume.id)

    with pytest.raises(ValueError, match="reserved AI provenance"):
        repo.save_report(
            application_id=application.id,
            resume_version_id=resume.id,
            jd_snapshot_id=application.jd_snapshot_id,
            fit=fit,
            risks=risks,
        )

    assert repo.get_application(application.id).latest_report_id is None


def test_approval_rejects_forged_reserved_provenance_but_accepts_normal_report(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    source_dir = repo.vault_path / "resumes" / "provenance"
    source_dir.mkdir(parents=True)
    pdf_path = source_dir / "resume.pdf"
    pdf_bytes = b"%PDF-1.4\nprovenance"
    pdf_path.write_bytes(pdf_bytes)
    resume = repo.create_resume_version(
        name="Current resume",
        role_family="software",
        tex_path=source_dir / "resume.tex",
        tex_sha256="a" * 64,
        pdf_path=pdf_path,
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
    )
    repo.select_resume(application.id, resume.id)
    forged_report_id = "rep_forged_provenance"
    with repo.connection() as connection:
        connection.execute(
            """
            INSERT INTO reports (
                id, application_id, resume_version_id, jd_snapshot_id,
                fit_json, risks_json, blocking_issue_count, warning_count,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)
            """,
            (
                forged_report_id,
                application.id,
                resume.id,
                application.jd_snapshot_id,
                '{"provenance":{"ai_task_id":"forged"},"score":100}',
                '{"blockers":[],"warnings":[]}',
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            "UPDATE applications SET latest_report_id = ?, status = ? WHERE id = ?",
            (forged_report_id, "ready_for_review", application.id),
        )

    with pytest.raises(ValueError, match="reserved AI provenance"):
        repo.approve_application(application.id, forged_report_id, "Reviewed")

    normal = repo.save_report(
        application_id=application.id,
        resume_version_id=resume.id,
        jd_snapshot_id=application.jd_snapshot_id,
        fit={"score": 90, "method": "deterministic"},
        risks={"blockers": [], "warnings": []},
    )
    approval = repo.approve_application(application.id, normal.id, "Reviewed")

    assert approval.report_id == normal.id


def test_approval_rejects_nested_reserved_provenance_forged_in_database(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    source_dir = repo.vault_path / "resumes" / "nested-provenance"
    source_dir.mkdir(parents=True)
    pdf_path = source_dir / "resume.pdf"
    pdf_bytes = b"%PDF-1.4\nnested provenance"
    pdf_path.write_bytes(pdf_bytes)
    resume = repo.create_resume_version(
        name="Current resume",
        role_family="software",
        tex_path=source_dir / "resume.tex",
        tex_sha256="a" * 64,
        pdf_path=pdf_path,
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
    )
    repo.select_resume(application.id, resume.id)
    report_id = "rep_nested_forged_provenance"
    with repo.connection() as connection:
        connection.execute(
            """
            INSERT INTO reports (
                id, application_id, resume_version_id, jd_snapshot_id,
                fit_json, risks_json, blocking_issue_count, warning_count,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)
            """,
            (
                report_id,
                application.id,
                resume.id,
                application.jd_snapshot_id,
                '{"details":{"items":[{"provenance":'
                '{"ai_task_id":"forged"}}]},"score":100}',
                '{"blockers":[],"warnings":[]}',
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            "UPDATE applications SET latest_report_id = ?, status = ? WHERE id = ?",
            (report_id, "ready_for_review", application.id),
        )

    with pytest.raises(ValueError, match="reserved AI provenance"):
        repo.approve_application(application.id, report_id, "Reviewed")

    assert repo.get_application(application.id).approval_id is None


def test_approval_and_submission_require_current_vault_pdf_bytes(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    source_dir = repo.vault_path / "resumes" / "packet"
    source_dir.mkdir(parents=True)
    missing_pdf = source_dir / "missing.pdf"
    resume = repo.create_resume_version(
        name="Packet resume",
        role_family="software",
        tex_path=source_dir / "resume.tex",
        tex_sha256="a" * 64,
        pdf_path=missing_pdf,
        pdf_sha256=hashlib.sha256(b"expected").hexdigest(),
    )
    repo.select_resume(application.id, resume.id)
    report = repo.save_report(
        application_id=application.id,
        resume_version_id=resume.id,
        jd_snapshot_id=application.jd_snapshot_id,
        fit={"score": 90},
        risks={"blockers": [], "warnings": []},
    )

    with pytest.raises(ValueError, match="PDF integrity"):
        repo.approve_application(application.id, report.id, "Reviewed")

    pdf_bytes = b"%PDF-1.4\ncurrent"
    missing_pdf.write_bytes(pdf_bytes)
    repo.update_resume_render(
        resume.id,
        pdf_path=missing_pdf,
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        render_status="rendered",
        parse_status="parsed",
        page_count=1,
    )
    approval = repo.approve_application(application.id, report.id, "Reviewed")
    missing_pdf.write_bytes(b"tampered")

    with pytest.raises(ValueError, match="PDF integrity"):
        repo.mark_submitted_manual(application.id, "Submitted")

    assert repo.get_application(application.id).approval_id == approval.id
    assert repo.get_application(application.id).submission_state == "not_submitted"


def test_update_resume_render_rejects_approved_selected_resume(tmp_path: Path):
    repo = make_repo(tmp_path)
    application, resume, _report, _approval = create_approved_application(
        repo, tmp_path
    )
    replacement = repo.vault_path / "resumes" / "approved" / "replacement.pdf"
    replacement.write_bytes(b"%PDF-1.4\nreplacement")

    with pytest.raises(ValueError, match="approved or submitted"):
        repo.update_resume_render(
            resume.id,
            pdf_path=replacement,
            pdf_sha256=hashlib.sha256(replacement.read_bytes()).hexdigest(),
            render_status="rendered",
            parse_status="parsed",
            page_count=1,
        )

    assert repo.get_resume_version(resume.id).pdf_path == resume.pdf_path
    assert repo.get_application(application.id).approval_id is not None


def test_select_resume_rejects_change_after_submission(tmp_path: Path):
    repo = make_repo(tmp_path)
    application, resume, _report, _approval = create_approved_application(
        repo, tmp_path
    )
    repo.mark_submitted_manual(application.id, "Submitted")
    replacement = repo.create_resume_version(
        name="Replacement",
        role_family="software",
        tex_path=tmp_path / "replacement.tex",
        tex_sha256="d" * 64,
    )

    before = repo.get_application(application.id)
    with pytest.raises(ValueError, match="submitted"):
        repo.select_resume(application.id, resume.id)
    assert repo.get_application(application.id) == before

    with pytest.raises(ValueError, match="submitted"):
        repo.select_resume(application.id, replacement.id)

    assert repo.get_application(application.id).selected_resume_version_id == resume.id


def test_public_jd_reanalysis_requires_aggregate_finalizer(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    resume = repo.create_resume_version(
        name="Current resume",
        role_family="software",
        tex_path=tmp_path / "resume.tex",
        tex_sha256="a" * 64,
    )
    repo.select_resume(application.id, resume.id)
    report = repo.save_report(
        application_id=application.id,
        resume_version_id=resume.id,
        jd_snapshot_id=application.jd_snapshot_id,
        fit={"score": 90},
        risks={"blockers": [], "warnings": []},
    )
    provider = create_provider(repo)
    provider_config = repo.get_ai_provider(provider.id)
    prompt = load_prompt_template("analyze_jd")
    task = repo.create_ai_task(
        application_id=application.id,
        task_type="analyze_jd",
        provider_config_id=provider.id,
        prompt_template_id=prompt.id,
        prompt_sha256=prompt.sha256,
        input_artifact_ids=[],
        privacy_mode="local_private",
        expected_provider_binding=provider_config.availability_binding,
        expected_provider_binding_sha256=(
            provider_config.availability_binding_sha256
        ),
    )
    claim = claim_task(repo, task.id)
    before_application = repo.get_application(application.id)
    before_jd = repo.get_jd_snapshot(application.jd_snapshot_id)

    with pytest.raises(ValueError, match="aggregate finalizer"):
        repo.update_jd_requirements_from_ai(
            snapshot_id=application.jd_snapshot_id,
            task_id=task.id,
            claim_token=claim,
            raw_ai={"required_skills": ["Python"]},
            uncertainty={"items": []},
        )

    assert repo.get_application(application.id) == before_application
    assert repo.get_jd_snapshot(application.jd_snapshot_id) == before_jd
    assert repo.get_report(report.id).id == report.id


def test_public_ai_business_writes_reject_approved_packet(tmp_path: Path):
    repo = make_repo(tmp_path)
    application, resume, report, _approval = create_approved_application(
        repo, tmp_path
    )
    before_application = repo.get_application(application.id)
    before_jd = repo.get_jd_snapshot(application.jd_snapshot_id)

    with pytest.raises(ValueError, match="aggregate finalizer"):
        repo.update_jd_requirements_from_ai(
            snapshot_id=application.jd_snapshot_id,
            task_id="ait_read_only",
            claim_token="inactive-claim",
            raw_ai={"required_skills": ["Rust"]},
            uncertainty={"items": []},
        )
    with pytest.raises(ValueError, match="approved or submitted"):
        repo.save_report(
            application_id=application.id,
            resume_version_id=resume.id,
            jd_snapshot_id=application.jd_snapshot_id,
            fit={"score": 1},
            risks={"blockers": [], "warnings": []},
        )

    assert repo.get_application(application.id) == before_application
    assert repo.get_jd_snapshot(application.jd_snapshot_id) == before_jd
    assert repo.get_report(report.id) == report


def test_approval_validation_holds_write_lock_until_audit_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    first_pdf = repo.vault_path / "resumes" / "first.pdf"
    first_pdf.write_bytes(b"%PDF-1.4\nfirst")
    first = repo.create_resume_version(
        name="First",
        role_family="software",
        tex_path=tmp_path / "first.tex",
        tex_sha256="a" * 64,
        pdf_path=first_pdf,
        pdf_sha256=hashlib.sha256(first_pdf.read_bytes()).hexdigest(),
    )
    second = repo.create_resume_version(
        name="Second",
        role_family="software",
        tex_path=tmp_path / "second.tex",
        tex_sha256="c" * 64,
        pdf_sha256="d" * 64,
    )
    repo.select_resume(application.id, first.id)
    report = repo.save_report(
        application_id=application.id,
        resume_version_id=first.id,
        jd_snapshot_id=application.jd_snapshot_id,
        fit={"score": 90},
        risks={"blockers": [], "warnings": []},
    )
    validation_complete = Event()
    release_validation = Event()
    original = repo._approval_packet_rows

    def pause_after_validation(connection, application_id, report_id):
        rows = original(connection, application_id, report_id)
        validation_complete.set()
        assert release_validation.wait(5)
        return rows

    monkeypatch.setattr(repo, "_approval_packet_rows", pause_after_validation)
    competing = CareerOpsRepository(repo.database_path, repo.vault_path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        approval_future = executor.submit(
            repo.approve_application, application.id, report.id, "Reviewed"
        )
        assert validation_complete.wait(5)
        selection_future = executor.submit(
            competing.select_resume, application.id, second.id
        )
        time.sleep(0.2)
        assert not selection_future.done()
        release_validation.set()
        approval = approval_future.result(timeout=5)
        selection_future.result(timeout=5)

    assert approval.resume_version_id == first.id
    assert approval.report_id == report.id


def test_approval_audit_failure_rolls_back_pending_cancellation_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application, _resume, report = prepare_approvable_application(repo, tmp_path)
    provider = create_provider(repo)
    pending = create_bound_task(repo, application.id, provider.id, "analyze_jd")
    terminal = create_bound_task(repo, application.id, provider.id, "analyze_jd")
    terminal_claim = claim_task(repo, terminal.id)
    repo.fail_ai_task(terminal.id, terminal_claim, "provider_error")
    before_application = repo.get_application(application.id)
    before_pending = repo.get_ai_task(pending.id)
    before_terminal = repo.get_ai_task(terminal.id)
    before_events = repo.list_audit_events()
    original = repo._write_audit
    attempted_events: list[str] = []

    def fail_approval_audit(connection, event_type, *args, **kwargs):
        attempted_events.append(event_type)
        if event_type == "application.approved":
            assert attempted_events[-2:] == ["ai_task.failed", "application.approved"]
            raise RuntimeError("injected approval audit failure")
        return original(connection, event_type, *args, **kwargs)

    monkeypatch.setattr(repo, "_write_audit", fail_approval_audit)
    with pytest.raises(RuntimeError, match="injected approval audit failure"):
        repo.approve_application(application.id, report.id, "Reviewed")

    assert repo.get_application(application.id) == before_application
    assert repo.get_ai_task(pending.id) == before_pending
    assert repo.get_ai_task(terminal.id) == before_terminal
    assert repo.list_audit_events() == before_events
    assert attempted_events == ["ai_task.failed", "application.approved"]
    with repo.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM approvals WHERE application_id = ?",
            (application.id,),
        ).fetchone()[0] == 0


def test_submission_failure_rolls_back_state_and_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application, _resume, report = prepare_approvable_application(repo, tmp_path)
    provider = create_provider(repo)
    pending = create_bound_task(repo, application.id, provider.id, "analyze_jd")
    terminal = create_bound_task(repo, application.id, provider.id, "analyze_jd")
    terminal_claim = claim_task(repo, terminal.id)
    repo.fail_ai_task(terminal.id, terminal_claim, "provider_error")
    repo.approve_application(application.id, report.id, "Reviewed")
    with repo.connection() as connection:
        connection.execute(
            """
            UPDATE ai_tasks
            SET status = 'pending', started_at = NULL, completed_at = NULL,
                error_summary = '', claim_token = NULL, claim_expires_at = NULL,
                invocation_started_at = NULL
            WHERE id = ?
            """,
            (pending.id,),
        )
    before_application = repo.get_application(application.id)
    before_pending = repo.get_ai_task(pending.id)
    before_terminal = repo.get_ai_task(terminal.id)
    before_events = repo.list_audit_events()
    original = repo._write_audit
    attempted_events: list[str] = []

    def fail_submission_audit(connection, event_type, *args, **kwargs):
        attempted_events.append(event_type)
        if event_type == "application.submitted_manual":
            assert attempted_events[-2:] == [
                "ai_task.failed",
                "application.submitted_manual",
            ]
            raise RuntimeError("injected submission audit failure")
        return original(connection, event_type, *args, **kwargs)

    monkeypatch.setattr(repo, "_write_audit", fail_submission_audit)
    with pytest.raises(RuntimeError, match="injected submission audit failure"):
        repo.mark_submitted_manual(application.id, "Submitted")

    assert repo.get_application(application.id) == before_application
    assert repo.get_ai_task(pending.id) == before_pending
    assert repo.get_ai_task(terminal.id) == before_terminal
    assert repo.list_audit_events() == before_events
    assert attempted_events == ["ai_task.failed", "application.submitted_manual"]


def test_database_contexts_commit_or_rollback_and_always_close(tmp_path: Path):
    database_path = tmp_path / "careerops.db"
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", ResourceWarning)
        initialize_database(database_path)
        repo = CareerOpsRepository(database_path, tmp_path / "vault")

        with repo.connection() as committed:
            committed.execute(
                """
                INSERT INTO profile_facts (
                    id, fact_key, fact_value, verification_state, privacy_level,
                    notes, created_at, updated_at
                ) VALUES ('fact_committed', 'skill', 'Python', 'verified', 'private', '', 'now', 'now')
                """
            )
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            committed.execute("SELECT 1")

        with pytest.raises(RuntimeError, match="rollback"):
            with repo.connection() as rolled_back:
                rolled_back.execute(
                    """
                    INSERT INTO profile_facts (
                        id, fact_key, fact_value, verification_state, privacy_level,
                        notes, created_at, updated_at
                    ) VALUES ('fact_rolled_back', 'skill', 'SQL', 'verified', 'private', '', 'now', 'now')
                    """
                )
                raise RuntimeError("rollback")
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            rolled_back.execute("SELECT 1")

        assert repo.get_profile_fact("fact_committed").fact_value == "Python"
        with pytest.raises(KeyError):
            repo.get_profile_fact("fact_rolled_back")
        gc.collect()

    resource_warnings = [item for item in captured if issubclass(item.category, ResourceWarning)]
    assert resource_warnings == []
    moved_path = tmp_path / "moved.db"
    database_path.replace(moved_path)
    moved_path.unlink()


def test_ai_task_is_persisted_pending_before_execution(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)

    source_task = create_task(repo, application.id, provider.id)
    source_claim = claim_task(repo, source_task.id)
    input_path = attempt_file(repo, source_task.id, "input.json")
    input_path.write_bytes(b"{}")
    artifact = repo.save_ai_artifact(
        source_task.id,
        source_claim,
        "context",
        input_path,
        hashlib.sha256(b"{}").hexdigest(),
        "1",
    )
    task = create_task(repo, application.id, provider.id, [artifact.id])

    assert task.status == "pending"
    assert task.started_at is None
    assert task.completed_at is None
    assert task.input_artifact_ids == [artifact.id]
    assert repo.get_ai_task(task.id).provider_config_id == provider.id
    assert {item.id for item in repo.list_ai_tasks(application.id)} == {
        source_task.id,
        task.id,
    }


def test_ai_task_creation_materializes_before_commit_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)

    def reject_public_reload(_task_id: str):
        raise RuntimeError("post-commit task getter must not run")

    monkeypatch.setattr(repo, "get_ai_task", reject_public_reload)

    task = create_task(repo, application.id, provider.id)

    verifier = CareerOpsRepository(repo.database_path, repo.vault_path)
    assert verifier.get_ai_task(task.id) == task
    assert task.status == "pending"


def test_only_one_worker_can_claim_pending_task(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    second_worker = CareerOpsRepository(repo.database_path, repo.vault_path)

    claim_token = repo.claim_ai_task(task.id)

    assert isinstance(claim_token, str)
    assert len(claim_token) >= 32
    assert second_worker.claim_ai_task(task.id) is None
    assert repo.get_ai_task(task.id).status == "running"


def test_legacy_success_terminal_shortcuts_require_aggregate_finalizer(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    completed_task = create_task(repo, application.id, provider.id)
    blocked_task = create_task(repo, application.id, provider.id)
    completed_claim = claim_task(repo, completed_task.id)
    blocked_claim = claim_task(repo, blocked_task.id)

    with pytest.raises(ValueError, match="aggregate finalizer"):
        repo.complete_ai_task(completed_task.id, completed_claim)
    with pytest.raises(ValueError, match="aggregate finalizer"):
        repo.block_ai_task(blocked_task.id, blocked_claim, "verification_failed")

    assert repo.get_ai_task(completed_task.id).status == "running"
    assert repo.get_ai_task(blocked_task.id).status == "running"


def test_expired_claim_requeues_but_invocation_fence_survives(tmp_path: Path):
    repo = CareerOpsRepository(
        tmp_path / "careerops.db",
        tmp_path / "vault",
        claim_lease_seconds=15,
    )
    repo.initialize()
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    first_claim = claim_task(repo, task.id)
    assert repo.begin_ai_task_invocation(task.id, first_claim) is True

    expire_claim(repo, task.id)
    assert repo.requeue_interrupted_ai_tasks() == 1
    second_claim = claim_task(repo, task.id)

    assert second_claim != first_claim
    assert repo.begin_ai_task_invocation(task.id, second_claim) is False


def test_task_start_complete_and_failure_are_durable_and_audited(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    resume = repo.create_resume_version(
        name="Current resume",
        role_family="software",
        tex_path=tmp_path / "resume.tex",
        tex_sha256="a" * 64,
    )
    repo.select_resume(application.id, resume.id)
    provider = create_provider(repo)
    completed_task = create_task(repo, application.id, provider.id)
    failed_task = create_task(repo, application.id, provider.id)

    completed_token = repo.start_ai_task(completed_task.id)
    assert repo.begin_ai_task_invocation(completed_task.id, completed_token) is True
    for artifact_type in ("context", "raw_output", "validated_output"):
        write_attempt_artifact(
            repo,
            completed_task.id,
            completed_token,
            artifact_type,
            artifact_type.encode("ascii"),
        )
    completed = repo.finalize_ai_task_attempt(
        task_id=completed_task.id,
        claim_token=completed_token,
        terminal_status="completed",
        error_summary="",
        expected_jd_snapshot_id=application.jd_snapshot_id,
        expected_resume_version_id=resume.id,
        findings=[],
        success_payload={
            "fit": {"score": 80},
            "risks": {"blockers": [], "warnings": []},
        },
    )
    failed_token = repo.start_ai_task(failed_task.id)
    failed = repo.fail_ai_task(
        failed_task.id, failed_token, "api_key=top-secret provider unavailable"
    )

    assert completed.status == "completed"
    assert completed.completed_at is not None
    assert failed.status == "failed"
    assert "top-secret" not in failed.error_summary
    events = repo.list_audit_events()
    assert any(event.event_type == "ai_task.completed" for event in events)
    failure_event = next(event for event in events if event.event_type == "ai_task.failed")
    assert "top-secret" not in str(failure_event.after_json)


def test_audit_and_error_persistence_redact_adversarial_secret_formats(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim_token = repo.start_ai_task(task.id)
    secret_payload = """
    {"api_key": "top secret with spaces", "password": "line one
    line two"}
    Authorization: Bearer authorization-token
    token='quoted token with spaces'
    secret=unquoted secret value with spaces
    """

    repo.create_profile_fact(
        fact_key="review.notes",
        fact_value="private",
        verification_state="verified",
        privacy_level="private",
        notes=secret_payload,
    )
    failed = repo.fail_ai_task(task.id, claim_token, secret_payload)

    assert failed.error_summary == "provider_error"
    persisted_audits = str(
        [
            event.after_json
            for event in repo.list_audit_events()
            if event.event_type in {"profile_fact.created", "ai_task.failed"}
        ]
    )
    for secret in (
        "top secret with spaces",
        "line one",
        "line two",
        "authorization-token",
        "quoted token with spaces",
        "unquoted secret value with spaces",
    ):
        assert secret not in persisted_audits


def test_audit_redaction_recurses_tuples_and_rejects_unsupported_containers(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    with repo.connection() as connection:
        repo._write_audit(
            connection,
            "redaction.tested",
            "test",
            "nested",
            {
                "items": (
                    {"api_key": "before-tuple-secret"},
                    ("Authorization: Bearer before-bearer-secret",),
                )
            },
            {
                "nested": [
                    ({"password": "after-tuple-secret"},),
                    {"headers": ("Bearer after-bearer-secret",)},
                ]
            },
        )

    event = next(
        item for item in repo.list_audit_events() if item.event_type == "redaction.tested"
    )
    serialized = str((event.before_json, event.after_json))
    for secret in (
        "before-tuple-secret",
        "before-bearer-secret",
        "after-tuple-secret",
        "after-bearer-secret",
    ):
        assert secret not in serialized
    assert isinstance(event.before_json["items"], list)
    assert isinstance(event.after_json["nested"][0], list)

    with pytest.raises(TypeError, match="Unsupported audit value"):
        with repo.connection() as connection:
            repo._write_audit(
                connection,
                "redaction.rejected",
                "test",
                "unsupported",
                None,
                {"unsupported": {"token=must-not-stringify"}},
            )


def test_rapid_audit_events_return_in_causal_microsecond_order(tmp_path: Path):
    repo = make_repo(tmp_path)
    expected_entity_ids = [f"ordered-{index}" for index in range(20)]
    with repo.connection() as connection:
        for entity_id in expected_entity_ids:
            repo._write_audit(
                connection,
                "ordering.tested",
                "test",
                entity_id,
                None,
                {"entity_id": entity_id},
            )

    events = [
        event
        for event in repo.list_audit_events()
        if event.event_type == "ordering.tested"
    ]
    timestamps = [event.created_at for event in events]
    assert [event.entity_id for event in events] == expected_entity_ids
    assert timestamps == sorted(timestamps)
    assert len(set(timestamps)) == len(timestamps)
    assert all(
        len(timestamp.rsplit(".", 1)[-1].split("+", 1)[0]) == 6
        for timestamp in timestamps
    )


def test_interrupted_tasks_requeue_without_losing_artifacts(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim_token = repo.start_ai_task(task.id)
    context_path = attempt_file(repo, task.id, "context.json")
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_bytes(b"context")

    artifact = repo.save_ai_artifact(
        task.id,
        claim_token,
        "context",
        context_path,
        hashlib.sha256(b"context").hexdigest(),
        "1",
    )

    expire_claim(repo, task.id)
    assert repo.requeue_interrupted_ai_tasks() == 1
    requeued = repo.get_ai_task(task.id)
    assert requeued.status == "pending"
    assert requeued.started_at is None
    assert repo.list_ai_artifacts(task.id) == [artifact]


def test_ai_artifacts_require_contained_existing_checksum_matched_files(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim_token = claim_task(repo, task.id)
    valid_path = attempt_file(repo, task.id, "valid.json")
    valid_path.write_bytes(b'{"valid": true}')
    valid_sha = hashlib.sha256(valid_path.read_bytes()).hexdigest()
    escaped_path = tmp_path / "escaped.json"
    escaped_path.write_bytes(b"escaped")

    with pytest.raises(ValueError, match="vault"):
        repo.save_ai_artifact(
            task.id,
            claim_token,
            "context",
            escaped_path,
            hashlib.sha256(b"escaped").hexdigest(),
            "1",
        )
    with pytest.raises(FileNotFoundError):
        repo.save_ai_artifact(
            task.id,
            claim_token,
            "context",
            attempt_file(repo, task.id, "missing.json"),
            valid_sha,
            "1",
        )
    with pytest.raises(ValueError, match="SHA-256"):
        repo.save_ai_artifact(
            task.id, claim_token, "context", valid_path, "not-a-checksum", "1"
        )
    with pytest.raises(ValueError, match="checksum"):
        repo.save_ai_artifact(
            task.id, claim_token, "context", valid_path, "0" * 64, "1"
        )

    artifact = repo.save_ai_artifact(
        task.id, claim_token, "context", valid_path, valid_sha, "1"
    )
    assert artifact.path != valid_path.resolve()
    assert artifact.path.read_bytes() == valid_path.read_bytes()


def test_ai_artifact_getters_detect_post_save_tampering(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    assert repo.begin_ai_task_invocation(task.id, claim) is True
    path = attempt_file(repo, task.id, "tampered.json")
    path.write_bytes(b"original")
    artifact = repo.save_ai_artifact(
        task.id,
        claim,
        "context",
        path,
        hashlib.sha256(b"original").hexdigest(),
        "1",
    )
    assert artifact.path != path.resolve()
    artifact.path.write_bytes(b"changed")

    with pytest.raises(ValueError, match="integrity"):
        repo.get_ai_artifact(artifact.id)
    with pytest.raises(ValueError, match="integrity"):
        repo.list_ai_artifacts(task.id)
    with pytest.raises(ValueError, match="integrity"):
        repo.finalize_ai_task_attempt(
            task_id=task.id,
            claim_token=claim,
            terminal_status="blocked",
            error_summary="invalid_output",
            expected_jd_snapshot_id=application.jd_snapshot_id,
            expected_resume_version_id=None,
            findings=[],
        )

    assert repo.get_ai_task(task.id).status == "running"
    assert repo.list_application_versions(application.id) == []


def test_ai_artifacts_reject_hardlinks_and_symlinks(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    target = attempt_file(repo, task.id, "target.json")
    hardlink = attempt_file(repo, task.id, "hardlink.json")
    target.write_bytes(b"bound")
    hardlink.hardlink_to(target)

    with pytest.raises(ValueError, match="regular"):
        repo.save_ai_artifact(
            task.id,
            claim,
            "context",
            hardlink,
            hashlib.sha256(b"bound").hexdigest(),
            "1",
        )

    hardlink.unlink()
    symlink = attempt_file(repo, task.id, "symlink.json")
    try:
        symlink.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ValueError, match="regular"):
        repo.save_ai_artifact(
            task.id,
            claim,
            "context",
            symlink,
            hashlib.sha256(b"bound").hexdigest(),
            "1",
        )


def test_finalization_rejects_unowned_artifact_metadata_and_rolls_back(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)
    assert repo.begin_ai_task_invocation(task.id, claim) is True

    with pytest.raises(ValueError, match="required artifacts"):
        repo.finalize_ai_task_attempt(
            task_id=task.id,
            claim_token=claim,
            terminal_status="blocked",
            error_summary="verification_failed",
            expected_jd_snapshot_id=application.jd_snapshot_id,
            expected_resume_version_id=None,
            findings=[],
        )

    assert repo.get_ai_task(task.id).status == "running"
    assert repo.list_application_versions(application.id) == []


def test_finalization_hashes_artifacts_before_acquiring_writer_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_bound_task(repo, application.id, provider.id, "analyze_jd")
    claim = claim_task(repo, task.id)
    assert repo.begin_ai_task_invocation(task.id, claim) is True
    write_attempt_artifact(repo, task.id, claim, "context", b"context")
    digest_started = Event()
    release_digest = Event()
    original_digest = repo._stable_artifact_digest

    def delayed_digest(path: Path):
        digest_started.set()
        assert release_digest.wait(10)
        return original_digest(path)

    monkeypatch.setattr(repo, "_stable_artifact_digest", delayed_digest)
    worker = CareerOpsRepository(repo.database_path, repo.vault_path)

    def finalize():
        return repo.finalize_ai_task_attempt(
            task_id=task.id,
            claim_token=claim,
            terminal_status="blocked",
            error_summary="invalid_output",
            expected_jd_snapshot_id=application.jd_snapshot_id,
            expected_resume_version_id=None,
            findings=[],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        finalization = pool.submit(finalize)
        assert digest_started.wait(5)
        renewal = pool.submit(worker.renew_ai_task_claim, task.id, claim)
        try:
            assert renewal.result(timeout=3) is True
        finally:
            release_digest.set()
        blocked = finalization.result(timeout=5)

    assert blocked.status == "blocked"


def test_finalization_preflight_digest_mismatch_leaves_business_state_untouched(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_bound_task(repo, application.id, provider.id, "analyze_jd")
    claim = claim_task(repo, task.id)
    assert repo.begin_ai_task_invocation(task.id, claim) is True
    artifact = write_attempt_artifact(repo, task.id, claim, "context", b"context")
    artifact.path.write_bytes(b"tampered")
    before_application = repo.get_application(application.id)
    before_jd = repo.get_jd_snapshot(application.jd_snapshot_id)

    with pytest.raises(ValueError, match="integrity"):
        repo.finalize_ai_task_attempt(
            task_id=task.id,
            claim_token=claim,
            terminal_status="blocked",
            error_summary="invalid_output",
            expected_jd_snapshot_id=application.jd_snapshot_id,
            expected_resume_version_id=None,
            findings=[],
        )

    assert repo.get_ai_task(task.id).status == "running"
    assert repo.get_application(application.id) == before_application
    assert repo.get_jd_snapshot(application.jd_snapshot_id) == before_jd
    assert repo.list_application_versions(application.id) == []


def test_finalization_rejects_cross_task_and_tampered_artifacts(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    owner_task = create_task(repo, application.id, provider.id)
    owner_claim = claim_task(repo, owner_task.id)
    owner_path = attempt_file(repo, owner_task.id, "owner.json")
    owner_path.write_bytes(b"owner")
    owner_artifact = repo.save_ai_artifact(
        owner_task.id,
        owner_claim,
        "context",
        owner_path,
        hashlib.sha256(b"owner").hexdigest(),
        "1",
    )
    target_task = create_task(repo, application.id, provider.id)
    target_claim = claim_task(repo, target_task.id)
    assert repo.begin_ai_task_invocation(target_task.id, target_claim) is True
    with repo.connection() as connection:
        connection.execute(
            "UPDATE ai_artifacts SET task_id = ?, claim_token = ? WHERE id = ?",
            (target_task.id, target_claim, owner_artifact.id),
        )

    with pytest.raises(ValueError, match="artifact"):
        repo.finalize_ai_task_attempt(
            task_id=target_task.id,
            claim_token=target_claim,
            terminal_status="blocked",
            error_summary="verification_failed",
            expected_jd_snapshot_id=application.jd_snapshot_id,
            expected_resume_version_id=None,
            findings=[],
        )
    with repo.connection() as connection:
        connection.execute("DELETE FROM ai_artifacts WHERE id = ?", (owner_artifact.id,))

    target_path = attempt_file(repo, target_task.id, "target.json")
    target_path.write_bytes(b"target")
    target_artifact = repo.save_ai_artifact(
        target_task.id,
        target_claim,
        "context",
        target_path,
        hashlib.sha256(b"target").hexdigest(),
        "1",
    )
    target_artifact.path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        repo.finalize_ai_task_attempt(
            task_id=target_task.id,
            claim_token=target_claim,
            terminal_status="blocked",
            error_summary="verification_failed",
            expected_jd_snapshot_id=application.jd_snapshot_id,
            expected_resume_version_id=None,
            findings=[],
        )

    assert repo.get_ai_task(target_task.id).status == "running"
    assert repo.list_application_versions(application.id) == []


def test_finalization_rejects_artifact_from_stale_claim(tmp_path: Path):
    repo = CareerOpsRepository(
        tmp_path / "careerops.db",
        tmp_path / "vault",
        claim_lease_seconds=15,
    )
    repo.initialize()
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    first_claim = claim_task(repo, task.id)
    path = attempt_file(repo, task.id, "stale.json")
    path.write_bytes(b"stale")
    artifact = repo.save_ai_artifact(
        task.id,
        first_claim,
        "context",
        path,
        hashlib.sha256(b"stale").hexdigest(),
        "1",
    )
    expire_claim(repo, task.id)
    assert repo.requeue_interrupted_ai_tasks() == 1
    second_claim = claim_task(repo, task.id)
    assert repo.begin_ai_task_invocation(task.id, second_claim) is True

    with pytest.raises(ValueError, match="required artifacts"):
        repo.finalize_ai_task_attempt(
            task_id=task.id,
            claim_token=second_claim,
            terminal_status="blocked",
            error_summary="verification_failed",
            expected_jd_snapshot_id=application.jd_snapshot_id,
            expected_resume_version_id=None,
            findings=[],
        )

    assert repo.get_ai_task(task.id).status == "running"
    assert repo.list_application_versions(application.id) == []


def test_save_ai_finding_redacts_and_canonicalizes_every_field(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task, finding = create_aggregate_finding(
        repo,
        application.id,
        provider.id,
        finding_type="risk",
        severity="warning",
        claim_text="api_key=PRIVATE_CLAIM_KEY",
        evidence_item_id=None,
        recommendation="Authorization: Bearer PRIVATE_RECOMMENDATION",
        verifier_state="needs_evidence",
        payload={"z": 1, "token": "PRIVATE_PAYLOAD_TOKEN", "a": 2},
    )

    assert "PRIVATE" not in finding.claim_text
    assert "PRIVATE" not in finding.recommendation
    assert finding.payload == {"a": 2, "token": "[REDACTED]", "z": 1}
    with repo.connection() as connection:
        payload_json = connection.execute(
            "SELECT payload_json FROM ai_findings WHERE id = ?", (finding.id,)
        ).fetchone()[0]
    assert payload_json == '{"a":2,"token":"[REDACTED]","z":1}'
    with pytest.raises(ValueError, match="size limit"):
        finalize_findings(
            repo,
            application.id,
            provider.id,
            [
                {
                    "finding_type": "x" * 300_000,
                    "severity": "warning",
                    "claim_text": "bounded",
                    "evidence_item_id": None,
                    "recommendation": "bounded",
                    "verifier_state": "needs_evidence",
                }
            ],
        )


def test_standalone_ai_findings_and_terminal_versions_fail_closed_after_recovery(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim = claim_task(repo, task.id)

    with pytest.raises(ValueError, match="aggregate finalizer"):
        repo.save_ai_finding(
            task_id=task.id,
            claim_token=claim,
            finding_type="forged",
            severity="warning",
            claim_text="Standalone finding",
            evidence_item_id=None,
            recommendation="Do not persist",
            verifier_state="needs_evidence",
        )
    with pytest.raises(ValueError, match="aggregate finalizer"):
        repo.record_application_version(
            application.id,
            "ai_task.completed",
            {"ai_task": {"id": task.id}, "provenance": {"forged": True}},
        )
    with pytest.raises(ValueError, match="aggregate finalizer"):
        repo.record_application_version(
            application.id,
            "ai_task.blocked",
            {"ai_task": {"id": task.id}},
            ai_task_id=task.id,
            claim_token=claim,
        )

    expire_claim(repo, task.id)
    restarted = CareerOpsRepository(repo.database_path, repo.vault_path)
    restarted.initialize()

    assert restarted.list_ai_findings(task.id) == []
    assert restarted.list_application_versions(application.id) == []


def test_public_report_and_jd_json_reject_nonfinite_and_oversize(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    resume = repo.create_resume_version(
        name="Resume",
        role_family="software",
        tex_path=tmp_path / "resume.tex",
        tex_sha256="a" * 64,
    )
    repo.select_resume(application.id, resume.id)
    with pytest.raises(ValueError, match="valid JSON"):
        repo.save_report(
            application_id=application.id,
            resume_version_id=resume.id,
            jd_snapshot_id=application.jd_snapshot_id,
            fit={"score": float("nan")},
            risks={"blockers": [], "warnings": []},
        )
    with pytest.raises(ValueError, match="size limit"):
        repo.save_report(
            application_id=application.id,
            resume_version_id=resume.id,
            jd_snapshot_id=application.jd_snapshot_id,
            fit={"detail": "x" * 300_000},
            risks={"blockers": [], "warnings": []},
        )

    provider = create_provider(repo)
    task = create_bound_task(repo, application.id, provider.id, "analyze_jd")
    claim = claim_task(repo, task.id)
    before = repo.get_jd_snapshot(application.jd_snapshot_id)
    with pytest.raises(ValueError, match="aggregate finalizer"):
        repo.update_jd_requirements_from_ai(
            snapshot_id=application.jd_snapshot_id,
            task_id=task.id,
            claim_token=claim,
            raw_ai={"score": float("nan")},
            uncertainty={},
        )
    assert repo.get_jd_snapshot(application.jd_snapshot_id) == before


def test_ai_tasks_reject_unknown_input_artifact_ids(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)

    with pytest.raises(ValueError, match="artifact"):
        create_task(repo, application.id, provider.id, ["artifact_missing"])


def test_ai_task_inputs_must_belong_to_same_application_and_are_normalized(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    first_application = create_application(repo)
    second_application = repo.create_application(
        company="Other Corp",
        role_title="Data Intern",
        location="Remote",
        source_url="https://example.com/jobs/2",
        jd_text="Analyze data.",
    )
    provider = create_provider(repo)
    source_task = create_task(repo, first_application.id, provider.id)
    source_claim = claim_task(repo, source_task.id)
    input_path = attempt_file(repo, source_task.id, "scoped-input.json")
    input_path.write_bytes(b"scoped")
    artifact = repo.save_ai_artifact(
        source_task.id,
        source_claim,
        "context",
        input_path,
        hashlib.sha256(b"scoped").hexdigest(),
        "1",
    )

    with pytest.raises(ValueError, match="same application"):
        create_task(repo, second_application.id, provider.id, [artifact.id])

    target_task = create_task(repo, first_application.id, provider.id, [artifact.id])
    with repo.connection() as connection:
        links = connection.execute(
            """
            SELECT task_id, artifact_id
            FROM ai_task_input_artifacts
            WHERE task_id = ?
            """,
            (target_task.id,),
        ).fetchall()
    assert [(row["task_id"], row["artifact_id"]) for row in links] == [
        (target_task.id, artifact.id)
    ]


def test_ai_task_inputs_are_canonical_ordered_and_reject_duplicates(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    source_task = create_task(repo, application.id, provider.id)
    source_claim = claim_task(repo, source_task.id)
    artifacts = []
    for index, content in enumerate((b"first", b"second")):
        input_path = attempt_file(repo, source_task.id, f"ordered-{index}.json")
        input_path.write_bytes(content)
        artifacts.append(
            repo.save_ai_artifact(
                source_task.id,
                source_claim,
                "context",
                input_path,
                hashlib.sha256(content).hexdigest(),
                "1",
            )
        )

    task_ids_before = {task.id for task in repo.list_ai_tasks(application.id)}
    with pytest.raises(ValueError, match="duplicate"):
        create_task(
            repo,
            application.id,
            provider.id,
            [artifacts[0].id, artifacts[0].id],
        )
    assert {task.id for task in repo.list_ai_tasks(application.id)} == task_ids_before

    expected_ids = [artifacts[1].id, artifacts[0].id]
    target_task = create_task(repo, application.id, provider.id, expected_ids)
    with repo.connection() as connection:
        rows = connection.execute(
            """
            SELECT ordinal, artifact_id
            FROM ai_task_input_artifacts
            WHERE task_id = ?
            ORDER BY ordinal
            """,
            (target_task.id,),
        ).fetchall()
        connection.execute(
            "UPDATE ai_tasks SET input_artifact_ids_json = ? WHERE id = ?",
            (json.dumps(["artifact_not_canonical"]), target_task.id),
        )

    assert [(row["ordinal"], row["artifact_id"]) for row in rows] == [
        (0, artifacts[1].id),
        (1, artifacts[0].id),
    ]
    assert repo.get_ai_task(target_task.id).input_artifact_ids == expected_ids
    listed = {task.id: task for task in repo.list_ai_tasks(application.id)}
    assert listed[target_task.id].input_artifact_ids == expected_ids


def test_ai_task_input_trigger_rejects_cross_application_link(tmp_path: Path):
    repo = make_repo(tmp_path)
    first_application = create_application(repo)
    second_application = repo.create_application(
        company="Other Corp",
        role_title="Data Intern",
        location="Remote",
        source_url="https://example.com/jobs/2",
        jd_text="Analyze data.",
    )
    provider = create_provider(repo)
    source_task = create_task(repo, first_application.id, provider.id)
    source_claim = claim_task(repo, source_task.id)
    input_path = attempt_file(repo, source_task.id, "trigger-input.json")
    input_path.write_bytes(b"trigger")
    artifact = repo.save_ai_artifact(
        source_task.id,
        source_claim,
        "context",
        input_path,
        hashlib.sha256(b"trigger").hexdigest(),
        "1",
    )
    target_task = create_task(repo, second_application.id, provider.id)

    with pytest.raises(sqlite3.IntegrityError, match="application"):
        with repo.connection() as connection:
            connection.execute(
                """
                INSERT INTO ai_task_input_artifacts (
                    task_id, artifact_id, ordinal, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (target_task.id, artifact.id, 0, "now"),
            )


def test_v6_ai_task_inputs_upgrade_to_valid_ordered_canonical_links(tmp_path: Path):
    repo = make_repo(tmp_path)
    first_application = create_application(repo)
    second_application = repo.create_application(
        company="Other Corp",
        role_title="Data Intern",
        location="Remote",
        source_url="https://example.com/jobs/2",
        jd_text="Analyze data.",
    )
    provider = create_provider(repo)

    first_source = create_task(repo, first_application.id, provider.id)
    first_claim = claim_task(repo, first_source.id)
    valid_artifacts = []
    for index, content in enumerate((b"valid-first", b"valid-second")):
        path = attempt_file(repo, first_source.id, f"migration-valid-{index}.json")
        path.write_bytes(content)
        valid_artifacts.append(
            repo.save_ai_artifact(
                first_source.id,
                first_claim,
                "context",
                path,
                hashlib.sha256(content).hexdigest(),
                "1",
            )
        )

    second_source = create_task(repo, second_application.id, provider.id)
    second_claim = claim_task(repo, second_source.id)
    cross_path = attempt_file(repo, second_source.id, "migration-cross.json")
    cross_path.write_bytes(b"cross")
    cross_artifact = repo.save_ai_artifact(
        second_source.id,
        second_claim,
        "context",
        cross_path,
        hashlib.sha256(b"cross").hexdigest(),
        "1",
    )
    target_task = create_task(repo, first_application.id, provider.id)
    legacy_ids = [
        valid_artifacts[1].id,
        cross_artifact.id,
        valid_artifacts[1].id,
        "artifact_missing",
        valid_artifacts[0].id,
    ]
    with repo.connection() as connection:
        connection.execute(
            "UPDATE ai_tasks SET input_artifact_ids_json = ? WHERE id = ?",
            (json.dumps(legacy_ids), target_task.id),
        )
        connection.execute("DROP TABLE ai_task_input_artifacts")
        connection.execute("PRAGMA user_version = 6")

    with closing(sqlite3.connect(repo.database_path)) as legacy_connection:
        assert legacy_connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert legacy_connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'ai_task_input_artifacts'
            """
        ).fetchone() is None

    repo.initialize()

    expected_ids = [valid_artifacts[1].id, valid_artifacts[0].id]
    with repo.connection() as connection:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(ai_task_input_artifacts)"
            ).fetchall()
        }
        rows = connection.execute(
            """
            SELECT ordinal, artifact_id
            FROM ai_task_input_artifacts
            WHERE task_id = ?
            ORDER BY ordinal
            """,
            (target_task.id,),
        ).fetchall()
        compatibility_json = connection.execute(
            "SELECT input_artifact_ids_json FROM ai_tasks WHERE id = ?",
            (target_task.id,),
        ).fetchone()["input_artifact_ids_json"]

    assert "ordinal" in columns
    assert [(row["ordinal"], row["artifact_id"]) for row in rows] == [
        (0, valid_artifacts[1].id),
        (1, valid_artifacts[0].id),
    ]
    assert json.loads(compatibility_json) == expected_ids
    assert repo.get_ai_task(target_task.id).input_artifact_ids == expected_ids


def test_simultaneous_claims_have_exactly_one_owner(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    workers = [
        CareerOpsRepository(repo.database_path, repo.vault_path),
        CareerOpsRepository(repo.database_path, repo.vault_path),
    ]
    barrier = Barrier(len(workers))

    def claim(worker: CareerOpsRepository):
        barrier.wait()
        return worker.claim_ai_task(task.id)

    with ThreadPoolExecutor(max_workers=len(workers)) as executor:
        tokens = list(executor.map(claim, workers))

    owned_tokens = [token for token in tokens if token is not None]
    assert len(owned_tokens) == 1
    assert isinstance(owned_tokens[0], str)


def test_requeue_invalidates_stale_worker_claim(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    worker_a = CareerOpsRepository(repo.database_path, repo.vault_path)
    worker_b = CareerOpsRepository(repo.database_path, repo.vault_path)

    stale_token = worker_a.claim_ai_task(task.id)
    assert isinstance(stale_token, str)
    expire_claim(repo, task.id)
    assert repo.requeue_interrupted_ai_tasks() == 1
    current_token = worker_b.claim_ai_task(task.id)
    assert isinstance(current_token, str)
    assert current_token != stale_token

    with pytest.raises(ValueError, match="aggregate finalizer"):
        worker_a.complete_ai_task(task.id, stale_token)
    with pytest.raises(ValueError, match="claim"):
        worker_a.fail_ai_task(task.id, stale_token, "provider_unavailable")

    assert worker_b.begin_ai_task_invocation(task.id, current_token) is True
    assert (
        worker_b.fail_ai_task(task.id, current_token, "provider_unavailable").status
        == "failed"
    )


def test_requeue_invalidates_stale_artifact_writes(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    worker_a = CareerOpsRepository(repo.database_path, repo.vault_path)
    worker_b = CareerOpsRepository(repo.database_path, repo.vault_path)
    stale_token = claim_task(worker_a, task.id)
    first_path = attempt_file(repo, task.id, "attempt-a.json", attempt="attempt-a")
    first_path.write_bytes(b"attempt-a")

    first_artifact = worker_a.save_ai_artifact(
        task.id,
        stale_token,
        "raw_output",
        first_path,
        hashlib.sha256(b"attempt-a").hexdigest(),
        "1",
    )
    expire_claim(repo, task.id)
    assert repo.requeue_interrupted_ai_tasks() == 1
    current_token = claim_task(worker_b, task.id)
    second_path = attempt_file(repo, task.id, "attempt-b.json", attempt="attempt-b")
    second_path.write_bytes(b"attempt-b")

    with pytest.raises(ValueError, match="claim"):
        worker_a.save_ai_artifact(
            task.id,
            stale_token,
            "raw_output",
            second_path,
            hashlib.sha256(b"attempt-b").hexdigest(),
            "1",
        )
    second_artifact = worker_b.save_ai_artifact(
        task.id,
        current_token,
        "raw_output",
        second_path,
        hashlib.sha256(b"attempt-b").hexdigest(),
        "1",
    )
    assert first_artifact.claim_token == stale_token
    assert second_artifact.claim_token == current_token
    assert repo.list_ai_artifacts(task.id) == [first_artifact, second_artifact]
    assert repo.list_ai_findings(task.id) == []


def test_competing_terminal_updates_allow_only_one_claim_owner_update(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    resume = repo.create_resume_version(
        name="Current resume",
        role_family="software",
        tex_path=tmp_path / "resume.tex",
        tex_sha256="a" * 64,
    )
    repo.select_resume(application.id, resume.id)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim_token = repo.claim_ai_task(task.id)
    assert isinstance(claim_token, str)
    assert repo.begin_ai_task_invocation(task.id, claim_token) is True
    for artifact_type in ("context", "raw_output", "validated_output"):
        write_attempt_artifact(
            repo,
            task.id,
            claim_token,
            artifact_type,
            artifact_type.encode("ascii"),
        )
    workers = [
        CareerOpsRepository(repo.database_path, repo.vault_path),
        CareerOpsRepository(repo.database_path, repo.vault_path),
    ]
    barrier = Barrier(len(workers))

    def complete():
        barrier.wait()
        return workers[0].finalize_ai_task_attempt(
            task_id=task.id,
            claim_token=claim_token,
            terminal_status="completed",
            error_summary="",
            expected_jd_snapshot_id=application.jd_snapshot_id,
            expected_resume_version_id=resume.id,
            findings=[],
            success_payload={
                "fit": {"score": 80},
                "risks": {"blockers": [], "warnings": []},
            },
        )

    def fail():
        barrier.wait()
        return workers[1].fail_ai_task(task.id, claim_token, "provider_unavailable")

    with ThreadPoolExecutor(max_workers=len(workers)) as executor:
        futures = [executor.submit(complete), executor.submit(fail)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result().status)
            except ValueError as exc:
                outcomes.append(str(exc))

    assert sum(item in {"completed", "failed"} for item in outcomes) == 1
    assert sum("claim" in item for item in outcomes) == 1


def test_ai_finding_decisions_are_persisted_and_audited(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task, finding = create_aggregate_finding(
        repo,
        application.id,
        provider.id,
        finding_type="unsupported_claim",
        severity="blocking",
        claim_text="Improved throughput by 50%",
        evidence_item_id=None,
        recommendation="Remove the unsupported metric",
        verifier_state="blocked",
        decision="pending",
        payload={"reason": "password=hunter2"},
    )
    decided = repo.update_ai_finding_decision(
        finding.id, "rejected", expected_decision="pending"
    )

    assert repo.get_ai_finding(finding.id) == decided
    assert decided.decision == "rejected"
    assert repo.list_ai_findings(task.id) == [decided]
    event = next(
        item for item in repo.list_audit_events() if item.event_type == "ai_finding.decision_updated"
    )
    assert "hunter2" not in str(event.after_json)


def test_ai_findings_only_start_pending_and_decisions_use_compare_and_set(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    finding_values = {
        "finding_type": "unsupported_claim",
        "severity": "blocking",
        "claim_text": "Improved throughput by 50%",
        "evidence_item_id": None,
        "recommendation": "Remove the unsupported metric",
        "verifier_state": "verified",
    }

    with pytest.raises(ValueError, match="pending"):
        finalize_findings(
            repo,
            application.id,
            provider.id,
            [{**finding_values, "decision": "accepted"}],
        )

    _task, finding = create_aggregate_finding(
        repo, application.id, provider.id, **finding_values
    )
    accepted = repo.update_ai_finding_decision(
        finding.id, "accepted", expected_decision="pending"
    )
    assert accepted.decision == "accepted"
    with pytest.raises(ValueError, match="decision"):
        repo.update_ai_finding_decision(
            finding.id, "rejected", expected_decision="pending"
        )
    with pytest.raises(ValueError, match="decision"):
        repo.update_ai_finding_decision(
            finding.id, "invented", expected_decision="accepted"
        )
    assert repo.get_ai_finding(finding.id).decision == "accepted"
    decision_events = [
        event
        for event in repo.list_audit_events()
        if event.event_type == "ai_finding.decision_updated"
    ]
    assert len(decision_events) == 1


@pytest.mark.parametrize("verifier_state", ["blocked", "rejected", "needs_evidence"])
def test_unsafe_verifier_states_cannot_be_accepted(
    tmp_path: Path, verifier_state: str
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    _task, finding = create_aggregate_finding(
        repo,
        application.id,
        provider.id,
        finding_type="risk",
        severity="blocking",
        claim_text="Unsafe claim",
        evidence_item_id=None,
        recommendation="Reject",
        verifier_state=verifier_state,
    )

    with pytest.raises(ValueError, match="verifier"):
        repo.update_ai_finding_decision(
            finding.id, "accepted", expected_decision="pending"
        )

    assert repo.get_ai_finding(finding.id).decision == "pending"
    assert not any(
        event.entity_id == finding.id
        for event in repo.list_audit_events()
        if event.event_type == "ai_finding.decision_updated"
    )


def approve_existing_application(
    repo: CareerOpsRepository, application_id: str
):
    application = repo.get_application(application_id)
    source_dir = repo.vault_path / "resumes" / application_id
    source_dir.mkdir(parents=True, exist_ok=True)
    tex_path = source_dir / "decision-race.tex"
    pdf_path = source_dir / "decision-race.pdf"
    tex_bytes = b"decision race resume"
    pdf_bytes = b"%PDF-1.4\ndecision race"
    tex_path.write_bytes(tex_bytes)
    pdf_path.write_bytes(pdf_bytes)
    resume = repo.create_resume_version(
        name="Decision race resume",
        role_family="software",
        tex_path=tex_path,
        tex_sha256=hashlib.sha256(tex_bytes).hexdigest(),
        pdf_path=pdf_path,
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
    )
    repo.select_resume(application_id, resume.id)
    report = repo.save_report(
        application_id=application_id,
        resume_version_id=resume.id,
        jd_snapshot_id=application.jd_snapshot_id,
        fit={"score": 90},
        risks={"blockers": [], "warnings": []},
    )
    return repo.approve_application(application_id, report.id, "Reviewed")


@pytest.mark.parametrize("submitted", [False, True], ids=["approved", "submitted"])
def test_finding_decision_rejects_application_that_became_read_only(
    tmp_path: Path, submitted: bool
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    _task, finding = create_aggregate_finding(
        repo,
        application.id,
        provider.id,
        finding_type="verified_claim",
        severity="info",
        claim_text="Verified claim",
        evidence_item_id=None,
        recommendation="Use",
        verifier_state="verified",
    )
    approve_existing_application(repo, application.id)
    if submitted:
        repo.mark_submitted_manual(application.id, "Submitted")

    with pytest.raises(ValueError, match="approved or submitted"):
        repo.update_ai_finding_decision(
            finding.id, "accepted", expected_decision="pending"
        )

    assert repo.get_ai_finding(finding.id).decision == "pending"
    assert not any(
        event.event_type == "ai_finding.decision_updated"
        and event.entity_id == finding.id
        for event in repo.list_audit_events()
    )


def test_finding_decision_waiting_on_approval_transaction_observes_read_only_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    _task, finding = create_aggregate_finding(
        repo,
        application.id,
        provider.id,
        finding_type="verified_claim",
        severity="info",
        claim_text="Verified claim",
        evidence_item_id=None,
        recommendation="Use",
        verifier_state="verified",
    )
    application = repo.get_application(application.id)
    source_dir = repo.vault_path / "resumes" / application.id
    source_dir.mkdir(parents=True, exist_ok=True)
    tex_path = source_dir / "concurrent-decision.tex"
    pdf_path = source_dir / "concurrent-decision.pdf"
    tex_bytes = b"concurrent decision resume"
    pdf_bytes = b"%PDF-1.4\nconcurrent decision"
    tex_path.write_bytes(tex_bytes)
    pdf_path.write_bytes(pdf_bytes)
    resume = repo.create_resume_version(
        name="Concurrent decision resume",
        role_family="software",
        tex_path=tex_path,
        tex_sha256=hashlib.sha256(tex_bytes).hexdigest(),
        pdf_path=pdf_path,
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
    )
    repo.select_resume(application.id, resume.id)
    report = repo.save_report(
        application_id=application.id,
        resume_version_id=resume.id,
        jd_snapshot_id=application.jd_snapshot_id,
        fit={"score": 90},
        risks={"blockers": [], "warnings": []},
    )
    approval_repo = CareerOpsRepository(repo.database_path, repo.vault_path)
    decision_repo = CareerOpsRepository(repo.database_path, repo.vault_path)
    approval_updated = Event()
    release_approval = Event()
    decision_started = Event()
    original_write_audit = approval_repo._write_audit

    def pause_approval_audit(
        connection, event_type, entity_type, entity_id, before, after
    ):
        original_write_audit(
            connection, event_type, entity_type, entity_id, before, after
        )
        if event_type == "application.approved":
            approval_updated.set()
            assert release_approval.wait(2)

    monkeypatch.setattr(approval_repo, "_write_audit", pause_approval_audit)

    def decide():
        decision_started.set()
        return decision_repo.update_ai_finding_decision(
            finding.id, "accepted", expected_decision="pending"
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        approval_future = executor.submit(
            approval_repo.approve_application,
            application.id,
            report.id,
            "Reviewed",
        )
        assert approval_updated.wait(2)
        decision_future = executor.submit(decide)
        assert decision_started.wait(2)
        release_approval.set()
        approval_future.result(timeout=2)
        with pytest.raises(ValueError, match="approved or submitted"):
            decision_future.result(timeout=2)

    assert repo.get_ai_finding(finding.id).decision == "pending"


def test_concurrent_finding_decisions_have_one_causal_audit(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    _task, finding = create_aggregate_finding(
        repo,
        application.id,
        provider.id,
        finding_type="verified_claim",
        severity="info",
        claim_text="Verified claim",
        evidence_item_id=None,
        recommendation="Use",
        verifier_state="verified",
    )
    workers = [
        CareerOpsRepository(repo.database_path, repo.vault_path),
        CareerOpsRepository(repo.database_path, repo.vault_path),
    ]
    decisions = ["accepted", "rejected"]
    barrier = Barrier(len(workers))

    def decide(worker_number: int):
        barrier.wait()
        return workers[worker_number].update_ai_finding_decision(
            finding.id,
            decisions[worker_number],
            expected_decision="pending",
        )

    with ThreadPoolExecutor(max_workers=len(workers)) as executor:
        futures = [executor.submit(decide, index) for index in range(len(workers))]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result().decision)
            except ValueError as exc:
                outcomes.append(str(exc))

    final = repo.get_ai_finding(finding.id)
    assert sum(item in {"accepted", "rejected"} for item in outcomes) == 1
    assert sum("decision" in item for item in outcomes) == 1
    events = [
        event
        for event in repo.list_audit_events()
        if event.event_type == "ai_finding.decision_updated"
        and event.entity_id == finding.id
    ]
    assert len(events) == 1
    assert events[0].before_json["id"] == finding.id
    assert events[0].before_json["decision"] == "pending"
    assert events[0].after_json["id"] == finding.id
    assert events[0].after_json["decision"] == final.decision
    assert events[0].before_json["updated_at"] == finding.updated_at
    assert events[0].after_json["updated_at"] == final.updated_at


def test_repository_rejects_non_finite_state_values(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)

    with pytest.raises(ValueError, match="privacy mode"):
        repo.upsert_ai_provider(
            provider_kind="mock",
            display_name="Invalid",
            privacy_mode="invented",
            endpoint="",
            model="invalid",
            enabled=True,
            availability_binding=provider_binding(
                model="invalid", privacy_mode="invented"
            ),
        )

    provider = create_provider(repo)
    provider_config = repo.get_ai_provider(provider.id)
    prompt = load_prompt_template("analyze_fit")
    authority = {
        "prompt_sha256": prompt.sha256,
        "expected_provider_binding": provider_config.availability_binding,
        "expected_provider_binding_sha256": (
            provider_config.availability_binding_sha256
        ),
    }
    with pytest.raises(ValueError, match="task type"):
        repo.create_ai_task(
            application_id=application.id,
            task_type="invented",
            provider_config_id=provider.id,
            privacy_mode="local_private",
            prompt_template_id="invalid",
            input_artifact_ids=[],
            **authority,
        )
    with pytest.raises(ValueError, match="privacy mode"):
        repo.create_ai_task(
            application_id=application.id,
            task_type="analyze_fit",
            provider_config_id=provider.id,
            privacy_mode="invented",
            prompt_template_id="analyze_fit_v1",
            input_artifact_ids=[],
            **authority,
        )

    with pytest.raises(ValueError, match="severity"):
        finalize_findings(
            repo,
            application.id,
            provider.id,
            [
                {
                    "finding_type": "risk",
                    "severity": "invented",
                    "claim_text": "Claim",
                    "evidence_item_id": None,
                    "recommendation": "Review",
                    "verifier_state": "blocked",
                }
            ],
        )
    with pytest.raises(ValueError, match="verifier state"):
        finalize_findings(
            repo,
            application.id,
            provider.id,
            [
                {
                    "finding_type": "risk",
                    "severity": "warning",
                    "claim_text": "Claim",
                    "evidence_item_id": None,
                    "recommendation": "Review",
                    "verifier_state": "invented",
                }
            ],
        )
    with pytest.raises(ValueError, match="privacy level"):
        repo.create_profile_fact(
            fact_key="skill",
            fact_value="Python",
            verification_state="verified",
            privacy_level="invented",
        )
    with pytest.raises(ValueError, match="privacy level"):
        repo.create_evidence_item(
            claim_text="Claim",
            evidence_type="project",
            verification_state="verified",
            privacy_level="invented",
        )


def test_sqlite_checks_reject_invalid_finite_states(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)

    with repo.connection() as connection:
        table_sql = {
            row["name"]: row["sql"]
            for row in connection.execute(
                """
                SELECT name, sql FROM sqlite_master
                WHERE type = 'table'
                  AND name IN ('ai_provider_configs', 'ai_tasks', 'ai_findings', 'profile_facts')
                """
            ).fetchall()
        }
        assert all("CHECK" in sql.upper() for sql in table_sql.values())
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE ai_tasks SET status = 'invented' WHERE id = ?", (task.id,)
            )


def test_provider_upsert_is_stable_and_updates_mutable_fields(tmp_path: Path):
    repo = make_repo(tmp_path)

    first = create_provider(repo)
    second = repo.upsert_ai_provider(
        provider_kind="mock",
        display_name="Updated mock",
        privacy_mode="local_private",
        endpoint="",
        model="fixture-v1",
        enabled=False,
        availability_binding=provider_binding(),
    )

    assert second.id == first.id
    assert second.created_at == first.created_at
    assert second.display_name == "Updated mock"
    assert second.enabled is False
    assert repo.get_ai_provider(second.id) == second
    assert repo.list_ai_providers() == [second]


def test_enabled_provider_binding_trigger_rejects_incomplete_json(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = create_provider(repo)
    incomplete = {
        "provider_kind": "mock",
        "model": "fixture-v1",
        "privacy_mode": "local_private",
        "endpoint": "",
        "identity_name": "",
    }

    with repo.connection() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="binding"):
            connection.execute(
                """
                UPDATE ai_provider_configs
                SET availability_binding_json = ?,
                    availability_binding_sha256 = ?
                WHERE id = ?
                """,
                (json.dumps(incomplete), "a" * 64, provider.id),
            )


def test_codex_provider_identities_with_same_model_coexist(tmp_path: Path):
    repo = make_repo(tmp_path)

    preferred = repo.upsert_ai_provider(
        provider_kind="codex",
        display_name="Codex account 2",
        privacy_mode="cloud_external",
        endpoint="codex-identity://codex2",
        identity_name="codex2",
        model="gpt-5.6-sol",
        enabled=True,
        availability_binding=provider_binding(
            provider_kind="codex",
            model="gpt-5.6-sol",
            privacy_mode="cloud_external",
            endpoint="codex-identity://codex2",
            identity_name="codex2",
        ),
    )
    alternate = repo.upsert_ai_provider(
        provider_kind="codex",
        display_name="Codex default",
        privacy_mode="cloud_external",
        endpoint="codex-identity://codex",
        identity_name="codex",
        model="gpt-5.6-sol",
        enabled=True,
        availability_binding=provider_binding(
            provider_kind="codex",
            model="gpt-5.6-sol",
            privacy_mode="cloud_external",
            endpoint="codex-identity://codex",
            identity_name="codex",
        ),
    )

    assert preferred.id != alternate.id
    assert preferred.identity_name == "codex2"
    assert alternate.identity_name == "codex"
    assert [item.id for item in repo.list_ai_providers()] == [
        preferred.id,
        alternate.id,
    ]
    with repo.connection() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE ai_provider_configs
                SET identity_name = ?, endpoint = ?, model = ?
                WHERE id = ?
                """,
                (
                    "codex",
                    "codex-identity://codex",
                    "gpt-5.6-sol-mutated",
                    preferred.id,
                ),
            )


def test_invocation_fence_survives_requeue_and_new_claim(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    first_claim = claim_task(repo, task.id)

    assert repo.begin_ai_task_invocation(task.id, first_claim) is True
    assert repo.begin_ai_task_invocation(task.id, first_claim) is False
    expire_claim(repo, task.id)
    assert repo.requeue_interrupted_ai_tasks() == 1
    second_claim = claim_task(repo, task.id)

    assert second_claim != first_claim
    assert repo.begin_ai_task_invocation(task.id, second_claim) is False
    assert repo.get_ai_task(task.id).invocation_started_at is not None


def test_v9_running_task_upgrade_consumes_invocation_fence(tmp_path: Path):
    database_path = tmp_path / "careerops.db"
    create_legacy_ai_database(database_path, 9)
    with connect_database(database_path) as connection:
        connection.execute(
            """
            INSERT INTO ai_provider_configs (
                id, provider_kind, display_name, enabled, privacy_mode,
                endpoint, model, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "aip_v9",
                "mock",
                "Version 9 provider",
                1,
                "local_private",
                "",
                "fixture-v1",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO ai_tasks (
                id, application_id, task_type, provider_config_id,
                prompt_template_id, input_artifact_ids_json, status,
                privacy_mode, started_at, completed_at, error_summary,
                claim_token, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ait_v9_running",
                "app_v0",
                "analyze_jd",
                "aip_v9",
                "analyze_jd_v1",
                "[]",
                "running",
                "local_private",
                "2026-02-01T00:00:00+00:00",
                None,
                "",
                "legacy-claim",
                "2026-01-31T00:00:00+00:00",
                "2026-02-01T00:00:01+00:00",
            ),
        )

    repo = CareerOpsRepository(database_path, tmp_path / "vault")
    repo.initialize()

    migrated = repo.get_ai_task("ait_v9_running")
    assert migrated.invocation_started_at == "2026-02-01T00:00:00+00:00"
    assert migrated.status == "pending"
    assert repo.requeue_interrupted_ai_tasks() == 0
    make_legacy_application_pristine_for_claim(repo)
    recovered_claim = claim_task(repo, migrated.id)
    assert repo.begin_ai_task_invocation(migrated.id, recovered_claim) is False


def test_profile_facts_enforce_verification_states_and_write_audits(tmp_path: Path):
    repo = make_repo(tmp_path)

    fact = repo.create_profile_fact(
        fact_key="skills.python",
        fact_value="Five years",
        verification_state="user_stated",
        privacy_level="private",
        notes="token=profile-secret",
    )

    assert repo.get_profile_fact(fact.id) == fact
    assert repo.list_profile_facts() == [fact]
    event = next(item for item in repo.list_audit_events() if item.event_type == "profile_fact.created")
    assert "profile-secret" not in str(event.after_json)
    with pytest.raises(ValueError, match="verification state"):
        repo.create_profile_fact(
            fact_key="skills.sql",
            fact_value="Advanced",
            verification_state="invented",
            privacy_level="private",
        )


def test_evidence_items_enforce_states_and_link_idempotently(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)

    evidence = repo.create_evidence_item(
        claim_text="Built a production Python service",
        evidence_type="project",
        source_path=tmp_path / "portfolio.md",
        source_url="https://example.com/portfolio",
        source_sha256="b" * 64,
        verification_state="verified",
        privacy_level="public",
        notes="Reviewed",
    )
    repo.link_application_evidence(application.id, evidence.id)
    repo.link_application_evidence(application.id, evidence.id)

    assert repo.get_evidence_item(evidence.id) == evidence
    assert repo.list_evidence_items() == [evidence]
    assert repo.list_application_evidence(application.id) == [evidence]
    event_types = [event.event_type for event in repo.list_audit_events()]
    assert "evidence_item.created" in event_types
    assert event_types.count("application.evidence_linked") == 1
    with pytest.raises(ValueError, match="verification state"):
        repo.create_evidence_item(
            claim_text="Unverified claim",
            evidence_type="note",
            verification_state="unknown",
            privacy_level="public",
        )


def test_atomic_evidence_create_and_link_rolls_back_item_when_link_audit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    before_items = repo.list_evidence_items()
    before_events = repo.list_audit_events()
    original_write_audit = repo._write_audit

    def fail_link_audit(connection, event_type, *args, **kwargs):
        if event_type == "application.evidence_linked":
            raise RuntimeError("injected evidence link failure")
        return original_write_audit(connection, event_type, *args, **kwargs)

    monkeypatch.setattr(repo, "_write_audit", fail_link_audit)

    with pytest.raises(RuntimeError, match="evidence link failure"):
        repo.create_and_link_evidence_item(
            application_id=application.id,
            claim_text="Built a Python service",
            evidence_type="project",
            source_url="https://example.com/project",
            verification_state="verified",
            privacy_level="public",
        )

    assert repo.list_evidence_items() == before_items
    assert repo.list_application_evidence(application.id) == []
    assert repo.list_audit_events() == before_events


def test_atomic_evidence_create_and_link_does_not_use_post_commit_public_getter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application = create_application(repo)

    def fail_public_reload(_evidence_item_id: str):
        raise RuntimeError("post-commit evidence reload must not run")

    monkeypatch.setattr(repo, "get_evidence_item", fail_public_reload)

    evidence = repo.create_and_link_evidence_item(
        application_id=application.id,
        claim_text="Built a Python service",
        evidence_type="project",
        source_url="https://example.com/project",
        verification_state="verified",
        privacy_level="public",
    )

    verifier = CareerOpsRepository(repo.database_path, repo.vault_path)
    persisted = verifier.get_evidence_item(evidence.id)
    assert persisted == evidence
    assert verifier.list_application_evidence(application.id) == [evidence]


def test_application_versions_are_append_only_and_monotonic(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)

    first = repo.record_application_version(
        application.id, "application.created", {"status": "draft"}
    )
    second = repo.record_application_version(
        application.id, "ai.reviewed", {"status": "needs_evidence"}
    )

    assert first.version_number == 1
    assert second.version_number == first.version_number + 1
    assert [item.id for item in repo.list_application_versions(application.id)] == [
        first.id,
        second.id,
    ]
    event = next(
        item for item in repo.list_audit_events() if item.event_type == "application.version_recorded"
    )
    assert event.entity_id == application.id


def test_simultaneous_application_versions_allocate_unique_monotonic_numbers(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    worker_count = 6
    workers = [
        CareerOpsRepository(repo.database_path, repo.vault_path) for _ in range(worker_count)
    ]
    barrier = Barrier(worker_count)

    def record(worker_number: int):
        barrier.wait()
        return workers[worker_number].record_application_version(
            application.id,
            "ai.reviewed",
            {"worker": worker_number},
        )

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        versions = list(executor.map(record, range(worker_count)))

    assert sorted(item.version_number for item in versions) == list(range(1, worker_count + 1))
    assert [
        item.version_number for item in repo.list_application_versions(application.id)
    ] == list(range(1, worker_count + 1))


def test_initialize_creates_ai_vaults_and_application_defaults(tmp_path: Path):
    repo = make_repo(tmp_path)

    application = create_application(repo)

    assert application.role_type == ""
    assert application.submitted_at is None
    assert (repo.vault_path / "ai-tasks").is_dir()
    assert (repo.vault_path / "ai-artifacts").is_dir()
    assert (repo.vault_path / "application-versions").is_dir()


def test_new_records_are_frozen_dataclasses(tmp_path: Path):
    from careerops.core.models import (
        AiArtifact,
        AiFinding,
        AiProviderConfig,
        AiTask,
        ApplicationVersion,
        EvidenceItem,
        ProfileFact,
    )

    repo = make_repo(tmp_path)
    provider = create_provider(repo)

    for record_type in (
        AiProviderConfig,
        AiTask,
        AiArtifact,
        AiFinding,
        EvidenceItem,
        ProfileFact,
        ApplicationVersion,
    ):
        assert record_type.__dataclass_params__.frozen is True
    with pytest.raises(FrozenInstanceError):
        provider.display_name = "Changed"


def test_v0_database_upgrades_without_data_loss(tmp_path: Path):
    database_path = tmp_path / "careerops.db"
    fixture_path = Path(__file__).parent / "fixtures" / "v0_schema.sql"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.executescript(fixture_path.read_text(encoding="utf-8"))
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        legacy_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(applications)").fetchall()
        }
        assert "role_type" not in legacy_columns
        assert "submitted_at" not in legacy_columns
        assert connection.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 1

    repo = CareerOpsRepository(database_path, tmp_path / "vault")
    repo.initialize()

    application = repo.get_application("app_v0")
    assert application.company == "Legacy Corp"
    assert application.role_type == ""
    assert application.submitted_at is None
    assert repo.get_report("rep_v0").fit == {"score": 90}
    assert repo.get_approval("apr_v0").approval_note == "legacy approval"
    assert any(event.id == "aud_v0" for event in repo.list_audit_events())
    with connect_database(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] >= 3
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'ai_tasks'"
        ).fetchone()


def test_v10_database_backfills_and_quarantines_codex_identities(tmp_path: Path):
    database_path = tmp_path / "careerops.db"
    create_legacy_ai_database(database_path, 10)
    with connect_database(database_path) as connection:
        connection.executemany(
            """
            INSERT INTO ai_provider_configs (
                id, provider_kind, display_name, enabled, privacy_mode,
                endpoint, model, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "aip_codex2_v10",
                    "codex",
                    "Legacy codex2",
                    1,
                    "cloud_external",
                    "codex-identity://codex2",
                    "gpt-5.6-sol",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
                (
                    "aip_codex_v10",
                    "codex",
                    "Legacy codex",
                    1,
                    "cloud_external",
                    "codex-identity://codex",
                    "gpt-5.6-sol",
                    "2026-01-01T00:00:01+00:00",
                    "2026-01-01T00:00:01+00:00",
                ),
                (
                    "aip_ambiguous_v10",
                    "codex",
                    "Legacy ambiguous Codex",
                    1,
                    "cloud_external",
                    "C:\\Program Files\\Codex\\codex.exe",
                    "gpt-5.6-sol",
                    "2026-01-01T00:00:02+00:00",
                    "2026-01-01T00:00:02+00:00",
                ),
                (
                    "aip_mock_v10",
                    "mock",
                    "Legacy mock",
                    1,
                    "local_private",
                    "",
                    "fixture-v1",
                    "2026-01-01T00:00:03+00:00",
                    "2026-01-01T00:00:03+00:00",
                ),
            ],
        )
        connection.execute(
            """
            INSERT INTO ai_tasks (
                id, application_id, task_type, provider_config_id,
                prompt_template_id, input_artifact_ids_json, status,
                privacy_mode, started_at, invocation_started_at, completed_at,
                error_summary, claim_token, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ait_ambiguous_v10",
                "app_v0",
                "analyze_jd",
                "aip_ambiguous_v10",
                "analyze_jd_v1",
                "[]",
                "pending",
                "cloud_external",
                None,
                None,
                None,
                "",
                None,
                "2026-01-01T00:00:04+00:00",
                "2026-01-01T00:00:04+00:00",
            ),
        )

    repo = CareerOpsRepository(database_path, tmp_path / "vault")
    repo.initialize()

    assert repo.get_ai_provider("aip_codex2_v10").identity_name == "codex2"
    assert repo.get_ai_provider("aip_codex_v10").identity_name == "codex"
    ambiguous = repo.get_ai_provider("aip_ambiguous_v10")
    assert ambiguous.enabled is False
    assert ambiguous.identity_name == ""
    assert ambiguous.model == "gpt-5.6-sol"
    assert repo.get_ai_task("ait_ambiguous_v10").provider_config_id == ambiguous.id
    assert repo.get_ai_provider("aip_mock_v10").identity_name == ""

    with connect_database(database_path) as connection:
        connection.execute(
            """
            UPDATE ai_provider_configs
            SET display_name = ?, enabled = 0
            WHERE id = ?
            """,
            ("Quarantined Codex", ambiguous.id),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE ai_provider_configs SET enabled = 1 WHERE id = ?",
                (ambiguous.id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE ai_provider_configs
                SET identity_name = ?, endpoint = ?
                WHERE id = ?
                """,
                ("codex", "codex-identity://codex", ambiguous.id),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO ai_provider_configs (
                    id, provider_kind, display_name, identity_name, enabled,
                    privacy_mode, endpoint, model, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "aip_invalid_non_codex",
                    "mock",
                    "Invalid mock identity",
                    "codex",
                    1,
                    "local_private",
                    "",
                    "fixture-v2",
                    "2026-01-01T00:00:05+00:00",
                    "2026-01-01T00:00:05+00:00",
                ),
            )

    assert repo.get_ai_provider(ambiguous.id).display_name == "Quarantined Codex"
    fresh = repo.upsert_ai_provider(
        provider_kind="codex",
        display_name="Fresh exact identity",
        privacy_mode="cloud_external",
        endpoint="codex-identity://codex2",
        identity_name="codex2",
        model="gpt-5.6-sol-fresh",
        enabled=True,
        availability_binding=provider_binding(
            provider_kind="codex",
            model="gpt-5.6-sol-fresh",
            privacy_mode="cloud_external",
            endpoint="codex-identity://codex2",
            identity_name="codex2",
        ),
    )
    assert fresh.enabled is True
    assert fresh.identity_name == "codex2"


def test_f32e429_v11_database_repairs_fences_and_legacy_codex(tmp_path: Path):
    database_path = tmp_path / "careerops.db"
    create_legacy_ai_database(database_path, 10)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO ai_provider_configs (
                id, provider_kind, display_name, enabled, privacy_mode,
                endpoint, model, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "aip_ambiguous_v11",
                "codex",
                "Ambiguous version 11 Codex",
                1,
                "cloud_external",
                "C:\\Tools\\codex.exe",
                "gpt-5.6-sol",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO ai_tasks (
                id, application_id, task_type, provider_config_id,
                prompt_template_id, input_artifact_ids_json, status,
                privacy_mode, started_at, invocation_started_at, completed_at,
                error_summary, claim_token, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ait_running_v11",
                "app_v0",
                "analyze_jd",
                "aip_ambiguous_v11",
                "analyze_jd_v1",
                "[]",
                "running",
                "cloud_external",
                "2026-02-02T00:00:00+00:00",
                None,
                None,
                "",
                "legacy-v11-claim",
                "2026-02-01T00:00:00+00:00",
                "2026-02-02T00:00:01+00:00",
            ),
        )
        promote_to_f32e429_v11(connection)
        connection.commit()

    repo = CareerOpsRepository(database_path, tmp_path / "vault")
    repo.initialize()

    provider = repo.get_ai_provider("aip_ambiguous_v11")
    task = repo.get_ai_task("ait_running_v11")
    assert provider.enabled is False
    assert provider.identity_name == ""
    assert task.invocation_started_at == "2026-02-02T00:00:00+00:00"
    assert task.status == "pending"
    assert repo.requeue_interrupted_ai_tasks() == 0
    make_legacy_application_pristine_for_claim(repo)
    recovered_claim = claim_task(repo, task.id)
    assert repo.begin_ai_task_invocation(task.id, recovered_claim) is False


def test_v12_database_quarantines_unbound_configs_and_fences_running_tasks(
    tmp_path: Path,
):
    database_path = tmp_path / "careerops.db"
    create_legacy_ai_database(database_path, 12)
    with connect_database(database_path) as connection:
        connection.execute(
            """
            INSERT INTO ai_provider_configs (
                id, provider_kind, display_name, identity_name, enabled,
                privacy_mode, endpoint, model, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "aip_unbound_v12",
                "mock",
                "Unbound version 12 provider",
                "",
                1,
                "local_private",
                "",
                "fixture-v1",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO ai_tasks (
                id, application_id, task_type, provider_config_id,
                prompt_template_id, input_artifact_ids_json, status,
                privacy_mode, started_at, invocation_started_at, completed_at,
                error_summary, claim_token, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ait_unbound_v12",
                "app_v0",
                "analyze_jd",
                "aip_unbound_v12",
                "analyze_jd_v1",
                "[]",
                "running",
                "local_private",
                "2026-02-03T00:00:00+00:00",
                None,
                None,
                "",
                "unsafe-v12-claim",
                "2026-02-02T00:00:00+00:00",
                "2026-02-03T00:00:01+00:00",
            ),
        )

    repo = CareerOpsRepository(database_path, tmp_path / "vault")
    repo.initialize()

    with connect_database(database_path) as connection:
        config = connection.execute(
            "SELECT * FROM ai_provider_configs WHERE id = 'aip_unbound_v12'"
        ).fetchone()
        task = connection.execute(
            "SELECT * FROM ai_tasks WHERE id = 'ait_unbound_v12'"
        ).fetchone()
        assert config["enabled"] == 0
        assert config["availability_binding_json"] == "{}"
        assert config["availability_binding_sha256"] == ""
        assert task["provider_binding_json"] == "{}"
        assert task["provider_binding_sha256"] == ""
        assert task["invocation_started_at"] == "2026-02-03T00:00:00+00:00"
        assert task["status"] == "pending"
        assert task["claim_expires_at"] is None
        with pytest.raises(sqlite3.IntegrityError, match="binding"):
            connection.execute(
                "UPDATE ai_provider_configs SET enabled = 1 WHERE id = ?",
                (config["id"],),
            )


def test_v15_upgrade_preserves_bindings_while_adding_container_image_identity(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    legacy_binding = provider_binding()
    legacy_binding.pop("container_image_reference")
    legacy_binding.pop("container_image_id")
    legacy_json = json.dumps(legacy_binding, sort_keys=True, separators=(",", ":"))
    legacy_sha256 = hashlib.sha256(legacy_json.encode("utf-8")).hexdigest()

    with repo.connection() as connection:
        connection.execute("DROP TRIGGER validate_provider_binding_sha_update")
        connection.execute("DROP TRIGGER validate_ai_task_authority_immutable")
        connection.execute(
            """
            UPDATE ai_provider_configs
            SET availability_binding_json = ?, availability_binding_sha256 = ?
            WHERE id = ?
            """,
            (legacy_json, legacy_sha256, provider.id),
        )
        connection.execute(
            """
            UPDATE ai_tasks
            SET provider_binding_json = ?, provider_binding_sha256 = ?
            WHERE id = ?
            """,
            (legacy_json, legacy_sha256, task.id),
        )
        connection.execute("PRAGMA user_version = 15")

    repo.initialize()

    expected_binding = {
        **legacy_binding,
        "container_image_reference": "",
        "container_image_id": "",
    }
    expected_json = json.dumps(
        expected_binding, sort_keys=True, separators=(",", ":")
    )
    expected_sha256 = hashlib.sha256(expected_json.encode("utf-8")).hexdigest()
    with repo.connection() as connection:
        upgraded_provider = connection.execute(
            "SELECT * FROM ai_provider_configs WHERE id = ?", (provider.id,)
        ).fetchone()
        upgraded_task = connection.execute(
            "SELECT * FROM ai_tasks WHERE id = ?", (task.id,)
        ).fetchone()

    assert LATEST_SCHEMA_VERSION == 16
    assert upgraded_provider["enabled"] == 0
    assert upgraded_provider["availability_binding_json"] == expected_json
    assert upgraded_provider["availability_binding_sha256"] == expected_sha256
    assert upgraded_task["provider_binding_json"] == expected_json
    assert upgraded_task["provider_binding_sha256"] == expected_sha256


def test_v12_upgrade_quarantines_duplicates_then_fails_closed_on_missing_artifact(
    tmp_path: Path,
):
    database_path = tmp_path / "careerops.db"
    create_legacy_ai_database(database_path, 12)
    shared_path = str(tmp_path / "vault" / "ai-tasks" / "legacy" / "shared.json")
    with connect_database(database_path) as connection:
        connection.execute(
            """
            INSERT INTO ai_provider_configs (
                id, provider_kind, display_name, identity_name, enabled,
                privacy_mode, endpoint, model, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "aip_v12_prompt",
                "mock",
                "Legacy provider",
                "",
                1,
                "local_private",
                "",
                "fixture-v1",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO ai_tasks (
                id, application_id, task_type, provider_config_id,
                prompt_template_id, input_artifact_ids_json, status,
                privacy_mode, started_at, invocation_started_at, completed_at,
                error_summary, claim_token, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ait_v12_prompt",
                "app_v0",
                "analyze_jd",
                "aip_v12_prompt",
                "analyze_jd_v1",
                "[]",
                "pending",
                "local_private",
                None,
                None,
                None,
                "",
                None,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        for artifact_id in ("aia_v12_first", "aia_v12_second"):
            stored_path = (
                shared_path.upper()
                if os.name == "nt" and artifact_id == "aia_v12_second"
                else shared_path
            )
            connection.execute(
                """
                INSERT INTO ai_artifacts (
                    id, task_id, claim_token, artifact_type, path, sha256,
                    schema_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    "ait_v12_prompt",
                    "legacy-claim",
                    "context",
                    stored_path,
                    "a" * 64,
                    "1",
                    "2026-01-01T00:00:00+00:00",
                ),
            )

    repo = CareerOpsRepository(database_path, tmp_path / "vault")
    with pytest.raises(ValueError, match="legacy AI artifact"):
        repo.initialize()
    task = repo.get_ai_task("ait_v12_prompt")
    make_legacy_application_pristine_for_claim(repo)
    claim = claim_task(repo, task.id)

    assert task.prompt_sha256 == ""
    assert repo.begin_ai_task_invocation(task.id, claim) is False
    with repo.connection() as connection:
        paths = connection.execute(
            "SELECT path FROM ai_artifacts ORDER BY id"
        ).fetchall()
        assert len({row["path"] for row in paths}) == 2
        assert any(".quarantined-" in row["path"] for row in paths)
        with pytest.raises(sqlite3.IntegrityError, match="path"):
            connection.execute(
                """
                INSERT INTO ai_artifacts (
                    id, task_id, claim_token, artifact_type, path, sha256,
                    schema_version, storage_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 2, ?)
                """,
                (
                    "aia_v12_duplicate",
                    task.id,
                    claim,
                    "context",
                    paths[0]["path"],
                    "b" * 64,
                    "1",
                    "2026-01-02T00:00:00+00:00",
                ),
            )


def test_interrupted_v13_migration_repairs_partial_binding_and_lease_columns(
    tmp_path: Path,
):
    database_path = tmp_path / "careerops.db"
    create_legacy_ai_database(database_path, 12)
    with connect_database(database_path) as connection:
        connection.execute(
            """
            ALTER TABLE ai_provider_configs
            ADD COLUMN availability_binding_json TEXT NOT NULL DEFAULT '{}'
            """
        )
        connection.execute(
            """
            ALTER TABLE ai_provider_configs
            ADD COLUMN availability_binding_sha256 TEXT NOT NULL DEFAULT ''
            """
        )
        connection.execute(
            """
            ALTER TABLE ai_tasks
            ADD COLUMN provider_binding_json TEXT NOT NULL DEFAULT '{}'
            """
        )
        connection.execute(
            """
            ALTER TABLE ai_tasks
            ADD COLUMN provider_binding_sha256 TEXT NOT NULL DEFAULT ''
            """
        )
        connection.execute(
            "ALTER TABLE ai_tasks ADD COLUMN claim_expires_at TEXT"
        )
        connection.execute(
            """
            INSERT INTO ai_provider_configs (
                id, provider_kind, display_name, identity_name, enabled,
                privacy_mode, endpoint, model, availability_binding_json,
                availability_binding_sha256, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "aip_partial_v13",
                "mock",
                "Partially bound provider",
                "",
                1,
                "local_private",
                "",
                "fixture-v1",
                '{"provider_kind":"mock"}',
                "a" * 64,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO ai_tasks (
                id, application_id, task_type, provider_config_id,
                prompt_template_id, input_artifact_ids_json, status,
                privacy_mode, started_at, invocation_started_at, completed_at,
                error_summary, claim_token, claim_expires_at,
                provider_binding_json, provider_binding_sha256,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ait_partial_v13",
                "app_v0",
                "analyze_jd",
                "aip_partial_v13",
                "analyze_jd_v1",
                "[]",
                "running",
                "local_private",
                "2026-03-01T00:00:00+00:00",
                None,
                None,
                "",
                "partial-claim",
                None,
                '{"provider_kind":"mock"}',
                "b" * 64,
                "2026-02-28T00:00:00+00:00",
                "2026-03-01T00:00:01+00:00",
            ),
        )

    repo = CareerOpsRepository(database_path, tmp_path / "vault")
    repo.initialize()

    with connect_database(database_path) as connection:
        provider = connection.execute(
            "SELECT * FROM ai_provider_configs WHERE id = 'aip_partial_v13'"
        ).fetchone()
        task = connection.execute(
            "SELECT * FROM ai_tasks WHERE id = 'ait_partial_v13'"
        ).fetchone()
        assert provider["enabled"] == 0
        assert provider["availability_binding_json"] == "{}"
        assert provider["availability_binding_sha256"] == ""
        assert task["provider_binding_json"] == "{}"
        assert task["provider_binding_sha256"] == ""
        assert task["invocation_started_at"] == "2026-03-01T00:00:00+00:00"
        assert task["status"] == "pending"
        assert task["claim_expires_at"] is None


def test_ai_foreign_key_indexes_exist_without_redundant_version_index(tmp_path: Path):
    repo = make_repo(tmp_path)

    def indexed_columns(connection: sqlite3.Connection, table: str) -> dict[str, tuple[str, ...]]:
        indexes = {}
        for index in connection.execute(f"PRAGMA index_list({table})").fetchall():
            indexes[index[1]] = tuple(
                row[2]
                for row in connection.execute(f"PRAGMA index_info({index[1]})").fetchall()
            )
        return indexes

    with repo.connection() as connection:
        task_indexes = indexed_columns(connection, "ai_tasks")
        finding_indexes = indexed_columns(connection, "ai_findings")
        version_indexes = indexed_columns(connection, "application_versions")

    assert ("provider_config_id",) in task_indexes.values()
    assert ("evidence_item_id",) in finding_indexes.values()
    assert "idx_application_versions_application" not in version_indexes
    assert ("application_id", "version_number") in version_indexes.values()
