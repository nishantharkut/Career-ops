from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from apps.api.careerops_api.main import create_app
from careerops.ai.contracts import (
    InvocationMetadata,
    ProviderAvailability,
    serialize_provider_output,
    validate_task_output,
)
from careerops.ai.orchestrator import AiOrchestrator
from careerops.ai.providers.base import build_provider_result
from careerops.core.repository import CareerOpsRepository
from careerops.core.status import PrivacyMode
from careerops.resumes.drafts import create_verified_resume_draft


PARENT_SOURCE = (
    "\\section{Experience}\n"
    "Built data pipeline.\n"
    "Led API migration.\n"
)


@dataclass
class SuggestionProvider:
    status: ProviderAvailability
    suggestions: list[dict[str, Any]]

    def availability(self) -> ProviderAvailability:
        return self.status

    def run(self, request: Any):
        output = validate_task_output(
            request.task_type,
            {
                "task_type": "suggest_resume_edits",
                "suggestions": self.suggestions,
            },
        )
        return build_provider_result(
            provider_kind=self.status.provider_kind,
            model=self.status.model,
            privacy_mode=self.status.privacy_mode,
            data_leaves_machine=self.status.data_leaves_machine,
            is_ai=self.status.is_ai,
            structured_output=output,
            raw_output=serialize_provider_output(output),
            invocation=InvocationMetadata(mode="test_fixture"),
        )


def _availability() -> ProviderAvailability:
    return ProviderAvailability(
        available=True,
        provider_kind="mock",
        model="resume-draft-fixture-v1",
        privacy_mode=PrivacyMode.LOCAL_PRIVATE,
        data_leaves_machine=False,
        is_ai=True,
        detail="available",
    )


def _binding(status: ProviderAvailability) -> dict[str, Any]:
    value = status.model_dump(mode="json")
    value.pop("available")
    value.pop("detail")
    return value


def _make_repo(tmp_path: Path) -> CareerOpsRepository:
    repo = CareerOpsRepository(tmp_path / "careerops.db", tmp_path / "vault")
    repo.initialize()
    return repo


def _create_application(repo: CareerOpsRepository):
    return repo.create_application(
        company="Example Corp",
        role_title="Software Engineer",
        location="Remote",
        source_url="https://example.com/jobs/1",
        jd_text="Python API data role",
    )


def _select_parent(
    repo: CareerOpsRepository,
    application_id: str,
    *,
    source: str = PARENT_SOURCE,
    select: bool = True,
):
    source_dir = (
        repo.vault_path
        / "resumes"
        / application_id
        / f"source-{uuid4().hex}"
    )
    source_dir.mkdir(parents=True, exist_ok=True)
    source_path = source_dir / "source.tex"
    source_bytes = source.encode("utf-8")
    source_path.write_bytes(source_bytes)
    resume = repo.create_resume_version(
        name="Current resume",
        role_family="software-engineering",
        tex_path=source_path,
        tex_sha256=hashlib.sha256(source_bytes).hexdigest(),
    )
    if select:
        repo.select_resume(application_id, resume.id)
    return resume


def _create_completed_suggestion_task(
    repo: CareerOpsRepository,
    application_id: str,
    *,
    suggestions: list[dict[str, Any]] | None = None,
):
    evidence = repo.create_and_link_evidence_item(
        application_id=application_id,
        claim_text="Built Python data pipeline. Led Python API migration.",
        evidence_type="portfolio",
        verification_state="verified",
        privacy_level="public",
    )
    suggestions = suggestions or [
        {
            "source_text": "Built data pipeline.",
            "replacement_text": "Built Python data pipeline.",
            "reason": "Aligns the wording with evidence",
            "evidence_refs": [evidence.id],
        }
    ]
    suggestions = [dict(item) for item in suggestions]
    for suggestion in suggestions:
        if suggestion.get("evidence_refs") == []:
            suggestion["evidence_refs"] = [evidence.id]
    status = _availability()
    config = repo.upsert_ai_provider_config(
        provider_kind=status.provider_kind,
        display_name="Resume draft fixture",
        privacy_mode=status.privacy_mode.value,
        endpoint=status.endpoint,
        identity_name=status.identity_name,
        model=status.model,
        enabled=True,
        availability_binding=_binding(status),
    )
    provider = SuggestionProvider(status, suggestions)
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    task = orchestrator.enqueue(
        application_id,
        "suggest_resume_edits",
        config.id,
    )
    completed = orchestrator.run(task.id)
    assert completed.status.value == "completed", (
        completed.error_summary,
        [
            (item.finding_type, item.payload)
            for item in repo.list_ai_findings(task.id)
        ],
    )
    findings = repo.list_ai_findings(task.id)
    assert findings
    for finding in findings:
        repo.update_ai_finding_decision(
            finding.id,
            "accepted",
            expected_decision="pending",
        )
    return completed, [repo.get_ai_finding(item.id) for item in findings], evidence


