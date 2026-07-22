import hashlib
from pathlib import Path

import pytest

from careerops.core.repository import CareerOpsRepository
from careerops.core.status import ApplicationStatus
from tests.ai_approval_helpers import create_ai_approval_report
from tests.pdf_helpers import write_pdf


def make_repo(tmp_path: Path) -> CareerOpsRepository:
    repo = CareerOpsRepository(tmp_path / "careerops.db", tmp_path / "vault")
    repo.initialize()
    return repo


def test_create_application_persists_jd_snapshot(tmp_path: Path):
    repo = make_repo(tmp_path)

    application = repo.create_application(
        company="Goldman Sachs",
        role_title="Engineering Summer Analyst",
        location="Bengaluru",
        source_url="https://example.com/goldman-role",
        jd_text="Responsibilities include scalable systems, Python, C++, and data platforms.",
    )

    snapshot = repo.get_jd_snapshot(application.jd_snapshot_id)

    assert application.status == ApplicationStatus.DRAFT
    assert snapshot.application_id == application.id
    assert snapshot.raw_text_path.exists()
    assert snapshot.raw_text_path.read_text(encoding="utf-8").startswith("Responsibilities")
    assert len(snapshot.raw_text_sha256) == 64


def test_resume_versions_are_separate_from_applications(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = repo.create_application(
        company="Swiggy",
        role_title="Data Science Intern",
        location="Bangalore",
        source_url="https://example.com/swiggy",
        jd_text="Python SQL machine learning analytics internship.",
    )

    resume = repo.create_resume_version(
        name="Swiggy DS resume",
        role_family="data-science",
        tex_path=tmp_path / "resume.tex",
        tex_sha256="a" * 64,
    )
    updated = repo.select_resume(application.id, resume.id)

    assert updated.selected_resume_version_id == resume.id
    assert repo.get_resume_version(resume.id).name == "Swiggy DS resume"
    assert repo.list_resume_versions()[0].id == resume.id


def test_selecting_new_resume_invalidates_existing_approval(tmp_path: Path):
    repo = make_repo(tmp_path)
    application = repo.create_application(
        company="Google",
        role_title="Software Engineering Intern",
        location="India",
        source_url="https://example.com/google",
        jd_text="C++ systems internship.",
    )
    first_pdf = repo.vault_path / "resumes" / "google.pdf"
    write_pdf(first_pdf, "Google systems resume")
    first_resume = repo.create_resume_version(
        name="Google resume",
        role_family="software",
        tex_path=tmp_path / "google.tex",
        tex_sha256="b" * 64,
        pdf_path=first_pdf,
        pdf_sha256=hashlib.sha256(first_pdf.read_bytes()).hexdigest(),
    )
    second_resume = repo.create_resume_version(
        name="Systems resume",
        role_family="software",
        tex_path=tmp_path / "systems.tex",
        tex_sha256="d" * 64,
        pdf_sha256="e" * 64,
    )

    repo.select_resume(application.id, first_resume.id)
    report = create_ai_approval_report(repo, application.id, first_resume.id)
    approval = repo.approve_application(application.id, report.id, "Reviewed manually")

    assert repo.get_application(application.id).approval_id == approval.id

    updated = repo.select_resume(application.id, second_resume.id)

    assert updated.selected_resume_version_id == second_resume.id
    assert updated.approval_id is None
    assert updated.status == ApplicationStatus.NEEDS_RESUME_UPDATE


def test_repository_writes_audit_events(tmp_path: Path):
    repo = make_repo(tmp_path)

    application = repo.create_application(
        company="QuickIntell",
        role_title="AI Engineering Intern",
        location="Remote",
        source_url="",
        jd_text="Healthcare automation internship.",
    )

    events = repo.list_audit_events()

    assert events[0].entity_id == application.id
    assert events[0].entity_type == "application"
    assert events[0].event_type == "application.created"


def test_atomic_resume_create_and_select_rolls_back_every_row_on_audit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path)
    application = repo.create_application(
        company="Example Corp",
        role_title="Engineer",
        location="Remote",
        source_url="",
        jd_text="Python role",
    )
    source_dir = repo.vault_path / "resumes" / "upload_atomic"
    source_dir.mkdir()
    tex_path = source_dir / "resume.tex"
    tex_path.write_text("Python developer", encoding="utf-8")
    before_application = repo.get_application(application.id)
    before_resume_ids = [item.id for item in repo.list_resume_versions()]
    original_write_audit = repo._write_audit

    def fail_after_selection(connection, event_type, *args, **kwargs):
        if event_type == "application.resume_selected":
            raise RuntimeError("injected selection audit failure")
        return original_write_audit(connection, event_type, *args, **kwargs)

    monkeypatch.setattr(repo, "_write_audit", fail_after_selection)

    with pytest.raises(RuntimeError, match="selection audit failure"):
        repo.create_and_select_resume_version(
            application_id=application.id,
            name="Atomic resume",
            role_family="software",
            tex_path=tex_path,
            tex_sha256=hashlib.sha256(tex_path.read_bytes()).hexdigest(),
        )

    assert repo.get_application(application.id) == before_application
    assert [item.id for item in repo.list_resume_versions()] == before_resume_ids


def test_atomic_resume_create_and_select_rejects_approved_application_without_insert(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    application = repo.create_application(
        company="Example Corp",
        role_title="Engineer",
        location="Remote",
        source_url="",
        jd_text="Python role",
    )
    pdf_path = repo.vault_path / "resumes" / "approved.pdf"
    write_pdf(pdf_path, "Approved resume")
    selected = repo.create_resume_version(
        name="Approved resume",
        role_family="software",
        tex_path=tmp_path / "approved.tex",
        tex_sha256="a" * 64,
        pdf_path=pdf_path,
        pdf_sha256=hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
    )
    repo.select_resume(application.id, selected.id)
    report = create_ai_approval_report(repo, application.id, selected.id)
    repo.approve_application(application.id, report.id, "Reviewed")
    tex_path = repo.vault_path / "resumes" / "rejected-upload.tex"
    tex_path.write_text("Replacement", encoding="utf-8")
    before = repo.list_resume_versions()

    with pytest.raises(ValueError, match="approved or submitted"):
        repo.create_and_select_resume_version(
            application_id=application.id,
            name="Replacement",
            role_family="software",
            tex_path=tex_path,
            tex_sha256=hashlib.sha256(tex_path.read_bytes()).hexdigest(),
        )

    assert repo.list_resume_versions() == before
    assert repo.get_application(application.id).approval_id is not None
