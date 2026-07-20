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
    role_type: str = ""
    submitted_at: str | None = None


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


@dataclass(frozen=True)
class AiProviderConfig:
    id: str
    provider_kind: str
    display_name: str
    enabled: bool
    privacy_mode: str
    endpoint: str
    model: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AiTask:
    id: str
    application_id: str
    task_type: str
    provider_config_id: str
    prompt_template_id: str
    input_artifact_ids: list[str]
    status: str
    privacy_mode: str
    started_at: str | None
    completed_at: str | None
    error_summary: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AiArtifact:
    id: str
    task_id: str
    artifact_type: str
    path: Path
    sha256: str
    schema_version: str
    created_at: str


@dataclass(frozen=True)
class AiFinding:
    id: str
    task_id: str
    finding_type: str
    severity: str
    claim_text: str
    evidence_item_id: str | None
    recommendation: str
    verifier_state: str
    decision: str
    payload: JsonDict
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class EvidenceItem:
    id: str
    claim_text: str
    evidence_type: str
    source_path: Path | None
    source_url: str | None
    source_sha256: str | None
    verification_state: str
    privacy_level: str
    notes: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ProfileFact:
    id: str
    fact_key: str
    fact_value: str
    verification_state: str
    privacy_level: str
    notes: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ApplicationVersion:
    id: str
    application_id: str
    version_number: int
    event_type: str
    snapshot: JsonDict
    created_at: str
