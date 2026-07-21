from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from threading import Event, Lock
import time
from types import SimpleNamespace
from typing import Any

import pytest

from careerops.ai.contracts import (
    InvocationMetadata,
    ProviderAvailability,
    output_schema_for_task,
    serialize_provider_output,
    validate_task_output,
)
from careerops.ai.providers.base import (
    ProviderUnavailableError,
    build_provider_result,
)
from careerops.ai.context import load_prompt_template
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
        "suggestions": [],
    },
    "draft_cover_letter": {
        "task_type": "draft_cover_letter",
        "plain_text": "I am excited to apply.",
        "evidence_refs": [],
    },
    "review_claims": {
        "task_type": "review_claims",
        "decisions": [],
    },
}


def make_repo(tmp_path: Path) -> CareerOpsRepository:
    repo = CareerOpsRepository(tmp_path / "careerops.db", tmp_path / "vault")
    repo.initialize()
    return repo


def create_application(repo: CareerOpsRepository):
    return repo.create_application(
        company="Example Corp",
        role_title="Software Engineer",
        location="Remote",
        source_url="https://example.com/jobs/1",
        jd_text=(
            "Company: Example Corp\n"
            "Title: Software Engineer\n"
            "Location: Remote\n"
            "Required skills:\n- Python\n"
        ),
    )


def create_provider_config(
    repo: CareerOpsRepository,
    *,
    provider_kind: str = "mock",
    model: str = "fixture-v1",
    privacy_mode: str = "local_private",
    endpoint: str = "",
    identity_name: str = "",
    enabled: bool = True,
    binding_status: ProviderAvailability | None = None,
):
    status = binding_status or availability(
        provider_kind=provider_kind,
        model=model,
        privacy_mode=PrivacyMode(privacy_mode),
        data_leaves_machine=privacy_mode == "cloud_external",
        endpoint=endpoint,
        identity_name=identity_name,
    )
    return repo.upsert_ai_provider_config(
        provider_kind=provider_kind,
        display_name=f"{provider_kind} {model}",
        privacy_mode=privacy_mode,
        endpoint=endpoint,
        identity_name=identity_name,
        model=model,
        enabled=enabled,
        availability_binding=provider_binding_values(status),
    )


@dataclass
class AvailabilityOnlyProvider:
    status: ProviderAvailability
    calls: int = 0

    def availability(self) -> ProviderAvailability:
        return self.status

    def run(self, _request: Any):
        self.calls += 1
        raise AssertionError("enqueue must not invoke the provider")


