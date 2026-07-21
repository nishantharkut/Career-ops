from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from apps.api.careerops_api.main import create_app
from careerops.core.repository import CareerOpsRepository, MAX_PERSISTED_JSON_BYTES


def make_repo(tmp_path: Path) -> CareerOpsRepository:
    repo = CareerOpsRepository(tmp_path / "careerops.db", tmp_path / "vault")
    repo.initialize()
    return repo


def create_application(repo: CareerOpsRepository):
    return repo.create_application(
        company="Example Corp",
        role_title="Software Engineer",
        location="Remote",
        source_url="https://example.com/job",
        jd_text="Company: Example Corp\nTitle: Software Engineer\nRequired skills:\n- Python\n",
    )


def select_resume(repo: CareerOpsRepository, application_id: str):
    directory = repo.vault_path / "resumes" / application_id
    directory.mkdir(parents=True, exist_ok=True)
    tex = directory / "resume.tex"
    pdf = directory / "resume.pdf"
    tex.write_text("Python developer", encoding="utf-8")
    pdf.write_bytes(b"%PDF-1.4\nfixture")
    resume = repo.create_resume_version(
        name="Resume",
        role_family="software",
        tex_path=tex,
        tex_sha256=hashlib.sha256(tex.read_bytes()).hexdigest(),
        pdf_path=pdf,
        pdf_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(),
        render_status="rendered",
        parse_status="parsed",
        page_count=1,
    )
    repo.select_resume(application_id, resume.id)
    return resume


def seeded_requirements() -> dict[str, object]:
    provenance = {"ai_task_id": "ait_authoritative", "prompt_sha256": "a" * 64}
    return {
        "raw_ai": {"required_skills": ["Python"], "provenance": provenance},
        "deterministic": {"required_skills": ["Python", "SQL"]},
        "user_corrected": {"required_skills": ["Python"]},
        "uncertainty": {"items": ["Location unclear"], "provenance": provenance},
    }


def seed_requirements(repo: CareerOpsRepository, snapshot_id: str) -> None:
    with repo.connection() as connection:
        connection.execute(
            "UPDATE jd_snapshots SET extracted_requirements_json = ? WHERE id = ?",
            (json.dumps(seeded_requirements()), snapshot_id),
        )


