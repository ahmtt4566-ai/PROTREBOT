BEGIN;
ALTER TABLE moderator_permissions DROP CONSTRAINT moderator_permissions_permission_check;
ALTER TABLE moderator_permissions ADD CONSTRAINT moderator_permissions_permission_check CHECK (permission IN (
 'customers.view','subscriptions.view','payments.view','support.view','support.manage','events.view','approvals.create','campaigns.manage'));
ALTER TABLE audit_log DROP CONSTRAINT audit_log_action_check;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_action_check CHECK (action IN (
 'ROLE_CHANGED','PERMISSION_GRANTED','PERMISSION_REVOKED','customer.viewed','customer.subscription.viewed','customer.payments.viewed',
 'support.case.viewed','support.case.taken','support.case.released','support.case.status_changed','support.note.added',
 'approval.requested','approval.approved','approval.rejected','approval.cancelled','approval.expired','approval.stale','approval.executed','approval.failed',
 'customer.events.viewed','campaign.created','campaign.updated','campaign.test_sent','campaign.send_requested',
 'campaign.send_started','campaign.cancelled','campaign.completed'));
ALTER TABLE audit_log DROP CONSTRAINT audit_log_target_type_check;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_target_type_check CHECK (target_type IN ('USER','MODERATOR_PERMISSION','SUPPORT_CASE','APPROVAL_REQUEST','CAMPAIGN'));
COMMIT;