def _mutate_finding(
    repo: CareerOpsRepository,
    finding_id: str,
    *,
    finding_type: str | None = None,
    verifier_state: str | None = None,
    decision: str | None = None,
    claim_text: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    assignments: list[str] = []
    values: list[Any] = []
    for column, value in (
        ("finding_type", finding_type),
        ("verifier_state", verifier_state),
        ("decision", decision),
        ("claim_text", claim_text),
    ):
        if value is not None:
            assignments.append(f"{column} = ?")
            values.append(value)
    if payload is not None:
        assignments.append("payload_json = ?")
        values.append(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        )
    values.append(finding_id)
    with repo.connection() as connection:
        connection.execute(
            f"UPDATE ai_findings SET {', '.join(assignments)} WHERE id = ?",
            tuple(values),
        )


def _draft_state(repo: CareerOpsRepository, application_id: str) -> tuple[Any, ...]:
    with repo.connection() as connection:
        resume_ids = tuple(
            row["id"]
            for row in connection.execute(
                "SELECT id FROM resume_versions ORDER BY id"
            ).fetchall()
        )
    draft_directories = tuple(
        sorted(
            path.name
            for path in (repo.vault_path / "resumes").iterdir()
            if path.is_dir() and path.name.startswith("res_")
        )
    )
    application = repo.get_application(application_id)
    return (
        resume_ids,
        draft_directories,
        application.selected_resume_version_id,
        application.latest_report_id,
        application.approval_id,
        application.submission_state,
    )


def _assert_rejected_without_draft(
    repo: CareerOpsRepository,
    application_id: str,
    task_id: str,
    finding_ids: list[str],
) -> None:
    before = _draft_state(repo, application_id)
    with pytest.raises((KeyError, ValueError)):
        create_verified_resume_draft(
            repo,
            application_id=application_id,
            task_id=task_id,
            finding_ids=finding_ids,
            name="Rejected draft",
        )
    assert _draft_state(repo, application_id) == before


def test_safe_findings_create_unrendered_child_without_mutating_parent(tmp_path: Path):
    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    parent = _select_parent(repo, application.id)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo, application.id
    )
    parent_bytes = parent.tex_path.read_bytes()

    child = create_verified_resume_draft(
        repo,
        application_id=application.id,
        task_id=task.id,
        finding_ids=[findings[0].id],
        name="Role tailored draft",
    )

    assert child.id != parent.id
    assert child.parent_resume_version_id == parent.id
    assert child.role_family == parent.role_family
    assert child.render_status == "not_rendered"
    assert child.parse_status == "not_parsed"
    assert child.pdf_path is None
    assert child.pdf_sha256 is None
    assert child.tex_path.read_text(encoding="utf-8") == PARENT_SOURCE.replace(
        "Built data pipeline.", "Built Python data pipeline."
    )
    assert parent.tex_path.read_bytes() == parent_bytes
    assert repo.get_application(application.id).selected_resume_version_id == child.id


@pytest.mark.parametrize(
    ("verifier_state", "decision"),
    [
        ("blocked", "accepted"),
        ("rejected", "accepted"),
        ("pending", "accepted"),
        ("needs_evidence", "accepted"),
        ("verified", "pending"),
        ("verified", "rejected"),
    ],
)
def test_unaccepted_or_unverified_finding_is_rejected(
    tmp_path: Path,
    verifier_state: str,
    decision: str,
):
    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    _select_parent(repo, application.id)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo, application.id
    )
    _mutate_finding(
        repo,
        findings[0].id,
        verifier_state=verifier_state,
        decision=decision,
    )

    _assert_rejected_without_draft(
        repo, application.id, task.id, [findings[0].id]
    )


