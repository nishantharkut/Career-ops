from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
from pathlib import Path
from threading import Event, Lock
from typing import Any

import pytest
from fastapi.testclient import TestClient

import apps.api.careerops_api.main as api_main
from apps.api.careerops_api.main import BasicAuthConfig, create_app
from careerops.ai.contracts import (
    InvocationMetadata,
    ProviderAvailability,
    serialize_provider_output,
    validate_task_output,
)
from careerops.ai.providers.base import ProviderUnavailableError, build_provider_result
from careerops.core.repository import CareerOpsRepository
from careerops.core.status import PrivacyMode


VALID_OUTPUTS: dict[str, dict[str, Any]] = {
    "analyze_jd": {
        "task_type": "analyze_jd",
        "normalized_title": "Software Engineer",
        "normalized_company": "Example Corp",
        "normalized_location": "Remote",
        "responsibilities": [],
        "required_skills": ["Python"],
        "preferred_skills": [],
        "keywords": ["Python"],
        "uncertainties": [],
    },
    "analyze_fit": {
        "task_type": "analyze_fit",
        "score": 80,
        "matches": [],
        "gaps": [],
        "risks": [],
        "recommendations": [],
    },
    "suggest_resume_edits": {
        "task_type": "suggest_resume_edits",
        "suggestions": [
            {
                "source_text": "Built PocketBuddy",
                "replacement_text": "Built PocketBuddy",
                "reason": "Aligns the wording with evidence",
                "evidence_refs": [],
            }
        ],
    },
    "draft_cover_letter": {
        "task_type": "draft_cover_letter",
        "plain_text": "I am excited to apply.",
        "evidence_refs": [],
    },
    "review_claims": {"task_type": "review_claims", "decisions": []},
}


def availability(
    *,
    provider_kind: str = "fixture",
    model: str = "fixture-v1",
    privacy_mode: PrivacyMode = PrivacyMode.LOCAL_PRIVATE,
    endpoint: str = "",
    identity_name: str = "",
    available: bool = True,
    is_ai: bool = True,
) -> ProviderAvailability:
    return ProviderAvailability(
        available=available,
        provider_kind=provider_kind,
        model=model,
        privacy_mode=privacy_mode,
        data_leaves_machine=privacy_mode == PrivacyMode.CLOUD_EXTERNAL,
        is_ai=is_ai,
        detail="available" if available else "unavailable",
        endpoint=endpoint,
        identity_name=identity_name,
    )


def binding(status: ProviderAvailability) -> dict[str, Any]:
    values = status.model_dump(mode="json")
    values.pop("available")
    values.pop("detail")
    return values


class ControlledProvider:
    def __init__(
        self,
        status: ProviderAvailability,
        *,
        entered: Event | None = None,
        release: Event | None = None,
        error: Exception | None = None,
    ) -> None:
        self.status = status
        self.entered = entered
        self.release = release
        self.error = error
        self.calls = 0
        self._lock = Lock()

    def availability(self) -> ProviderAvailability:
        return self.status

    def run(self, request: Any):
        with self._lock:
            self.calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.release is not None and not self.release.wait(timeout=10):
            raise RuntimeError("test provider release timed out")
        if self.error is not None:
            raise self.error
        output = validate_task_output(request.task_type, VALID_OUTPUTS[request.task_type])
        raw_output = serialize_provider_output(output)
        return build_provider_result(
            provider_kind=self.status.provider_kind,
            model=self.status.model,
            privacy_mode=self.status.privacy_mode,
            data_leaves_machine=self.status.data_leaves_machine,
            is_ai=self.status.is_ai,
            structured_output=output,
            raw_output=raw_output,
            invocation=InvocationMetadata(
                mode="test_fixture",
                endpoint=self.status.endpoint,
                identity_name=self.status.identity_name,
            ),
        )


@dataclass
class AvailabilityProvider:
    status: ProviderAvailability
    calls: int = 0

    def availability(self) -> ProviderAvailability:
        return self.status

    def run(self, _request: Any):
        self.calls += 1
        raise AssertionError("provider should not run")


def make_repo(tmp_path: Path) -> CareerOpsRepository:
    repo = CareerOpsRepository(tmp_path / "careerops.db", tmp_path / "vault")
    repo.initialize()
    return repo


def create_application(repo: CareerOpsRepository, *, company: str = "Example Corp"):
    return repo.create_application(
        company=company,
        role_title="Software Engineer",
        location="Remote",
        source_url="https://example.com/jobs/1",
        jd_text=(
            f"Company: {company}\n"
            "Title: Software Engineer\n"
            "Location: Remote\n"
            "Required skills:\n- Python\n"
        ),
    )


