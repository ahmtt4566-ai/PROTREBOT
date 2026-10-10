-- Manual application only. No backfill or snapshot changes.
BEGIN;
CREATE TABLE approval_requests (
    id TEXT PRIMARY KEY,
    action_type TEXT NOT NULL CHECK (action_type IN ('account.deactivate','account.reactivate')),
    target_user_id TEXT NOT NULL,
    requester_user_id TEXT NOT NULL,
    requester_role TEXT NOT NULL CHECK (requester_role = 'MODERATOR'),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (payload = '{}'::jsonb),
    target_snapshot JSONB NOT NULL CHECK (
        jsonb_typeof(target_snapshot) = 'object'
        AND target_snapshot ?& ARRAY['active','role','auth_version']
        AND (target_snapshot - ARRAY['active','role','auth_version']) = '{}'::jsonb
        AND jsonb_typeof(target_snapshot->'active') = 'boolean'
        AND target_snapshot->>'role' = 'CUSTOMER'
        AND (target_snapshot->>'auth_version')::bigint >= 1
    ),
    reason TEXT NOT NULL CHECK (char_length(reason) = 0 OR char_length(reason) BETWEEN 10 AND 500),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending','approved','rejected','cancelled','expired','stale','executing','executed','failed')),
    decided_by TEXT,
    decided_at TIMESTAMPTZ,
    decision_note TEXT CHECK (char_length(decision_note) <= 500),
    executed_at TIMESTAMPTZ,
    result_code TEXT CHECK (result_code IS NULL OR result_code IN (
        'ok','already_target_state','canonical_applied','protected_positions',
        'agents_revoke_pending','execution_failed','target_changed','requester_changed')),
    expires_at TIMESTAMPTZ NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (decided_by IS NULL OR decided_by <> requester_user_id)
);
CREATE UNIQUE INDEX approval_one_pending_action
    ON approval_requests (target_user_id,action_type) WHERE status = 'pending';
CREATE INDEX approval_requester_created ON approval_requests (requester_user_id,created_at DESC,id);
CREATE INDEX approval_status_created ON approval_requests (status,created_at DESC,id);

CREATE FUNCTION approval_guard() RETURNS trigger AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(71010006);
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'pending' OR NEW.version <> 1
            OR char_length(btrim(NEW.reason)) NOT BETWEEN 10 AND 500
            OR NEW.decided_by IS NOT NULL OR NEW.decided_at IS NOT NULL
            OR NEW.decision_note IS NOT NULL OR NEW.executed_at IS NOT NULL OR NEW.result_code IS NOT NULL
            OR EXISTS (SELECT 1 FROM commercial_erased_users e WHERE e.user_hash IN (
                encode(sha256(convert_to(NEW.target_user_id,'UTF8')),'hex'),
                encode(sha256(convert_to(NEW.requester_user_id,'UTF8')),'hex'))) THEN
            RAISE EXCEPTION 'Invalid approval request' USING ERRCODE = '55000';
        END IF;
        NEW.created_at := clock_timestamp();
        NEW.expires_at := NEW.created_at + INTERVAL '72 hours';
        RETURN NEW;
    END IF;
    IF pg_trigger_depth() > 1 AND NEW.reason = '' AND NEW.decision_note = ''
       AND (to_jsonb(NEW) - ARRAY['reason','decision_note']) = (to_jsonb(OLD) - ARRAY['reason','decision_note'])
       AND EXISTS (SELECT 1 FROM commercial_erased_users e WHERE e.user_hash IN (
           encode(sha256(convert_to(OLD.target_user_id,'UTF8')),'hex'),
           encode(sha256(convert_to(OLD.requester_user_id,'UTF8')),'hex'))) THEN
        RETURN NEW;
    END IF;
    IF (to_jsonb(NEW) - ARRAY['status','decided_by','decided_at','decision_note','executed_at','result_code','version'])
       IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['status','decided_by','decided_at','decision_note','executed_at','result_code','version']) THEN
        RAISE EXCEPTION 'Approval identity is immutable' USING ERRCODE = '55000';
    END IF;
    IF NEW.version <> OLD.version + 1 OR NOT (
        (OLD.status = 'pending' AND NEW.status IN ('approved','rejected','cancelled','expired','stale'))
        OR (OLD.status = 'approved' AND NEW.status = 'executing')
        OR (OLD.status = 'executing' AND NEW.status IN ('executed','failed','stale'))
        -- Rechecked target can become stale between claim commit and execution.
        OR (OLD.status = 'executing' AND NEW.status = 'executing'
            AND OLD.result_code IS NULL AND NEW.result_code = 'canonical_applied')
        -- Explicit OWNER agent-only retry. Canonical execution is never replayed.
        OR (OLD.status = 'failed' AND NEW.status = 'executing'
            AND OLD.result_code = 'agents_revoke_pending' AND NEW.result_code = 'canonical_applied')
    ) THEN
        RAISE EXCEPTION 'Invalid approval transition' USING ERRCODE = '55000';
    END IF;
    IF OLD.status <> 'pending' AND (
        NEW.decided_by IS DISTINCT FROM OLD.decided_by OR NEW.decided_at IS DISTINCT FROM OLD.decided_at
        OR NEW.decision_note IS DISTINCT FROM OLD.decision_note) THEN
        RAISE EXCEPTION 'Approval decision is immutable' USING ERRCODE = '55000';
    END IF;
    IF NEW.status IN ('approved','rejected') AND (NEW.decided_by IS NULL OR NEW.decided_at IS NULL) THEN
        RAISE EXCEPTION 'Approval decision required' USING ERRCODE = '55000';
    END IF;
    IF NEW.status = 'rejected' AND COALESCE(char_length(btrim(NEW.decision_note)),0) = 0 THEN
        RAISE EXCEPTION 'Rejection note required' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE FUNCTION approval_reject_delete() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Approvals cannot be deleted' USING ERRCODE = '55000';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER approval_insert_update BEFORE INSERT OR UPDATE ON approval_requests
    FOR EACH ROW EXECUTE FUNCTION approval_guard();
CREATE TRIGGER approval_no_delete BEFORE DELETE OR TRUNCATE ON approval_requests
    FOR EACH STATEMENT EXECUTE FUNCTION approval_reject_delete();
CREATE FUNCTION approval_erase_text() RETURNS trigger AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(71010006);
    UPDATE approval_requests SET reason = '', decision_note = ''
      WHERE encode(sha256(convert_to(target_user_id,'UTF8')),'hex') = NEW.user_hash
         OR encode(sha256(convert_to(requester_user_id,'UTF8')),'hex') = NEW.user_hash;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER approval_erasure_tombstone AFTER INSERT ON commercial_erased_users
    FOR EACH ROW EXECUTE FUNCTION approval_erase_text();
ALTER TABLE approval_requests ENABLE ALWAYS TRIGGER approval_insert_update;
ALTER TABLE approval_requests ENABLE ALWAYS TRIGGER approval_no_delete;
ALTER TABLE commercial_erased_users ENABLE ALWAYS TRIGGER approval_erasure_tombstone;
COMMIT;
