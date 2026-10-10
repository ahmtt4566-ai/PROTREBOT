-- Apply separately after 20261010_002_audit_log.sql. No audit rows are changed.
BEGIN;
ALTER TABLE audit_log DROP CONSTRAINT audit_log_action_check;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_action_check CHECK (action IN (
    'ROLE_CHANGED', 'PERMISSION_GRANTED', 'PERMISSION_REVOKED',
    'customer.viewed', 'customer.subscription.viewed', 'customer.payments.viewed'
));
COMMIT;