class ScriptedProvider:
    def __init__(
        self,
        status: ProviderAvailability,
        *,
        outputs: dict[str, dict[str, Any]] | None = None,
        error: Exception | None = None,
        on_run: Any | None = None,
        result_overrides: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.outputs = outputs or VALID_OUTPUTS
        self.error = error
        self.on_run = on_run
        self.result_overrides = result_overrides or {}
        self.calls = 0
        self.requests: list[Any] = []
        self._lock = Lock()

    def availability(self) -> ProviderAvailability:
        return self.status

    def run(self, request: Any):
        with self._lock:
            self.calls += 1
            self.requests.append(request)
        if self.on_run is not None:
            self.on_run(request, self.calls)
        if self.error is not None:
            raise self.error
        payload = self.outputs[request.task_type]
        output = validate_task_output(request.task_type, payload)
        raw_output = serialize_provider_output(output)
        values = {
            "provider_kind": self.status.provider_kind,
            "model": self.status.model,
            "privacy_mode": self.status.privacy_mode,
            "data_leaves_machine": self.status.data_leaves_machine,
            "is_ai": self.status.is_ai,
            "structured_output": output,
            "raw_output": raw_output,
            "invocation": InvocationMetadata(
                mode="test_fixture",
                endpoint=self.status.endpoint,
                identity_name=self.status.identity_name,
                executable=self.status.executable,
                executable_sha256=self.status.executable_sha256,
                executable_version=self.status.executable_version,
                codex_home=self.status.codex_home,
                model_digest=self.status.model_digest,
                model_metadata_sha256=self.status.model_metadata_sha256,
                show_metadata_sha256=self.status.show_metadata_sha256,
                attestation_version=self.status.attestation_version,
                runtime_instance_id=self.status.runtime_instance_id,
                runtime_handle=self.status.runtime_handle,
                issuer_instance_id=self.status.issuer_instance_id,
                egress_policy=self.status.egress_policy,
                cloud_disabled=self.status.cloud_disabled,
                egress_enforced=self.status.egress_enforced,
                isolation_evidence_sha256=(
                    self.status.isolation_evidence_sha256
                ),
                isolation_evidence_signature=(
                    self.status.isolation_evidence_signature
                ),
                reasoning_effort=self.status.reasoning_effort,
            ),
            **self.result_overrides,
        }
        return build_provider_result(**values)


class InvalidSchemaProvider(ScriptedProvider):
    def run(self, request: Any):
        with self._lock:
            self.calls += 1
            self.requests.append(request)
        raw_output = '{"task_type":"analyze_fit","score":"invalid"}'
        digest = hashlib.sha256(raw_output.encode("utf-8")).hexdigest()
        return SimpleNamespace(
            provider_kind=self.status.provider_kind,
            model=self.status.model,
            privacy_mode=self.status.privacy_mode,
            data_leaves_machine=self.status.data_leaves_machine,
            is_ai=self.status.is_ai,
            structured_output={"task_type": "analyze_fit", "score": "invalid"},
            raw_output=raw_output,
            raw_output_sha256=digest,
            invocation=InvocationMetadata(
                mode="test_fixture",
                endpoint=self.status.endpoint,
                raw_output_sha256=digest,
            ),
        )


def availability(
    *,
    provider_kind: str = "mock",
    model: str = "fixture-v1",
    privacy_mode: PrivacyMode = PrivacyMode.LOCAL_PRIVATE,
    data_leaves_machine: bool = False,
    endpoint: str = "",
    identity_name: str = "",
    executable: str = "",
    executable_sha256: str = "",
    executable_version: str = "",
    codex_home: str = "",
    reasoning_effort: str = "",
    available: bool = True,
) -> ProviderAvailability:
    return ProviderAvailability(
        available=available,
        provider_kind=provider_kind,
        model=model,
        privacy_mode=privacy_mode,
        data_leaves_machine=data_leaves_machine,
        is_ai=True,
        detail="available" if available else "unavailable",
        endpoint=endpoint,
        identity_name=identity_name,
        executable=executable,
        executable_sha256=executable_sha256,
        executable_version=executable_version,
        codex_home=codex_home,
        reasoning_effort=reasoning_effort,
    )


def provider_binding_values(status: ProviderAvailability) -> dict[str, Any]:
    values = status.model_dump(mode="json")
    values.pop("available")
    values.pop("detail")
    return values


def select_resume(repo: CareerOpsRepository, application_id: str, text: str = "Python"):
    source_dir = repo.vault_path / "resumes" / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    tex_path = source_dir / f"resume-{application_id}.tex"
    pdf_path = source_dir / f"resume-{application_id}.pdf"
    tex_path.write_text(text, encoding="utf-8")
    pdf_bytes = b"%PDF-1.4\nfixture resume"
    pdf_path.write_bytes(pdf_bytes)
    resume = repo.create_resume_version(
        name="Current resume",
        role_family="software",
        tex_path=tex_path,
        tex_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        pdf_path=pdf_path,
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
    )
    repo.select_resume(application_id, resume.id)
    return resume


def test_repository_aggregate_finalizer_blocks_active_claim_and_audits_it(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    config = create_provider_config(repo)
    prompt = load_prompt_template("analyze_jd")
    task = repo.create_ai_task(
        application_id=application.id,
        task_type="analyze_jd",
        provider_config_id=config.id,
        prompt_template_id=prompt.id,
        prompt_sha256=prompt.sha256,
        input_artifact_ids=[],
        privacy_mode="local_private",
        expected_provider_binding=config.availability_binding,
        expected_provider_binding_sha256=config.availability_binding_sha256,
    )
    claim_token = repo.start_ai_task(task.id)
    assert repo.begin_ai_task_invocation(task.id, claim_token) is True
    attempt = repo.vault_path / "ai-tasks" / task.id / "attempt-test"
    attempt.mkdir(parents=True)
    for artifact_type in ("context", "raw_output", "validated_output"):
        content = artifact_type.encode("ascii")
        path = attempt / f"{artifact_type}.json"
        path.write_bytes(content)
        repo.save_ai_artifact(
            task.id,
            claim_token,
            artifact_type,
            path,
            hashlib.sha256(content).hexdigest(),
            "1",
        )

    blocked = repo.finalize_ai_task_attempt(
        task_id=task.id,
        claim_token=claim_token,
        terminal_status="blocked",
        error_summary="verification_failed",
        expected_jd_snapshot_id=application.jd_snapshot_id,
        expected_resume_version_id=None,
        findings=[],
    )

    assert blocked.status == "blocked"
    assert blocked.completed_at is not None
    assert blocked.error_summary == "verification_failed"
    event = repo.list_audit_events()[-1]
    assert event.event_type == "ai_task.blocked"
    assert event.after_json["status"] == "blocked"
    with pytest.raises(ValueError, match="aggregate finalizer"):
        repo.complete_ai_task(task.id, claim_token)


@pytest.mark.parametrize(
    "shortcut", ["complete", "block", "update_jd", "save_report"]
)
def test_public_ai_shortcuts_cannot_bypass_aggregate_finalization(
    tmp_path: Path, shortcut: str
):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    resume = select_resume(repo, application.id, "Python developer")
    config = create_provider_config(repo)
    provider = AvailabilityOnlyProvider(availability())
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    task_type = "analyze_jd" if shortcut in {"block", "update_jd"} else "analyze_fit"
    task = orchestrator.enqueue(application.id, task_type, config.id)
    claim_token = repo.start_ai_task(task.id)
    assert repo.begin_ai_task_invocation(task.id, claim_token) is True
    before_application = repo.get_application(application.id)
    before_jd = repo.get_jd_snapshot(application.jd_snapshot_id)

    with pytest.raises(ValueError, match="aggregate finalizer"):
        if shortcut == "complete":
            repo.complete_ai_task(task.id, claim_token)
        elif shortcut == "block":
            repo.block_ai_task(task.id, claim_token, "verification_failed")
        elif shortcut == "update_jd":
            repo.update_jd_requirements_from_ai(
                snapshot_id=application.jd_snapshot_id,
                task_id=task.id,
                claim_token=claim_token,
                raw_ai={
                    "required_skills": ["FORGED"],
                    "provenance": {"provider_kind": "forged"},
                },
                uncertainty={"provenance": {"ai_task_id": "forged"}},
            )
        else:
            repo.save_report(
                application_id=application.id,
                resume_version_id=resume.id,
                jd_snapshot_id=application.jd_snapshot_id,
                fit={
                    "score": 100,
                    "provenance": {"provider_kind": "forged"},
                },
                risks={"blockers": [], "warnings": []},
                ai_task_id=task.id,
                claim_token=claim_token,
            )

    assert provider.calls == 0
    assert repo.get_ai_task(task.id).status == "running"
    assert repo.list_ai_artifacts(task.id) == []
    assert repo.list_application_versions(application.id) == []
    assert repo.get_application(application.id) == before_application
    assert repo.get_jd_snapshot(application.jd_snapshot_id) == before_jd
    with repo.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM reports WHERE application_id = ?",
            (application.id,),
        ).fetchone()[0] == 0


def test_enqueue_persists_pending_task_without_running_provider(tmp_path: Path):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    config = create_provider_config(repo)
    provider = AvailabilityOnlyProvider(
        ProviderAvailability(
            available=True,
            provider_kind="mock",
            model="fixture-v1",
            privacy_mode=PrivacyMode.LOCAL_PRIVATE,
            data_leaves_machine=False,
            is_ai=True,
            detail="available",
        )
    )
    orchestrator = AiOrchestrator(repo, {config.id: provider})

    task = orchestrator.enqueue(application.id, "analyze_jd", config.id)

    assert repo.get_ai_task(task.id).status == "pending"
    assert task.provider_config_id == config.id
    assert task.prompt_template_id == "analyze_jd_v1"
    assert task.prompt_sha256 == load_prompt_template("analyze_jd").sha256
    assert task.privacy_mode == PrivacyMode.LOCAL_PRIVATE
    assert provider.calls == 0


@pytest.mark.parametrize("submitted", [False, True], ids=["approved", "submitted"])
def test_enqueue_rejects_read_only_application_before_provider_access(
    tmp_path: Path, submitted: bool
):
    from careerops.ai.orchestrator import AiOrchestrator

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
    repo.approve_application(application.id, report.id, "Reviewed")
    if submitted:
        repo.mark_submitted_manual(application.id, "Submitted")
    config = create_provider_config(repo)

    class ProviderMustNotBeConsulted:
        def availability(self):
            raise AssertionError("read-only enqueue must not inspect the provider")

        def run(self, _request):
            raise AssertionError("read-only enqueue must not invoke the provider")

    orchestrator = AiOrchestrator(repo, {config.id: ProviderMustNotBeConsulted()})

    with pytest.raises(ValueError, match="read-only"):
        orchestrator.enqueue(application.id, "analyze_jd", config.id)

    assert repo.list_ai_tasks(application.id) == []


def test_heartbeat_setup_failure_fails_exact_claim_without_invoking_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    config = create_provider_config(repo)
    provider = ScriptedProvider(availability())
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    task = orchestrator.enqueue(application.id, "analyze_jd", config.id)

    def fail_heartbeat_setup(_task_id: str, _claim_token: str):
        raise RuntimeError("heartbeat secret=do-not-store")

    monkeypatch.setattr(orchestrator, "_start_claim_heartbeat", fail_heartbeat_setup)

    result = orchestrator.run(task.id)
    rerun = orchestrator.run(task.id)

    assert result.status == "failed"
    assert result.error_summary == "provider_error"
    assert repo.get_ai_task(task.id) == result
    assert rerun == result
    assert provider.calls == 0
    assert "do-not-store" not in str(result)


def test_heartbeat_renewal_exception_fails_closed(tmp_path: Path, monkeypatch):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = CareerOpsRepository(
        tmp_path / "careerops.db",
        tmp_path / "vault",
        claim_lease_seconds=15,
    )
    repo.initialize()
    application = create_application(repo)
    config = create_provider_config(repo)
    renewal_failed = Event()

    def raise_renewal_error(_task_id: str, _claim_token: str) -> bool:
        renewal_failed.set()
        raise RuntimeError("renewal transport detail secret=do-not-store")

    def wait_for_renewal(_request: Any, _calls: int) -> None:
        assert renewal_failed.wait(2)

    monkeypatch.setattr(
        type(repo),
        "claim_heartbeat_interval",
        property(lambda _repository: 0.05),
    )
    monkeypatch.setattr(repo, "renew_ai_task_claim", raise_renewal_error)
    provider = ScriptedProvider(availability(), on_run=wait_for_renewal)
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    task = orchestrator.enqueue(application.id, "analyze_jd", config.id)

    result = orchestrator.run(task.id)

    assert result.status == "failed"
    assert result.error_summary == "provider_error"
    assert provider.calls == 0
    assert repo.list_application_versions(application.id) == []


def test_run_waits_for_blocked_heartbeat_thread_before_returning(
    tmp_path: Path, monkeypatch
):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = CareerOpsRepository(
        tmp_path / "careerops.db",
        tmp_path / "vault",
        claim_lease_seconds=15,
    )
    repo.initialize()
    application = create_application(repo)
    config = create_provider_config(repo)
    renewal_entered = Event()
    release_renewal = Event()
    provider_returned = Event()

    def blocked_renewal(_task_id: str, _claim_token: str) -> bool:
        renewal_entered.set()
        assert release_renewal.wait(5)
        return True

    def wait_for_heartbeat(_request: Any, _calls: int) -> None:
        assert renewal_entered.wait(2)
        provider_returned.set()

    monkeypatch.setattr(
        type(repo),
        "claim_heartbeat_interval",
        property(lambda _repository: 0.05),
    )
    monkeypatch.setattr(repo, "renew_ai_task_claim", blocked_renewal)
    provider = ScriptedProvider(availability(), on_run=wait_for_heartbeat)
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    task = orchestrator.enqueue(application.id, "analyze_jd", config.id)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(orchestrator.run, task.id)
        assert renewal_entered.wait(2)
        assert provider_returned.wait(2)
        try:
            assert future.done() is False
        finally:
            release_renewal.set()
        result = future.result(timeout=3)

    assert result.status == "completed"
    assert len(repo.list_application_versions(application.id)) == 1
    assert not any(
        thread.name == f"ai-task-heartbeat-{task.id}" and thread.is_alive()
        for thread in __import__("threading").enumerate()
    )


def test_expired_contended_heartbeat_requeues_without_reinvoking_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = CareerOpsRepository(
        tmp_path / "careerops.db",
        tmp_path / "vault",
        claim_lease_seconds=15,
    )
    repo.initialize()
    application = create_application(repo)
    config = create_provider_config(repo)
    provider_started = Event()
    release_provider = Event()
    renewal_entered = Event()
    renewal_may_proceed = Event()
    renewal_db_started = Event()
    renewal_finished = Event()
    renewal_results: list[bool] = []
    original_renew = repo.renew_ai_task_claim

    monkeypatch.setattr(
        type(repo),
        "claim_heartbeat_interval",
        property(lambda _repository: 0.05),
    )

    def renew_under_contention(task_id: str, claim_token: str) -> bool:
        if not provider_started.is_set():
            return original_renew(task_id, claim_token)
        renewal_entered.set()
        assert renewal_may_proceed.wait(10)
        renewal_db_started.set()
        renewed = original_renew(task_id, claim_token)
        renewal_results.append(renewed)
        renewal_finished.set()
        return renewed

    def wait_in_provider(_request: Any, _calls: int) -> None:
        provider_started.set()
        assert release_provider.wait(10)

    monkeypatch.setattr(repo, "renew_ai_task_claim", renew_under_contention)
    provider = ScriptedProvider(availability(), on_run=wait_in_provider)
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    task = orchestrator.enqueue(application.id, "analyze_jd", config.id)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(orchestrator.run, task.id)
        assert provider_started.wait(5)
        try:
            assert renewal_entered.wait(5)
            with repo.connection() as blocker:
                blocker.execute("BEGIN IMMEDIATE")
                blocker.execute(
                    "UPDATE ai_tasks SET claim_expires_at = ? WHERE id = ?",
                    ("1970-01-01T00:00:00+00:00", task.id),
                )
                renewal_may_proceed.set()
                assert renewal_db_started.wait(5)
                blocker.commit()
                assert renewal_finished.wait(5)
        finally:
            renewal_may_proceed.set()
            release_provider.set()
        interrupted = future.result(timeout=5)

    monkeypatch.setattr(repo, "renew_ai_task_claim", original_renew)
    recovered = orchestrator.run(task.id)

    assert renewal_results == [False]
    assert interrupted.status == "pending"
    assert recovered.status == "failed"
    assert recovered.error_summary == "provider_error"
    assert provider.calls == 1
    assert repo.list_application_versions(application.id) == []


def test_heartbeat_renews_through_post_provider_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = CareerOpsRepository(
        tmp_path / "careerops.db",
        tmp_path / "vault",
        claim_lease_seconds=15,
    )
    repo.initialize()
    application = create_application(repo)
    config = create_provider_config(repo)
    provider_returned = Event()
    post_provider_renewed = Event()
    original_renew = repo.renew_ai_task_claim

    monkeypatch.setattr(
        type(repo),
        "claim_heartbeat_interval",
        property(lambda _repository: 0.05),
    )

    def record_post_provider_renewal(task_id: str, claim_token: str) -> bool:
        renewed = original_renew(task_id, claim_token)
        if renewed and provider_returned.is_set():
            post_provider_renewed.set()
        return renewed

    def finish_provider(_request: Any, _calls: int) -> None:
        provider_returned.set()

    monkeypatch.setattr(
        repo, "renew_ai_task_claim", record_post_provider_renewal
    )
    provider = ScriptedProvider(availability(), on_run=finish_provider)
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    original_validate = orchestrator._validate_result_metadata

    def wait_for_post_provider_renewal(config, availability, result) -> None:
        assert provider_returned.is_set()
        assert post_provider_renewed.wait(5)
        original_validate(config, availability, result)

    monkeypatch.setattr(
        orchestrator, "_validate_result_metadata", wait_for_post_provider_renewal
    )
    task = orchestrator.enqueue(application.id, "analyze_jd", config.id)

    completed = orchestrator.run(task.id)

    assert completed.status == "completed"
    assert provider.calls == 1
    assert {artifact.artifact_type for artifact in repo.list_ai_artifacts(task.id)} == {
        "context",
        "raw_output",
        "validated_output",
    }
    assert len(repo.list_application_versions(application.id)) == 1


def test_slow_artifact_materialization_does_not_starve_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = CareerOpsRepository(
        tmp_path / "careerops.db",
        tmp_path / "vault",
        claim_lease_seconds=15,
    )
    repo.initialize()
    application = create_application(repo)
    config = create_provider_config(repo)
    slow_digest_active = Event()
    renewed_during_slow_digest = Event()
    digest_lock = Lock()
    digest_calls = 0
    original_digest = repo._stable_artifact_digest
    original_renew = repo.renew_ai_task_claim

    monkeypatch.setattr(
        type(repo),
        "claim_heartbeat_interval",
        property(lambda _repository: 0.05),
    )

    def delay_second_digest(path: Path):
        nonlocal digest_calls
        with digest_lock:
            digest_calls += 1
            call_number = digest_calls
        if call_number == 2:
            slow_digest_active.set()
            try:
                time.sleep(6.2)
            finally:
                slow_digest_active.clear()
        return original_digest(path)

    def record_renewal(task_id: str, claim_token: str) -> bool:
        renewed = original_renew(task_id, claim_token)
        if renewed and slow_digest_active.is_set():
            renewed_during_slow_digest.set()
        return renewed

    monkeypatch.setattr(repo, "_stable_artifact_digest", delay_second_digest)
    monkeypatch.setattr(repo, "renew_ai_task_claim", record_renewal)
    provider = ScriptedProvider(availability())
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    task = orchestrator.enqueue(application.id, "analyze_jd", config.id)

    completed = orchestrator.run(task.id)

    assert completed.status == "completed"
    assert provider.calls == 1
    assert renewed_during_slow_digest.is_set()


def test_post_provider_heartbeat_loss_requeues_and_preserves_invocation_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = CareerOpsRepository(
        tmp_path / "careerops.db",
        tmp_path / "vault",
        claim_lease_seconds=15,
    )
    repo.initialize()
    application = create_application(repo)
    config = create_provider_config(repo)
    post_processing_entered = Event()
    release_processing = Event()
    renewal_entered = Event()
    renewal_may_proceed = Event()
    renewal_db_started = Event()
    renewal_finished = Event()
    renewal_results: list[bool] = []
    original_renew = repo.renew_ai_task_claim

    monkeypatch.setattr(
        type(repo),
        "claim_heartbeat_interval",
        property(lambda _repository: 0.05),
    )

    def renew_under_contention(task_id: str, claim_token: str) -> bool:
        if not post_processing_entered.is_set():
            return original_renew(task_id, claim_token)
        renewal_entered.set()
        assert renewal_may_proceed.wait(10)
        renewal_db_started.set()
        renewed = original_renew(task_id, claim_token)
        renewal_results.append(renewed)
        renewal_finished.set()
        return renewed

    monkeypatch.setattr(repo, "renew_ai_task_claim", renew_under_contention)
    provider = ScriptedProvider(availability())
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    original_validate = orchestrator._validate_result_metadata

    def pause_post_provider_processing(config, availability, result) -> None:
        post_processing_entered.set()
        assert release_processing.wait(10)
        original_validate(config, availability, result)

    monkeypatch.setattr(
        orchestrator,
        "_validate_result_metadata",
        pause_post_provider_processing,
    )
    task = orchestrator.enqueue(application.id, "analyze_jd", config.id)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(orchestrator.run, task.id)
        assert post_processing_entered.wait(5)
        try:
            assert renewal_entered.wait(5)
            with repo.connection() as blocker:
                blocker.execute("BEGIN IMMEDIATE")
                blocker.execute(
                    "UPDATE ai_tasks SET claim_expires_at = ? WHERE id = ?",
                    ("1970-01-01T00:00:00+00:00", task.id),
                )
                renewal_may_proceed.set()
                assert renewal_db_started.wait(5)
                blocker.commit()
                assert renewal_finished.wait(5)
        finally:
            renewal_may_proceed.set()
            release_processing.set()
        interrupted = future.result(timeout=5)

    monkeypatch.setattr(repo, "renew_ai_task_claim", original_renew)
    recovered = orchestrator.run(task.id)

    assert renewal_results == [False]
    assert interrupted.status == "pending"
    assert recovered.status == "failed"
    assert recovered.error_summary == "provider_error"
    assert provider.calls == 1
    assert [
        artifact.artifact_type for artifact in repo.list_ai_artifacts(task.id)
    ] == ["context"]
    assert repo.list_application_versions(application.id) == []


def test_enqueue_requires_task_prerequisites_and_compatible_egress(tmp_path: Path):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    config = create_provider_config(repo)
    provider = ScriptedProvider(availability(data_leaves_machine=True))
    orchestrator = AiOrchestrator(repo, {config.id: provider})

    with pytest.raises(ValueError, match="metadata"):
        orchestrator.enqueue(application.id, "analyze_fit", config.id)
    assert repo.list_ai_tasks(application.id) == []

    provider.status = availability()
    with pytest.raises(ValueError, match="resume"):
        orchestrator.enqueue(application.id, "analyze_fit", config.id)
    assert repo.list_ai_tasks(application.id) == []


def test_enqueue_rejects_provider_reregistration_before_atomic_insert(
    tmp_path: Path, monkeypatch
):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    original_status = availability(reasoning_effort="medium")
    config = create_provider_config(repo, binding_status=original_status)
    provider = AvailabilityOnlyProvider(original_status)
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    original_create = repo.create_ai_task

    def reregister_then_create(**values):
        changed = availability(reasoning_effort="high")
        repo.upsert_ai_provider_config(
            provider_kind="mock",
            display_name="changed runtime",
            privacy_mode="local_private",
            endpoint="",
            identity_name="",
            model="fixture-v1",
            enabled=True,
            availability_binding=provider_binding_values(changed),
        )
        return original_create(**values)

    monkeypatch.setattr(repo, "create_ai_task", reregister_then_create)

    with pytest.raises(ValueError, match="changed during enqueue"):
        orchestrator.enqueue(application.id, "analyze_jd", config.id)

    assert provider.calls == 0
    assert repo.list_ai_tasks(application.id) == []


def test_exact_config_selects_second_same_kind_provider_once(tmp_path: Path):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    preferred_status = availability(
        provider_kind="codex",
        model="gpt-5.6-sol",
        privacy_mode=PrivacyMode.CLOUD_EXTERNAL,
        data_leaves_machine=True,
        endpoint="codex-identity://codex2",
        identity_name="codex2",
        executable="C:/Codex/codex.exe",
        executable_sha256="a" * 64,
        executable_version="codex-cli 1.0.0",
        codex_home="C:/Users/test/codex-account2/.codex",
        reasoning_effort="high",
    )
    alternate_status = preferred_status.model_copy(
        update={
            "endpoint": "codex-identity://codex",
            "identity_name": "codex",
            "codex_home": "C:/Users/test/.codex",
        }
    )
    preferred_config = create_provider_config(
        repo,
        provider_kind="codex",
        model="gpt-5.6-sol",
        privacy_mode="cloud_external",
        endpoint="codex-identity://codex2",
        identity_name="codex2",
        binding_status=preferred_status,
    )
    alternate_config = create_provider_config(
        repo,
        provider_kind="codex",
        model="gpt-5.6-sol",
        privacy_mode="cloud_external",
        endpoint="codex-identity://codex",
        identity_name="codex",
        binding_status=alternate_status,
    )
    preferred = ScriptedProvider(preferred_status)
    alternate = ScriptedProvider(alternate_status)
    orchestrator = AiOrchestrator(
        repo,
        {preferred_config.id: preferred, alternate_config.id: alternate},
    )
    task = orchestrator.enqueue(application.id, "analyze_jd", alternate_config.id)

    result = orchestrator.run(task.id)

    assert result.status == "completed"
    assert preferred_config.id != alternate_config.id
    assert preferred.calls == 0
    assert alternate.calls == 1
    version = repo.list_application_versions(application.id)[0]
    assert version.snapshot["provenance"]["provider_identity_name"] == "codex"


def test_codex_identity_mismatch_fails_closed_without_provider_call(tmp_path: Path):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    config = create_provider_config(
        repo,
        provider_kind="codex",
        model="gpt-5.6-sol",
        privacy_mode="cloud_external",
        endpoint="codex-identity://codex",
        identity_name="codex",
    )
    provider = AvailabilityOnlyProvider(
        availability(
            provider_kind="codex",
            model="gpt-5.6-sol",
            privacy_mode=PrivacyMode.CLOUD_EXTERNAL,
            data_leaves_machine=True,
            endpoint="codex-identity://codex2",
            identity_name="codex2",
        )
    )
    orchestrator = AiOrchestrator(repo, {config.id: provider})

    with pytest.raises(ValueError, match="metadata"):
        orchestrator.enqueue(application.id, "analyze_jd", config.id)

    assert provider.calls == 0
    assert repo.list_ai_tasks(application.id) == []


def test_pending_task_fails_closed_after_provider_binding_reregistration(
    tmp_path: Path,
):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    initial = availability(
        provider_kind="codex",
        model="gpt-5.6-sol",
        privacy_mode=PrivacyMode.CLOUD_EXTERNAL,
        data_leaves_machine=True,
        endpoint="codex-identity://codex2",
        identity_name="codex2",
        executable="C:/Codex/codex.exe",
        executable_sha256="a" * 64,
        executable_version="codex-cli 1.0.0",
        codex_home="C:/Users/test/codex-account2/.codex",
        reasoning_effort="high",
    )
    config = create_provider_config(
        repo,
        provider_kind="codex",
        model="gpt-5.6-sol",
        privacy_mode="cloud_external",
        endpoint="codex-identity://codex2",
        identity_name="codex2",
        binding_status=initial,
    )
    provider = ScriptedProvider(initial)
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    task = orchestrator.enqueue(application.id, "analyze_jd", config.id)
    changed = initial.model_copy(
        update={"codex_home": "C:/Users/test/.codex"}
    )

    updated_config = repo.upsert_ai_provider_config(
        provider_kind="codex",
        display_name="Re-registered codex2",
        privacy_mode="cloud_external",
        endpoint="codex-identity://codex2",
        identity_name="codex2",
        model="gpt-5.6-sol",
        enabled=True,
        availability_binding=provider_binding_values(changed),
    )
    provider.status = changed
    result = orchestrator.run(task.id)

    assert updated_config.id == config.id
    assert task.provider_binding_sha256 != updated_config.availability_binding_sha256
    assert result.status == "failed"
    assert result.error_summary == "provider_unavailable"
    assert provider.calls == 0
    assert repo.list_application_versions(application.id) == []


def test_same_identity_model_with_different_binary_fails_before_enqueue(
    tmp_path: Path,
):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    registered = availability(
        provider_kind="codex",
        model="gpt-5.6-sol",
        privacy_mode=PrivacyMode.CLOUD_EXTERNAL,
        data_leaves_machine=True,
        endpoint="codex-identity://codex",
        identity_name="codex",
        executable="C:/Codex/codex.exe",
        executable_sha256="a" * 64,
        executable_version="codex-cli 1.0.0",
        codex_home="C:/Users/test/.codex",
        reasoning_effort="high",
    )
    config = create_provider_config(
        repo,
        provider_kind="codex",
        model="gpt-5.6-sol",
        privacy_mode="cloud_external",
        endpoint="codex-identity://codex",
        identity_name="codex",
        binding_status=registered,
    )
    changed = registered.model_copy(
        update={
            "executable": "D:/Codex/codex.exe",
            "executable_sha256": "b" * 64,
        }
    )
    provider = AvailabilityOnlyProvider(changed)
    orchestrator = AiOrchestrator(repo, {config.id: provider})

    with pytest.raises(ValueError, match="metadata"):
        orchestrator.enqueue(application.id, "analyze_jd", config.id)

    assert provider.calls == 0
    assert repo.list_ai_tasks(application.id) == []


def test_provider_failure_never_falls_back_or_mutates_application(tmp_path: Path):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    resume = select_resume(repo, application.id)
    original_report = repo.save_report(
        application_id=application.id,
        resume_version_id=resume.id,
        jd_snapshot_id=application.jd_snapshot_id,
        fit={"score": 100},
        risks={"blockers": [], "warnings": []},
    )
    before_application = repo.get_application(application.id)
    before_jd = repo.get_jd_snapshot(application.jd_snapshot_id)
    before_report = repo.get_report(original_report.id)

    selected_config = create_provider_config(repo, model="selected")
    fallback_config = create_provider_config(repo, model="fallback")
    selected = ScriptedProvider(
        availability(model="selected"),
        error=ProviderUnavailableError("api_key=PRIVATE_PROVIDER_SECRET"),
    )
    fallback = ScriptedProvider(availability(model="fallback"))
    orchestrator = AiOrchestrator(
        repo,
        {selected_config.id: selected, fallback_config.id: fallback},
    )
    task = orchestrator.enqueue(application.id, "analyze_fit", selected_config.id)

    result = orchestrator.run(task.id)

    assert result.status == "failed"
    assert result.error_summary == "provider_unavailable"
    assert selected.calls == 1
    assert fallback.calls == 0
    assert repo.get_application(application.id) == before_application
    assert repo.get_jd_snapshot(application.jd_snapshot_id) == before_jd
    assert repo.get_report(original_report.id) == before_report


def test_context_state_drift_cannot_attach_output_to_a_new_resume(tmp_path: Path):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    original_resume = select_resume(repo, application.id, "Python developer")
    replacement_path = repo.vault_path / "resumes" / "sources" / "replacement.tex"
    replacement_text = "Rust developer"
    replacement_path.write_text(replacement_text, encoding="utf-8")
    replacement_resume = repo.create_resume_version(
        name="Replacement resume",
        role_family="software",
        tex_path=replacement_path,
        tex_sha256=hashlib.sha256(replacement_text.encode("utf-8")).hexdigest(),
    )

    def replace_selection(_request: Any, _calls: int) -> None:
        assert repo.get_application(application.id).selected_resume_version_id == (
            original_resume.id
        )
        repo.select_resume(application.id, replacement_resume.id)

    config = create_provider_config(repo)
    provider = ScriptedProvider(availability(), on_run=replace_selection)
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    task = orchestrator.enqueue(application.id, "analyze_fit", config.id)

    result = orchestrator.run(task.id)

    assert result.status == "failed"
    updated = repo.get_application(application.id)
    assert updated.selected_resume_version_id == replacement_resume.id
    assert updated.latest_report_id is None
    assert repo.list_application_versions(application.id) == []


def test_concurrent_run_claims_once_and_invokes_provider_once(tmp_path: Path):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    config = create_provider_config(repo)
    entered = Event()
    release = Event()

    def pause_provider(_request: Any, _calls: int) -> None:
        entered.set()
        assert release.wait(timeout=5)

    provider = ScriptedProvider(availability(), on_run=pause_provider)
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    task = orchestrator.enqueue(application.id, "analyze_jd", config.id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(orchestrator.run, task.id)
        assert entered.wait(timeout=5)
        second = executor.submit(orchestrator.run, task.id)
        second_result = second.result(timeout=5)
        release.set()
        first_result = first.result(timeout=5)

    assert provider.calls == 1
    assert second_result.status == "running"
    assert first_result.status == "completed"


def test_verifier_findings_and_schema_errors_block_but_runtime_errors_fail(
    tmp_path: Path,
):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    select_resume(repo, application.id)

    blocked_config = create_provider_config(repo, model="blocked")
    blocked_outputs = {
        **VALID_OUTPUTS,
        "analyze_fit": {
            **VALID_OUTPUTS["analyze_fit"],
            "matches": ["Kubernetes"],
        },
    }
    blocked_provider = ScriptedProvider(
        availability(model="blocked"), outputs=blocked_outputs
    )
    blocked_orchestrator = AiOrchestrator(
        repo, {blocked_config.id: blocked_provider}
    )
    blocked_task = blocked_orchestrator.enqueue(
        application.id, "analyze_fit", blocked_config.id
    )

    blocked = blocked_orchestrator.run(blocked_task.id)

    assert blocked.status == "blocked"
    assert blocked.error_summary == "verification_failed"
    findings = repo.list_ai_findings(blocked.id)
    assert {item.finding_type for item in findings} == {"unsupported_claim"}
    assert all(item.severity == "blocking" for item in findings)

    invalid_config = create_provider_config(repo, model="invalid-schema")
    invalid_provider = InvalidSchemaProvider(availability(model="invalid-schema"))
    invalid_orchestrator = AiOrchestrator(
        repo, {invalid_config.id: invalid_provider}
    )
    invalid_task = invalid_orchestrator.enqueue(
        application.id, "analyze_fit", invalid_config.id
    )

    invalid = invalid_orchestrator.run(invalid_task.id)

    assert invalid.status == "blocked"
    assert invalid.error_summary == "invalid_output"
    assert [item.finding_type for item in repo.list_ai_findings(invalid.id)] == [
        "schema_invalid"
    ]

    failed_config = create_provider_config(repo, model="runtime-error")
    failed_provider = ScriptedProvider(
        availability(model="runtime-error"),
        error=RuntimeError("password=PRIVATE_RUNTIME_SECRET"),
    )
    failed_orchestrator = AiOrchestrator(
        repo, {failed_config.id: failed_provider}
    )
    failed_task = failed_orchestrator.enqueue(
        application.id, "analyze_fit", failed_config.id
    )

    failed = failed_orchestrator.run(failed_task.id)

    assert failed.status == "failed"
    assert failed.error_summary == "provider_error"
    assert "PRIVATE_RUNTIME_SECRET" not in str(repo.list_audit_events())


def test_result_metadata_mismatch_fails_without_validated_output(tmp_path: Path):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    config = create_provider_config(repo)
    provider = ScriptedProvider(
        availability(), result_overrides={"model": "different-model"}
    )
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    task = orchestrator.enqueue(application.id, "analyze_jd", config.id)

    result = orchestrator.run(task.id)

    assert result.status == "failed"
    assert result.error_summary == "provider_error"
    assert provider.calls == 1
    assert "validated_output" not in {
        artifact.artifact_type for artifact in repo.list_ai_artifacts(task.id)
    }


def test_run_rejects_provider_config_privacy_drift_before_invocation(tmp_path: Path):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    config = create_provider_config(repo)
    provider = ScriptedProvider(availability())
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    task = orchestrator.enqueue(application.id, "analyze_jd", config.id)
    changed = availability(
        privacy_mode=PrivacyMode.CLOUD_EXTERNAL,
        data_leaves_machine=True,
    )
    repo.upsert_ai_provider_config(
        provider_kind="mock",
        display_name="Changed privacy",
        privacy_mode="cloud_external",
        endpoint="",
        model="fixture-v1",
        enabled=True,
        availability_binding=provider_binding_values(changed),
    )
    provider.status = changed

    result = orchestrator.run(task.id)

    assert result.status == "failed"
    assert result.error_summary == "provider_unavailable"
    assert provider.calls == 0


def test_attempt_artifacts_are_atomic_checksummed_and_never_contain_sidecar(
    tmp_path: Path,
):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    select_resume(repo, application.id, "Python developer")
    private_marker = "PRIVATE_SIDECAR_MARKER_68f2"
    repo.create_profile_fact(
        fact_key="private.note",
        fact_value=private_marker,
        verification_state="private_do_not_publish",
        privacy_level="private",
    )
    config = create_provider_config(repo)
    provider = ScriptedProvider(availability())
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    task = orchestrator.enqueue(application.id, "analyze_fit", config.id)

    result = orchestrator.run(task.id)

    assert result.status == "completed"
    artifacts = repo.list_ai_artifacts(task.id)
    assert {item.artifact_type for item in artifacts} == {
        "context",
        "raw_output",
        "validated_output",
    }
    for artifact in artifacts:
        content = artifact.path.read_bytes()
        assert hashlib.sha256(content).hexdigest() == artifact.sha256
    request = provider.requests[0]
    context_artifact = next(
        item for item in artifacts if item.artifact_type == "context"
    )
    assert request.context_snapshot == context_artifact.path.read_bytes()
    assert request.context_path != context_artifact.path
    assert request.context_path.relative_to(repo.vault_path).parts[0] == "ai-tasks"
    assert context_artifact.path.relative_to(repo.vault_path).parts[0] == (
        "ai-artifacts"
    )
    assert json.loads(request.schema_path.read_text(encoding="utf-8")) == (
        output_schema_for_task("analyze_fit")
    )
    for path in request.task_dir.iterdir():
        assert path.is_file()
        content = path.read_bytes()
        assert private_marker.encode() not in content
        assert b'"verification_version"' not in content
        assert b"authority_seal" not in content
        assert ".tmp-" not in path.name


def test_jd_source_bytes_preserve_crlf_without_decode_normalization(tmp_path: Path):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    jd_text = "Build Python APIs.\r\nKeep exact lines.\n"
    application = repo.create_application(
        company="Example Corp",
        role_title="Engineer",
        location="Remote",
        source_url="https://example.com/job",
        jd_text=jd_text,
    )
    snapshot = repo.get_jd_snapshot(application.jd_snapshot_id)
    assert snapshot.raw_text_path.read_bytes() == jd_text.encode("utf-8")
    config = create_provider_config(repo)
    provider = ScriptedProvider(availability())
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    task = orchestrator.enqueue(application.id, "analyze_jd", config.id)

    result = orchestrator.run(task.id)

    assert result.status in {"completed", "blocked"}
    context = json.loads(provider.requests[0].context_text())
    assert context["job_description"]["text"] == jd_text


def test_resume_source_bytes_preserve_crlf_latex(tmp_path: Path):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    latex_bytes = b"\\section{Skills}\r\nPython\\\\\r\n"
    latex_path = repo.vault_path / "resumes" / "exact.tex"
    latex_path.write_bytes(latex_bytes)
    resume = repo.create_resume_version(
        name="Exact resume",
        role_family="software",
        tex_path=latex_path,
        tex_sha256=hashlib.sha256(latex_bytes).hexdigest(),
    )
    repo.select_resume(application.id, resume.id)
    config = create_provider_config(repo)
    provider = ScriptedProvider(availability())
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    task = orchestrator.enqueue(application.id, "analyze_fit", config.id)

    result = orchestrator.run(task.id)

    assert result.status == "completed"
    context = json.loads(provider.requests[0].context_text())
    assert context["selected_resume"]["source_text"] == latex_bytes.decode("utf-8")


def test_requeue_invalidates_stale_attempt_without_reinvoking_provider(tmp_path: Path):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    select_resume(repo, application.id)
    config = create_provider_config(repo)

    def interrupt_first_attempt(_request: Any, calls: int) -> None:
        if calls != 1:
            return
        with repo.connection() as connection:
            connection.execute(
                "UPDATE ai_tasks SET claim_expires_at = ? WHERE id = ?",
                ("1970-01-01T00:00:00+00:00", task.id),
            )
        assert repo.requeue_interrupted_ai_tasks() == 1
        repo.create_profile_fact(
            fact_key="skill",
            fact_value="SQL",
            verification_state="verified",
            privacy_level="public",
        )

    provider = ScriptedProvider(availability(), on_run=interrupt_first_attempt)
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    task = orchestrator.enqueue(application.id, "analyze_fit", config.id)

    interrupted = orchestrator.run(task.id)
    recovered = orchestrator.run(task.id)

    assert interrupted.status == "pending"
    assert recovered.status == "failed"
    assert recovered.error_summary == "provider_error"
    assert provider.calls == 1
    artifacts = repo.list_ai_artifacts(task.id)
    contexts = [item for item in artifacts if item.artifact_type == "context"]
    assert len(contexts) == 1
    assert repo.list_application_versions(application.id) == []


def test_live_orchestrator_renews_claim_while_provider_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = CareerOpsRepository(
        tmp_path / "careerops.db",
        tmp_path / "vault",
        claim_lease_seconds=15,
    )
    repo.initialize()
    application = create_application(repo)
    config = create_provider_config(repo)
    provider_started = Event()
    release_provider = Event()
    claim_renewed = Event()
    original_renew = repo.renew_ai_task_claim

    monkeypatch.setattr(
        type(repo),
        "claim_heartbeat_interval",
        property(lambda _repository: 0.05),
    )

    def record_renewal(task_id: str, claim_token: str) -> bool:
        renewed = original_renew(task_id, claim_token)
        if renewed:
            claim_renewed.set()
        return renewed

    def wait_in_provider(_request: Any, _calls: int) -> None:
        provider_started.set()
        assert release_provider.wait(5)

    provider = ScriptedProvider(availability(), on_run=wait_in_provider)
    monkeypatch.setattr(repo, "renew_ai_task_claim", record_renewal)
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    task = orchestrator.enqueue(application.id, "analyze_jd", config.id)
    recovery = CareerOpsRepository(repo.database_path, repo.vault_path)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(orchestrator.run, task.id)
        assert provider_started.wait(5)
        assert claim_renewed.wait(5)
        assert recovery.requeue_interrupted_ai_tasks() == 0
        release_provider.set()
        result = future.result(timeout=5)

    assert result.status == "completed"
    assert provider.calls == 1


def test_finalization_crash_recovery_never_reinvokes_or_writes_business_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from careerops.ai.orchestrator import AiOrchestrator

    class SimulatedCrash(BaseException):
        pass

    repo = make_repo(tmp_path)
    application = create_application(repo)
    select_resume(repo, application.id, "Python developer")
    config = create_provider_config(repo)
    provider = ScriptedProvider(availability())
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    before = repo.get_application(application.id)
    original_write_audit = repo._write_audit
    crash_pending = True

    def crash_during_finalization(*args: Any, **kwargs: Any) -> None:
        nonlocal crash_pending
        event_type = args[1] if len(args) > 1 else kwargs["event_type"]
        if crash_pending and event_type == "application.version_recorded":
            crash_pending = False
            raise SimulatedCrash("simulated process crash")
        original_write_audit(*args, **kwargs)

    monkeypatch.setattr(repo, "_write_audit", crash_during_finalization)
    task = orchestrator.enqueue(application.id, "analyze_fit", config.id)

    with pytest.raises(SimulatedCrash):
        orchestrator.run(task.id)

    assert repo.get_ai_task(task.id).status == "running"
    assert repo.get_application(application.id) == before
    assert repo.list_application_versions(application.id) == []
    with repo.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM reports WHERE application_id = ?",
            (application.id,),
        ).fetchone()[0] == 0
    assert provider.calls == 1

    with repo.connection() as connection:
        connection.execute(
            "UPDATE ai_tasks SET claim_expires_at = ? WHERE id = ?",
            ("1970-01-01T00:00:00+00:00", task.id),
        )
    assert repo.requeue_interrupted_ai_tasks() == 1
    recovered = orchestrator.run(task.id)

    assert recovered.status == "failed"
    assert recovered.error_summary == "provider_error"
    assert repo.get_application(application.id) == before
    assert repo.list_application_versions(application.id) == []
    assert provider.calls == 1


def test_atomic_snapshot_failure_cleans_temp_and_fails_without_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from careerops.ai import orchestrator as orchestrator_module
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    config = create_provider_config(repo)
    provider = ScriptedProvider(availability())
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    task = orchestrator.enqueue(application.id, "analyze_jd", config.id)

    def fail_replace(_source: Any, _destination: Any) -> None:
        raise OSError("PRIVATE_FILESYSTEM_MARKER")

    monkeypatch.setattr(orchestrator_module.os, "replace", fail_replace)

    result = orchestrator.run(task.id)

    assert result.status == "failed"
    assert result.error_summary == "provider_error"
    assert provider.calls == 0
    task_root = repo.vault_path / "ai-tasks" / task.id
    assert list(task_root.rglob("*.tmp-*")) == []


def test_finalization_failure_rolls_back_report_application_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    select_resume(repo, application.id, "Python developer")
    config = create_provider_config(repo)
    provider = ScriptedProvider(availability())
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    before = repo.get_application(application.id)
    with repo.connection() as connection:
        before_report_ids = [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM reports WHERE application_id = ? ORDER BY id",
                (application.id,),
            ).fetchall()
        ]
    original_write_audit = repo._write_audit

    def fail_version_audit(*args: Any, **kwargs: Any) -> None:
        event_type = args[1] if len(args) > 1 else kwargs["event_type"]
        if event_type == "application.version_recorded":
            raise RuntimeError("simulated finalization failure")
        original_write_audit(*args, **kwargs)

    monkeypatch.setattr(repo, "_write_audit", fail_version_audit)
    task = orchestrator.enqueue(application.id, "analyze_fit", config.id)

    result = orchestrator.run(task.id)

    assert result.status == "failed"
    assert provider.calls == 1
    assert repo.get_application(application.id) == before
    with repo.connection() as connection:
        after_report_ids = [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM reports WHERE application_id = ? ORDER BY id",
                (application.id,),
            ).fetchall()
        ]
    assert after_report_ids == before_report_ids
    assert repo.list_application_versions(application.id) == []


