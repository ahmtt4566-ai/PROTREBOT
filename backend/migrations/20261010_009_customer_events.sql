-- Manual application only; no backfill or provider traffic.
BEGIN;
CREATE TABLE customer_events (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN (
        'auth.login_failed','auth.login_succeeded','auth.mfa_failed','auth.password_reset_requested',
        'auth.password_reset_completed','auth.email_verification_sent','auth.email_verification_failed',
        'auth.email_verified','account.session_revoked','api.error')),
    code TEXT NOT NULL CHECK (code IN (
        'invalid_credentials','login_ok','invalid_mfa','reset_requested','reset_completed',
        'verification_sent','verification_failed','email_verified','session_revoked',
        'server_error','request_failed','event_limit')),
    feature TEXT NOT NULL CHECK (feature IN ('auth','security','verification','account','api','events')),
    http_status INTEGER CHECK (http_status BETWEEN 100 AND 599),
    request_id UUID,
    count BIGINT NOT NULL DEFAULT 1 CHECK (count >= 1),
    first_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX customer_events_user_time ON customer_events(user_id,last_at DESC,id);
CREATE INDEX customer_events_retention ON customer_events(last_at);
CREATE FUNCTION customer_event_guard() RETURNS trigger AS $$
DECLARE
    existing TEXT;
    stamp TIMESTAMPTZ := clock_timestamp();
    rows_in_hour INTEGER;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.last_at < stamp - INTERVAL '90 days'
           OR (pg_trigger_depth() > 1 AND EXISTS (
             SELECT 1 FROM commercial_erased_users e WHERE e.user_hash =
               encode(sha256(convert_to(OLD.user_id,'UTF8')),'hex'))) THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'Customer event deletion forbidden' USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF pg_trigger_depth() > 1 AND NEW.count = OLD.count + 1 AND NEW.last_at >= OLD.last_at
           AND (to_jsonb(NEW) - ARRAY['count','last_at']) = (to_jsonb(OLD) - ARRAY['count','last_at']) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'Customer event mutation forbidden' USING ERRCODE = '55000';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('customer-events:' || NEW.user_id,0));
    stamp := clock_timestamp();
    IF NOT EXISTS (SELECT 1 FROM commercial_auth_users WHERE user_id = NEW.user_id)
       OR EXISTS (SELECT 1 FROM commercial_erased_users e WHERE e.user_hash =
           encode(sha256(convert_to(NEW.user_id,'UTF8')),'hex')) THEN
        RETURN NULL;
    END IF;
    NEW.count := 1;
    NEW.first_at := stamp;
    NEW.last_at := stamp;
    SELECT id INTO existing FROM customer_events WHERE user_id = NEW.user_id
      AND kind = NEW.kind AND code = NEW.code AND feature = NEW.feature
      AND last_at >= stamp - INTERVAL '10 minutes' ORDER BY last_at DESC LIMIT 1 FOR UPDATE;
    IF existing IS NULL THEN
        SELECT COUNT(*) INTO rows_in_hour FROM customer_events
          WHERE user_id = NEW.user_id AND first_at >= stamp - INTERVAL '1 hour';
        IF rows_in_hour >= 49 THEN
            SELECT id INTO existing FROM customer_events WHERE user_id = NEW.user_id
              AND code = 'event_limit' AND first_at >= stamp - INTERVAL '1 hour'
              ORDER BY first_at DESC LIMIT 1 FOR UPDATE;
            NEW.kind := 'api.error';
            NEW.code := 'event_limit';
            NEW.feature := 'events';
            NEW.http_status := NULL;
            NEW.request_id := NULL;
        END IF;
    END IF;
    IF existing IS NOT NULL THEN
        UPDATE customer_events SET count = count + 1, last_at = stamp WHERE id = existing;
        RETURN NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER customer_events_guard BEFORE INSERT OR UPDATE OR DELETE ON customer_events
    FOR EACH ROW EXECUTE FUNCTION customer_event_guard();
CREATE FUNCTION customer_events_no_truncate() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Customer events cannot be truncated' USING ERRCODE = '55000';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER customer_events_truncate_guard BEFORE TRUNCATE ON customer_events
    FOR EACH STATEMENT EXECUTE FUNCTION customer_events_no_truncate();
CREATE FUNCTION customer_events_erase() RETURNS trigger AS $$
DECLARE
    erased_id TEXT;
BEGIN
    FOR erased_id IN SELECT user_id FROM commercial_auth_users
        WHERE encode(sha256(convert_to(user_id,'UTF8')),'hex') = NEW.user_hash LOOP
        PERFORM pg_advisory_xact_lock(hashtextextended('customer-events:' || erased_id,0));
    END LOOP;
    DELETE FROM customer_events WHERE encode(sha256(convert_to(user_id,'UTF8')),'hex') = NEW.user_hash;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER customer_events_erasure AFTER INSERT ON commercial_erased_users
    FOR EACH ROW EXECUTE FUNCTION customer_events_erase();
ALTER TABLE customer_events ENABLE ALWAYS TRIGGER customer_events_guard;
ALTER TABLE customer_events ENABLE ALWAYS TRIGGER customer_events_truncate_guard;
ALTER TABLE commercial_erased_users ENABLE ALWAYS TRIGGER customer_events_erasure;
COMMIT;
