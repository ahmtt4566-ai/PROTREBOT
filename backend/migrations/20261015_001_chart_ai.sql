-- PREPARE ONLY. Manual application requires separate approval.
-- No backfill, provider call, scheduler registration or feature enablement.
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('chart-ai-schema-v1',0));

CREATE TABLE IF NOT EXISTS chart_ai_quota_periods (
    user_hash TEXT NOT NULL CHECK (user_hash ~ '^[a-f0-9]{64}$'),
    period_key TEXT NOT NULL,
    access_type TEXT NOT NULL CHECK (access_type IN ('trial','subscriber','owner')),
    period_start TIMESTAMPTZ,
    period_end TIMESTAMPTZ,
    quota_limit INTEGER,
    used INTEGER NOT NULL DEFAULT 0 CHECK (used >= 0),
    reserved INTEGER NOT NULL DEFAULT 0 CHECK (reserved BETWEEN 0 AND 1),
    PRIMARY KEY (user_hash,period_key),
    CHECK (quota_limit IS NULL OR used + reserved <= quota_limit),
    CHECK (
        (access_type='trial' AND period_key='trial' AND quota_limit IS NOT NULL AND quota_limit=3
            AND period_start IS NOT NULL AND period_end IS NOT NULL
            AND period_end=period_start + INTERVAL '168 hours')
        OR (access_type='subscriber' AND quota_limit IS NOT NULL AND quota_limit=20
            AND period_start IS NOT NULL AND period_end IS NOT NULL
            AND period_end=period_start + INTERVAL '168 hours'
            AND extract(isodow FROM period_start AT TIME ZONE 'UTC')=1
            AND period_start AT TIME ZONE 'UTC'=date_trunc('day',period_start AT TIME ZONE 'UTC')
            AND period_key='week:' || to_char(period_start AT TIME ZONE 'UTC','YYYY-MM-DD'))
        OR (access_type='owner' AND period_key='owner' AND quota_limit IS NULL
            AND period_start IS NULL AND period_end IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS chart_ai_acknowledgements (
    user_hash TEXT NOT NULL CHECK (user_hash ~ '^[a-f0-9]{64}$'),
    policy_version TEXT NOT NULL CHECK (policy_version ~ '^[a-z0-9-]{1,64}$'),
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (user_hash,policy_version)
);

CREATE TABLE IF NOT EXISTS chart_ai_monthly_budget (
    month_start DATE PRIMARY KEY CHECK (extract(day FROM month_start)=1),
    spent_microusd BIGINT NOT NULL DEFAULT 0 CHECK (spent_microusd >= 0),
    reserved_microusd BIGINT NOT NULL DEFAULT 0 CHECK (reserved_microusd >= 0),
    warning_enqueued BOOLEAN NOT NULL DEFAULT FALSE,
    CHECK (spent_microusd + reserved_microusd <= 25000000)
);

CREATE TABLE IF NOT EXISTS chart_ai_requests (
    user_hash TEXT NOT NULL,
    request_key UUID NOT NULL,
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    period_key TEXT NOT NULL,
    month_start DATE NOT NULL REFERENCES chart_ai_monthly_budget(month_start),
    status TEXT NOT NULL CHECK (status IN ('processing','succeeded','failed','expired')),
    lease_token UUID NOT NULL,
    lease_expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    provider_started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    result_expires_at TIMESTAMPTZ,
    result_text TEXT CHECK (result_text IS NULL OR length(result_text) BETWEEN 1 AND 32000),
    error_code TEXT CHECK (error_code IS NULL OR error_code ~ '^[a-z_]{1,64}$'),
    budget_state TEXT NOT NULL CHECK (budget_state IN ('reserved','held','settled')),
    cost_microusd BIGINT CHECK (cost_microusd BETWEEN 0 AND 20000),
    PRIMARY KEY (user_hash,request_key),
    FOREIGN KEY (user_hash,period_key) REFERENCES chart_ai_quota_periods(user_hash,period_key),
    CHECK (
        (status='processing' AND completed_at IS NULL AND result_expires_at IS NULL
            AND result_text IS NULL AND error_code IS NULL AND budget_state='reserved'
            AND cost_microusd IS NULL)
        OR (status IN ('succeeded','expired') AND completed_at IS NOT NULL
            AND provider_started_at IS NOT NULL AND error_code IS NULL
            AND result_expires_at IS NOT NULL
            AND result_expires_at=completed_at + INTERVAL '24 hours'
            AND budget_state='settled' AND cost_microusd IS NOT NULL
            AND ((status='succeeded' AND result_text IS NOT NULL)
                OR (status='expired' AND result_text IS NULL)))
        OR (status='failed' AND completed_at IS NOT NULL AND result_expires_at IS NULL
            AND result_text IS NULL AND error_code IS NOT NULL
            AND ((budget_state='settled' AND cost_microusd IS NOT NULL)
                OR (budget_state='held' AND cost_microusd IS NULL AND provider_started_at IS NOT NULL)))
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS chart_ai_one_processing_per_user ON chart_ai_requests(user_hash)
    WHERE status='processing';
CREATE INDEX IF NOT EXISTS chart_ai_expired_leases ON chart_ai_requests(lease_expires_at)
    WHERE status='processing';
CREATE INDEX IF NOT EXISTS chart_ai_expired_results ON chart_ai_requests(result_expires_at)
    WHERE status='succeeded';

-- Preserve the installed contracts verbatim and add only the new typed branch.
DO $$
DECLARE original_kind TEXT;
        original_payload TEXT;
        has_kind BOOLEAN;
        has_payload BOOLEAN;
BEGIN
    SELECT pg_get_expr(conbin,conrelid) INTO original_kind FROM pg_constraint
        WHERE conrelid='notification_outbox'::regclass AND conname='outbox_kind_priority';
    SELECT pg_get_expr(conbin,conrelid) INTO original_payload FROM pg_constraint
        WHERE conrelid='notification_outbox'::regclass AND conname='outbox_payload_contract';
    IF original_kind IS NULL OR original_payload IS NULL THEN
        RAISE EXCEPTION 'Existing notification contracts required' USING ERRCODE='55000';
    END IF;
    has_kind := strpos(original_kind,quote_literal('chart_ai.budget_warning')) > 0;
    has_payload := strpos(original_payload,quote_literal('chart_ai.budget_warning')) > 0;
    IF has_kind OR has_payload THEN
        IF NOT (has_kind AND has_payload) THEN
            RAISE EXCEPTION 'Incomplete chart AI notification contract' USING ERRCODE='55000';
        END IF;
        RETURN;
    END IF;
    ALTER TABLE notification_outbox DROP CONSTRAINT outbox_kind_priority;
    EXECUTE format(
        'ALTER TABLE notification_outbox ADD CONSTRAINT outbox_kind_priority CHECK ((%s) OR (kind=''chart_ai.budget_warning'' AND priority=0))',
        original_kind);
    ALTER TABLE notification_outbox DROP CONSTRAINT outbox_payload_contract;
    EXECUTE format(
        'ALTER TABLE notification_outbox ADD CONSTRAINT outbox_payload_contract CHECK ((%s) OR (
            kind=''chart_ai.budget_warning'' AND jsonb_typeof(payload)=''object''
            AND payload ?& ARRAY[''month'',''spent_microusd'',''budget_microusd'']
            AND payload-ARRAY[''month'',''spent_microusd'',''budget_microusd'']=''{}''::jsonb
            AND jsonb_typeof(payload->''month'')=''string''
            AND payload->>''month'' ~ ''^[0-9]{4}-(0[1-9]|1[0-2])$''
            AND jsonb_typeof(payload->''spent_microusd'')=''number''
            AND payload->>''spent_microusd'' ~ ''^[0-9]+$''
            AND (payload->>''spent_microusd'')::bigint BETWEEN 20000000 AND 25000000
            AND payload->''budget_microusd''=''25000000''::jsonb))',
        original_payload);
END $$;
COMMIT;
