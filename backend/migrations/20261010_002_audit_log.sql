-- Apply manually before enabling audited administrative writes.
-- Independent IDs deliberately have no foreign keys or erasure triggers.
-- DB administrators can alter/disable triggers; this is not tamper-proof
-- storage against privileged database administrators.
BEGIN;

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    actor_user_id TEXT NOT NULL,
    actor_role TEXT NOT NULL CHECK (actor_role IN ('OWNER', 'MODERATOR', 'CUSTOMER')),
    action TEXT NOT NULL CHECK (action IN (
        'ROLE_CHANGED', 'PERMISSION_GRANTED', 'PERMISSION_REVOKED'
    )),
    target_type TEXT NOT NULL CHECK (target_type IN ('USER', 'MODERATOR_PERMISSION')),
    target_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    approval_request_id TEXT,
    reason TEXT,
    "before" JSONB NOT NULL CHECK (jsonb_typeof("before") = 'object'),
    "after" JSONB NOT NULL CHECK (jsonb_typeof("after") = 'object'),
    ip_masked TEXT
);

CREATE INDEX IF NOT EXISTS audit_log_created_at ON audit_log (created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS audit_log_actor_user_id ON audit_log (actor_user_id);
CREATE INDEX IF NOT EXISTS audit_log_target_id ON audit_log (target_id);
CREATE INDEX IF NOT EXISTS audit_log_action ON audit_log (action);

CREATE OR REPLACE FUNCTION audit_log_server_time() RETURNS trigger AS $$
BEGIN
    NEW.created_at := clock_timestamp();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION audit_log_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only' USING ERRCODE = '55000';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_insert_time BEFORE INSERT ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_server_time();
CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_reject_mutation();
ALTER TABLE audit_log ENABLE ALWAYS TRIGGER audit_log_insert_time;
ALTER TABLE audit_log ENABLE ALWAYS TRIGGER audit_log_append_only;

COMMIT;
