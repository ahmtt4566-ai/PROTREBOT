-- PREPARE ONLY. Manual application requires separate approval.
-- Apply after 20261015_001_chart_ai.sql; no runtime schema installation.
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('chart-ai-schema-v1',0));

ALTER TABLE chart_ai_requests
    ADD COLUMN IF NOT EXISTS generation_attempts SMALLINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS accounted_microusd BIGINT NOT NULL DEFAULT 0;

UPDATE chart_ai_requests SET generation_attempts=1
    WHERE generation_attempts=0 AND provider_started_at IS NOT NULL;
UPDATE chart_ai_requests SET accounted_microusd=cost_microusd
    WHERE accounted_microusd=0 AND cost_microusd IS NOT NULL AND cost_microusd>0;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid='chart_ai_requests'::regclass AND conname='chart_ai_retry_accounting') THEN
        ALTER TABLE chart_ai_requests ADD CONSTRAINT chart_ai_retry_accounting CHECK (
            generation_attempts BETWEEN 0 AND 2
            AND accounted_microusd BETWEEN 0 AND 20000
            AND ((provider_started_at IS NULL AND generation_attempts=0 AND accounted_microusd=0)
                 OR (provider_started_at IS NOT NULL AND generation_attempts BETWEEN 1 AND 2))
            AND (cost_microusd IS NULL OR cost_microusd=accounted_microusd)
        );
    END IF;
END $$;
COMMIT;
