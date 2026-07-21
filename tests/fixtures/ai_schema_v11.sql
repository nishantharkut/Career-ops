ALTER TABLE ai_provider_configs
ADD COLUMN identity_name TEXT NOT NULL DEFAULT '';

CREATE TRIGGER validate_codex_identity_insert
BEFORE INSERT ON ai_provider_configs
WHEN NEW.provider_kind = 'codex'
 AND (
    NEW.identity_name NOT IN ('codex2', 'codex')
    OR NEW.endpoint != ('codex-identity://' || NEW.identity_name)
 )
BEGIN
    SELECT RAISE(ABORT, 'invalid Codex identity binding');
END;

CREATE TRIGGER validate_codex_identity_update
BEFORE UPDATE ON ai_provider_configs
WHEN NEW.provider_kind = 'codex'
 AND (
    NEW.identity_name NOT IN ('codex2', 'codex')
    OR NEW.endpoint != ('codex-identity://' || NEW.identity_name)
 )
BEGIN
    SELECT RAISE(ABORT, 'invalid Codex identity binding');
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

PRAGMA user_version = 11;
