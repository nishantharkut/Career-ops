from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.careerops_api.main import create_app
from careerops.core.repository import CareerOpsRepository


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