def test_finalization_failure_rolls_back_jd_requirements_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    config = create_provider_config(repo)
    provider = ScriptedProvider(availability())
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    before = repo.get_jd_snapshot(application.jd_snapshot_id)
    original_write_audit = repo._write_audit

    def fail_version_audit(*args: Any, **kwargs: Any) -> None:
        event_type = args[1] if len(args) > 1 else kwargs["event_type"]
        if event_type == "application.version_recorded":
            raise RuntimeError("simulated finalization failure")
        original_write_audit(*args, **kwargs)

    monkeypatch.setattr(repo, "_write_audit", fail_version_audit)
    task = orchestrator.enqueue(application.id, "analyze_jd", config.id)

    result = orchestrator.run(task.id)

    assert result.status == "failed"
    assert provider.calls == 1
    assert repo.get_jd_snapshot(application.jd_snapshot_id) == before
    assert repo.get_application(application.id) == application
    assert repo.list_application_versions(application.id) == []


def test_analyze_fit_creates_normal_report_with_complete_provenance(tmp_path: Path):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    resume = select_resume(repo, application.id, "Python developer")
    config = create_provider_config(repo)
    provider = ScriptedProvider(availability())
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    task = orchestrator.enqueue(application.id, "analyze_fit", config.id)

    result = orchestrator.run(task.id)

    assert result.status == "completed"
    updated_application = repo.get_application(application.id)
    assert updated_application.latest_report_id is not None
    report = repo.get_report(updated_application.latest_report_id)
    assert report.application_id == application.id
    assert report.resume_version_id == resume.id
    assert report.jd_snapshot_id == application.jd_snapshot_id
    assert report.fit["score"] == 80
    assert report.risks == {
        "blockers": [],
        "provenance": report.fit["provenance"],
        "warnings": [],
    }
    artifacts = {
        item.artifact_type: item for item in repo.list_ai_artifacts(task.id)
    }
    provenance = report.fit["provenance"]
    assert provenance == {
        "ai_task_id": task.id,
        "context_sha256": artifacts["context"].sha256,
        "output_checksums": {
            "raw_output": artifacts["raw_output"].sha256,
            "validated_output": artifacts["validated_output"].sha256,
        },
        "privacy_mode": "local_private",
        "prompt_sha256": provider.requests[0].context_text()
        and json.loads(provider.requests[0].context_text())["prompt"]["sha256"],
        "prompt_template_id": "analyze_fit_v1",
        "provider_kind": "mock",
        "provider_identity_name": "",
        "provider_model": "fixture-v1",
        "provider_binding": config.availability_binding,
        "provider_binding_sha256": config.availability_binding_sha256,
        "provider_config_id": config.id,
    }
    assert updated_application.approval_id is None
    assert updated_application.submission_state == "not_submitted"


