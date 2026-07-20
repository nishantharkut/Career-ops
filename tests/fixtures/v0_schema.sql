PRAGMA foreign_keys = ON;
PRAGMA user_version = 0;

CREATE TABLE applications (
    id TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    role_title TEXT NOT NULL,
    location TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    jd_snapshot_id TEXT NOT NULL,
    status TEXT NOT NULL,
    selected_resume_version_id TEXT,
    latest_report_id TEXT,
    approval_id TEXT,
    submission_state TEXT NOT NULL,
    deadline TEXT,
    notes TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE jd_snapshots (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    raw_text_path TEXT NOT NULL,
    raw_text_sha256 TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    extracted_requirements_json TEXT NOT NULL,
    parser_version TEXT NOT NULL
);

CREATE TABLE resume_versions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role_family TEXT NOT NULL,
    tex_path TEXT NOT NULL,
    tex_sha256 TEXT NOT NULL,
    pdf_path TEXT,
    pdf_sha256 TEXT,
    parent_resume_version_id TEXT,
    render_status TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    page_count INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE evidence_items (
    id TEXT PRIMARY KEY,
    claim_text TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    source_path TEXT,
    source_url TEXT,
    source_sha256 TEXT,
    verification_state TEXT NOT NULL,
    privacy_level TEXT NOT NULL,
    notes TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE reports (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    resume_version_id TEXT NOT NULL,
    jd_snapshot_id TEXT NOT NULL,
    fit_json TEXT NOT NULL,
    risks_json TEXT NOT NULL,
    blocking_issue_count INTEGER NOT NULL,
    warning_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE approvals (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    resume_version_id TEXT NOT NULL,
    report_id TEXT NOT NULL,
    pdf_sha256 TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    approval_note TEXT NOT NULL
);

CREATE TABLE audit_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

INSERT INTO applications (
    id, company, role_title, location, source_url, source_kind,
    jd_snapshot_id, status, selected_resume_version_id, latest_report_id,
    approval_id, submission_state, deadline, notes, created_at, updated_at
) VALUES (
    'app_v0', 'Legacy Corp', 'Engineer', 'Remote', '', 'manual',
    'jd_v0', 'approved', 'res_v0', 'rep_v0', 'apr_v0', 'not_submitted',
    NULL, 'legacy notes', '2026-07-20T00:00:00+00:00', '2026-07-20T00:00:00+00:00'
);

INSERT INTO reports (
    id, application_id, resume_version_id, jd_snapshot_id, fit_json,
    risks_json, blocking_issue_count, warning_count, created_at
) VALUES (
    'rep_v0', 'app_v0', 'res_v0', 'jd_v0', '{"score": 90}',
    '{"blockers": [], "warnings": []}', 0, 0, '2026-07-20T00:00:00+00:00'
);

INSERT INTO approvals (
    id, application_id, resume_version_id, report_id, pdf_sha256,
    approved_at, approval_note
) VALUES (
    'apr_v0', 'app_v0', 'res_v0', 'rep_v0',
    'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
    '2026-07-20T00:00:00+00:00', 'legacy approval'
);

INSERT INTO audit_events (
    id, event_type, entity_type, entity_id, before_json, after_json, created_at
) VALUES (
    'aud_v0', 'application.created', 'application', 'app_v0', '{}',
    '{"company": "Legacy Corp"}', '2026-07-20T00:00:00+00:00'
);