def test_wrong_finding_type_task_and_application_are_rejected(tmp_path: Path):
    repo = _make_repo(tmp_path)
    first_application = _create_application(repo)
    _select_parent(repo, first_application.id)
    first_task, first_findings, _evidence = _create_completed_suggestion_task(
        repo, first_application.id
    )

    _mutate_finding(
        repo,
        first_findings[0].id,
        finding_type="claim_review_decision",
    )
    _assert_rejected_without_draft(
        repo, first_application.id, first_task.id, [first_findings[0].id]
    )
    _mutate_finding(
        repo,
        first_findings[0].id,
        finding_type="resume_edit_suggestion",
    )

    second_task, second_findings, _evidence = _create_completed_suggestion_task(
        repo, first_application.id
    )
    _assert_rejected_without_draft(
        repo, first_application.id, first_task.id, [second_findings[0].id]
    )

    second_application = _create_application(repo)
    _select_parent(repo, second_application.id)
    foreign_task, foreign_findings, _evidence = _create_completed_suggestion_task(
        repo, second_application.id
    )
    _assert_rejected_without_draft(
        repo,
        first_application.id,
        foreign_task.id,
        [foreign_findings[0].id],
    )


@pytest.mark.parametrize("task_status", ["pending", "blocked", "failed", "running"])
def test_non_completed_task_is_rejected(tmp_path: Path, task_status: str):
    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    _select_parent(repo, application.id)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo, application.id
    )
    with repo.connection() as connection:
        connection.execute(
            "UPDATE ai_tasks SET status = ? WHERE id = ?",
            (task_status, task.id),
        )

    _assert_rejected_without_draft(
        repo, application.id, task.id, [findings[0].id]
    )


def test_wrong_task_type_and_duplicate_finding_ids_are_rejected(tmp_path: Path):
    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    _select_parent(repo, application.id)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo, application.id
    )
    with repo.connection() as connection:
        connection.execute(
            "UPDATE ai_tasks SET task_type = 'analyze_fit' WHERE id = ?",
            (task.id,),
        )
    _assert_rejected_without_draft(
        repo, application.id, task.id, [findings[0].id]
    )
    with repo.connection() as connection:
        connection.execute(
            "UPDATE ai_tasks SET task_type = 'suggest_resume_edits' WHERE id = ?",
            (task.id,),
        )
    _assert_rejected_without_draft(
        repo,
        application.id,
        task.id,
        [findings[0].id, findings[0].id],
    )