def test_analyze_jd_preserves_user_corrections_in_provenance_groups(tmp_path: Path):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    existing = {
        "raw_ai": {"required_skills": ["Old AI value"]},
        "deterministic": {"required_skills": ["Python"]},
        "user_corrected": {
            "required_skills": ["Rust"],
            "note": "Confirmed by recruiter",
        },
        "uncertainty": {"items": ["Old uncertainty"]},
    }
    with repo.connection() as connection:
        connection.execute(
            "UPDATE jd_snapshots SET extracted_requirements_json = ? WHERE id = ?",
            (json.dumps(existing), application.jd_snapshot_id),
        )
    config = create_provider_config(repo)
    provider = ScriptedProvider(availability())
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    task = orchestrator.enqueue(application.id, "analyze_jd", config.id)

    result = orchestrator.run(task.id)

    assert result.status == "completed"
    requirements = repo.get_jd_snapshot(
        application.jd_snapshot_id
    ).extracted_requirements_json
    assert set(requirements) == {
        "raw_ai",
        "deterministic",
        "user_corrected",
        "uncertainty",
    }
    assert requirements["deterministic"] == existing["deterministic"]
    assert requirements["user_corrected"] == existing["user_corrected"]
    assert requirements["raw_ai"]["required_skills"] == ["Python"]
    assert requirements["raw_ai"]["provenance"]["ai_task_id"] == task.id
    assert requirements["uncertainty"]["items"] == []


