from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.careerops_api.main import create_app
from careerops.core.repository import CareerOpsRepository


def make_client(tmp_path: Path) -> TestClient:
    repo = CareerOpsRepository(tmp_path / "careerops.db", tmp_path / "vault")
    repo.initialize()
    return TestClient(create_app(repo))


def test_day_one_api_flow_blocks_then_approves_clean_packet(tmp_path: Path):
    client = make_client(tmp_path)

    create_response = client.post(
        "/applications",
        json={
            "company": "Swiggy",
            "role_title": "Data Science Intern",
            "location": "Bangalore",
            "source_url": "https://example.com/swiggy",
            "jd_text": Path("fixtures/sample_jd.txt").read_text(encoding="utf-8"),
        },
    )
    assert create_response.status_code == 201
    application = create_response.json()

    with Path("fixtures/sample_resume.tex").open("rb") as handle:
        resume_response = client.post(
            f"/applications/{application['id']}/resume",
            data={"name": "Swiggy resume", "role_family": "data-science"},
            files={"tex_file": ("sample_resume.tex", handle, "application/x-tex")},
        )
    assert resume_response.status_code == 201
    resume = resume_response.json()

    render_response = client.post(f"/resumes/{resume['id']}/render")
    assert render_response.status_code == 200
    rendered_resume = render_response.json()
    assert rendered_resume["render_status"] == "rendered"
    assert rendered_resume["page_count"] == 1

    risky_report_response = client.post(
        f"/applications/{application['id']}/analyze",
        json={
            "claims": [
                {
                    "text": "Improved extraction accuracy from 50% to 80% across 70+ payer formats.",
                    "verification_state": "needs_verification",
                    "public_safe": True,
                }
            ]
        },
    )
    assert risky_report_response.status_code == 200
    risky_report = risky_report_response.json()
    assert risky_report["blocking_issue_count"] > 0

    blocked_approval = client.post(
        f"/applications/{application['id']}/approve",
        json={"report_id": risky_report["id"], "note": "Trying unsafe approval"},
    )
    assert blocked_approval.status_code == 409

    clean_report_response = client.post(
        f"/applications/{application['id']}/analyze",
        json={
            "claims": [
                {
                    "text": "PocketBuddy -- Amazon HackOn finalist project deployed on AWS.",
                    "verification_state": "user_stated",
                    "public_safe": True,
                }
            ]
        },
    )
    assert clean_report_response.status_code == 200
    clean_report = clean_report_response.json()
    assert clean_report["blocking_issue_count"] == 0

    approval_response = client.post(
        f"/applications/{application['id']}/approve",
        json={"report_id": clean_report["id"], "note": "Reviewed PDF and report"},
    )
    assert approval_response.status_code == 200
    assert approval_response.json()["approval_note"] == "Reviewed PDF and report"

    submitted_response = client.post(
        f"/applications/{application['id']}/mark-submitted",
        json={"note": "Submitted manually through form"},
    )
    assert submitted_response.status_code == 200
    assert submitted_response.json()["status"] == "submitted_manual"


def test_health_endpoint(tmp_path: Path):
    client = make_client(tmp_path)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}
