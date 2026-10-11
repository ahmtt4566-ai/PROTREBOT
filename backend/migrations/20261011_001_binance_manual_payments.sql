-- Prepare only: manual application after separate approval. No access grants.
BEGIN;
CREATE TABLE binance_manual_payment_requests (
    id TEXT PRIMARY KEY CHECK (id ~ '^[a-f0-9]{32}$'),
    user_id TEXT NOT NULL,
    reference_code TEXT NOT NULL CONSTRAINT binance_manual_reference_unique UNIQUE
        CHECK (reference_code ~ '^KT-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{6}$'),
    amount NUMERIC(12,2) NOT NULL DEFAULT 119.90 CHECK (amount = 119.90),
    currency TEXT NOT NULL DEFAULT 'USDT' CHECK (currency = 'USDT'),
    method TEXT NOT NULL DEFAULT 'binance_manual' CHECK (method = 'binance_manual'),
    status TEXT NOT NULL DEFAULT 'created' CHECK (status IN ('created','pending','approved','rejected','expired')),
    transaction_number TEXT CONSTRAINT binance_manual_transaction_unique UNIQUE
        CHECK (transaction_number IS NULL OR transaction_number ~ '^[A-Za-z0-9_-]{1,128}$'),
    transaction_fingerprint TEXT CONSTRAINT binance_manual_fingerprint_unique UNIQUE
        CHECK (transaction_fingerprint IS NULL OR transaction_fingerprint ~ '^[a-f0-9]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL,
    submitted_at TIMESTAMPTZ,
    decided_by TEXT,
    decided_at TIMESTAMPTZ,
    decision_note TEXT CHECK (decision_note IS NULL OR char_length(btrim(decision_note)) BETWEEN 1 AND 500),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    erased_at TIMESTAMPTZ,
    CHECK (expires_at = created_at + INTERVAL '48 hours'),
    CHECK ((transaction_number IS NULL AND transaction_fingerprint IS NULL)
        OR (transaction_fingerprint IS NOT NULL AND (transaction_number IS NOT NULL OR erased_at IS NOT NULL))),
    CHECK ((status IN ('created','expired') AND transaction_fingerprint IS NULL AND submitted_at IS NULL)
        OR (status IN ('pending','approved','rejected') AND transaction_fingerprint IS NOT NULL
            AND submitted_at IS NOT NULL AND submitted_at < expires_at)),
    CHECK ((status IN ('approved','rejected') AND decided_by IS NOT NULL AND decided_at IS NOT NULL)
        OR (status NOT IN ('approved','rejected') AND decided_by IS NULL AND decided_at IS NULL AND decision_note IS NULL)),
    CHECK (status <> 'rejected' OR decision_note IS NOT NULL OR erased_at IS NOT NULL)
);
CREATE UNIQUE INDEX binance_manual_one_open_user ON binance_manual_payment_requests(user_id)
    WHERE status IN ('created','pending') AND erased_at IS NULL;
CREATE INDEX binance_manual_expiry ON binance_manual_payment_requests(expires_at,id)
    WHERE status = 'created' AND erased_at IS NULL;
CREATE INDEX binance_manual_user_history ON binance_manual_payment_requests(user_id,created_at DESC,id);

CREATE FUNCTION binance_manual_payment_guard() RETURNS trigger AS $$
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
    IF OLD.erased_at IS NOT NULL OR
       (to_jsonb(NEW) - ARRAY['status','transaction_number','transaction_fingerprint','submitted_at','version'])
       IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['status','transaction_number','transaction_fingerprint','submitted_at','version'])
       OR NEW.version <> OLD.version + 1 OR OLD.status <> 'created' THEN
        RAISE EXCEPTION 'Manual payment identity or terminal state is immutable' USING ERRCODE = '55000';
    END IF;
    -- OWNER decisions and their atomic grant/audit integration are deliberately not enabled yet.
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
        RAISE EXCEPTION 'Manual payment transition is not enabled' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE FUNCTION binance_manual_payment_reject_delete() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Manual payment history cannot be deleted' USING ERRCODE = '55000';
END;
$$ LANGUAGE plpgsql;
CREATE FUNCTION binance_manual_payment_erase() RETURNS trigger AS $$
BEGIN
    UPDATE binance_manual_payment_requests SET user_id = 'ERASED', transaction_number = NULL,
      decision_note = NULL, erased_at = clock_timestamp()
      WHERE encode(sha256(convert_to(user_id,'UTF8')),'hex') = NEW.user_hash AND erased_at IS NULL;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER binance_manual_payment_insert_update BEFORE INSERT OR UPDATE ON binance_manual_payment_requests
    FOR EACH ROW EXECUTE FUNCTION binance_manual_payment_guard();
CREATE TRIGGER binance_manual_payment_no_delete BEFORE DELETE OR TRUNCATE ON binance_manual_payment_requests
    FOR EACH STATEMENT EXECUTE FUNCTION binance_manual_payment_reject_delete();
CREATE TRIGGER binance_manual_payment_erasure AFTER INSERT ON commercial_erased_users
    FOR EACH ROW EXECUTE FUNCTION binance_manual_payment_erase();
ALTER TABLE binance_manual_payment_requests ENABLE ALWAYS TRIGGER binance_manual_payment_insert_update;
ALTER TABLE binance_manual_payment_requests ENABLE ALWAYS TRIGGER binance_manual_payment_no_delete;
ALTER TABLE commercial_erased_users ENABLE ALWAYS TRIGGER binance_manual_payment_erasure;
COMMIT;