def test_analyze_jd_invalidates_stale_fit_report_atomically(tmp_path: Path):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    resume = select_resume(repo, application.id, "Python developer")
    report = repo.save_report(
        application_id=application.id,
        resume_version_id=resume.id,
        jd_snapshot_id=application.jd_snapshot_id,
        fit={"score": 90},
        risks={"blockers": [], "warnings": []},
    )
    config = create_provider_config(repo)
    provider = ScriptedProvider(availability())
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    task = orchestrator.enqueue(application.id, "analyze_jd", config.id)

    completed = orchestrator.run(task.id)

    updated = repo.get_application(application.id)
    assert completed.status == "completed"
    assert updated.latest_report_id is None
    assert updated.status == "needs_resume_update"
    assert repo.get_report(report.id).id == report.id
    version = repo.list_application_versions(application.id)[-1]
    assert version.snapshot["side_effect"]["invalidated_report_id"] == report.id
    with pytest.raises(ValueError, match="current application report"):
        repo.approve_application(application.id, report.id, "Reviewed")


def test_analyze_jd_cannot_enqueue_for_an_approved_packet(tmp_path: Path):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    resume = select_resume(repo, application.id, "Python developer")
    report = repo.save_report(
        application_id=application.id,
        resume_version_id=resume.id,
        jd_snapshot_id=application.jd_snapshot_id,
        fit={"score": 90},
        risks={"blockers": [], "warnings": []},
    )
    repo.approve_application(application.id, report.id, "Reviewed")
    before_application = repo.get_application(application.id)
    before_jd = repo.get_jd_snapshot(application.jd_snapshot_id)
    config = create_provider_config(repo)
    provider = ScriptedProvider(availability())
    orchestrator = AiOrchestrator(repo, {config.id: provider})

    with pytest.raises(ValueError, match="read-only"):
        orchestrator.enqueue(application.id, "analyze_jd", config.id)

    assert provider.calls == 0
    assert repo.list_ai_tasks(application.id) == []
    assert repo.get_application(application.id) == before_application
    assert repo.get_jd_snapshot(application.jd_snapshot_id) == before_jd
    assert repo.list_application_versions(application.id) == []