def test_finding_outside_completed_authority_snapshot_is_rejected(tmp_path: Path):
    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    _select_parent(repo, application.id)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo, application.id
    )
    original = findings[0]
    unauthorized_id = "aif_unauthorized"
    with repo.connection() as connection:
        connection.execute(
            """
            INSERT INTO ai_findings (
                id, task_id, claim_token, finding_type, severity, claim_text,
                evidence_item_id, recommendation, verifier_state, decision,
                payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                unauthorized_id,
                task.id,
                original.claim_token,
                original.finding_type,
                original.severity.value,
                original.claim_text,
                original.evidence_item_id,
                original.recommendation,
                "verified",
                "accepted",
                json.dumps(original.payload, separators=(",", ":"), sort_keys=True),
                original.created_at,
                original.updated_at,
            ),
        )

    _assert_rejected_without_draft(
        repo, application.id, task.id, [unauthorized_id]
    )


def test_changed_selected_resume_is_rejected(tmp_path: Path):
    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    _select_parent(repo, application.id)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo, application.id
    )
    _select_parent(repo, application.id, source="Replacement parent source")

    _assert_rejected_without_draft(
        repo, application.id, task.id, [findings[0].id]
    )


@pytest.mark.parametrize("tamper", ["bytes", "missing", "duplicate"])
def test_tampered_missing_or_duplicate_context_artifact_is_rejected(
    tmp_path: Path,
    tamper: str,
):
    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    _select_parent(repo, application.id)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo, application.id
    )
    context = next(
        artifact
        for artifact in repo.list_ai_artifacts(task.id)
        if artifact.artifact_type == "context"
    )
    if tamper == "bytes":
        context.path.write_text("{}", encoding="utf-8")
    elif tamper == "missing":
        context.path.unlink()
    else:
        duplicate_id = "aia_duplicate_context"
        duplicate_path = (
            repo.vault_path
            / "ai-artifacts"
            / task.id
            / duplicate_id
            / "context.json"
        )
        duplicate_path.parent.mkdir()
        duplicate_path.write_bytes(context.path.read_bytes())
        with repo.connection() as connection:
            connection.execute(
                """
                INSERT INTO ai_artifacts (
                    id, task_id, claim_token, artifact_type, path, sha256,
                    schema_version, storage_version, created_at
                ) VALUES (?, ?, ?, 'context', ?, ?, '1', 2, ?)
                """,
                (
                    duplicate_id,
                    task.id,
                    context.claim_token,
                    str(duplicate_path),
                    context.sha256,
                    context.created_at,
                ),
            )

    _assert_rejected_without_draft(
        repo, application.id, task.id, [findings[0].id]
    )


def test_tampered_parent_source_is_rejected(tmp_path: Path):
    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    parent = _select_parent(repo, application.id)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo, application.id
    )
    parent.tex_path.write_text("Tampered parent", encoding="utf-8")

    _assert_rejected_without_draft(
        repo, application.id, task.id, [findings[0].id]
    )


@pytest.mark.parametrize("invalid_span", ["missing", "duplicate"])
def test_missing_or_duplicate_source_span_has_zero_side_effects(
    tmp_path: Path,
    invalid_span: str,
):
    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    source = "Python alpha Python\nBuilt data pipeline."
    _select_parent(repo, application.id, source=source)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo,
        application.id,
        suggestions=[
            {
                "source_text": "Built data pipeline.",
                "replacement_text": "Built Python data pipeline.",
                "reason": "Aligns the wording with evidence",
                "evidence_refs": [],
            }
        ],
    )
    payload = dict(findings[0].payload)
    payload["source_text"] = "absent" if invalid_span == "missing" else "Python"
    _mutate_finding(repo, findings[0].id, payload=payload)

    _assert_rejected_without_draft(
        repo, application.id, task.id, [findings[0].id]
    )


def test_overlapping_source_spans_have_zero_side_effects(tmp_path: Path):
    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    _select_parent(repo, application.id)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo,
        application.id,
        suggestions=[
            {
                "source_text": "Built data pipeline.",
                "replacement_text": "Built Python data pipeline.",
                "reason": "Aligns the wording with evidence",
                "evidence_refs": [],
            },
            {
                "source_text": "Led API migration.",
                "replacement_text": "Led Python API migration.",
                "reason": "Aligns the wording with evidence",
                "evidence_refs": [],
            },
        ],
    )
    first_payload = dict(findings[0].payload)
    second_payload = dict(findings[1].payload)
    first_payload["source_text"] = "Built data pipeline.\nLed API"
    second_payload["source_text"] = "API migration."
    _mutate_finding(repo, findings[0].id, payload=first_payload)
    _mutate_finding(repo, findings[1].id, payload=second_payload)

    _assert_rejected_without_draft(
        repo,
        application.id,
        task.id,
        [findings[0].id, findings[1].id],
    )


def test_all_non_overlapping_edits_are_applied_atomically(tmp_path: Path):
    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    _select_parent(repo, application.id)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo,
        application.id,
        suggestions=[
            {
                "source_text": "Built data pipeline.",
                "replacement_text": "Built Python data pipeline.",
                "reason": "Aligns the wording with evidence",
                "evidence_refs": [],
            },
            {
                "source_text": "Led API migration.",
                "replacement_text": "Led Python API migration.",
                "reason": "Aligns the wording with evidence",
                "evidence_refs": [],
            },
        ],
    )

    child = create_verified_resume_draft(
        repo,
        application_id=application.id,
        task_id=task.id,
        finding_ids=[item.id for item in reversed(findings)],
        name="Atomic draft",
    )

    assert child.tex_path.read_text(encoding="utf-8") == (
        PARENT_SOURCE.replace(
            "Built data pipeline.", "Built Python data pipeline."
        ).replace("Led API migration.", "Led Python API migration.")
    )


@pytest.mark.parametrize(
    "replacement",
    [
        "API key: live-secret-value",
        "Delivered Accurio migration.",
        "Improved throughput by 50%.",
        "Built an unsupported Kubernetes control plane.",
    ],
)
def test_secret_nda_metric_and_unsupported_claims_are_rejected(
    tmp_path: Path,
    replacement: str,
):
    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    _select_parent(repo, application.id)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo, application.id
    )
    payload = dict(findings[0].payload)
    payload["replacement_text"] = replacement
    _mutate_finding(
        repo,
        findings[0].id,
        claim_text=replacement,
        payload=payload,
    )

    _assert_rejected_without_draft(
        repo, application.id, task.id, [findings[0].id]
    )


@pytest.mark.parametrize(
    ("verification_state", "privacy_level", "claim"),
    [
        ("rejected", "public", "Project Phoenix private roadmap"),
        ("verified", "private", "Project Nightfall private roadmap"),
    ],
)
def test_rejected_and_private_claim_material_is_rejected(
    tmp_path: Path,
    verification_state: str,
    privacy_level: str,
    claim: str,
):
    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    _select_parent(repo, application.id)
    repo.create_profile_fact(
        fact_key="restricted_project",
        fact_value=claim,
        verification_state=verification_state,
        privacy_level=privacy_level,
    )
    task, findings, _evidence = _create_completed_suggestion_task(
        repo, application.id
    )
    payload = dict(findings[0].payload)
    replacement = f"Built {claim}."
    payload["replacement_text"] = replacement
    _mutate_finding(
        repo,
        findings[0].id,
        claim_text=replacement,
        payload=payload,
    )

    _assert_rejected_without_draft(
        repo, application.id, task.id, [findings[0].id]
    )


@pytest.mark.parametrize("evidence_case", ["unverified", "private", "unlinked", "not_in_context"])
def test_evidence_must_be_linked_verified_public_and_in_task_context(
    tmp_path: Path,
    evidence_case: str,
):
    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    _select_parent(repo, application.id)
    task, findings, evidence = _create_completed_suggestion_task(
        repo, application.id
    )
    evidence_id = evidence.id
    with repo.connection() as connection:
        if evidence_case == "unverified":
            connection.execute(
                "UPDATE evidence_items SET verification_state = 'rejected' WHERE id = ?",
                (evidence.id,),
            )
        elif evidence_case == "private":
            connection.execute(
                "UPDATE evidence_items SET privacy_level = 'private' WHERE id = ?",
                (evidence.id,),
            )
        elif evidence_case == "unlinked":
            connection.execute(
                "DELETE FROM application_evidence_links WHERE application_id = ? AND evidence_item_id = ?",
                (application.id, evidence.id),
            )
        else:
            later = repo.create_evidence_item(
                claim_text="Built a Python data pipeline.",
                evidence_type="portfolio",
                verification_state="verified",
                privacy_level="public",
            )
            connection.execute(
                "INSERT INTO application_evidence_links (application_id, evidence_item_id, created_at) VALUES (?, ?, ?)",
                (application.id, later.id, evidence.created_at),
            )
            evidence_id = later.id
    payload = dict(findings[0].payload)
    payload["evidence_refs"] = [evidence_id]
    _mutate_finding(repo, findings[0].id, payload=payload)

    _assert_rejected_without_draft(
        repo, application.id, task.id, [findings[0].id]
    )


def test_database_failure_cleans_only_new_draft_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    _select_parent(repo, application.id)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo, application.id
    )
    before = _draft_state(repo, application.id)

    def fail_commit(**_values: Any):
        raise sqlite3.OperationalError("simulated database failure")

    monkeypatch.setattr(repo, "create_and_select_resume_draft", fail_commit)

    with pytest.raises(sqlite3.OperationalError):
        create_verified_resume_draft(
            repo,
            application_id=application.id,
            task_id=task.id,
            finding_ids=[findings[0].id],
            name="Database failure draft",
        )
    assert _draft_state(repo, application.id) == before


def test_invalid_generated_resume_id_cannot_escape_resume_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from careerops.resumes import drafts as drafts_module

    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    _select_parent(repo, application.id)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo, application.id
    )
    monkeypatch.setattr(drafts_module, "new_id", lambda _prefix: "../escape")

    with pytest.raises(ValueError):
        create_verified_resume_draft(
            repo,
            application_id=application.id,
            task_id=task.id,
            finding_ids=[findings[0].id],
            name="Path escape draft",
        )

    assert not (repo.vault_path / "escape").exists()
    assert repo.get_application(application.id).selected_resume_version_id is not None


def test_selection_race_fails_closed_and_cleans_new_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    _select_parent(repo, application.id)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo, application.id
    )
    replacement = _select_parent(
        repo,
        application.id,
        source="Race replacement",
        select=False,
    )
    original = repo.create_and_select_resume_draft

    def race(**values: Any):
        with repo.connection() as connection:
            connection.execute(
                "UPDATE applications SET selected_resume_version_id = ? WHERE id = ?",
                (replacement.id, application.id),
            )
        return original(**values)

    monkeypatch.setattr(repo, "create_and_select_resume_draft", race)
    before_resume_ids = {item.id for item in repo.list_resume_versions()}

    with pytest.raises(ValueError):
        create_verified_resume_draft(
            repo,
            application_id=application.id,
            task_id=task.id,
            finding_ids=[findings[0].id],
            name="Race draft",
        )
    assert {item.id for item in repo.list_resume_versions()} == before_resume_ids
    assert list((repo.vault_path / "resumes").glob("res_*/source.tex")) == []


def test_completed_authority_race_fails_closed_and_cleans_new_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    parent = _select_parent(repo, application.id)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo, application.id
    )
    original = repo.create_and_select_resume_draft

    def race(**values: Any):
        with repo.connection() as connection:
            version_rows = connection.execute(
                """
                SELECT * FROM application_versions
                WHERE application_id = ? AND event_type = 'ai_task.completed'
                ORDER BY version_number
                """,
                (application.id,),
            ).fetchall()
            authority_row = next(
                row
                for row in version_rows
                if json.loads(row["snapshot_json"])["ai_task"]["id"] == task.id
            )
            next_version = connection.execute(
                """
                SELECT COALESCE(MAX(version_number), 0) + 1
                FROM application_versions WHERE application_id = ?
                """,
                (application.id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO application_versions (
                    id, application_id, version_number, event_type,
                    snapshot_json, created_at
                ) VALUES (?, ?, ?, 'ai_task.completed', ?, ?)
                """,
                (
                    f"appv_{uuid4().hex}",
                    application.id,
                    next_version,
                    authority_row["snapshot_json"],
                    authority_row["created_at"],
                ),
            )
        return original(**values)

    monkeypatch.setattr(repo, "create_and_select_resume_draft", race)
    before_resume_ids = {item.id for item in repo.list_resume_versions()}

    with pytest.raises(ValueError, match="authority.*ambiguous"):
        create_verified_resume_draft(
            repo,
            application_id=application.id,
            task_id=task.id,
            finding_ids=[findings[0].id],
            name="Authority race draft",
        )

    assert {item.id for item in repo.list_resume_versions()} == before_resume_ids
    assert repo.get_application(application.id).selected_resume_version_id == parent.id
    assert list((repo.vault_path / "resumes").glob("res_*/source.tex")) == []