def create_provider_config(
    repo: CareerOpsRepository,
    status: ProviderAvailability,
    *,
    enabled: bool = True,
    display_name: str = "Fixture provider",
):
    return repo.upsert_ai_provider_config(
        provider_kind=status.provider_kind,
        display_name=display_name,
        privacy_mode=status.privacy_mode.value,
        endpoint=status.endpoint,
        identity_name=status.identity_name,
        model=status.model,
        enabled=enabled,
        availability_binding=binding(status),
    )


def select_resume(repo: CareerOpsRepository, application_id: str):
    source_dir = repo.vault_path / "resumes" / application_id
    source_dir.mkdir(parents=True, exist_ok=True)
    tex_bytes = b"Built PocketBuddy"
    pdf_bytes = b"%PDF-1.4\nfixture resume"
    tex_path = source_dir / "resume.tex"
    pdf_path = source_dir / "resume.pdf"
    tex_path.write_bytes(tex_bytes)
    pdf_path.write_bytes(pdf_bytes)
    resume = repo.create_resume_version(
        name="Current resume",
        role_family="software",
        tex_path=tex_path,
        tex_sha256=hashlib.sha256(tex_bytes).hexdigest(),
        pdf_path=pdf_path,
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        render_status="rendered",
        parse_status="parsed",
        page_count=1,
    )
    repo.select_resume(application_id, resume.id)
    return resume


