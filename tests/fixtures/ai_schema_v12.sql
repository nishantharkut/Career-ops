ALTER TABLE applications ADD COLUMN role_type TEXT NOT NULL DEFAULT '';
ALTER TABLE applications ADD COLUMN submitted_at TEXT;

CREATE TABLE ai_provider_configs (
    id TEXT PRIMARY KEY,
    provider_kind TEXT NOT NULL,
    display_name TEXT NOT NULL,
    identity_name TEXT NOT NULL DEFAULT '',
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
    invocation_started_at TEXT,
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

CREATE TABLE profile_facts (
    id TEXT PRIMARY KEY,
    fact_key TEXT NOT NULL,
    fact_value TEXT NOT NULL,
    verification_state TEXT NOT NULL,
    privacy_level TEXT NOT NULL,
    notes TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE application_evidence_links (
    application_id TEXT NOT NULL REFERENCES applications(id),
    evidence_item_id TEXT NOT NULL REFERENCES evidence_items(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(application_id, evidence_item_id)
);

CREATE INDEX idx_ai_tasks_application ON ai_tasks(application_id, created_at);
CREATE INDEX idx_ai_tasks_provider ON ai_tasks(provider_config_id);
CREATE INDEX idx_ai_tasks_status ON ai_tasks(status, created_at);
CREATE INDEX idx_ai_artifacts_task ON ai_artifacts(task_id, created_at);
CREATE INDEX idx_ai_artifacts_claim ON ai_artifacts(task_id, claim_token);
CREATE INDEX idx_ai_findings_task ON ai_findings(task_id, created_at);
CREATE INDEX idx_ai_findings_claim ON ai_findings(task_id, claim_token);
CREATE INDEX idx_ai_findings_evidence ON ai_findings(evidence_item_id);
CREATE INDEX idx_profile_facts_key ON profile_facts(fact_key, created_at);
CREATE INDEX idx_application_evidence_item
ON application_evidence_links(evidence_item_id, created_at);
CREATE INDEX idx_ai_task_input_artifact
ON ai_task_input_artifacts(artifact_id, task_id);

CREATE TRIGGER validate_ai_artifacts_claim_token
BEFORE INSERT ON ai_artifacts
WHEN NEW.claim_token IS NULL OR NEW.claim_token = ''
BEGIN
    SELECT RAISE(ABORT, 'missing AI attempt claim token');
END;

CREATE TRIGGER validate_ai_findings_claim_token
BEFORE INSERT ON ai_findings
WHEN NEW.claim_token IS NULL OR NEW.claim_token = ''
BEGIN
    SELECT RAISE(ABORT, 'missing AI attempt claim token');
END;

CREATE TRIGGER validate_ai_task_input_application_insert
BEFORE INSERT ON ai_task_input_artifacts
WHEN NOT EXISTS (
    SELECT 1
    FROM ai_tasks AS target
    JOIN ai_artifacts AS artifact ON artifact.id = NEW.artifact_id
    JOIN ai_tasks AS owner ON owner.id = artifact.task_id
    WHERE target.id = NEW.task_id
      AND target.application_id = owner.application_id
)
BEGIN
    SELECT RAISE(ABORT, 'input artifact application mismatch');
END;

CREATE TRIGGER validate_ai_task_input_application_update
BEFORE UPDATE ON ai_task_input_artifacts
WHEN NOT EXISTS (
    SELECT 1
    FROM ai_tasks AS target
    JOIN ai_artifacts AS artifact ON artifact.id = NEW.artifact_id
    JOIN ai_tasks AS owner ON owner.id = artifact.task_id
    WHERE target.id = NEW.task_id
      AND target.application_id = owner.application_id
)
BEGIN
    SELECT RAISE(ABORT, 'input artifact application mismatch');
END;

CREATE TRIGGER validate_ai_provider_configs_insert_states
BEFORE INSERT ON ai_provider_configs
WHEN NEW.enabled NOT IN (0, 1)
  OR NEW.privacy_mode NOT IN (
    'local_private', 'trusted_private', 'cloud_external', 'verifier_only'
  )
BEGIN SELECT RAISE(ABORT, 'invalid finite state'); END;

CREATE TRIGGER validate_ai_provider_configs_update_states
BEFORE UPDATE ON ai_provider_configs
WHEN NEW.enabled NOT IN (0, 1)
  OR NEW.privacy_mode NOT IN (
    'local_private', 'trusted_private', 'cloud_external', 'verifier_only'
  )
BEGIN SELECT RAISE(ABORT, 'invalid finite state'); END;

CREATE TRIGGER validate_ai_tasks_insert_states
BEFORE INSERT ON ai_tasks
WHEN NEW.task_type NOT IN (
    'analyze_jd', 'analyze_fit', 'suggest_resume_edits',
    'draft_cover_letter', 'review_claims'
  )
  OR NEW.status NOT IN ('pending', 'running', 'completed', 'blocked', 'failed')
  OR NEW.privacy_mode NOT IN (
    'local_private', 'trusted_private', 'cloud_external', 'verifier_only'
  )
BEGIN SELECT RAISE(ABORT, 'invalid finite state'); END;

CREATE TRIGGER validate_ai_tasks_update_states
BEFORE UPDATE ON ai_tasks
WHEN NEW.task_type NOT IN (
    'analyze_jd', 'analyze_fit', 'suggest_resume_edits',
    'draft_cover_letter', 'review_claims'
  )
  OR NEW.status NOT IN ('pending', 'running', 'completed', 'blocked', 'failed')
  OR NEW.privacy_mode NOT IN (
    'local_private', 'trusted_private', 'cloud_external', 'verifier_only'
  )
BEGIN SELECT RAISE(ABORT, 'invalid finite state'); END;

CREATE TRIGGER validate_ai_findings_insert_states
BEFORE INSERT ON ai_findings
WHEN NEW.severity NOT IN ('info', 'warning', 'blocking')
  OR NEW.verifier_state NOT IN (
    'pending', 'verified', 'needs_evidence', 'blocked', 'rejected'
  )
  OR NEW.decision NOT IN ('pending', 'accepted', 'rejected')
BEGIN SELECT RAISE(ABORT, 'invalid finite state'); END;

CREATE TRIGGER validate_ai_findings_update_states
BEFORE UPDATE ON ai_findings
WHEN NEW.severity NOT IN ('info', 'warning', 'blocking')
  OR NEW.verifier_state NOT IN (
    'pending', 'verified', 'needs_evidence', 'blocked', 'rejected'
  )
  OR NEW.decision NOT IN ('pending', 'accepted', 'rejected')
BEGIN SELECT RAISE(ABORT, 'invalid finite state'); END;

CREATE TRIGGER validate_profile_facts_insert_states
BEFORE INSERT ON profile_facts
WHEN NEW.verification_state NOT IN (
    'verified', 'user_stated', 'needs_verification',
    'rejected', 'private_do_not_publish'
  )
  OR NEW.privacy_level NOT IN ('public', 'private', 'sensitive')
BEGIN SELECT RAISE(ABORT, 'invalid finite state'); END;

CREATE TRIGGER validate_profile_facts_update_states
BEFORE UPDATE ON profile_facts
WHEN NEW.verification_state NOT IN (
    'verified', 'user_stated', 'needs_verification',
    'rejected', 'private_do_not_publish'
  )
  OR NEW.privacy_level NOT IN ('public', 'private', 'sensitive')
BEGIN SELECT RAISE(ABORT, 'invalid finite state'); END;

CREATE TRIGGER validate_evidence_items_insert_states
BEFORE INSERT ON evidence_items
WHEN NEW.verification_state NOT IN (
    'verified', 'user_stated', 'needs_verification',
    'rejected', 'private_do_not_publish'
  )
  OR NEW.privacy_level NOT IN ('public', 'private', 'sensitive')
BEGIN SELECT RAISE(ABORT, 'invalid finite state'); END;

CREATE TRIGGER validate_evidence_items_update_states
BEFORE UPDATE ON evidence_items
WHEN NEW.verification_state NOT IN (
    'verified', 'user_stated', 'needs_verification',
    'rejected', 'private_do_not_publish'
  )
  OR NEW.privacy_level NOT IN ('public', 'private', 'sensitive')
BEGIN SELECT RAISE(ABORT, 'invalid finite state'); END;

CREATE TRIGGER validate_provider_identity_insert
BEFORE INSERT ON ai_provider_configs
WHEN (NEW.provider_kind != 'codex' AND NEW.identity_name != '')
 OR (
    NEW.provider_kind = 'codex'
    AND NOT (
        (NEW.identity_name = 'codex2' AND NEW.endpoint = 'codex-identity://codex2')
        OR (NEW.identity_name = 'codex' AND NEW.endpoint = 'codex-identity://codex')
        OR (
            NEW.enabled = 0
            AND NEW.identity_name = ''
            AND NEW.endpoint NOT IN (
                'codex-identity://codex2', 'codex-identity://codex'
            )
        )
    )
 )
BEGIN
    SELECT RAISE(ABORT, 'invalid provider identity binding');
END;

CREATE TRIGGER validate_provider_identity_update
BEFORE UPDATE ON ai_provider_configs
WHEN (NEW.provider_kind != 'codex' AND NEW.identity_name != '')
 OR (
    NEW.provider_kind = 'codex'
    AND NOT (
        (NEW.identity_name = 'codex2' AND NEW.endpoint = 'codex-identity://codex2')
        OR (NEW.identity_name = 'codex' AND NEW.endpoint = 'codex-identity://codex')
        OR (
            NEW.enabled = 0
            AND NEW.identity_name = ''
            AND NEW.endpoint NOT IN (
                'codex-identity://codex2', 'codex-identity://codex'
            )
        )
    )
 )
BEGIN
    SELECT RAISE(ABORT, 'invalid provider identity binding');
END;

CREATE TRIGGER validate_provider_binding_immutable
BEFORE UPDATE ON ai_provider_configs
WHEN NEW.provider_kind != OLD.provider_kind
  OR NEW.endpoint != OLD.endpoint
  OR NEW.model != OLD.model
  OR NEW.identity_name != OLD.identity_name
BEGIN
    SELECT RAISE(ABORT, 'provider configuration binding is immutable');
END;

PRAGMA user_version = 12;