def test_parent_substitution_cannot_write_resume_source_outside_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from careerops.resumes import drafts as drafts_module

    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    parent = _select_parent(repo, application.id)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo, application.id
    )
    resume_id = "res_" + ("a" * 32)
    draft_dir = repo.vault_path / "resumes" / resume_id
    moved_dir = repo.vault_path / "resumes" / "moved-resume-draft"
    outside_dir = tmp_path / "outside-vault"
    outside_dir.mkdir()
    real_uuid4 = uuid4
    substitution_attempted = False
    substituted = False

    def substitute_parent():
        nonlocal substituted, substitution_attempted
        if not substituted:
            substitution_attempted = True
            draft_dir.rename(moved_dir)
            if os.name == "nt":
                subprocess.run(
                    [
                        "cmd.exe",
                        "/d",
                        "/c",
                        "mklink",
                        "/J",
                        str(draft_dir),
                        str(outside_dir),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            else:
                draft_dir.symlink_to(outside_dir, target_is_directory=True)
            substituted = True
        return real_uuid4()

    monkeypatch.setattr(drafts_module, "new_id", lambda _prefix: resume_id)
    monkeypatch.setattr(drafts_module, "uuid4", substitute_parent)
    before_resume_ids = {item.id for item in repo.list_resume_versions()}

    try:
        with pytest.raises(ValueError):
            create_verified_resume_draft(
                repo,
                application_id=application.id,
                task_id=task.id,
                finding_ids=[findings[0].id],
                name="Substitution race draft",
            )
        assert substitution_attempted
        assert list(outside_dir.iterdir()) == []
        assert {item.id for item in repo.list_resume_versions()} == before_resume_ids
        assert repo.get_application(application.id).selected_resume_version_id == parent.id
    finally:
        for path in outside_dir.iterdir():
            if path.is_file():
                path.unlink()
        if draft_dir.exists() or draft_dir.is_symlink():
            draft_dir.rmdir()
        if moved_dir.exists():
            shutil.rmtree(moved_dir)


@pytest.mark.skipif(os.name != "nt", reason="Windows native publication path")
def test_windows_publication_failure_cleans_owned_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from careerops.resumes import drafts as drafts_module

    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    parent = _select_parent(repo, application.id)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo, application.id
    )
    resume_id = "res_" + ("b" * 32)
    draft_dir = repo.vault_path / "resumes" / resume_id

    def fail_rename(*_args: Any) -> None:
        raise OSError("simulated native rename failure")

    monkeypatch.setattr(drafts_module, "new_id", lambda _prefix: resume_id)
    monkeypatch.setattr(drafts_module, "_windows_rename_relative", fail_rename)

    with pytest.raises(ValueError, match="changed during publication"):
        create_verified_resume_draft(
            repo,
            application_id=application.id,
            task_id=task.id,
            finding_ids=[findings[0].id],
            name="Failed publication draft",
        )

    assert not draft_dir.exists()
    assert repo.get_application(application.id).selected_resume_version_id == parent.id


def test_success_returns_inserted_model_without_post_commit_getter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    _select_parent(repo, application.id)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo, application.id
    )
    original = repo.create_and_select_resume_draft

    def commit_then_disable_getter(**values: Any):
        result = original(**values)
        monkeypatch.setattr(
            repo,
            "get_resume_version",
            lambda _resume_id: (_ for _ in ()).throw(RuntimeError("post-commit getter")),
        )
        return result

    monkeypatch.setattr(repo, "create_and_select_resume_draft", commit_then_disable_getter)

    child = create_verified_resume_draft(
        repo,
        application_id=application.id,
        task_id=task.id,
        finding_ids=[findings[0].id],
        name="No getter draft",
    )

    assert child.parent_resume_version_id is not None


