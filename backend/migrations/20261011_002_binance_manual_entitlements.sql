-- Prepare only. Apply separately after 20261011_001; no backfill or Stripe writes.
BEGIN;
ALTER TABLE audit_log DROP CONSTRAINT audit_log_action_check;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_action_check CHECK (action IN (
 'ROLE_CHANGED','PERMISSION_GRANTED','PERMISSION_REVOKED','customer.viewed','customer.subscription.viewed','customer.payments.viewed',
 'support.case.viewed','support.case.taken','support.case.released','support.case.status_changed','support.note.added',
 'approval.requested','approval.approved','approval.rejected','approval.cancelled','approval.expired','approval.stale','approval.executed','approval.failed',
 'customer.events.viewed','campaign.created','campaign.updated','campaign.test_sent','campaign.send_requested',
 'campaign.send_started','campaign.cancelled','campaign.completed','payment.binance.approved','payment.binance.rejected'));
ALTER TABLE audit_log DROP CONSTRAINT audit_log_target_type_check;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_target_type_check
 CHECK (target_type IN ('USER','MODERATOR_PERMISSION','SUPPORT_CASE','APPROVAL_REQUEST','CAMPAIGN','BINANCE_PAYMENT'));

CREATE TABLE binance_manual_periods (
    payment_request_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (period_end = period_start + INTERVAL '720 hours')
);
CREATE INDEX binance_manual_period_user ON binance_manual_periods(user_id,period_end DESC)
    WHERE revoked_at IS NULL;

CREATE OR REPLACE FUNCTION binance_manual_payment_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'created' OR NEW.version <> 1 OR NEW.transaction_number IS NOT NULL
           OR NEW.transaction_fingerprint IS NOT NULL OR NEW.submitted_at IS NOT NULL
           OR NEW.decided_by IS NOT NULL OR NEW.decided_at IS NOT NULL
           OR NEW.decision_note IS NOT NULL OR NEW.erased_at IS NOT NULL
           OR NOT EXISTS (SELECT 1 FROM commercial_auth_users WHERE user_id = NEW.user_id
               AND security->'active' = 'true'::jsonb AND security->'email_verified' = 'true'::jsonb)
           OR EXISTS (SELECT 1 FROM commercial_erased_users WHERE user_hash =
               encode(sha256(convert_to(NEW.user_id,'UTF8')),'hex')) THEN
            RAISE EXCEPTION 'Invalid manual payment creation' USING ERRCODE = '55000';
        END IF;
        NEW.created_at := clock_timestamp();
        NEW.expires_at := NEW.created_at + INTERVAL '48 hours';
        RETURN NEW;
    END IF;
    IF pg_trigger_depth() > 1 AND NEW.erased_at IS NOT NULL AND OLD.erased_at IS NULL
       AND NEW.user_id = 'ERASED' AND NEW.transaction_number IS NULL AND NEW.decision_note IS NULL
       AND (to_jsonb(NEW) - ARRAY['user_id','transaction_number','decision_note','erased_at'])
           = (to_jsonb(OLD) - ARRAY['user_id','transaction_number','decision_note','erased_at'])
       AND EXISTS (SELECT 1 FROM commercial_erased_users WHERE user_hash =
           encode(sha256(convert_to(OLD.user_id,'UTF8')),'hex')) THEN
        RETURN NEW;
    END IF;
    IF OLD.erased_at IS NOT NULL OR NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'Invalid payment version or erased request' USING ERRCODE = '55000';
    END IF;
    IF OLD.status = 'pending' AND NEW.status IN ('approved','rejected') THEN
        IF (to_jsonb(NEW) - ARRAY['status','decided_by','decided_at','decision_note','version'])
           IS DISTINCT FROM
           (to_jsonb(OLD) - ARRAY['status','decided_by','decided_at','decision_note','version'])
           OR NOT EXISTS (SELECT 1 FROM commercial_auth_users WHERE user_id=NEW.decided_by
               AND security->>'role'='OWNER' AND security->'active'='true'::jsonb)
           OR NEW.decided_at IS NULL
           OR (NEW.status='rejected' AND COALESCE(char_length(btrim(NEW.decision_note)),0)=0) THEN
            RAISE EXCEPTION 'Invalid OWNER payment decision' USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;
    IF (to_jsonb(NEW) - ARRAY['status','transaction_number','transaction_fingerprint','submitted_at','version'])
       IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['status','transaction_number','transaction_fingerprint','submitted_at','version'])
       OR OLD.status <> 'created' THEN
        RAISE EXCEPTION 'Manual payment identity or terminal state is immutable' USING ERRCODE = '55000';
    END IF;
    IF NEW.status = 'pending' THEN
        IF clock_timestamp() >= OLD.expires_at OR NEW.transaction_number IS NULL
           OR NEW.transaction_fingerprint IS DISTINCT FROM
               encode(sha256(convert_to('binance_manual:' || NEW.transaction_number,'UTF8')),'hex') THEN
            RAISE EXCEPTION 'Invalid or late manual payment submission' USING ERRCODE = '55000';
        END IF;
        NEW.submitted_at := clock_timestamp();
    ELSIF NEW.status = 'expired' THEN
        IF clock_timestamp() < OLD.expires_at OR NEW.transaction_number IS NOT NULL
           OR NEW.transaction_fingerprint IS NOT NULL OR NEW.submitted_at IS NOT NULL THEN
            RAISE EXCEPTION 'Manual payment is not due for expiry' USING ERRCODE = '55000';
        END IF;
    ELSE
        RAISE EXCEPTION 'Invalid manual payment transition' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION binance_manual_period_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.revoked_at IS NOT NULL OR NOT EXISTS (
            SELECT 1 FROM binance_manual_payment_requests WHERE id=NEW.payment_request_id
              AND user_id=NEW.user_id AND status='approved' AND erased_at IS NULL) THEN
            RAISE EXCEPTION 'Approved payment required for period' USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;
    IF pg_trigger_depth() > 1 AND NEW.user_id='ERASED' AND NEW.revoked_at IS NOT NULL
       AND (to_jsonb(NEW) - ARRAY['user_id','revoked_at']) =
           (to_jsonb(OLD) - ARRAY['user_id','revoked_at'])
       AND EXISTS (SELECT 1 FROM commercial_erased_users WHERE user_hash =
           encode(sha256(convert_to(OLD.user_id,'UTF8')),'hex')) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'Payment periods are immutable' USING ERRCODE = '55000';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER binance_manual_period_insert_update BEFORE INSERT OR UPDATE ON binance_manual_periods
    FOR EACH ROW EXECUTE FUNCTION binance_manual_period_guard();
