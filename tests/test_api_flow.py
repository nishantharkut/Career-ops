import hashlib
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from apps.api.careerops_api import main as api_main
from apps.api.careerops_api.main import create_app
from careerops.core.repository import CareerOpsRepository
from careerops.renderers.latex import RenderResult
from tests.ai_approval_helpers import create_ai_approval_report
from tests.pdf_helpers import write_pdf


def make_client(tmp_path: Path) -> TestClient:
    repo = CareerOpsRepository(tmp_path / "careerops.db", tmp_path / "vault")
    repo.initialize()
    return TestClient(create_app(repo))


def make_repo_and_client(tmp_path: Path) -> tuple[CareerOpsRepository, TestClient]:
    repo = CareerOpsRepository(tmp_path / "careerops.db", tmp_path / "vault")
    repo.initialize()
    return repo, TestClient(create_app(repo, provider_registry={}))


def create_reviewable_packet(repo: CareerOpsRepository):
    application = repo.create_application(
        company="Example Corp",
        role_title="Engineer",
        location="Remote",
        source_url="",
        jd_text="Python role",
    )
    source_dir = repo.vault_path / "resumes" / application.id
    source_dir.mkdir()
    tex_path = source_dir / "resume.tex"
    pdf_path = source_dir / "resume.pdf"
    tex_path.write_text("Python developer", encoding="utf-8")
    write_pdf(pdf_path, "Reviewed Python developer resume")
    resume = repo.create_resume_version(
        name="Reviewed resume",
        role_family="software",
        tex_path=tex_path,
        tex_sha256=hashlib.sha256(tex_path.read_bytes()).hexdigest(),
        pdf_path=pdf_path,
        pdf_sha256=hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
        render_status="rendered",
        parse_status="parsed",
        page_count=1,
    )
    repo.select_resume(application.id, resume.id)
    report = create_ai_approval_report(repo, application.id, resume.id)
    return application, resume, report


def test_verifier_endpoint_reports_risks_but_cannot_bypass_ai_approval(tmp_path: Path):
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
    assert clean_report["ai_task_id"] is None

    approval_response = client.post(
        f"/applications/{application['id']}/approve",
        json={"report_id": clean_report["id"], "note": "Reviewed PDF and report"},
    )
    assert approval_response.status_code == 409

    submitted_response = client.post(
        f"/applications/{application['id']}/mark-submitted",
        json={"note": "Submitted manually through form"},
    )
    assert submitted_response.status_code == 409
    packet = client.get(f"/applications/{application['id']}/packet").json()
    assert packet["latest_report"]["id"] == clean_report["id"]
    assert packet["approval_eligibility"] == {
        "eligible": False,
        "reason": "Run AI review for the current JD and resume before approval.",
        "ai_task_id": None,
    }


def test_health_endpoint(tmp_path: Path):
    client = make_client(tmp_path)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_resume_upload_ignores_traversal_filename_and_uses_fixed_owned_name(tmp_path: Path):
    client = make_client(tmp_path)
    application = client.post(
        "/applications",
        json={
            "company": "Example Corp",
            "role_title": "Engineer",
            "jd_text": "Company: Example Corp\nTitle: Engineer",
        },
    ).json()
    outside = tmp_path / "outside.tex"

    response = client.post(
        f"/applications/{application['id']}/resume",
        data={"name": "Resume", "role_family": "software"},
        files={"tex_file": ("../../outside.tex", b"valid latex", "application/x-tex")},
    )

    assert response.status_code == 201
    assert not outside.exists()
    uploaded = list((tmp_path / "vault" / "resumes").glob("upload_*/resume.tex"))
    assert len(uploaded) == 1
    assert uploaded[0].read_bytes() == b"valid latex"
    assert "tex_path" not in response.json()


@pytest.mark.parametrize(
    ("content", "expected_status"),
    [
        (b"x" * 2_000_001, 413),
        (b"\\documentclass{article}\n\xff", 422),
    ],
    ids=["oversize", "invalid-utf8"],
)
def test_resume_upload_rejects_oversize_or_invalid_utf8_and_cleans_partial_directory(
    tmp_path: Path, content: bytes, expected_status: int
):
    client = make_client(tmp_path)
    application = client.post(
        "/applications",
        json={
            "company": "Example Corp",
            "role_title": "Engineer",
            "jd_text": "Company: Example Corp\nTitle: Engineer",
        },
    ).json()
    resume_root = tmp_path / "vault" / "resumes"
    before = set(resume_root.iterdir())

    response = client.post(
        f"/applications/{application['id']}/resume",
        data={"name": "Resume", "role_family": "software"},
        files={"tex_file": ("resume.tex", content, "application/x-tex")},
    )

    assert response.status_code == expected_status
    assert set(resume_root.iterdir()) == before


