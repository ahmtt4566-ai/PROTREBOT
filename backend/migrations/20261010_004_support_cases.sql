-- Apply manually. Reads legacy snapshots only; no snapshot writes or backfill.
BEGIN;
CREATE TABLE support_cases (
    id TEXT PRIMARY KEY,
    legacy_ticket_id TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    subject TEXT NOT NULL CHECK (char_length(subject) <= 160),
    message TEXT NOT NULL CHECK (char_length(message) <= 2000),
    priority TEXT NOT NULL CHECK (priority IN ('LOW', 'NORMAL', 'HIGH')),
    legacy_status TEXT NOT NULL CHECK (legacy_status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED', 'CLOSED')),
    legacy_response_note TEXT NOT NULL DEFAULT '' CHECK (char_length(legacy_response_note) <= 1000),
    created_at_legacy TIMESTAMPTZ NOT NULL,
    case_status TEXT NOT NULL DEFAULT 'NEW' CHECK (case_status IN ('NEW', 'OPEN', 'WAITING', 'RESOLVED', 'CLOSED')),
    assignee_user_id TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    synced_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX support_cases_status ON support_cases (case_status, created_at_legacy DESC, id);
CREATE INDEX support_cases_assignee ON support_cases (assignee_user_id);
CREATE INDEX support_cases_user ON support_cases (user_id);
CREATE TABLE support_notes (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES support_cases(id),
    author_user_id TEXT NOT NULL,
    body TEXT NOT NULL CHECK (char_length(body) BETWEEN 0 AND 2000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX support_notes_case ON support_notes (case_id, created_at, id);
CREATE INDEX support_notes_author ON support_notes (author_user_id);
CREATE TABLE support_sync_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    synced_at TIMESTAMPTZ
);

-- Shared transaction lock serializes support writes with erasure tombstones.
CREATE FUNCTION support_case_guard() RETURNS trigger AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(61010004);
    IF EXISTS (SELECT 1 FROM commercial_erased_users
        WHERE user_hash = encode(sha256(convert_to(NEW.user_id, 'UTF8')), 'hex')) THEN
        IF TG_OP = 'INSERT' THEN
            RAISE EXCEPTION 'Erased support customer' USING ERRCODE = '55000';
        END IF;
        NEW.subject := '';
        NEW.message := '';
        NEW.legacy_response_note := '';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER support_case_erasure_guard BEFORE INSERT OR UPDATE ON support_cases
    FOR EACH ROW EXECUTE FUNCTION support_case_guard();

CREATE FUNCTION support_note_guard() RETURNS trigger AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(61010004);
    IF TG_OP = 'UPDATE' THEN
        -- Only a nested erasure trigger may blank the body, with tombstone proof.
        IF pg_trigger_depth() > 1 AND NEW.body = ''
           AND (to_jsonb(NEW) - 'body') = (to_jsonb(OLD) - 'body')
           AND EXISTS (
             SELECT 1 FROM commercial_erased_users e
             WHERE e.user_hash = encode(sha256(convert_to(OLD.author_user_id, 'UTF8')), 'hex')
                OR e.user_hash = (
                    SELECT encode(sha256(convert_to(c.user_id, 'UTF8')), 'hex')
                    FROM support_cases c WHERE c.id = OLD.case_id)
           ) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'support_notes is append-only' USING ERRCODE = '55000';
    END IF;
    IF char_length(btrim(NEW.body)) = 0 OR EXISTS (
        SELECT 1 FROM commercial_erased_users e
        WHERE e.user_hash = encode(sha256(convert_to(NEW.author_user_id, 'UTF8')), 'hex')
           OR e.user_hash = (
                SELECT encode(sha256(convert_to(c.user_id, 'UTF8')), 'hex')
                FROM support_cases c WHERE c.id = NEW.case_id)
    ) THEN
        RAISE EXCEPTION 'Invalid support note' USING ERRCODE = '55000';
    END IF;
    NEW.created_at := clock_timestamp();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE FUNCTION support_notes_reject_delete() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'support_notes is append-only' USING ERRCODE = '55000';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER support_note_insert_update BEFORE INSERT OR UPDATE ON support_notes
    FOR EACH ROW EXECUTE FUNCTION support_note_guard();
CREATE TRIGGER support_notes_no_delete BEFORE DELETE OR TRUNCATE ON support_notes
    FOR EACH STATEMENT EXECUTE FUNCTION support_notes_reject_delete();

CREATE FUNCTION support_erase_content() RETURNS trigger AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(61010004);
    UPDATE support_cases SET subject = '', message = '', legacy_response_note = '',
        updated_at = clock_timestamp()
        WHERE encode(sha256(convert_to(user_id, 'UTF8')), 'hex') = NEW.user_hash;
    UPDATE support_notes n SET body = ''
        WHERE encode(sha256(convert_to(n.author_user_id, 'UTF8')), 'hex') = NEW.user_hash
           OR EXISTS (SELECT 1 FROM support_cases c WHERE c.id = n.case_id
             AND encode(sha256(convert_to(c.user_id, 'UTF8')), 'hex') = NEW.user_hash);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER support_erasure_tombstone AFTER INSERT ON commercial_erased_users
    FOR EACH ROW EXECUTE FUNCTION support_erase_content();
ALTER TABLE support_cases ENABLE ALWAYS TRIGGER support_case_erasure_guard;
ALTER TABLE support_notes ENABLE ALWAYS TRIGGER support_note_insert_update;
ALTER TABLE support_notes ENABLE ALWAYS TRIGGER support_notes_no_delete;
ALTER TABLE commercial_erased_users ENABLE ALWAYS TRIGGER support_erasure_tombstone;
COMMIT;
