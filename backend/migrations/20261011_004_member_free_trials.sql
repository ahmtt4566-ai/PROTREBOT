BEGIN;

CREATE TABLE member_free_trials (
  user_hash TEXT PRIMARY KEY CHECK (user_hash ~ '^[a-f0-9]{64}$'),
  user_id TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ends_at TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '168 hours',
  revoked_at TIMESTAMPTZ,
  CHECK (ends_at = started_at + INTERVAL '168 hours')
);

CREATE FUNCTION member_free_trial_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'Trial history cannot be deleted' USING ERRCODE='55000';
  END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.user_hash <> encode(sha256(convert_to(NEW.user_id,'UTF8')),'hex')
       OR NEW.revoked_at IS NOT NULL
       OR NOT EXISTS (SELECT 1 FROM commercial_auth_users u WHERE u.user_id=NEW.user_id
         AND u.security->>'role'='CUSTOMER' AND u.security->'active'='true'::jsonb
         AND u.security->'email_verified'='true'::jsonb)
       OR EXISTS (SELECT 1 FROM commercial_erased_users e WHERE e.user_hash=NEW.user_hash) THEN
      RAISE EXCEPTION 'Verified active trial owner required' USING ERRCODE='55000';
    END IF;
  ELSIF NOT (pg_trigger_depth()>1 AND NEW.user_id='ERASED' AND NEW.revoked_at IS NOT NULL
      AND NEW.user_hash=OLD.user_hash AND NEW.started_at=OLD.started_at AND NEW.ends_at=OLD.ends_at) THEN
    RAISE EXCEPTION 'Trial period is immutable' USING ERRCODE='55000';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER member_free_trial_immutable BEFORE INSERT OR UPDATE OR DELETE ON member_free_trials
FOR EACH ROW EXECUTE FUNCTION member_free_trial_guard();
ALTER TABLE member_free_trials ENABLE ALWAYS TRIGGER member_free_trial_immutable;

CREATE FUNCTION member_free_trial_erasure() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  UPDATE member_free_trials SET user_id='ERASED',revoked_at=clock_timestamp()
  WHERE user_hash=NEW.user_hash AND revoked_at IS NULL;
  RETURN NEW;
END $$;
CREATE TRIGGER member_free_trial_tombstone AFTER INSERT ON commercial_erased_users
FOR EACH ROW EXECUTE FUNCTION member_free_trial_erasure();
ALTER TABLE commercial_erased_users ENABLE ALWAYS TRIGGER member_free_trial_tombstone;

COMMIT;