def test_render_rejects_pdf_without_extractable_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo, client = make_repo_and_client(tmp_path)
    application = client.post(
        "/applications",
        json={
            "company": "Example Corp",
            "role_title": "Engineer",
            "jd_text": "Python role",
        },
    ).json()
    resume = client.post(
        f"/applications/{application['id']}/resume",
        data={"name": "Resume", "role_family": "software"},
        files={"tex_file": ("resume.tex", b"valid latex", "application/x-tex")},
    ).json()

    def render_blank(_source: Path, output_dir: Path) -> RenderResult:
        pdf_path = output_dir / "resume.pdf"
        content = write_pdf(pdf_path, None)
        log_path = output_dir / "render.log"
        log_path.write_text("fixture", encoding="utf-8")
        return RenderResult(
            True,
            pdf_path,
            log_path,
            hashlib.sha256(content).hexdigest(),
            "",
            "fixture",
        )

    monkeypatch.setattr(api_main, "render_latex", render_blank)

    response = client.post(f"/resumes/{resume['id']}/render")

    assert response.status_code == 200
    assert response.json()["render_status"] == "failed"
    assert response.json()["parse_status"] == "not_parsed"
    assert response.json()["pdf_sha256"] is None
    assert repo.get_resume_version(resume["id"]).pdf_path is None
    attempts = list(
        (repo.vault_path / "resumes" / resume["id"] / "render").glob("attempt_*")
    )
    assert len(attempts) == 1
    assert (attempts[0] / "render.log").read_text(encoding="utf-8") == "fixture"


def test_rerender_of_approved_resume_is_rejected_before_pdf_bytes_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = CareerOpsRepository(tmp_path / "careerops.db", tmp_path / "vault")
    repo.initialize()
    client = TestClient(create_app(repo, provider_registry={}), raise_server_exceptions=False)
    application = client.post(
        "/applications",
        json={
            "company": "Example Corp",
            "role_title": "Engineer",
            "jd_text": "Python role",
        },
    ).json()
    resume = client.post(
        f"/applications/{application['id']}/resume",
        data={"name": "Resume", "role_family": "software"},
        files={"tex_file": ("resume.tex", b"valid latex", "application/x-tex")},
    ).json()

    def render_text(_source: Path, output_dir: Path) -> RenderResult:
        pdf_path = output_dir / "resume.pdf"
        content = write_pdf(pdf_path, "Approved resume content")
        log_path = output_dir / "render.log"
        log_path.write_text("fixture", encoding="utf-8")
        return RenderResult(
            True,
            pdf_path,
            log_path,
            hashlib.sha256(content).hexdigest(),
            "",
            "fixture",
        )

    monkeypatch.setattr(api_main, "render_latex", render_text)
    rendered = client.post(f"/resumes/{resume['id']}/render")
    assert rendered.status_code == 200
    current = repo.get_resume_version(resume["id"])
    assert current.pdf_path is not None
    approved_bytes = current.pdf_path.read_bytes()
    report = create_ai_approval_report(repo, application["id"], resume["id"])
    repo.approve_application(application["id"], report.id, "Reviewed")
    renderer_called = False

    def overwrite_pdf(_source: Path, output_dir: Path) -> RenderResult:
        nonlocal renderer_called
        renderer_called = True
        pdf_path = output_dir / "resume.pdf"
        content = write_pdf(pdf_path, "Changed after approval")
        log_path = output_dir / "render.log"
        return RenderResult(
            True,
            pdf_path,
            log_path,
            hashlib.sha256(content).hexdigest(),
            "",
            "fixture",
        )

    monkeypatch.setattr(api_main, "render_latex", overwrite_pdf)

    response = client.post(f"/resumes/{resume['id']}/render")

    assert response.status_code == 409
    assert renderer_called is False
    assert current.pdf_path.read_bytes() == approved_bytes