def canonical_json_size(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def ascii_requirements_with_size(target_size: int) -> dict[str, str]:
    value = {f"field_{index:02d}": "x" * 4_000 for index in range(63)}
    value["field_63"] = ""
    remaining = target_size - canonical_json_size(value)
    assert 0 <= remaining <= 4_000
    value["field_63"] = "x" * remaining
    assert canonical_json_size(value) == target_size
    return value


def test_repository_user_corrections_are_transactional_and_preserve_provenance(tmp_path: Path):
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
    seed_requirements(repo, application.jd_snapshot_id)
    before = seeded_requirements()

    updated = repo.update_user_corrected_requirements(
        application.id,
        {"required_skills": ["Python", "Rust"], "note": "Confirmed by recruiter"},
    )

    assert set(updated.extracted_requirements_json) == {
        "raw_ai",
        "deterministic",
        "user_corrected",
        "uncertainty",
    }
    assert updated.extracted_requirements_json["raw_ai"] == before["raw_ai"]
    assert updated.extracted_requirements_json["deterministic"] == before["deterministic"]
    assert updated.extracted_requirements_json["uncertainty"] == before["uncertainty"]
    assert updated.extracted_requirements_json["user_corrected"]["required_skills"] == [
        "Python",
        "Rust",
    ]
    refreshed = repo.get_application(application.id)
    assert refreshed.latest_report_id is None
    assert refreshed.approval_id is None
    versions = repo.list_application_versions(application.id)
    assert versions[-1].event_type == "requirements.user_corrected_updated"
    assert versions[-1].snapshot["invalidated_report_id"] == report.id


def test_user_corrections_do_not_reload_through_public_getter_after_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application = create_application(repo)

    def reject_public_reload(_snapshot_id: str):
        raise RuntimeError("post-commit JD getter must not run")

    monkeypatch.setattr(repo, "get_jd_snapshot", reject_public_reload)

    updated = repo.update_user_corrected_requirements(
        application.id, {"generation": "first"}
    )

    verifier = CareerOpsRepository(repo.database_path, repo.vault_path)
    assert updated.extracted_requirements_json["user_corrected"] == {
        "generation": "first"
    }
    assert verifier.get_jd_snapshot(updated.id) == updated


def test_user_corrections_return_their_own_commit_when_a_second_update_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    writer = CareerOpsRepository(repo.database_path, repo.vault_path)
    first_committed = Event()
    second_committed = Event()
    original_connection = repo.connection
    delay_first_commit = True

    @contextmanager
    def pause_after_first_commit():
        nonlocal delay_first_commit
        with original_connection() as connection:
            yield connection
        if delay_first_commit:
            delay_first_commit = False
            first_committed.set()
            assert second_committed.wait(timeout=5)

    monkeypatch.setattr(repo, "connection", pause_after_first_commit)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first_future = pool.submit(
            repo.update_user_corrected_requirements,
            application.id,
            {"generation": "first"},
        )
        assert first_committed.wait(timeout=2)
        writer.update_user_corrected_requirements(
            application.id, {"generation": "second"}
        )
        second_committed.set()
        first = first_future.result(timeout=5)

    persisted = writer.get_jd_snapshot(application.jd_snapshot_id)
    assert first.extracted_requirements_json["user_corrected"] == {
        "generation": "first"
    }
    assert persisted.extracted_requirements_json["user_corrected"] == {
        "generation": "second"
    }


def test_profile_fact_creation_does_not_reload_through_public_getter_after_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)

    def reject_public_reload(_fact_id: str):
        raise RuntimeError("post-commit profile getter must not run")

    monkeypatch.setattr(repo, "get_profile_fact", reject_public_reload)

    fact = repo.create_profile_fact(
        fact_key="skills.python",
        fact_value="Advanced",
        verification_state="verified",
        privacy_level="public",
    )

    verifier = CareerOpsRepository(repo.database_path, repo.vault_path)
    assert verifier.get_profile_fact(fact.id) == fact


def test_existing_evidence_link_does_not_reload_through_public_getter_after_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    evidence = repo.create_evidence_item(
        claim_text="Built a Python service",
        evidence_type="project",
        verification_state="verified",
        privacy_level="public",
    )

    def reject_public_reload(_evidence_id: str):
        raise RuntimeError("post-commit evidence getter must not run")

    monkeypatch.setattr(repo, "get_evidence_item", reject_public_reload)

    linked = repo.link_application_evidence(application.id, evidence.id)

    verifier = CareerOpsRepository(repo.database_path, repo.vault_path)
    assert linked == evidence
    assert verifier.list_application_evidence(application.id) == [evidence]


def test_standalone_evidence_creation_materializes_before_commit_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)

    def reject_public_reload(_evidence_id: str):
        raise RuntimeError("post-commit evidence getter must not run")

    monkeypatch.setattr(repo, "get_evidence_item", reject_public_reload)

    evidence = repo.create_evidence_item(
        claim_text="Built a Python service",
        evidence_type="project",
        verification_state="verified",
        privacy_level="public",
    )

    verifier = CareerOpsRepository(repo.database_path, repo.vault_path)
    assert verifier.get_evidence_item(evidence.id) == evidence


def test_application_version_materializes_before_commit_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application = create_application(repo)

    def reject_public_reload(_version_id: str):
        raise RuntimeError("post-commit version getter must not run")

    monkeypatch.setattr(repo, "get_application_version", reject_public_reload)

    version = repo.record_application_version(
        application.id, "packet.reviewed", {"state": "reviewed"}
    )

    verifier = CareerOpsRepository(repo.database_path, repo.vault_path)
    assert verifier.get_application_version(version.id) == version


