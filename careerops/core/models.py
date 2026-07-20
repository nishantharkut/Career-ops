from dataclasses import dataclass
from pathlib import Path
from typing import Any

from careerops.core.status import ApplicationStatus, SubmissionState


JsonDict = dict[str, Any]


@dataclass(frozen=True)
class Application:
    id: str
    company: str
    role_title: str
    location: str
    source_url: str
    source_kind: str
    jd_snapshot_id: str
    status: ApplicationStatus
    selected_resume_version_id: str | None
    latest_report_id: str | None
    approval_id: str | None
    submission_state: SubmissionState
    deadline: str | None
    notes: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class JobDescriptionSnapshot:
    id: str
    application_id: str
    source_url: str
    raw_text_path: Path
    raw_text_sha256: str
    captured_at: str
    extracted_requirements_json: JsonDict
    parser_version: str


@dataclass(frozen=True)
class ResumeVersion:
    id: str
    name: str
    role_family: str
    tex_path: Path
    tex_sha256: str
    pdf_path: Path | None
    pdf_sha256: str | None
    parent_resume_version_id: str | None
    render_status: str
    parse_status: str
    page_count: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Report:
    id: str
    application_id: str
    resume_version_id: str
    jd_snapshot_id: str
    fit: JsonDict
    risks: JsonDict
    blocking_issue_count: int
    warning_count: int
    created_at: str


@dataclass(frozen=True)
class Approval:
    id: str
    application_id: str
    resume_version_id: str
    report_id: str
    pdf_sha256: str
    approved_at: str
    approval_note: str


@dataclass(frozen=True)
class AuditEvent:
    id: str
    event_type: str
    entity_type: str
    entity_id: str
    before_json: JsonDict
    after_json: JsonDict
    created_at: str