@pytest.mark.parametrize("terminal_state", ["approved", "submitted"])
def test_resume_upload_cannot_mutate_approved_or_submitted_packet(
    tmp_path: Path, terminal_state: str
):
    repo, client = make_repo_and_client(tmp_path)
    application, _resume, report = create_reviewable_packet(repo)
    repo.approve_application(application.id, report.id, "Reviewed")
    if terminal_state == "submitted":
        repo.mark_submitted_manual(application.id, "Submitted")
    before_packet = client.get(f"/applications/{application.id}/packet").json()
    before_resumes = repo.list_resume_versions()
    resume_root = repo.vault_path / "resumes"
    before_directories = set(resume_root.iterdir())

    response = client.post(
        f"/applications/{application.id}/resume",
        data={"name": "Forbidden replacement", "role_family": "software"},
        files={"tex_file": ("resume.tex", b"replacement", "application/x-tex")},
    )

    assert response.status_code == 409
    assert client.get(f"/applications/{application.id}/packet").json() == before_packet
    assert repo.list_resume_versions() == before_resumes
    assert set(resume_root.iterdir()) == before_directories


def test_resume_upload_cleans_file_when_application_becomes_approved_before_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo, client = make_repo_and_client(tmp_path)
    application, selected, report = create_reviewable_packet(repo)
    original_atomic = getattr(repo, "create_and_select_resume_version", None)

    def approve_then_attempt(**values):
        repo.approve_application(application.id, report.id, "Concurrent approval")
        if original_atomic is None:
            raise ValueError("atomic upload method is missing")
        return original_atomic(**values)

    monkeypatch.setattr(
        repo, "create_and_select_resume_version", approve_then_attempt, raising=False
    )
    before_resume_ids = [item.id for item in repo.list_resume_versions()]
    resume_root = repo.vault_path / "resumes"
    before_directories = set(resume_root.iterdir())

    response = client.post(
        f"/applications/{application.id}/resume",
        data={"name": "Racing replacement", "role_family": "software"},
        files={"tex_file": ("resume.tex", b"replacement", "application/x-tex")},
    )

    assert response.status_code == 409
    assert [item.id for item in repo.list_resume_versions()] == before_resume_ids
    assert set(resume_root.iterdir()) == before_directories
    after = repo.get_application(application.id)
    assert after.approval_id is not None
    assert after.selected_resume_version_id == selected.id


def test_resume_upload_success_does_not_depend_on_post_commit_public_getter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo, client = make_repo_and_client(tmp_path)
    application = repo.create_application(
        company="Example Corp",
        role_title="Engineer",
        location="Remote",
        source_url="",
        jd_text="Python role",
    )

    def fail_public_reload(_resume_version_id: str):
        raise RuntimeError("post-commit resume reload must not run")

    monkeypatch.setattr(repo, "get_resume_version", fail_public_reload)

    response = client.post(
        f"/applications/{application.id}/resume",
        data={"name": "Atomic resume", "role_family": "software"},
        files={"tex_file": ("resume.tex", b"valid latex", "application/x-tex")},
    )

    assert response.status_code == 201
    verifier = CareerOpsRepository(repo.database_path, repo.vault_path)
    selected_id = verifier.get_application(application.id).selected_resume_version_id
    assert selected_id == response.json()["id"]
    selected = verifier.get_resume_version(selected_id)
    assert selected.tex_path.is_file()
    assert selected.tex_path.read_bytes() == b"valid latex"
    assert selected.tex_sha256 == hashlib.sha256(b"valid latex").hexdigest()


@pytest.mark.parametrize(
    "claims",
    [
        [
            {
                "text": "Claim",
                "verification_state": "verified",
                "public_safe": True,
                "unknown": "field",
            }
        ],
        [{"text": {"nested": "claim"}, "verification_state": "verified", "public_safe": True}],
        [{"text": "x" * 4_001, "verification_state": "verified", "public_safe": True}],
        [{"text": "Claim", "verification_state": "invented", "public_safe": True}],
        [{"text": "Claim", "verification_state": "verified", "public_safe": 1}],
        [
            {"text": "Claim", "verification_state": "verified", "public_safe": True}
            for _ in range(101)
        ],
    ],
    ids=["unknown", "nested", "huge", "enum", "non-bool", "too-many"],
)
def test_analyze_claims_reject_unknown_nested_unbounded_or_coerced_values(
    tmp_path: Path, claims: list[object]
):
    client = make_client(tmp_path)

    response = client.post("/applications/app_missing/analyze", json={"claims": claims})

    assert response.status_code == 422