CREATE TRIGGER binance_manual_period_no_delete BEFORE DELETE OR TRUNCATE ON binance_manual_periods
    FOR EACH STATEMENT EXECUTE FUNCTION binance_manual_payment_reject_delete();

CREATE FUNCTION binance_manual_decision_complete() RETURNS trigger AS $$
DECLARE payment RECORD;
BEGIN
    IF TG_TABLE_NAME='binance_manual_periods' THEN
        SELECT * INTO payment FROM binance_manual_payment_requests WHERE id=NEW.payment_request_id;
    ELSE
        SELECT * INTO payment FROM binance_manual_payment_requests WHERE id=NEW.id;
    END IF;
    IF payment.erased_at IS NOT NULL THEN
        RETURN NEW;
    END IF;
    IF payment.status IN ('approved','rejected') AND (
        NOT EXISTS (SELECT 1 FROM audit_log WHERE target_type='BINANCE_PAYMENT' AND target_id=payment.id
            AND actor_role='OWNER' AND actor_user_id=payment.decided_by
            AND action='payment.binance.' || payment.status)
        OR (payment.status='approved' AND NOT EXISTS (
            SELECT 1 FROM binance_manual_periods WHERE payment_request_id=payment.id
              AND user_id=payment.user_id AND revoked_at IS NULL))
        OR (payment.status='rejected' AND EXISTS (
            SELECT 1 FROM binance_manual_periods WHERE payment_request_id=payment.id))) THEN
        RAISE EXCEPTION 'Decision requires atomic audit and matching period' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER binance_manual_payment_complete AFTER INSERT OR UPDATE ON binance_manual_payment_requests
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION binance_manual_decision_complete();
CREATE CONSTRAINT TRIGGER binance_manual_period_complete AFTER INSERT ON binance_manual_periods
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION binance_manual_decision_complete();

CREATE FUNCTION binance_manual_period_erase() RETURNS trigger AS $$
BEGIN
    UPDATE binance_manual_periods SET user_id='ERASED',revoked_at=clock_timestamp()
      WHERE encode(sha256(convert_to(user_id,'UTF8')),'hex')=NEW.user_hash;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER binance_manual_period_erasure AFTER INSERT ON commercial_erased_users
    FOR EACH ROW EXECUTE FUNCTION binance_manual_period_erase();
ALTER TABLE binance_manual_periods ENABLE ALWAYS TRIGGER binance_manual_period_insert_update;
ALTER TABLE binance_manual_periods ENABLE ALWAYS TRIGGER binance_manual_period_no_delete;
ALTER TABLE binance_manual_periods ENABLE ALWAYS TRIGGER binance_manual_period_complete;
ALTER TABLE binance_manual_payment_requests ENABLE ALWAYS TRIGGER binance_manual_payment_complete;
ALTER TABLE commercial_erased_users ENABLE ALWAYS TRIGGER binance_manual_period_erasure;
COMMIT;
