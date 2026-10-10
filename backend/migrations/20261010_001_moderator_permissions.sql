-- Requires commercial_auth_users and commercial_erasure_user_guard.
-- Apply separately before enabling moderator endpoints; the application does
-- not auto-apply this migration. OWNER permissions are implicit, not seeded.
BEGIN;

CREATE TABLE IF NOT EXISTS moderator_permissions (
    user_id TEXT NOT NULL REFERENCES commercial_auth_users(user_id) ON DELETE CASCADE,
    permission TEXT NOT NULL CHECK (permission IN (
        'customers.view',
        'subscriptions.view',
        'payments.view',
        'support.view',
        'support.manage',
        'events.view',
        'approvals.create'
    )),
    granted_by TEXT NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, permission)
);

DROP TRIGGER IF EXISTS commercial_erasure_guard ON moderator_permissions;
CREATE TRIGGER commercial_erasure_guard BEFORE INSERT OR UPDATE ON moderator_permissions
    FOR EACH ROW EXECUTE FUNCTION commercial_erasure_user_guard();

COMMIT;
