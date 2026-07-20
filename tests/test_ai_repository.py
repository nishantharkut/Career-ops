from dataclasses import FrozenInstanceError
from pathlib import Path
import sqlite3

import pytest

from careerops.core.database import SCHEMA, connect_database
from careerops.core.repository import CareerOpsRepository


def make_repo(tmp_path: Path) -> CareerOpsRepository:
    repo = CareerOpsRepository(tmp_path / "careerops.db", tmp_path / "vault")
    repo.initialize()
    return repo


def create_application(repo: CareerOpsRepository):
    return repo.create_application(
        company="Example Corp",
        role_title="Software Engineering Intern",
        location="Remote",
        source_url="https://example.com/jobs/1",
        jd_text="Build reliable Python services.",
    )


def create_provider(repo: CareerOpsRepository):
    return repo.upsert_ai_provider(
        provider_kind="mock",
        display_name="CI mock",
        privacy_mode="local_private",
        endpoint="",
        model="fixture-v1",
        enabled=True,
    )


def create_task(repo: CareerOpsRepository, application_id: str, provider_id: str):
    return repo.create_ai_task(
        application_id=application_id,
        task_type="analyze_fit",
        provider_config_id=provider_id,
        privacy_mode="local_private",
        prompt_template_id="analyze_fit_v1",
        input_artifact_ids=["artifact_input"],
    )


def test_ai_task_is_persisted_pending_before_execution(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)

    task = create_task(repo, application.id, provider.id)

    assert task.status == "pending"
    assert task.started_at is None
    assert task.completed_at is None
    assert task.input_artifact_ids == ["artifact_input"]
    assert repo.get_ai_task(task.id).provider_config_id == provider.id
    assert [item.id for item in repo.list_ai_tasks(application.id)] == [task.id]


def test_only_one_worker_can_claim_pending_task(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    second_worker = CareerOpsRepository(repo.database_path, repo.vault_path)

    assert repo.claim_ai_task(task.id) is True
    assert second_worker.claim_ai_task(task.id) is False
    assert repo.get_ai_task(task.id).status == "running"


def test_task_start_complete_and_failure_are_durable_and_audited(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    completed_task = create_task(repo, application.id, provider.id)
    failed_task = create_task(repo, application.id, provider.id)

    started = repo.start_ai_task(completed_task.id)
    completed = repo.complete_ai_task(started.id)
    repo.start_ai_task(failed_task.id)
    failed = repo.fail_ai_task(failed_task.id, "api_key=top-secret provider unavailable")

    assert completed.status == "completed"
    assert completed.completed_at is not None
    assert failed.status == "failed"
    assert "top-secret" not in failed.error_summary
    events = repo.list_audit_events()
    assert any(event.event_type == "ai_task.completed" for event in events)
    failure_event = next(event for event in events if event.event_type == "ai_task.failed")
    assert "top-secret" not in str(failure_event.after_json)


def test_interrupted_tasks_requeue_without_losing_artifacts(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    repo.start_ai_task(task.id)
    context_path = tmp_path / "vault" / "ai-tasks" / task.id / "context.json"

    artifact = repo.save_ai_artifact(task.id, "context", context_path, "a" * 64, "1")

    assert repo.requeue_interrupted_ai_tasks() == 1
    requeued = repo.get_ai_task(task.id)
    assert requeued.status == "pending"
    assert requeued.started_at is None
    assert repo.list_ai_artifacts(task.id) == [artifact]


def test_ai_finding_decisions_are_persisted_and_audited(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)

    finding = repo.save_ai_finding(
        task_id=task.id,
        finding_type="unsupported_claim",
        severity="blocking",
        claim_text="Improved throughput by 50%",
        evidence_item_id=None,
        recommendation="Remove the unsupported metric",
        verifier_state="blocked",
        decision="pending",
        payload={"reason": "password=hunter2"},
    )
    decided = repo.update_ai_finding_decision(finding.id, "rejected")

    assert repo.get_ai_finding(finding.id) == decided
    assert decided.decision == "rejected"
    assert repo.list_ai_findings(task.id) == [decided]
    event = next(
        item for item in repo.list_audit_events() if item.event_type == "ai_finding.decision_updated"
    )
    assert "hunter2" not in str(event.after_json)


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
    )

    assert second.id == first.id
    assert second.created_at == first.created_at
    assert second.display_name == "Updated mock"
    assert second.enabled is False
    assert repo.get_ai_provider(second.id) == second
    assert repo.list_ai_providers() == [second]


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
    with sqlite3.connect(database_path) as connection:
        connection.executescript(SCHEMA)
        connection.execute(
            """
            INSERT INTO applications (
                id, company, role_title, location, source_url, source_kind,
                jd_snapshot_id, status, selected_resume_version_id, latest_report_id,
                approval_id, submission_state, deadline, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "app_v0", "Legacy Corp", "Engineer", "Remote", "", "manual", "jd_v0",
                "approved", "res_v0", "rep_v0", "apr_v0", "not_submitted", None,
                "legacy notes", "2026-07-20T00:00:00+00:00", "2026-07-20T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO reports (
                id, application_id, resume_version_id, jd_snapshot_id, fit_json,
                risks_json, blocking_issue_count, warning_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rep_v0", "app_v0", "res_v0", "jd_v0", '{"score": 90}',
                '{"blockers": [], "warnings": []}', 0, 0, "2026-07-20T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO approvals (
                id, application_id, resume_version_id, report_id, pdf_sha256,
                approved_at, approval_note
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "apr_v0", "app_v0", "res_v0", "rep_v0", "c" * 64,
                "2026-07-20T00:00:00+00:00", "legacy approval",
            ),
        )
        connection.execute(
            """
            INSERT INTO audit_events (
                id, event_type, entity_type, entity_id, before_json, after_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "aud_v0", "application.created", "application", "app_v0", "{}",
                '{"company": "Legacy Corp"}', "2026-07-20T00:00:00+00:00",
            ),
        )

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
