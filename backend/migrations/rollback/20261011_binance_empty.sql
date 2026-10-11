-- Empty-install rollback ONLY, all three Binance migrations in reverse order.
-- Stop writers and disable payment access/notifications/Pay ID first.
-- If payment history exists, preserve this schema and use the operational
-- flag/code rollback instead. Never remove payment/audit/dedupe history.
BEGIN;
LOCK TABLE binance_manual_payment_requests,binance_manual_periods,binance_notification_reads,
    notification_outbox,audit_log IN ACCESS EXCLUSIVE MODE;
DO $$
BEGIN
 IF EXISTS (SELECT 1 FROM binance_manual_payment_requests)
    OR EXISTS (SELECT 1 FROM binance_manual_periods)
    OR EXISTS (SELECT 1 FROM binance_notification_reads)
    OR EXISTS (SELECT 1 FROM notification_outbox WHERE kind IN ('payment.binance.email','payment.binance.bell'))
    OR EXISTS (SELECT 1 FROM audit_log WHERE action IN ('payment.binance.approved','payment.binance.rejected')
        OR target_type='BINANCE_PAYMENT') THEN
  RAISE EXCEPTION 'Binance history exists: use non-destructive operational rollback' USING ERRCODE='55000';
 END IF;
END;
$$;
-- 003: restore prior typed outbox contracts; existing outbox guards stay installed.
DROP TABLE binance_notification_reads;
DROP FUNCTION binance_notification_read_guard();
ALTER TABLE notification_outbox DROP CONSTRAINT outbox_kind_priority;
ALTER TABLE notification_outbox ADD CONSTRAINT outbox_kind_priority CHECK (
 (kind IN ('approval.pending_digest','approval.decision') AND priority=0) OR (kind='campaign.info' AND priority=10));
ALTER TABLE notification_outbox DROP CONSTRAINT outbox_payload_contract;
ALTER TABLE notification_outbox ADD CONSTRAINT outbox_payload_contract CHECK (jsonb_typeof(payload)='object' AND (
 (kind='approval.pending_digest' AND payload ? 'bucket' AND payload - ARRAY['bucket','count']='{}'::jsonb
  AND jsonb_typeof(payload->'bucket')='number' AND payload->>'bucket' ~ '^[0-9]+$'
  AND (NOT payload ? 'count' OR (jsonb_typeof(payload->'count')='number' AND payload->>'count' ~ '^[0-9]+$')))
 OR (kind='approval.decision' AND payload ?& ARRAY['approval_id','result']
  AND payload - ARRAY['approval_id','result']='{}'::jsonb AND jsonb_typeof(payload->'approval_id')='string'
  AND payload->>'approval_id' ~ '^[A-Za-z0-9_-]{1,160}$'
  AND jsonb_typeof(payload->'result')='number' AND payload->>'result' IN ('1','2','3','4'))
 OR (kind='campaign.info' AND payload ? 'campaign_id' AND payload - ARRAY['campaign_id','test']='{}'::jsonb
  AND jsonb_typeof(payload->'campaign_id')='string' AND payload->>'campaign_id' ~ '^[A-Za-z0-9_-]{1,160}$'
  AND (NOT payload ? 'test' OR payload->'test'='1'::jsonb))
 OR (recipient_user_id='ERASED' AND payload='{}'::jsonb)));
-- 002, then 001: remove only the empty feature objects, without CASCADE.
DROP TRIGGER binance_manual_period_erasure ON commercial_erased_users;
DROP TRIGGER binance_manual_payment_erasure ON commercial_erased_users;
DROP TRIGGER binance_manual_payment_complete ON binance_manual_payment_requests;
DROP TABLE binance_manual_periods;
DROP FUNCTION binance_manual_period_guard();
DROP FUNCTION binance_manual_period_erase();
DROP FUNCTION binance_manual_decision_complete();
DROP TABLE binance_manual_payment_requests;
DROP FUNCTION binance_manual_payment_guard();
DROP FUNCTION binance_manual_payment_erase();
DROP FUNCTION binance_manual_payment_reject_delete();
ALTER TABLE audit_log DROP CONSTRAINT audit_log_action_check;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_action_check CHECK (action IN (
 'ROLE_CHANGED','PERMISSION_GRANTED','PERMISSION_REVOKED','customer.viewed','customer.subscription.viewed','customer.payments.viewed',
 'support.case.viewed','support.case.taken','support.case.released','support.case.status_changed','support.note.added',
 'approval.requested','approval.approved','approval.rejected','approval.cancelled','approval.expired','approval.stale','approval.executed','approval.failed',
 'customer.events.viewed','campaign.created','campaign.updated','campaign.test_sent','campaign.send_requested',
 'campaign.send_started','campaign.cancelled','campaign.completed'));
ALTER TABLE audit_log DROP CONSTRAINT audit_log_target_type_check;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_target_type_check
 CHECK (target_type IN ('USER','MODERATOR_PERMISSION','SUPPORT_CASE','APPROVAL_REQUEST','CAMPAIGN'));
COMMIT;