def test_post_commit_handle_release_cannot_report_false_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from careerops.resumes import drafts as drafts_module

    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    _select_parent(repo, application.id)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo, application.id
    )
    original_commit = repo.create_and_select_resume_draft
    original_close = drafts_module._close_published_source

    def commit_then_fail_release(**values: Any):
        result = original_commit(**values)

        def close_then_fail(published: Any) -> None:
            original_close(published)
            raise OSError("simulated post-commit handle release failure")

        monkeypatch.setattr(
            drafts_module, "_close_published_source", close_then_fail
        )
        return result

    monkeypatch.setattr(
        repo, "create_and_select_resume_draft", commit_then_fail_release
    )

    child = create_verified_resume_draft(
        repo,
        application_id=application.id,
        task_id=task.id,
        finding_ids=[findings[0].id],
        name="Committed draft",
    )

    assert repo.get_application(application.id).selected_resume_version_id == child.id


def _make_application_approved(
    repo: CareerOpsRepository,
    application_id: str,
    parent_id: str,
) -> None:
    pdf_path = repo.vault_path / "resumes" / application_id / "resume.pdf"
    pdf_bytes = b"%PDF-1.4\nfixture"
    pdf_path.write_bytes(pdf_bytes)
    repo.update_resume_render(
        parent_id,
        pdf_path=pdf_path,
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        render_status="rendered",
        parse_status="parsed",
        page_count=1,
    )
    application = repo.get_application(application_id)
    report = repo.save_report(
        application_id=application_id,
        resume_version_id=parent_id,
        jd_snapshot_id=application.jd_snapshot_id,
        fit={"score": 100},
        risks={"blockers": [], "warnings": []},
    )
    repo.approve_application(application_id, report.id, "Reviewed")


