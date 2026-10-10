-- Manual application only. No backfill or delivery.
BEGIN;
CREATE TABLE campaigns (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 120),
    subject TEXT NOT NULL CHECK (char_length(subject) BETWEEN 3 AND 120 AND subject !~ E'[\\r\\n]'),
    body TEXT NOT NULL CHECK (char_length(body) BETWEEN 1 AND 5000),
    kind TEXT NOT NULL DEFAULT 'info' CHECK (kind = 'info'),
    audience TEXT NOT NULL CHECK (audience IN ('all_users','premium_users','team_only')),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','pending_approval','sending','sent','cancelled','failed')),
    created_by TEXT NOT NULL,
    created_by_role TEXT NOT NULL CHECK (created_by_role IN ('OWNER','MODERATOR')),
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    scheduled_at TIMESTAMPTZ,
    approval_request_id TEXT,
    recipient_count INTEGER NOT NULL DEFAULT 0 CHECK (recipient_count >= 0),
    queued_count INTEGER NOT NULL DEFAULT 0 CHECK (queued_count >= 0),
    sent_count INTEGER NOT NULL DEFAULT 0 CHECK (sent_count >= 0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    skipped_count INTEGER NOT NULL DEFAULT 0 CHECK (skipped_count >= 0),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    send_key_hash TEXT CHECK (send_key_hash IS NULL OR send_key_hash ~ '^[0-9a-f]{64}$')
);
CREATE INDEX campaigns_creator ON campaigns(created_by,created_at DESC,id);
CREATE TABLE campaign_recipients (
    campaign_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued','sent','failed','skipped')),
    skip_reason TEXT CHECK (skip_reason IS NULL OR skip_reason IN ('opt_out','recipient_unavailable','cancelled','recipient_erased')),
    outbox_id TEXT,
    PRIMARY KEY (campaign_id,user_id)
);
CREATE INDEX campaign_recipients_outbox ON campaign_recipients(outbox_id);
CREATE TABLE email_preferences (
    user_id TEXT PRIMARY KEY,
    announcements_opt_out BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
-- Shared across processes: count provider attempts, including test and retry sends.
CREATE TABLE campaign_delivery_budget (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    day DATE NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_send_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO campaign_delivery_budget(id,day) VALUES (1,(clock_timestamp() AT TIME ZONE 'UTC')::date);
CREATE TABLE campaign_test_limits (
    user_id TEXT PRIMARY KEY,
    window_start TIMESTAMPTZ NOT NULL,
    attempts INTEGER NOT NULL CHECK (attempts BETWEEN 1 AND 5)
);
CREATE FUNCTION campaign_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' OR NEW.version <> 1 OR NEW.started_at IS NOT NULL
           OR NEW.finished_at IS NOT NULL OR NEW.approval_request_id IS NOT NULL OR NEW.send_key_hash IS NOT NULL
           OR NEW.recipient_count + NEW.queued_count + NEW.sent_count + NEW.failed_count + NEW.skipped_count <> 0 THEN
            RAISE EXCEPTION 'Invalid campaign creation' USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;
    IF pg_trigger_depth() > 1 AND NEW.title = '[erased]' AND NEW.subject = '[erased]' AND NEW.body = '[erased]'
       AND NEW.status = (CASE WHEN OLD.status IN ('sent','failed','cancelled') THEN OLD.status ELSE 'cancelled' END)
       AND (to_jsonb(NEW) - ARRAY['title','subject','body','status'])
         = (to_jsonb(OLD) - ARRAY['title','subject','body','status'])
       AND EXISTS (SELECT 1 FROM commercial_erased_users WHERE user_hash =
           encode(sha256(convert_to(OLD.created_by,'UTF8')),'hex')) THEN RETURN NEW; END IF;
    IF NEW.id <> OLD.id OR NEW.created_by <> OLD.created_by OR NEW.created_by_role <> OLD.created_by_role
       OR NEW.created_at <> OLD.created_at OR NEW.kind <> OLD.kind OR NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'Campaign identity immutable' USING ERRCODE = '55000';
    END IF;
    IF OLD.status <> 'draft' AND (NEW.title,NEW.subject,NEW.body,NEW.audience,NEW.content_hash,NEW.scheduled_at)
       IS DISTINCT FROM (OLD.title,OLD.subject,OLD.body,OLD.audience,OLD.content_hash,OLD.scheduled_at) THEN
        RAISE EXCEPTION 'Campaign content frozen' USING ERRCODE = '55000';
    END IF;
    IF NOT ((OLD.status = 'draft' AND NEW.status IN ('draft','pending_approval','sending','cancelled'))
       OR (OLD.status = 'pending_approval' AND NEW.status IN ('draft','sending','cancelled'))
       OR (OLD.status = 'sending' AND NEW.status IN ('sending','sent','failed','cancelled'))) THEN
        RAISE EXCEPTION 'Invalid campaign transition' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER campaigns_guard BEFORE INSERT OR UPDATE ON campaigns FOR EACH ROW EXECUTE FUNCTION campaign_guard();
CREATE FUNCTION campaign_no_delete() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' AND TG_TABLE_NAME IN ('campaign_recipients','email_preferences')
       AND pg_trigger_depth() > 1 AND EXISTS (SELECT 1 FROM commercial_erased_users WHERE user_hash =
           encode(sha256(convert_to(OLD.user_id,'UTF8')),'hex')) THEN RETURN OLD; END IF;
    RAISE EXCEPTION 'Campaign deletion forbidden' USING ERRCODE = '55000';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER campaigns_no_delete BEFORE DELETE OR TRUNCATE ON campaigns FOR EACH STATEMENT EXECUTE FUNCTION campaign_no_delete();
CREATE TRIGGER recipients_no_delete BEFORE DELETE ON campaign_recipients FOR EACH ROW EXECUTE FUNCTION campaign_no_delete();
CREATE TRIGGER preferences_no_delete BEFORE DELETE ON email_preferences FOR EACH ROW EXECUTE FUNCTION campaign_no_delete();
CREATE TRIGGER recipients_no_truncate BEFORE TRUNCATE ON campaign_recipients FOR EACH STATEMENT EXECUTE FUNCTION campaign_no_delete();
CREATE TRIGGER preferences_no_truncate BEFORE TRUNCATE ON email_preferences FOR EACH STATEMENT EXECUTE FUNCTION campaign_no_delete();
CREATE FUNCTION campaign_recipient_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.campaign_id <> OLD.campaign_id OR NEW.user_id <> OLD.user_id OR NEW.outbox_id IS DISTINCT FROM OLD.outbox_id
       OR OLD.status <> 'queued' OR NEW.status NOT IN ('sent','failed','skipped') THEN
        RAISE EXCEPTION 'Invalid campaign recipient transition' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER recipients_guard BEFORE UPDATE ON campaign_recipients FOR EACH ROW EXECUTE FUNCTION campaign_recipient_guard();
CREATE FUNCTION campaign_erasure() RETURNS trigger AS $$
BEGIN
    UPDATE campaigns SET title='[erased]',subject='[erased]',body='[erased]',
      status=CASE WHEN status IN ('sent','failed','cancelled') THEN status ELSE 'cancelled' END
      WHERE encode(sha256(convert_to(created_by,'UTF8')),'hex')=NEW.user_hash;
    DELETE FROM campaign_recipients WHERE encode(sha256(convert_to(user_id,'UTF8')),'hex')=NEW.user_hash;
    DELETE FROM email_preferences WHERE encode(sha256(convert_to(user_id,'UTF8')),'hex')=NEW.user_hash;
    DELETE FROM campaign_test_limits WHERE encode(sha256(convert_to(user_id,'UTF8')),'hex')=NEW.user_hash;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER campaigns_erasure AFTER INSERT ON commercial_erased_users FOR EACH ROW EXECUTE FUNCTION campaign_erasure();
ALTER TABLE campaigns ENABLE ALWAYS TRIGGER campaigns_guard;
ALTER TABLE campaigns ENABLE ALWAYS TRIGGER campaigns_no_delete;
ALTER TABLE campaign_recipients ENABLE ALWAYS TRIGGER recipients_guard;
ALTER TABLE campaign_recipients ENABLE ALWAYS TRIGGER recipients_no_delete;
ALTER TABLE campaign_recipients ENABLE ALWAYS TRIGGER recipients_no_truncate;
ALTER TABLE email_preferences ENABLE ALWAYS TRIGGER preferences_no_delete;
ALTER TABLE email_preferences ENABLE ALWAYS TRIGGER preferences_no_truncate;
ALTER TABLE commercial_erased_users ENABLE ALWAYS TRIGGER campaigns_erasure;
COMMIT;