def test_create_task_is_persisted_running_queryable_and_reloadable(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    select_resume(repo, application.id)
    status = availability()
    config = create_provider_config(repo, status)
    entered = Event()
    release = Event()
    provider = ControlledProvider(status, entered=entered, release=release)
    app = create_app(repo, provider_registry={config.id: provider})

    with TestClient(app) as submit_client, TestClient(app) as query_client:
        with ThreadPoolExecutor(max_workers=1) as executor:
            accepted = executor.submit(
                submit_client.post,
                f"/applications/{application.id}/ai-tasks",
                json={
                    "task_type": "suggest_resume_edits",
                    "provider_config_id": config.id,
                    "input_artifact_ids": [],
                },
            )
            assert entered.wait(timeout=5)
            persisted = repo.list_ai_tasks(application.id)
            assert len(persisted) == 1
            running = query_client.get(f"/ai-tasks/{persisted[0].id}")
            assert running.status_code == 200
            assert running.json()["status"] == "running"
            assert running.json()["privacy_mode"] == "local_private"
            release.set()
            response = accepted.result(timeout=10)

    assert response.status_code == 202
    task_id = response.json()["id"]
    assert response.json()["provider_config_id"] == config.id
    assert provider.calls == 1
    with TestClient(create_app(repo, provider_registry={config.id: provider})) as reloaded:
        terminal = reloaded.get(f"/ai-tasks/{task_id}")
        findings = reloaded.get(f"/ai-tasks/{task_id}/findings")
        assert terminal.status_code == 200
        assert terminal.json()["status"] == "completed"
        assert len(findings.json()) == 1
        finding_id = findings.json()[0]["id"]
        accepted_finding = reloaded.post(
            f"/ai-findings/{finding_id}/decision", json={"decision": "accepted"}
        )
        conflict = reloaded.post(
            f"/ai-findings/{finding_id}/decision", json={"decision": "rejected"}
        )
        assert accepted_finding.status_code == 200
        assert conflict.status_code == 409


def test_background_failure_keeps_202_and_never_falls_back(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    selected_status = availability(model="selected-v1")
    fallback_status = availability(model="fallback-v1")
    selected_config = create_provider_config(repo, selected_status)
    fallback_config = create_provider_config(repo, fallback_status)
    selected = ControlledProvider(
        selected_status, error=ProviderUnavailableError("private provider detail")
    )
    fallback = ControlledProvider(fallback_status)
    client = TestClient(
        create_app(
            repo,
            provider_registry={
                selected_config.id: selected,
                fallback_config.id: fallback,
            },
        )
    )

    response = client.post(
        f"/applications/{application.id}/ai-tasks",
        json={
            "task_type": "analyze_jd",
            "provider_config_id": selected_config.id,
            "input_artifact_ids": [],
        },
    )

    assert response.status_code == 202
    task = client.get(f"/ai-tasks/{response.json()['id']}").json()
    assert task["status"] == "failed"
    assert task["error_summary"] == "provider_unavailable"
    assert "private provider detail" not in str(task)
    assert selected.calls == 1
    assert fallback.calls == 0


def test_api_heartbeat_setup_failure_returns_202_with_durable_failed_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    status = availability()
    config = create_provider_config(repo, status)
    provider = ControlledProvider(status)
    app = create_app(repo, provider_registry={config.id: provider})

    def fail_heartbeat_setup(_task_id: str, _claim_token: str):
        raise RuntimeError("heartbeat setup detail must not leak")

    monkeypatch.setattr(
        app.state.ai_orchestrator, "_start_claim_heartbeat", fail_heartbeat_setup
    )
    client = TestClient(app)

    response = client.post(
        f"/applications/{application.id}/ai-tasks",
        json={"task_type": "analyze_jd", "provider_config_id": config.id},
    )

    assert response.status_code == 202
    task = client.get(f"/ai-tasks/{response.json()['id']}").json()
    assert task["status"] == "failed"
    assert task["error_summary"] == "provider_error"
    assert "must not leak" not in str(task)
    assert provider.calls == 0


def test_provider_listing_combines_persisted_and_honest_runtime_state(tmp_path: Path):
    repo = make_repo(tmp_path)
    present_status = availability(model="present")
    missing_status = availability(model="missing")
    disabled_status = availability(model="disabled")
    present = create_provider_config(repo, present_status)
    missing = create_provider_config(repo, missing_status)
    disabled = create_provider_config(repo, disabled_status, enabled=False)
    client = TestClient(
        create_app(
            repo,
            provider_registry={present.id: AvailabilityProvider(present_status)},
        )
    )

    providers = {item["id"]: item for item in client.get("/ai/providers").json()}

    assert providers[present.id]["available"] is True
    assert providers[present.id]["current_metadata"]["model"] == "present"
    assert providers[missing.id]["available"] is False
    assert providers[missing.id]["availability_detail"] == "runtime_not_registered"
    assert providers[disabled.id]["available"] is False
    assert "executable" not in providers[present.id]["current_metadata"]
    assert "codex_home" not in providers[present.id]["current_metadata"]

    missing_launch = client.post(
        f"/applications/{create_application(repo).id}/ai-tasks",
        json={"task_type": "analyze_jd", "provider_config_id": missing.id},
    )
    disabled_launch = client.post(
        f"/applications/{create_application(repo).id}/ai-tasks",
        json={"task_type": "analyze_jd", "provider_config_id": disabled.id},
    )
    assert missing_launch.status_code == 409
    assert disabled_launch.status_code == 409


def test_provider_listing_redacts_secret_shaped_live_metadata(tmp_path: Path):
    repo = make_repo(tmp_path)
    status = availability(model="secret-metadata").model_copy(
        update={"detail": "token=provider-list-secret"}
    )
    config = create_provider_config(repo, status)
    client = TestClient(
        create_app(repo, provider_registry={config.id: AvailabilityProvider(status)})
    )

    response = client.get("/ai/providers")

    assert response.status_code == 200
    assert "provider-list-secret" not in response.text
    assert "[REDACTED]" in response.text


def test_default_provider_setup_is_fail_closed_and_keeps_codex_identities_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    codex2_status = availability(
        provider_kind="codex",
        model="gpt-fixture",
        privacy_mode=PrivacyMode.CLOUD_EXTERNAL,
        endpoint="codex-identity://codex2",
        identity_name="codex2",
    )
    codex_status = codex2_status.model_copy(
        update={"endpoint": "codex-identity://codex", "identity_name": "codex"}
    )
    codex2 = AvailabilityProvider(codex2_status)
    codex = AvailabilityProvider(codex_status)
    monkeypatch.setenv("CAREEROPS_CODEX_ENABLED", "true")
    monkeypatch.setenv("CAREEROPS_OLLAMA_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("CAREEROPS_OLLAMA_MODEL", "fixture-local")
    monkeypatch.setattr(
        api_main, "discover_codex_providers", lambda **_kwargs: [codex2, codex]
    )

    providers = TestClient(create_app(repo)).get("/ai/providers").json()

    identities = [
        item["identity_name"] for item in providers if item["provider_kind"] == "codex"
    ]
    assert identities == ["codex2", "codex"]
    assert len({item["id"] for item in providers if item["provider_kind"] == "codex"}) == 2
    deterministic = next(
        item for item in providers if item["provider_kind"] == "deterministic_verifier"
    )
    ollama = next(item for item in providers if item["provider_kind"] == "ollama")
    assert deterministic["is_ai"] is False
    assert "AI" not in deterministic["display_name"]
    assert ollama["available"] is False
    assert ollama["enabled"] is False
    assert ollama["endpoint"] == "http://127.0.0.1:11434"
    assert ollama["model"] == "fixture-local"


def test_default_provider_setup_always_lists_unmanaged_ollama_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from careerops.ai.providers.ollama import OllamaProvider

    repo = make_repo(tmp_path)
    monkeypatch.delenv("CAREEROPS_OLLAMA_URL", raising=False)
    monkeypatch.delenv("CAREEROPS_OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("CAREEROPS_CODEX_ENABLED", raising=False)

    def reject_network(*_args, **_kwargs):
        raise AssertionError("default unmanaged Ollama registration must not probe")

    monkeypatch.setattr(OllamaProvider, "_request_json", reject_network)

    providers = TestClient(create_app(repo)).get("/ai/providers").json()
    ollama = next(item for item in providers if item["provider_kind"] == "ollama")

    assert ollama["endpoint"] == "http://127.0.0.1:11434"
    assert ollama["model"] == "qwen2.5:7b"
    assert ollama["enabled"] is False
    assert ollama["available"] is False


def test_packet_reloads_selected_resume_report_latest_task_evidence_and_versions(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    resume = select_resume(repo, application.id)
    report = repo.save_report(
        application_id=application.id,
        resume_version_id=resume.id,
        jd_snapshot_id=application.jd_snapshot_id,
        fit={"score": 80},
        risks={"blockers": [], "warnings": []},
    )
    evidence = repo.create_evidence_item(
        claim_text="Built PocketBuddy",
        evidence_type="project",
        source_url="https://example.com/project",
        verification_state="verified",
        privacy_level="public",
    )
    repo.link_application_evidence(application.id, evidence.id)
    repo.record_application_version(application.id, "packet.seeded", {"report_id": report.id})
    status = availability()
    config = create_provider_config(repo, status)
    provider = AvailabilityProvider(status)
    from careerops.ai.orchestrator import AiOrchestrator

    task = AiOrchestrator(repo, {config.id: provider}).enqueue(
        application.id, "analyze_jd", config.id
    )

    first = TestClient(create_app(repo, provider_registry={config.id: provider})).get(
        f"/applications/{application.id}/packet"
    )
    reloaded = TestClient(create_app(repo, provider_registry={config.id: provider})).get(
        f"/applications/{application.id}/packet"
    )

    assert first.status_code == 200
    packet = reloaded.json()
    assert packet["selected_resume"]["id"] == resume.id
    assert packet["latest_report"]["id"] == report.id
    assert packet["latest_ai_task"]["id"] == task.id
    assert packet["approval"] is None
    assert packet["evidence"][0]["id"] == evidence.id
    assert packet["versions"][0]["event_type"] == "packet.seeded"
    assert "tex_path" not in str(packet)
    assert "raw_text_path" not in str(packet)


def test_artifact_downloads_are_bound_to_latest_packet_and_verify_integrity(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo, company="Example/Corp")
    resume = select_resume(repo, application.id)
    report = repo.save_report(
        application_id=application.id,
        resume_version_id=resume.id,
        jd_snapshot_id=application.jd_snapshot_id,
        fit={"score": 80},
        risks={"blockers": [], "warnings": []},
    )
    approval = repo.approve_application(application.id, report.id, "Reviewed")
    client = TestClient(create_app(repo, provider_registry={}))

    jd = client.get(f"/applications/{application.id}/artifacts/jd")
    pdf = client.get(f"/applications/{application.id}/artifacts/resume-pdf")
    report_download = client.get(f"/applications/{application.id}/artifacts/report")
    approval_download = client.get(f"/applications/{application.id}/artifacts/approval")

    assert jd.status_code == 200
    assert b"Company: Example/Corp" in jd.content
    assert pdf.status_code == 200
    assert pdf.content == resume.pdf_path.read_bytes()
    assert report_download.json()["id"] == report.id
    assert approval_download.json()["id"] == approval.id
    for response in (jd, pdf, report_download, approval_download):
        disposition = response.headers["content-disposition"]
        assert ".." not in disposition
        assert "/" not in disposition

    resume.pdf_path.write_bytes(b"tampered")
    assert client.get(f"/applications/{application.id}/artifacts/resume-pdf").status_code == 409
    snapshot = repo.get_jd_snapshot(application.jd_snapshot_id)
    snapshot.raw_text_path.write_text("tampered", encoding="utf-8")
    assert client.get(f"/applications/{application.id}/artifacts/jd").status_code == 409


@pytest.mark.parametrize(
    "mismatch",
    [
        "application_approval",
        "application_latest_report",
        "application_selected_resume",
        "approval_application",
        "approval_report",
        "approval_resume",
        "report_application",
        "report_resume",
        "report_jd",
        "approval_pdf_checksum",
        "resume_pdf_checksum",
        "resume_pdf_outside",
        "resume_pdf_tamper",
    ],
)
def test_approval_artifact_fails_closed_for_every_packet_or_pdf_mismatch(
    tmp_path: Path, mismatch: str
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    resume = select_resume(repo, application.id)
    report = repo.save_report(
        application_id=application.id,
        resume_version_id=resume.id,
        jd_snapshot_id=application.jd_snapshot_id,
        fit={"score": 100},
        risks={"blockers": [], "warnings": []},
    )
    approval = repo.approve_application(application.id, report.id, "Reviewed")
    foreign_application = create_application(repo, company="Foreign Corp")
    foreign_resume = select_resume(repo, foreign_application.id)
    foreign_report = repo.save_report(
        application_id=foreign_application.id,
        resume_version_id=foreign_resume.id,
        jd_snapshot_id=foreign_application.jd_snapshot_id,
        fit={"score": 100},
        risks={"blockers": [], "warnings": []},
    )
    foreign_approval = repo.approve_application(
        foreign_application.id, foreign_report.id, "Foreign review"
    )
    outside_pdf = tmp_path / "outside.pdf"
    outside_pdf.write_bytes(resume.pdf_path.read_bytes())

    with repo.connection() as connection:
        if mismatch == "application_approval":
            connection.execute(
                "UPDATE applications SET approval_id = ? WHERE id = ?",
                (foreign_approval.id, application.id),
            )
        elif mismatch == "application_latest_report":
            connection.execute(
                "UPDATE applications SET latest_report_id = ? WHERE id = ?",
                (foreign_report.id, application.id),
            )
        elif mismatch == "application_selected_resume":
            connection.execute(
                "UPDATE applications SET selected_resume_version_id = ? WHERE id = ?",
                (foreign_resume.id, application.id),
            )
        elif mismatch == "approval_application":
            connection.execute(
                "UPDATE approvals SET application_id = ? WHERE id = ?",
                (foreign_application.id, approval.id),
            )
        elif mismatch == "approval_report":
            connection.execute(
                "UPDATE approvals SET report_id = ? WHERE id = ?",
                (foreign_report.id, approval.id),
            )
        elif mismatch == "approval_resume":
            connection.execute(
                "UPDATE approvals SET resume_version_id = ? WHERE id = ?",
                (foreign_resume.id, approval.id),
            )
        elif mismatch == "report_application":
            connection.execute(
                "UPDATE reports SET application_id = ? WHERE id = ?",
                (foreign_application.id, report.id),
            )
        elif mismatch == "report_resume":
            connection.execute(
                "UPDATE reports SET resume_version_id = ? WHERE id = ?",
                (foreign_resume.id, report.id),
            )
        elif mismatch == "report_jd":
            connection.execute(
                "UPDATE reports SET jd_snapshot_id = ? WHERE id = ?",
                (foreign_application.jd_snapshot_id, report.id),
            )
        elif mismatch == "approval_pdf_checksum":
            connection.execute(
                "UPDATE approvals SET pdf_sha256 = ? WHERE id = ?",
                ("b" * 64, approval.id),
            )
        elif mismatch == "resume_pdf_checksum":
            connection.execute(
                "UPDATE resume_versions SET pdf_sha256 = ? WHERE id = ?",
                ("b" * 64, resume.id),
            )
        elif mismatch == "resume_pdf_outside":
            connection.execute(
                "UPDATE resume_versions SET pdf_path = ? WHERE id = ?",
                (str(outside_pdf), resume.id),
            )
    if mismatch == "resume_pdf_tamper":
        resume.pdf_path.write_bytes(b"tampered")

    response = TestClient(create_app(repo, provider_registry={})).get(
        f"/applications/{application.id}/artifacts/approval"
    )

    assert response.status_code == 409


def test_artifact_routes_reject_missing_stale_and_non_vault_paths(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    client = TestClient(create_app(repo, provider_registry={}))
    assert client.get(f"/applications/{application.id}/artifacts/resume-pdf").status_code == 404
    assert client.get(f"/applications/{application.id}/artifacts/report").status_code == 404
    assert client.get(f"/applications/{application.id}/artifacts/approval").status_code == 404
    assert client.get("/applications/app_missing/artifacts/jd").status_code == 404

    resume = select_resume(repo, application.id)
    report = repo.save_report(
        application_id=application.id,
        resume_version_id=resume.id,
        jd_snapshot_id=application.jd_snapshot_id,
        fit={"score": 80},
        risks={"blockers": [], "warnings": []},
    )
    repo.approve_application(application.id, report.id, "Reviewed")
    foreign_application = create_application(repo, company="Foreign Corp")
    foreign_resume = select_resume(repo, foreign_application.id)
    with repo.connection() as connection:
        connection.execute(
            "UPDATE applications SET selected_resume_version_id = ? WHERE id = ?",
            (foreign_resume.id, application.id),
        )
    assert client.get(f"/applications/{application.id}/artifacts/report").status_code == 409
    assert client.get(f"/applications/{application.id}/artifacts/approval").status_code == 409

    outside = tmp_path / "outside.txt"
    outside.write_text("foreign", encoding="utf-8")
    with repo.connection() as connection:
        connection.execute(
            "UPDATE jd_snapshots SET raw_text_path = ? WHERE id = ?",
            (str(outside), application.jd_snapshot_id),
        )
        connection.execute(
            "UPDATE resume_versions SET pdf_path = ?, pdf_sha256 = ? WHERE id = ?",
            (
                str(outside),
                hashlib.sha256(outside.read_bytes()).hexdigest(),
                foreign_resume.id,
            ),
        )
    assert client.get(f"/applications/{application.id}/artifacts/jd").status_code == 400
    assert client.get(f"/applications/{application.id}/artifacts/resume-pdf").status_code == 400


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/ai/providers", None),
        ("get", "/profile-facts", None),
        ("post", "/profile-facts", {}),
        ("get", "/applications/app_missing/packet", None),
        ("post", "/applications/app_missing/ai-tasks", {}),
        ("get", "/ai-tasks/task_missing", None),
        ("get", "/ai-tasks/task_missing/findings", None),
        ("post", "/ai-findings/finding_missing/decision", {}),
        ("get", "/applications/app_missing/requirements", None),
        ("put", "/applications/app_missing/requirements", {}),
        ("post", "/applications/app_missing/evidence", {}),
        ("get", "/applications/app_missing/evidence", None),
        ("get", "/applications/app_missing/versions", None),
        ("get", "/applications/app_missing/artifacts/jd", None),
        ("get", "/applications/app_missing/artifacts/resume-pdf", None),
        ("get", "/applications/app_missing/artifacts/report", None),
        ("get", "/applications/app_missing/artifacts/approval", None),
    ],
)
def test_basic_auth_protects_every_new_route(
    tmp_path: Path, method: str, path: str, body: dict[str, Any] | None
):
    client = TestClient(
        create_app(
            make_repo(tmp_path),
            provider_registry={},
            auth=BasicAuthConfig("careerops", "secret"),
        )
    )

    response = client.request(method, path, json=body)

    assert response.status_code == 401
    assert client.get("/health").status_code == 200


def test_task_routes_validate_ownership_ids_and_bodies(tmp_path: Path):
    repo = make_repo(tmp_path)
    first = create_application(repo)
    second = create_application(repo, company="Other Corp")
    status = availability()
    config = create_provider_config(repo, status)
    provider = ControlledProvider(status)
    from careerops.ai.orchestrator import AiOrchestrator

    orchestrator = AiOrchestrator(repo, {config.id: provider})
    task = orchestrator.enqueue(first.id, "analyze_jd", config.id)
    orchestrator.run(task.id)
    foreign_artifact_id = repo.list_ai_artifacts(task.id)[0].id
    client = TestClient(create_app(repo, provider_registry={config.id: provider}))

    assert client.get("/ai-tasks/not-an-id").status_code == 422
    assert client.get("/ai-tasks/ait_missing/findings").status_code == 404
    assert client.post(
        f"/applications/{second.id}/ai-tasks",
        json={
            "task_type": "analyze_jd",
            "provider_config_id": config.id,
            "input_artifact_ids": [foreign_artifact_id],
        },
    ).status_code == 400
    assert client.post(
        f"/applications/{first.id}/ai-tasks",
        json={
            "task_type": "invented",
            "provider_config_id": config.id,
            "unknown": True,
        },
    ).status_code == 422
    assert client.get(f"/ai-tasks/{task.id}").status_code == 200
