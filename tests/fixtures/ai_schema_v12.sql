DROP TRIGGER IF EXISTS validate_codex_identity_insert;
DROP TRIGGER IF EXISTS validate_codex_identity_update;
DROP TRIGGER IF EXISTS validate_provider_binding_immutable;

UPDATE ai_tasks
SET invocation_started_at = COALESCE(started_at, updated_at, created_at)
WHERE status = 'running' AND invocation_started_at IS NULL;

UPDATE ai_provider_configs
SET identity_name = ''
WHERE provider_kind != 'codex';

UPDATE ai_provider_configs
SET identity_name = 'codex2'
WHERE provider_kind = 'codex'
  AND endpoint = 'codex-identity://codex2';

UPDATE ai_provider_configs
SET identity_name = 'codex'
WHERE provider_kind = 'codex'
  AND endpoint = 'codex-identity://codex';

UPDATE ai_provider_configs
SET enabled = 0, identity_name = ''
WHERE provider_kind = 'codex'
  AND endpoint NOT IN ('codex-identity://codex2', 'codex-identity://codex');

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
