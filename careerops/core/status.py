from enum import StrEnum


class ApplicationStatus(StrEnum):
    DRAFT = "draft"
    ANALYZING = "analyzing"
    NEEDS_EVIDENCE = "needs_evidence"
    NEEDS_RESUME_UPDATE = "needs_resume_update"
    READY_FOR_REVIEW = "ready_for_review"
    APPROVED = "approved"
    SUBMITTED_MANUAL = "submitted_manual"
    ARCHIVED = "archived"


class SubmissionState(StrEnum):
    NOT_SUBMITTED = "not_submitted"
    SUBMITTED_MANUAL = "submitted_manual"
