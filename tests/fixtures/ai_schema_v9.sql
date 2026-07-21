CREATE TABLE ai_provider_configs (
    id TEXT PRIMARY KEY,
    provider_kind TEXT NOT NULL,
    display_name TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    privacy_mode TEXT NOT NULL CHECK (
        privacy_mode IN (
            'local_private', 'trusted_private', 'cloud_external', 'verifier_only'
        )
    ),
    endpoint TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(provider_kind, endpoint, model)
);

CREATE TABLE ai_tasks (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id),
    task_type TEXT NOT NULL CHECK (
        task_type IN (
            'analyze_jd', 'analyze_fit', 'suggest_resume_edits',
            'draft_cover_letter', 'review_claims'
        )
    ),
    provider_config_id TEXT NOT NULL REFERENCES ai_provider_configs(id),
    prompt_template_id TEXT NOT NULL,
    input_artifact_ids_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'completed', 'blocked', 'failed')
    ),
    privacy_mode TEXT NOT NULL CHECK (
        privacy_mode IN (
            'local_private', 'trusted_private', 'cloud_external', 'verifier_only'
        )
    ),
    started_at TEXT,
    completed_at TEXT,
    error_summary TEXT NOT NULL,
    claim_token TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE ai_artifacts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES ai_tasks(id),
    claim_token TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE ai_task_input_artifacts (
    task_id TEXT NOT NULL REFERENCES ai_tasks(id),
    artifact_id TEXT NOT NULL REFERENCES ai_artifacts(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at TEXT NOT NULL,
    UNIQUE(task_id, ordinal),
    UNIQUE(task_id, artifact_id)
);

CREATE TABLE ai_findings (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES ai_tasks(id),
    claim_token TEXT NOT NULL,
    finding_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    claim_text TEXT NOT NULL,
    evidence_item_id TEXT REFERENCES evidence_items(id),
    recommendation TEXT NOT NULL,
    verifier_state TEXT NOT NULL,
    decision TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE application_versions (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id),
    version_number INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(application_id, version_number)
);

PRAGMA user_version = 9;
