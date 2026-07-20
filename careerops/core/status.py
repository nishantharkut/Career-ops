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


class AiTaskType(StrEnum):
    ANALYZE_JD = "analyze_jd"
    ANALYZE_FIT = "analyze_fit"
    SUGGEST_RESUME_EDITS = "suggest_resume_edits"
    DRAFT_COVER_LETTER = "draft_cover_letter"
    REVIEW_CLAIMS = "review_claims"


class PrivacyMode(StrEnum):
    LOCAL_PRIVATE = "local_private"
    TRUSTED_PRIVATE = "trusted_private"
    CLOUD_EXTERNAL = "cloud_external"
    VERIFIER_ONLY = "verifier_only"


class AiTaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class FindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


class VerifierState(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    NEEDS_EVIDENCE = "needs_evidence"
    BLOCKED = "blocked"
    REJECTED = "rejected"


class VerificationState(StrEnum):
    VERIFIED = "verified"
    USER_STATED = "user_stated"
    NEEDS_VERIFICATION = "needs_verification"
    REJECTED = "rejected"
    PRIVATE_DO_NOT_PUBLISH = "private_do_not_publish"


class PrivacyLevel(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    SENSITIVE = "sensitive"


class FindingDecision(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