def test_suggest_resume_edits_creates_findings_without_resume_mutation(tmp_path: Path):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    resume = select_resume(repo, application.id, "Built PocketBuddy")
    outputs = {
        **VALID_OUTPUTS,
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
    }
    config = create_provider_config(repo)
    provider = ScriptedProvider(availability(), outputs=outputs)
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    before_resumes = repo.list_resume_versions()
    task = orchestrator.enqueue(
        application.id, "suggest_resume_edits", config.id
    )

    result = orchestrator.run(task.id)

    assert result.status == "completed"
    assert repo.get_application(application.id).selected_resume_version_id == resume.id
    assert repo.list_resume_versions() == before_resumes
    findings = repo.list_ai_findings(task.id)
    assert len(findings) == 1
    assert findings[0].finding_type == "resume_edit_suggestion"
    assert findings[0].verifier_state == "verified"
    assert findings[0].payload["replacement_text"] == "Built PocketBuddy"


def test_draft_cover_letter_versions_text_without_advancing_submission(tmp_path: Path):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    select_resume(repo, application.id, "Python developer")
    config = create_provider_config(repo)
    provider = ScriptedProvider(availability())
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    before = repo.get_application(application.id)
    task = orchestrator.enqueue(
        application.id, "draft_cover_letter", config.id
    )

    result = orchestrator.run(task.id)

    assert result.status == "completed"
    after = repo.get_application(application.id)
    assert after.approval_id == before.approval_id
    assert after.submission_state == before.submission_state
    assert after.submitted_at == before.submitted_at
    draft = next(
        item
        for item in repo.list_ai_artifacts(task.id)
        if item.artifact_type == "draft_text"
    )
    assert draft.path.read_text(encoding="utf-8") == "I am excited to apply."
    version = repo.list_application_versions(application.id)[0]
    assert version.snapshot["side_effect"] == {
        "draft_text_artifact_id": draft.id,
        "draft_text_sha256": draft.sha256,
    }