def test_requirements_api_only_updates_user_corrected_and_rejects_unknown_fields(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    seed_requirements(repo, application.jd_snapshot_id)
    client = TestClient(create_app(repo, provider_registry={}))

    before = client.get(f"/applications/{application.id}/requirements")
    updated = client.put(
        f"/applications/{application.id}/requirements",
        json={"user_corrected": {"required_skills": [" Rust "]}},
    )
    unknown = client.put(
        f"/applications/{application.id}/requirements",
        json={"user_corrected": {}, "raw_ai": {"forged": True}},
    )

    assert before.status_code == 200
    assert set(before.json()) == {"raw_ai", "deterministic", "user_corrected", "uncertainty"}
    assert updated.status_code == 200
    assert updated.json()["raw_ai"] == before.json()["raw_ai"]
    assert updated.json()["deterministic"] == before.json()["deterministic"]
    assert updated.json()["uncertainty"] == before.json()["uncertainty"]
    assert updated.json()["user_corrected"] == {"required_skills": ["Rust"]}
    assert unknown.status_code == 422


@pytest.mark.parametrize(
    ("user_corrected", "expected_status"),
    [
        (ascii_requirements_with_size(MAX_PERSISTED_JSON_BYTES - 1), 404),
        (ascii_requirements_with_size(MAX_PERSISTED_JSON_BYTES + 1), 422),
        ({f"field_{index:02d}": "€" * 4_000 for index in range(21)}, 404),
        ({f"field_{index:02d}": "€" * 4_000 for index in range(22)}, 422),
    ],
    ids=["ascii-under", "ascii-over", "unicode-under", "unicode-over"],
)
def test_requirements_request_enforces_canonical_utf8_byte_limit(
    tmp_path: Path,
    user_corrected: dict[str, str],
    expected_status: int,
):
    client = TestClient(create_app(make_repo(tmp_path), provider_registry={}))

    response = client.put(
        "/applications/app_missing/requirements",
        json={"user_corrected": user_corrected},
    )

    assert response.status_code == expected_status


@pytest.mark.parametrize("terminal_state", ["approved", "submitted"])
def test_requirements_reject_approved_and_submitted_packet_mutation(
    tmp_path: Path, terminal_state: str
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
    repo.approve_application(application.id, report.id, "Reviewed")
    if terminal_state == "submitted":
        repo.mark_submitted_manual(application.id, "Submitted")
    before = repo.get_jd_snapshot(application.jd_snapshot_id).extracted_requirements_json
    client = TestClient(create_app(repo, provider_registry={}))

    response = client.put(
        f"/applications/{application.id}/requirements",
        json={"user_corrected": {"required_skills": ["Rust"]}},
    )

    assert response.status_code == 409
    assert repo.get_jd_snapshot(application.jd_snapshot_id).extracted_requirements_json == before


@pytest.mark.parametrize(
    "verification_state",
    [
        "verified",
        "user_stated",
        "needs_verification",
        "rejected",
        "private_do_not_publish",
    ],
)
def test_profile_fact_api_accepts_exact_verification_states(
    tmp_path: Path, verification_state: str
):
    client = TestClient(create_app(make_repo(tmp_path), provider_registry={}))

    response = client.post(
        "/profile-facts",
        json={
            "fact_key": " skills.python ",
            "fact_value": " Advanced ",
            "verification_state": verification_state,
            "privacy_level": "private",
            "notes": " Reviewed ",
        },
    )

    assert response.status_code == 201
    assert response.json()["fact_key"] == "skills.python"
    assert response.json()["fact_value"] == "Advanced"
    assert response.json()["verification_state"] == verification_state
    assert client.get("/profile-facts").json()[0]["id"] == response.json()["id"]


@pytest.mark.parametrize(
    "field,value",
    [("verification_state", "invented"), ("privacy_level", "secret")],
)
def test_profile_fact_api_rejects_invalid_enums(tmp_path: Path, field: str, value: str):
    client = TestClient(create_app(make_repo(tmp_path), provider_registry={}))
    payload = {
        "fact_key": "skills.python",
        "fact_value": "Advanced",
        "verification_state": "verified",
        "privacy_level": "private",
    }
    payload[field] = value

    response = client.post("/profile-facts", json=payload)

    assert response.status_code == 422


def test_evidence_create_link_and_list_keeps_private_and_rejected_visible(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    other = create_application(repo)
    existing = repo.create_evidence_item(
        claim_text="Existing private note",
        evidence_type="note",
        verification_state="private_do_not_publish",
        privacy_level="private",
    )
    client = TestClient(create_app(repo, provider_registry={}))

    public = client.post(
        f"/applications/{application.id}/evidence",
        json={
            "claim_text": "Built a production service",
            "evidence_type": "project",
            "source_url": "https://example.com/project",
            "verification_state": "verified",
            "privacy_level": "public",
        },
    )
    private = client.post(
        f"/applications/{application.id}/evidence", json={"evidence_id": existing.id}
    )
    rejected = client.post(
        f"/applications/{application.id}/evidence",
        json={
            "claim_text": "Rejected claim",
            "evidence_type": "note",
            "verification_state": "rejected",
            "privacy_level": "public",
        },
    )
    listed = client.get(f"/applications/{application.id}/evidence")

    assert public.status_code == 201
    assert public.json()["publishable"] is True
    assert private.status_code == 200
    assert private.json()["publishable"] is False
    assert rejected.status_code == 201
    assert rejected.json()["publishable"] is False
    assert {item["id"] for item in listed.json()} == {
        public.json()["id"],
        existing.id,
        rejected.json()["id"],
    }
    assert client.get(f"/applications/{other.id}/evidence").json() == []
    assert "source_path" not in str(listed.json())


@pytest.mark.parametrize(
    ("verification_state", "privacy_level", "publishable"),
    [
        ("verified", "public", True),
        ("user_stated", "public", False),
        ("needs_verification", "public", False),
        ("rejected", "public", False),
        ("private_do_not_publish", "public", False),
        ("verified", "private", False),
        ("verified", "sensitive", False),
    ],
)
def test_evidence_publishable_requires_verified_public_state(
    tmp_path: Path,
    verification_state: str,
    privacy_level: str,
    publishable: bool,
):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    client = TestClient(create_app(repo, provider_registry={}))

    response = client.post(
        f"/applications/{application.id}/evidence",
        json={
            "claim_text": "Bound publication claim",
            "evidence_type": "note",
            "verification_state": verification_state,
            "privacy_level": privacy_level,
        },
    )

    assert response.status_code == 201
    assert response.json()["publishable"] is publishable


def test_evidence_api_rejects_arbitrary_paths_unknown_fields_and_missing_ids(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    client = TestClient(create_app(repo, provider_registry={}))

    arbitrary_path = client.post(
        f"/applications/{application.id}/evidence",
        json={
            "claim_text": "Claim",
            "evidence_type": "file",
            "source_path": "../../secret.txt",
            "verification_state": "verified",
            "privacy_level": "public",
        },
    )
    missing = client.post(
        f"/applications/{application.id}/evidence", json={"evidence_id": "evd_missing"}
    )
    malformed = client.post(
        f"/applications/{application.id}/evidence", json={"claim_text": "Only a claim"}
    )

    assert arbitrary_path.status_code == 422
    assert missing.status_code == 404
    assert malformed.status_code == 422


def test_application_versions_api_is_application_scoped(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = create_application(repo)
    other = create_application(repo)
    first = repo.record_application_version(application.id, "first", {"sequence": 1})
    second = repo.record_application_version(application.id, "second", {"sequence": 2})
    repo.record_application_version(other.id, "foreign", {"sequence": 3})
    client = TestClient(create_app(repo, provider_registry={}))

    response = client.get(f"/applications/{application.id}/versions")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [first.id, second.id]
    assert client.get("/applications/app_missing/versions").status_code == 404
