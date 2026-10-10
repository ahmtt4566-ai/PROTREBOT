-- Manual application only. No provider call or backfill.
BEGIN;
CREATE TABLE notification_outbox (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('approval.pending_digest','approval.decision')),
    recipient_user_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sending','sent','failed','dead')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 8),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_error_code TEXT CHECK (last_error_code IS NULL OR last_error_code IN (
        'provider_unavailable','provider_timeout','provider_rejected','invalid_response','delivery_error',
        'recipient_unavailable','recipient_erased','lease_expired','attempts_exhausted','nothing_to_notify')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    sent_at TIMESTAMPTZ,
    CHECK (jsonb_typeof(payload) = 'object' AND (
        (kind = 'approval.pending_digest' AND payload ? 'bucket'
            AND payload - ARRAY['bucket','count'] = '{}'::jsonb
            AND jsonb_typeof(payload->'bucket') = 'number'
            AND payload->>'bucket' ~ '^[0-9]+$'
            AND (NOT payload ? 'count' OR (
                jsonb_typeof(payload->'count') = 'number' AND payload->>'count' ~ '^[0-9]+$')))
        OR (kind = 'approval.decision' AND payload ?& ARRAY['approval_id','result']
            AND payload - ARRAY['approval_id','result'] = '{}'::jsonb
            AND jsonb_typeof(payload->'approval_id') = 'string'
            AND payload->>'approval_id' ~ '^[A-Za-z0-9_-]{1,160}$'
            AND jsonb_typeof(payload->'result') = 'number' AND payload->>'result' IN ('1','2','3','4'))
        OR (recipient_user_id = 'ERASED' AND payload = '{}'::jsonb)
    )),
    CHECK ((status = 'sent') = (sent_at IS NOT NULL))
);
CREATE INDEX notification_outbox_due ON notification_outbox(next_attempt_at,id)
    WHERE status IN ('pending','failed','sending');

CREATE FUNCTION notification_outbox_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'pending' OR NEW.attempts <> 0 OR NEW.sent_at IS NOT NULL
           OR NEW.last_error_code IS NOT NULL OR NEW.recipient_user_id = 'ERASED'
           OR EXISTS (SELECT 1 FROM commercial_erased_users e WHERE e.user_hash =
               encode(sha256(convert_to(NEW.recipient_user_id,'UTF8')),'hex')) THEN
            RAISE EXCEPTION 'Invalid outbox creation' USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;
    IF pg_trigger_depth() > 1 AND NEW.recipient_user_id = 'ERASED' AND NEW.payload = '{}'::jsonb
       AND NEW.dedupe_key = 'erased:' || OLD.id
       AND NEW.last_error_code = 'recipient_erased'
       AND NEW.status = (CASE WHEN OLD.status = 'sent' THEN 'sent' ELSE 'dead' END)
       AND (to_jsonb(NEW) - ARRAY['recipient_user_id','payload','dedupe_key','status','last_error_code'])
           = (to_jsonb(OLD) - ARRAY['recipient_user_id','payload','dedupe_key','status','last_error_code'])
       AND EXISTS (SELECT 1 FROM commercial_erased_users e WHERE e.user_hash =
           encode(sha256(convert_to(OLD.recipient_user_id,'UTF8')),'hex')) THEN
        RETURN NEW;
    END IF;
    IF OLD.status IN ('sent','dead') THEN
        RAISE EXCEPTION 'Terminal outbox is immutable' USING ERRCODE = '55000';
    END IF;
    IF (to_jsonb(NEW) - ARRAY['status','attempts','next_attempt_at','last_error_code','sent_at','payload'])
       IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['status','attempts','next_attempt_at','last_error_code','sent_at','payload'])
       OR NOT (
           (OLD.status IN ('pending','failed') AND NEW.status = 'sending' AND OLD.attempts < 8
                AND NEW.attempts = OLD.attempts + 1)
           OR (OLD.status = 'sending' AND NEW.status IN ('sent','failed','pending','dead')
                AND NEW.attempts = OLD.attempts)
       ) THEN
        RAISE EXCEPTION 'Invalid outbox transition' USING ERRCODE = '55000';
    END IF;
    IF NEW.payload IS DISTINCT FROM OLD.payload AND NOT (
        OLD.kind = 'approval.pending_digest' AND NOT OLD.payload ? 'count'
        AND NEW.status = 'sending' AND NEW.payload - 'count' = OLD.payload AND NEW.payload ? 'count'
    ) THEN
        RAISE EXCEPTION 'Outbox payload is immutable' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE FUNCTION notification_outbox_reject_delete() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Outbox cannot be deleted' USING ERRCODE = '55000';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER notification_outbox_insert_update BEFORE INSERT OR UPDATE ON notification_outbox
    FOR EACH ROW EXECUTE FUNCTION notification_outbox_guard();
CREATE TRIGGER notification_outbox_no_delete BEFORE DELETE OR TRUNCATE ON notification_outbox
    FOR EACH STATEMENT EXECUTE FUNCTION notification_outbox_reject_delete();
CREATE FUNCTION notification_outbox_erase() RETURNS trigger AS $$
BEGIN
    UPDATE notification_outbox SET recipient_user_id = 'ERASED', payload = '{}'::jsonb,
      dedupe_key = 'erased:' || id, status = CASE WHEN status = 'sent' THEN 'sent' ELSE 'dead' END,
      last_error_code = 'recipient_erased'
      WHERE encode(sha256(convert_to(recipient_user_id,'UTF8')),'hex') = NEW.user_hash;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER notification_outbox_erasure AFTER INSERT ON commercial_erased_users
    FOR EACH ROW EXECUTE FUNCTION notification_outbox_erase();
ALTER TABLE notification_outbox ENABLE ALWAYS TRIGGER notification_outbox_insert_update;
ALTER TABLE notification_outbox ENABLE ALWAYS TRIGGER notification_outbox_no_delete;
ALTER TABLE commercial_erased_users ENABLE ALWAYS TRIGGER notification_outbox_erasure;
COMMIT;