@pytest.mark.parametrize("immutable_state", ["approved", "submitted", "approved_status"])
def test_approved_and_submitted_applications_are_immutable(
    tmp_path: Path,
    immutable_state: str,
):
    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    parent = _select_parent(repo, application.id)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo, application.id
    )
    if immutable_state == "approved_status":
        with repo.connection() as connection:
            connection.execute(
                "UPDATE applications SET status = 'approved' WHERE id = ?",
                (application.id,),
            )
    else:
        _make_application_approved(repo, application.id, parent.id)
        if immutable_state == "submitted":
            repo.mark_submitted_manual(application.id, "Submitted")

    _assert_rejected_without_draft(
        repo, application.id, task.id, [findings[0].id]
    )


def test_child_selection_invalidates_report_and_records_version_provenance(
    tmp_path: Path,
):
    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    parent = _select_parent(repo, application.id)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo, application.id
    )
    report = repo.save_report(
        application_id=application.id,
        resume_version_id=parent.id,
        jd_snapshot_id=application.jd_snapshot_id,
        fit={"score": 80},
        risks={"blockers": [], "warnings": []},
    )

    child = create_verified_resume_draft(
        repo,
        application_id=application.id,
        task_id=task.id,
        finding_ids=[findings[0].id],
        name="Versioned draft",
    )

    updated = repo.get_application(application.id)
    assert updated.selected_resume_version_id == child.id
    assert updated.latest_report_id is None
    assert updated.approval_id is None
    assert updated.status.value == "needs_resume_update"
    assert updated.submission_state.value == "not_submitted"
    assert updated.submitted_at is None
    assert repo.get_report(report.id).id == report.id
    version = repo.list_application_versions(application.id)[-1]
    assert version.event_type == "resume_draft.created"
    assert version.snapshot["parent_resume_version_id"] == parent.id
    assert version.snapshot["resume_version_id"] == child.id
    assert version.snapshot["task_id"] == task.id
    assert version.snapshot["finding_ids"] == [findings[0].id]
    assert version.snapshot["invalidated_report_id"] == report.id


