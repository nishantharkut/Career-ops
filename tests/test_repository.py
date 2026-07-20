from pathlib import Path

from careerops.core.repository import CareerOpsRepository
from careerops.core.status import ApplicationStatus


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
    first_resume = repo.create_resume_version(
        name="Google resume",
        role_family="software",
        tex_path=tmp_path / "google.tex",
        tex_sha256="b" * 64,
        pdf_sha256="c" * 64,
    )
    second_resume = repo.create_resume_version(
        name="Systems resume",
        role_family="software",
        tex_path=tmp_path / "systems.tex",
        tex_sha256="d" * 64,
        pdf_sha256="e" * 64,
    )

    repo.select_resume(application.id, first_resume.id)
    report = repo.save_report(
        application_id=application.id,
        resume_version_id=first_resume.id,
        jd_snapshot_id=application.jd_snapshot_id,
        fit={"score": 88, "matched_requirements": ["C++"]},
        risks={"blockers": [], "warnings": []},
    )
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