def test_review_claims_creates_decision_findings_without_authority_mutation(
    tmp_path: Path,
):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    fact = repo.create_profile_fact(
        fact_key="project",
        fact_value="Built PocketBuddy",
        verification_state="needs_verification",
        privacy_level="public",
    )
    outputs = {
        **VALID_OUTPUTS,
        "review_claims": {
            "task_type": "review_claims",
            "decisions": [
                {
                    "claim_id": fact.id,
                    "decision": "needs_review",
                    "reason": "Needs human review",
                    "evidence_refs": [],
                }
            ],
        },
    }
    config = create_provider_config(repo)
    provider = ScriptedProvider(availability(), outputs=outputs)
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    before_fact = repo.get_profile_fact(fact.id)
    before_evidence = repo.list_evidence_items()
    task = orchestrator.enqueue(application.id, "review_claims", config.id)

    result = orchestrator.run(task.id)

    assert result.status == "completed"
    assert repo.get_profile_fact(fact.id) == before_fact
    assert repo.list_evidence_items() == before_evidence
    findings = repo.list_ai_findings(task.id)
    assert len(findings) == 1
    assert findings[0].finding_type == "claim_review_decision"
    assert findings[0].verifier_state == "needs_evidence"
    assert findings[0].payload["decision"] == "needs_review"


