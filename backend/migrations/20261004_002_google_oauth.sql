BEGIN;
CREATE TABLE IF NOT EXISTS commercial_google_attempts (
    handle_hash TEXT PRIMARY KEY,
    binding_hash TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('authorization', 'consent')),
    payload TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    user_id TEXT
);
CREATE INDEX IF NOT EXISTS commercial_google_attempts_expiry
    ON commercial_google_attempts (expires_at);
CREATE TABLE IF NOT EXISTS commercial_google_identities (
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    user_id TEXT NOT NULL UNIQUE REFERENCES commercial_auth_users(user_id) ON DELETE CASCADE,
    user_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (issuer, subject)
);
COMMIT;
