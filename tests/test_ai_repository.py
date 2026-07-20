from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import FrozenInstanceError
import gc
import hashlib
from pathlib import Path
import sqlite3
from threading import Barrier
import warnings

import pytest

from careerops.core.database import connect_database, initialize_database
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


def create_task(
    repo: CareerOpsRepository,
    application_id: str,
    provider_id: str,
    input_artifact_ids: list[str] | None = None,
):
    return repo.create_ai_task(
        application_id=application_id,
        task_type="analyze_fit",
        provider_config_id=provider_id,
        privacy_mode="local_private",
        prompt_template_id="analyze_fit_v1",
        input_artifact_ids=input_artifact_ids or [],
    )


def claim_task(repo: CareerOpsRepository, task_id: str) -> str:
    claim_token = repo.claim_ai_task(task_id)
    assert isinstance(claim_token, str)
    return claim_token


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
    input_path = repo.vault_path / "ai-artifacts" / "input.json"
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


def test_task_start_complete_and_failure_are_durable_and_audited(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    completed_task = create_task(repo, application.id, provider.id)
    failed_task = create_task(repo, application.id, provider.id)

    completed_token = repo.start_ai_task(completed_task.id)
    completed = repo.complete_ai_task(completed_task.id, completed_token)
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
    context_path = tmp_path / "vault" / "ai-tasks" / task.id / "context.json"
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
    valid_path = repo.vault_path / "ai-artifacts" / "valid.json"
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
            repo.vault_path / "ai-artifacts" / "missing.json",
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
    assert artifact.path == valid_path.resolve()


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
    input_path = repo.vault_path / "ai-artifacts" / "scoped-input.json"
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
    assert repo.requeue_interrupted_ai_tasks() == 1
    current_token = worker_b.claim_ai_task(task.id)
    assert isinstance(current_token, str)
    assert current_token != stale_token

    with pytest.raises(ValueError, match="claim"):
        worker_a.complete_ai_task(task.id, stale_token)
    with pytest.raises(ValueError, match="claim"):
        worker_a.fail_ai_task(task.id, stale_token, "provider_unavailable")

    assert worker_b.complete_ai_task(task.id, current_token).status == "completed"


def test_requeue_invalidates_stale_artifact_and_finding_writes(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    worker_a = CareerOpsRepository(repo.database_path, repo.vault_path)
    worker_b = CareerOpsRepository(repo.database_path, repo.vault_path)
    stale_token = claim_task(worker_a, task.id)
    first_path = repo.vault_path / "ai-artifacts" / "attempt-a.json"
    first_path.write_bytes(b"attempt-a")

    first_artifact = worker_a.save_ai_artifact(
        task.id,
        stale_token,
        "raw_output",
        first_path,
        hashlib.sha256(b"attempt-a").hexdigest(),
        "1",
    )
    first_finding = worker_a.save_ai_finding(
        task_id=task.id,
        claim_token=stale_token,
        finding_type="risk",
        severity="warning",
        claim_text="Attempt A",
        evidence_item_id=None,
        recommendation="Review",
        verifier_state="needs_evidence",
    )
    assert repo.requeue_interrupted_ai_tasks() == 1
    current_token = claim_task(worker_b, task.id)
    second_path = repo.vault_path / "ai-artifacts" / "attempt-b.json"
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
    with pytest.raises(ValueError, match="claim"):
        worker_a.save_ai_finding(
            task_id=task.id,
            claim_token=stale_token,
            finding_type="risk",
            severity="warning",
            claim_text="Stale attempt A",
            evidence_item_id=None,
            recommendation="Review",
            verifier_state="needs_evidence",
        )

    second_artifact = worker_b.save_ai_artifact(
        task.id,
        current_token,
        "raw_output",
        second_path,
        hashlib.sha256(b"attempt-b").hexdigest(),
        "1",
    )
    second_finding = worker_b.save_ai_finding(
        task_id=task.id,
        claim_token=current_token,
        finding_type="risk",
        severity="warning",
        claim_text="Attempt B",
        evidence_item_id=None,
        recommendation="Review",
        verifier_state="needs_evidence",
    )

    assert first_artifact.claim_token == stale_token
    assert second_artifact.claim_token == current_token
    assert first_finding.claim_token == stale_token
    assert second_finding.claim_token == current_token
    assert repo.list_ai_artifacts(task.id) == [first_artifact, second_artifact]
    assert repo.list_ai_findings(task.id) == [first_finding, second_finding]


def test_competing_terminal_updates_allow_only_one_claim_owner_update(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim_token = repo.claim_ai_task(task.id)
    assert isinstance(claim_token, str)
    workers = [
        CareerOpsRepository(repo.database_path, repo.vault_path),
        CareerOpsRepository(repo.database_path, repo.vault_path),
    ]
    barrier = Barrier(len(workers))

    def complete():
        barrier.wait()
        return workers[0].complete_ai_task(task.id, claim_token)

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
    task = create_task(repo, application.id, provider.id)
    claim_token = claim_task(repo, task.id)

    finding = repo.save_ai_finding(
        task_id=task.id,
        claim_token=claim_token,
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
    task = create_task(repo, application.id, provider.id)
    claim_token = claim_task(repo, task.id)
    finding_values = {
        "task_id": task.id,
        "claim_token": claim_token,
        "finding_type": "unsupported_claim",
        "severity": "blocking",
        "claim_text": "Improved throughput by 50%",
        "evidence_item_id": None,
        "recommendation": "Remove the unsupported metric",
        "verifier_state": "verified",
    }

    with pytest.raises(ValueError, match="pending"):
        repo.save_ai_finding(**finding_values, decision="accepted")

    finding = repo.save_ai_finding(**finding_values)
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
    task = create_task(repo, application.id, provider.id)
    claim_token = claim_task(repo, task.id)
    finding = repo.save_ai_finding(
        task_id=task.id,
        claim_token=claim_token,
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


def test_concurrent_finding_decisions_have_one_causal_audit(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    provider = create_provider(repo)
    task = create_task(repo, application.id, provider.id)
    claim_token = claim_task(repo, task.id)
    finding = repo.save_ai_finding(
        task_id=task.id,
        claim_token=claim_token,
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
        )

    provider = create_provider(repo)
    with pytest.raises(ValueError, match="task type"):
        repo.create_ai_task(
            application_id=application.id,
            task_type="invented",
            provider_config_id=provider.id,
            privacy_mode="local_private",
            prompt_template_id="invalid",
            input_artifact_ids=[],
        )
    with pytest.raises(ValueError, match="privacy mode"):
        repo.create_ai_task(
            application_id=application.id,
            task_type="analyze_fit",
            provider_config_id=provider.id,
            privacy_mode="invented",
            prompt_template_id="analyze_fit_v1",
            input_artifact_ids=[],
        )

    task = repo.create_ai_task(
        application_id=application.id,
        task_type="analyze_fit",
        provider_config_id=provider.id,
        privacy_mode="local_private",
        prompt_template_id="analyze_fit_v1",
        input_artifact_ids=[],
    )
    claim_token = claim_task(repo, task.id)
    with pytest.raises(ValueError, match="severity"):
        repo.save_ai_finding(
            task_id=task.id,
            claim_token=claim_token,
            finding_type="risk",
            severity="invented",
            claim_text="Claim",
            evidence_item_id=None,
            recommendation="Review",
            verifier_state="blocked",
        )
    with pytest.raises(ValueError, match="verifier state"):
        repo.save_ai_finding(
            task_id=task.id,
            claim_token=claim_token,
            finding_type="risk",
            severity="warning",
            claim_text="Claim",
            evidence_item_id=None,
            recommendation="Review",
            verifier_state="invented",
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
