-- Extend typed contracts without weakening account approval checks.
BEGIN;
ALTER TABLE approval_requests DROP CONSTRAINT approval_requests_action_type_check;
ALTER TABLE approval_requests DROP CONSTRAINT approval_requests_payload_check;
ALTER TABLE approval_requests DROP CONSTRAINT approval_requests_target_snapshot_check;
ALTER TABLE approval_requests DROP CONSTRAINT approval_requests_result_code_check;
ALTER TABLE approval_requests ALTER COLUMN target_user_id DROP NOT NULL;
ALTER TABLE approval_requests ADD CONSTRAINT approval_action_contract CHECK (
  (action_type IN ('account.deactivate','account.reactivate') AND target_user_id IS NOT NULL
   AND payload='{}'::jsonb AND jsonb_typeof(target_snapshot)='object'
   AND target_snapshot ?& ARRAY['active','role','auth_version']
   AND target_snapshot - ARRAY['active','role','auth_version']='{}'::jsonb
   AND jsonb_typeof(target_snapshot->'active')='boolean' AND target_snapshot->>'role'='CUSTOMER'
   AND jsonb_typeof(target_snapshot->'auth_version')='number'
   AND target_snapshot->>'auth_version' ~ '^[1-9][0-9]*$'
   AND (target_snapshot->>'auth_version')::bigint >= 1)
  OR (action_type='campaign.send' AND target_user_id IS NULL AND target_snapshot='{}'::jsonb
   AND jsonb_typeof(payload)='object' AND payload ?& ARRAY['campaign_id','content_hash']
   AND payload - ARRAY['campaign_id','content_hash']='{}'::jsonb
   AND jsonb_typeof(payload->'campaign_id')='string' AND payload->>'campaign_id' ~ '^[A-Za-z0-9_-]{1,160}$'
   AND jsonb_typeof(payload->'content_hash')='string' AND payload->>'content_hash' ~ '^[0-9a-f]{64}$')
);
ALTER TABLE approval_requests ADD CONSTRAINT approval_result_contract CHECK (result_code IS NULL OR result_code IN (
 'ok','already_target_state','canonical_applied','protected_positions','agents_revoke_pending',
 'execution_failed','target_changed','requester_changed','campaign_changed'));
CREATE UNIQUE INDEX approval_one_pending_campaign ON approval_requests((payload->>'campaign_id'))
 WHERE action_type='campaign.send' AND status='pending';
ALTER TABLE notification_outbox ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;
ALTER TABLE notification_outbox DROP CONSTRAINT notification_outbox_kind_check;
ALTER TABLE notification_outbox ADD CONSTRAINT outbox_kind_priority CHECK (
 (kind IN ('approval.pending_digest','approval.decision') AND priority=0) OR (kind='campaign.info' AND priority=10));
-- The unnamed payload CHECK is identified narrowly, not by catalog ordering.
DO $$
DECLARE item RECORD;
BEGIN
 FOR item IN SELECT conname FROM pg_constraint WHERE conrelid='notification_outbox'::regclass
   AND contype='c' AND pg_get_constraintdef(oid) LIKE '%jsonb_typeof(payload)%' LOOP
   EXECUTE format('ALTER TABLE notification_outbox DROP CONSTRAINT %I',item.conname);
 END LOOP;
END;
$$;
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
ALTER TABLE notification_outbox DROP CONSTRAINT notification_outbox_last_error_code_check;
ALTER TABLE notification_outbox ADD CONSTRAINT outbox_error_contract CHECK (last_error_code IS NULL OR last_error_code IN (
 'provider_unavailable','provider_timeout','provider_rejected','invalid_response','delivery_error',
 'recipient_unavailable','recipient_erased','lease_expired','attempts_exhausted','nothing_to_notify',
 'campaign_cancelled','opt_out','permanent_rejection'));
-- Preserve the original guard verbatim except for campaign-only terminal cancellation and deferral.
CREATE OR REPLACE FUNCTION notification_outbox_guard() RETURNS trigger AS $$
BEGIN
 IF TG_OP='INSERT' THEN
  IF NEW.status <> 'pending' OR NEW.attempts <> 0 OR NEW.sent_at IS NOT NULL OR NEW.last_error_code IS NOT NULL
   OR NEW.recipient_user_id='ERASED' OR EXISTS (SELECT 1 FROM commercial_erased_users WHERE user_hash =
    encode(sha256(convert_to(NEW.recipient_user_id,'UTF8')),'hex')) THEN
   RAISE EXCEPTION 'Invalid outbox creation' USING ERRCODE='55000'; END IF;
  RETURN NEW;
 END IF;
 IF pg_trigger_depth()>1 AND NEW.recipient_user_id='ERASED' AND NEW.payload='{}'::jsonb
  AND NEW.dedupe_key='erased:' || OLD.id AND NEW.last_error_code='recipient_erased'
  AND NEW.status=(CASE WHEN OLD.status='sent' THEN 'sent' ELSE 'dead' END)
  AND (to_jsonb(NEW)-ARRAY['recipient_user_id','payload','dedupe_key','status','last_error_code'])
   = (to_jsonb(OLD)-ARRAY['recipient_user_id','payload','dedupe_key','status','last_error_code'])
  AND EXISTS (SELECT 1 FROM commercial_erased_users WHERE user_hash =
   encode(sha256(convert_to(OLD.recipient_user_id,'UTF8')),'hex')) THEN RETURN NEW; END IF;
 IF OLD.status IN ('sent','dead') THEN RAISE EXCEPTION 'Terminal outbox is immutable' USING ERRCODE='55000'; END IF;
 IF (to_jsonb(NEW)-ARRAY['status','attempts','next_attempt_at','last_error_code','sent_at','payload'])
  IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','attempts','next_attempt_at','last_error_code','sent_at','payload'])
  OR NOT (
   (OLD.status IN ('pending','failed') AND NEW.status='sending' AND OLD.attempts<8 AND NEW.attempts=OLD.attempts+1)
   OR (OLD.status='sending' AND NEW.status IN ('sent','failed','pending','dead') AND NEW.attempts=OLD.attempts)
   OR (OLD.kind='campaign.info' AND OLD.status IN ('pending','failed') AND NEW.status='dead'
       AND NEW.attempts=OLD.attempts AND NEW.last_error_code='campaign_cancelled')
   OR (OLD.kind='campaign.info' AND OLD.status IN ('pending','failed') AND NEW.status=OLD.status
       AND NEW.attempts=OLD.attempts AND NEW.next_attempt_at>OLD.next_attempt_at
       AND NEW.last_error_code IS NOT DISTINCT FROM OLD.last_error_code)
  ) THEN RAISE EXCEPTION 'Invalid outbox transition' USING ERRCODE='55000'; END IF;
 IF NEW.payload IS DISTINCT FROM OLD.payload AND NOT (
   OLD.kind='approval.pending_digest' AND NOT OLD.payload ? 'count' AND NEW.status='sending'
   AND NEW.payload-'count'=OLD.payload AND NEW.payload ? 'count') THEN
   RAISE EXCEPTION 'Outbox payload is immutable' USING ERRCODE='55000'; END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE INDEX outbox_priority_due ON notification_outbox(priority,next_attempt_at,id)
 WHERE status IN ('pending','failed');
COMMIT;
