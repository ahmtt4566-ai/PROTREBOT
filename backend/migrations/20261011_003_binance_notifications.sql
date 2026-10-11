-- Prepare only; after 20261011_002 and the existing campaign outbox migrations.
BEGIN;
ALTER TABLE notification_outbox DROP CONSTRAINT outbox_kind_priority;
ALTER TABLE notification_outbox ADD CONSTRAINT outbox_kind_priority CHECK (
 (kind IN ('approval.pending_digest','approval.decision','payment.binance.email','payment.binance.bell') AND priority=0)
 OR (kind='campaign.info' AND priority=10));
ALTER TABLE notification_outbox DROP CONSTRAINT outbox_payload_contract;
ALTER TABLE notification_outbox ADD CONSTRAINT outbox_payload_contract CHECK (jsonb_typeof(payload)='object' AND (
 (kind='approval.pending_digest' AND payload ? 'bucket' AND payload-ARRAY['bucket','count']='{}'::jsonb
  AND jsonb_typeof(payload->'bucket')='number' AND payload->>'bucket' ~ '^[0-9]+$'
  AND (NOT payload ? 'count' OR (jsonb_typeof(payload->'count')='number' AND payload->>'count' ~ '^[0-9]+$')))
 OR (kind='approval.decision' AND payload ?& ARRAY['approval_id','result']
  AND payload-ARRAY['approval_id','result']='{}'::jsonb AND jsonb_typeof(payload->'approval_id')='string'
  AND payload->>'approval_id' ~ '^[A-Za-z0-9_-]{1,160}$'
  AND jsonb_typeof(payload->'result')='number' AND payload->>'result' IN ('1','2','3','4'))
 OR (kind='campaign.info' AND payload ? 'campaign_id' AND payload-ARRAY['campaign_id','test']='{}'::jsonb
  AND jsonb_typeof(payload->'campaign_id')='string' AND payload->>'campaign_id' ~ '^[A-Za-z0-9_-]{1,160}$'
  AND (NOT payload ? 'test' OR payload->'test'='1'::jsonb))
 OR (kind IN ('payment.binance.email','payment.binance.bell') AND payload ?& ARRAY['payment_id','event']
  AND payload-ARRAY['payment_id','event']='{}'::jsonb
  AND jsonb_typeof(payload->'payment_id')='string' AND payload->>'payment_id' ~ '^[a-f0-9]{32}$'
  AND jsonb_typeof(payload->'event')='string' AND payload->>'event' IN ('pending','approved','rejected','renewal'))
 OR (recipient_user_id='ERASED' AND payload='{}'::jsonb)));
CREATE TABLE binance_notification_reads (
 outbox_id TEXT PRIMARY KEY,
 read_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE FUNCTION binance_notification_read_guard() RETURNS trigger AS $$
BEGIN
 IF TG_OP <> 'INSERT' THEN
  RAISE EXCEPTION 'Notification receipts are immutable' USING ERRCODE='55000';
 END IF;
 IF NOT EXISTS (SELECT 1 FROM notification_outbox WHERE id=NEW.outbox_id
  AND kind='payment.binance.bell' AND status='sent' AND recipient_user_id <> 'ERASED') THEN
  RAISE EXCEPTION 'Delivered bell required' USING ERRCODE='55000';
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER binance_notification_read_insert_update BEFORE INSERT OR UPDATE ON binance_notification_reads
 FOR EACH ROW EXECUTE FUNCTION binance_notification_read_guard();
CREATE TRIGGER binance_notification_read_no_delete BEFORE DELETE OR TRUNCATE ON binance_notification_reads
 FOR EACH STATEMENT EXECUTE FUNCTION notification_outbox_reject_delete();
ALTER TABLE binance_notification_reads ENABLE ALWAYS TRIGGER binance_notification_read_insert_update;
ALTER TABLE binance_notification_reads ENABLE ALWAYS TRIGGER binance_notification_read_no_delete;
COMMIT;