def test_api_creates_draft_and_validates_body_and_authority(tmp_path: Path):
    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    _select_parent(repo, application.id)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo, application.id
    )
    client = TestClient(create_app(repo, provider_registry={}))
    payload = {
        "task_id": task.id,
        "finding_ids": [findings[0].id],
        "name": "Role tailored draft",
    }

    response = client.post(
        f"/applications/{application.id}/resume-drafts",
        json=payload,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["parent_resume_version_id"] is not None
    assert body["render_status"] == "not_rendered"
    assert "tex_path" not in body
    assert "pdf_path" not in body

    malformed = dict(payload, finding_ids=[])
    assert client.post(
        f"/applications/{application.id}/resume-drafts", json=malformed
    ).status_code == 422
    duplicate = dict(payload, finding_ids=[findings[0].id, findings[0].id])
    assert client.post(
        f"/applications/{application.id}/resume-drafts", json=duplicate
    ).status_code == 422
    extra = dict(payload, unexpected=True)
    assert client.post(
        f"/applications/{application.id}/resume-drafts", json=extra
    ).status_code == 422


def test_api_maps_missing_foreign_and_unsafe_requests(tmp_path: Path):
    repo = _make_repo(tmp_path)
    application = _create_application(repo)
    _select_parent(repo, application.id)
    task, findings, _evidence = _create_completed_suggestion_task(
        repo, application.id
    )
    client = TestClient(create_app(repo, provider_registry={}))

    assert client.post(
        "/applications/app_missing/resume-drafts",
        json={
            "task_id": task.id,
            "finding_ids": [findings[0].id],
            "name": "Missing app",
        },
    ).status_code == 404
    assert client.post(
        f"/applications/{application.id}/resume-drafts",
        json={
            "task_id": "ait_missing",
            "finding_ids": [findings[0].id],
            "name": "Missing task",
        },
    ).status_code == 404
    assert client.post(
        f"/applications/{application.id}/resume-drafts",
        json={
            "task_id": task.id,
            "finding_ids": ["aif_missing"],
            "name": "Missing finding",
        },
    ).status_code == 404

    foreign_application = _create_application(repo)
    _select_parent(repo, foreign_application.id)
    assert client.post(
        f"/applications/{foreign_application.id}/resume-drafts",
        json={
            "task_id": task.id,
            "finding_ids": [findings[0].id],
            "name": "Foreign task",
        },
    ).status_code == 409

    payload = dict(findings[0].payload)
    payload["replacement_text"] = "API key: unsafe-value"
    _mutate_finding(repo, findings[0].id, payload=payload)
    assert client.post(
        f"/applications/{application.id}/resume-drafts",
        json={
            "task_id": task.id,
            "finding_ids": [findings[0].id],
            "name": "Unsafe draft",
        },
    ).status_code == 409
