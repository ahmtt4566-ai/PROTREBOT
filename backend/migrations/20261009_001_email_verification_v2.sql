BEGIN;
SELECT pg_advisory_xact_lock(hashtext('email-verification-v2-migration'));
LOCK TABLE commercial_auth_users IN SHARE ROW EXCLUSIVE MODE;
CREATE TABLE IF NOT EXISTS commercial_feature_migrations (
    name TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
WITH first_run AS (
    INSERT INTO commercial_feature_migrations (name)
    VALUES ('email-verification-v2')
    ON CONFLICT (name) DO NOTHING
    RETURNING applied_at
)
UPDATE commercial_auth_users AS users
SET security = users.security || jsonb_build_object(
    'email_verified', TRUE,
    'email_verified_at', COALESCE(users.security->>'email_verified_at', first_run.applied_at::text)
)
FROM first_run
WHERE NULLIF(users.security->>'email', '') IS NOT NULL
  AND NULLIF(users.security->>'closed_at', '') IS NULL;
COMMIT;