def test_application_versions_capture_attempt_without_changing_resume_history(
    tmp_path: Path,
):
    from careerops.ai.orchestrator import AiOrchestrator

    repo = make_repo(tmp_path)
    application = create_application(repo)
    select_resume(repo, application.id, "Python developer")
    config = create_provider_config(repo)
    provider = ScriptedProvider(availability())
    orchestrator = AiOrchestrator(repo, {config.id: provider})
    resume_ids = [item.id for item in repo.list_resume_versions()]
    task = orchestrator.enqueue(application.id, "analyze_fit", config.id)

    assert orchestrator.run(task.id).status == "completed"

    assert [item.id for item in repo.list_resume_versions()] == resume_ids
    versions = repo.list_application_versions(application.id)
    assert len(versions) == 1
    assert versions[0].event_type == "ai_task.completed"
    snapshot = versions[0].snapshot
    assert snapshot["ai_task"] == {
        "id": task.id,
        "input_artifact_ids": [],
        "provider_config_id": config.id,
        "status": "completed",
        "task_type": "analyze_fit",
    }
    persisted_artifacts = repo.list_ai_artifacts(task.id)
    assert snapshot["artifacts"] == [
        {
            "artifact_type": item.artifact_type,
            "id": item.id,
            "sha256": item.sha256,
        }
        for item in persisted_artifacts
    ]
    assert snapshot["finding_ids"] == []


def test_orchestrator_is_exported_from_ai_package():
    from careerops.ai import AiOrchestrator

    assert AiOrchestrator.__name__ == "AiOrchestrator"


def test_application_version_and_audit_payloads_are_safe_bounded_canonical_json(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    secret = "PRIVATE_VERSION_SECRET_73d1"

    version = repo.record_application_version(
        application.id,
        "ai.tested",
        {
            "nested": {"token": secret},
            "provider": "mock",
        },
    )

    assert version.snapshot == {
        "nested": {"token": "[REDACTED]"},
        "provider": "mock",
    }
    with repo.connection() as connection:
        version_json = connection.execute(
            "SELECT snapshot_json FROM application_versions WHERE id = ?",
            (version.id,),
        ).fetchone()[0]
        audit_rows = connection.execute(
            """
            SELECT before_json, after_json FROM audit_events
            WHERE event_type = 'application.version_recorded'
            """
        ).fetchall()
    persisted_json = [version_json] + [
        value for row in audit_rows for value in (row["before_json"], row["after_json"])
    ]
    for raw in persisted_json:
        assert secret not in raw
        assert len(raw.encode("utf-8")) <= 256_000
        assert raw == json.dumps(
            json.loads(raw),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    with pytest.raises(ValueError, match="size limit"):
        repo.record_application_version(
            application.id,
            "ai.too_large",
            {"value": "x" * 300_000},
        )
    with pytest.raises(ValueError, match="JSON"):
        repo.record_application_version(
            application.id,
            "ai.not_finite",
            {"score": float("nan")},
        )
    assert [item.id for item in repo.list_application_versions(application.id)] == [
        version.id
    ]
