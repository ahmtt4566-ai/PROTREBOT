-- Apply before starting/upgrading application workers, with writers quiesced.
-- Exported from ensure_commercial_schema and ensure_erasure_schema.
-- Requires the existing stores' normal schema; optional stores are not created here.
-- Reapply after installing optional stores to install their conditional guards.
-- Backfill uses only PostgreSQL v22-commercial snapshots, never local JSON backups.
BEGIN;

CREATE TABLE IF NOT EXISTS application_state_snapshots (
  state_key TEXT PRIMARY KEY,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS commercial_auth_users (
  user_id TEXT PRIMARY KEY,
  auth_version BIGINT NOT NULL,
  security JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS commercial_erased_users (
  user_hash TEXT PRIMARY KEY, erased_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION commercial_erasure_user_guard() RETURNS trigger AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM commercial_erased_users
             WHERE user_hash = encode(sha256(convert_to(NEW.user_id, 'UTF8')), 'hex')) THEN
    IF TG_TABLE_NAME = 'assistant_calls' THEN
      NEW.user_id := 'ERASED';
      RETURN NEW;
    ELSIF TG_TABLE_NAME = 'subscriptions' THEN
      NEW.user_id := 'ERASED';
      NEW.stripe_customer_id := NULL;
      NEW.stripe_subscription_id := NULL;
      NEW.stripe_price_id := NULL;
      RETURN NEW;
    ELSIF TG_TABLE_NAME = 'error_events' THEN
      NEW.user_id := NULL; NEW.request_id := NULL;
      NEW.message := 'Account erased'; NEW.context := '{}'::jsonb;
      NEW.details := '{}'::jsonb; NEW.stack := NULL; NEW.notes := NULL;
      NEW.route := NULL; NEW.fingerprint := 'ERASED';
      RETURN NEW;
    END IF;
    RETURN NULL;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS commercial_erasure_guard ON commercial_auth_users;
CREATE TRIGGER commercial_erasure_guard BEFORE INSERT OR UPDATE ON commercial_auth_users
  FOR EACH ROW EXECUTE FUNCTION commercial_erasure_user_guard();

CREATE OR REPLACE FUNCTION commercial_erasure_json(data jsonb) RETURNS jsonb AS $$
DECLARE field text; item jsonb; result jsonb; identity text;
BEGIN
  IF jsonb_typeof(data) = 'array' THEN
    SELECT COALESCE(jsonb_agg(commercial_erasure_json(value)), '[]'::jsonb)
      INTO result FROM jsonb_array_elements(data);
    RETURN result;
  ELSIF jsonb_typeof(data) <> 'object' THEN
    RETURN data;
  END IF;
  FOREACH field IN ARRAY ARRAY['id','user_id','_user_id','owner_user_id','actor','subject']
  LOOP
    identity := data->>field;
    IF identity IS NOT NULL AND EXISTS (SELECT 1 FROM commercial_erased_users
        WHERE user_hash = encode(sha256(convert_to(identity, 'UTF8')), 'hex')) THEN
      SELECT COALESCE(jsonb_object_agg(key,value), '{}'::jsonb) INTO result
      FROM jsonb_each(data) WHERE key IN ('plan','status','kind','created_at','updated_at',
        'amount','amount_usd','amount_usdt','currency','price','quantity','symbol','side',
        'realized_pnl','pnl','profit','fee','fees','cost_usd');
      RETURN result || '{"user_id":"ERASED"}'::jsonb;
    END IF;
  END LOOP;
  result := '{}'::jsonb;
  FOR field,item IN SELECT key,value FROM jsonb_each(data) LOOP
    IF NOT EXISTS (SELECT 1 FROM commercial_erased_users
        WHERE user_hash = encode(sha256(convert_to(field, 'UTF8')), 'hex')) THEN
      result := result || jsonb_build_object(field,commercial_erasure_json(item));
    END IF;
  END LOOP;
  RETURN result;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION commercial_erasure_payload_guard() RETURNS trigger AS $$
DECLARE cleaned jsonb;
BEGIN
  cleaned := commercial_erasure_json(NEW.payload);
  IF TG_TABLE_NAME = 'protrebot_cloud_evidence' AND cleaned IS DISTINCT FROM NEW.payload THEN
    NEW.event_key := 'erased-event:' || md5(random()::text || clock_timestamp()::text);
  END IF;
  NEW.payload := cleaned;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION commercial_erasure_snapshot_guard() RETURNS trigger AS $$
DECLARE erased_user jsonb; uid text; field text; items jsonb;
BEGIN
  IF NEW.state_key LIKE 'binance_demo:user:%' OR NEW.state_key LIKE 'v21_demo:user:%' THEN
    IF EXISTS (SELECT 1 FROM commercial_erased_users
               WHERE user_hash = encode(sha256(convert_to(split_part(NEW.state_key, ':', 3), 'UTF8')), 'hex')) THEN
      RETURN NULL;
    END IF;
  END IF;
  IF NEW.state_key = 'v22-commercial' THEN
    FOR erased_user IN SELECT value FROM jsonb_array_elements(COALESCE(NEW.payload->'users', '[]'::jsonb))
      WHERE EXISTS (SELECT 1 FROM commercial_erased_users
        WHERE user_hash = encode(sha256(convert_to(value->>'id', 'UTF8')), 'hex'))
    LOOP
      uid := erased_user->>'id';
      FOREACH field IN ARRAY ARRAY['users','profiles','licenses','agents','auth_tokens',
          'pairing_codes','leads','support_tickets','acceptances','stripe_checkout_sessions']
      LOOP
        SELECT COALESCE(jsonb_agg(value), '[]'::jsonb) INTO items
        FROM jsonb_array_elements(COALESCE(NEW.payload->field, '[]'::jsonb))
        WHERE COALESCE(value->>'id','') <> uid AND COALESCE(value->>'user_id','') <> uid
          AND COALESCE(value->>'email','') <> COALESCE(erased_user->>'email', '');
        NEW.payload := jsonb_set(NEW.payload, ARRAY[field], items);
      END LOOP;
      FOREACH field IN ARRAY ARRAY['subscriptions','demo_invoices','audit']
      LOOP
        SELECT COALESCE(jsonb_agg(CASE
          WHEN value->>'user_id' = uid OR value->>'actor' = uid OR value->>'subject' = uid
          THEN (SELECT COALESCE(jsonb_object_agg(key, val), '{}'::jsonb)
                FROM jsonb_each(value) AS entry(key,val)
                WHERE key IN ('plan','status','kind','created_at','updated_at',
                  'amount','amount_usd','amount_usdt','currency','price','quantity','symbol','side','realized_pnl'))
                || '{"user_id":"ERASED"}'::jsonb
          ELSE value END), '[]'::jsonb) INTO items
        FROM jsonb_array_elements(COALESCE(NEW.payload->field, '[]'::jsonb));
        NEW.payload := jsonb_set(NEW.payload, ARRAY[field], items);
      END LOOP;
    END LOOP;
  END IF;
  IF NEW.state_key = 'v22-commercial' THEN
    SELECT COALESCE(jsonb_agg(value || jsonb_build_object('auth_version',auth.auth_version)
               || auth.security), '[]'::jsonb) INTO items
    FROM jsonb_array_elements(COALESCE(NEW.payload->'users','[]'::jsonb))
    JOIN commercial_auth_users AS auth ON auth.user_id = value->>'id';
    NEW.payload := jsonb_set(NEW.payload, ARRAY['users'], items);
  END IF;
  NEW.payload := commercial_erasure_json(NEW.payload);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS commercial_erasure_guard ON application_state_snapshots;
CREATE TRIGGER commercial_erasure_guard BEFORE INSERT OR UPDATE ON application_state_snapshots
  FOR EACH ROW EXECUTE FUNCTION commercial_erasure_snapshot_guard();

-- Matches runtime bootstrap: never overwrite existing canonical security/version.
-- The tombstone trigger above rejects erased identities during this backfill.
INSERT INTO commercial_auth_users (user_id, auth_version, security)
SELECT u->>'id', COALESCE((u->>'auth_version')::bigint, 1),
       jsonb_build_object('password', u->'password', 'active', u->'active',
                          'role', u->'role', 'email_verified', u->'email_verified')
FROM application_state_snapshots,
     jsonb_array_elements(payload->'users') AS u
WHERE state_key = 'v22-commercial'
ON CONFLICT (user_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS commercial_auth_limits (
  bucket TEXT PRIMARY KEY,
  window_start TIMESTAMPTZ NOT NULL,
  attempts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trading_accounts (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  environment TEXT NOT NULL CHECK (environment IN ('DEMO', 'TESTNET', 'PAPER', 'LIVE')),
  account_reference TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'UNASSIGNED',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (user_id, provider, environment, account_reference)
);

CREATE INDEX IF NOT EXISTS trading_accounts_user_id_idx ON trading_accounts (user_id);

CREATE TABLE IF NOT EXISTS subscriptions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  stripe_customer_id TEXT,
  stripe_subscription_id TEXT UNIQUE,
  stripe_price_id TEXT,
  plan TEXT NOT NULL CHECK (plan IN ('TRIAL', 'MASTER_MODE')),
  status TEXT NOT NULL CHECK (status IN ('TRIALING', 'ACTIVE', 'PAST_DUE', 'UNPAID', 'CANCELLED', 'EXPIRED')),
  trial_start TIMESTAMPTZ,
  trial_end TIMESTAMPTZ,
  current_period_start TIMESTAMPTZ,
  current_period_end TIMESTAMPTZ,
  cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
  canceled_at TIMESTAMPTZ,
  last_payment_status TEXT,
  last_payment_at TIMESTAMPTZ,
  failed_payment_attempts INTEGER NOT NULL DEFAULT 0,
  grace_until TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS subscriptions_user_id_idx ON subscriptions (user_id);
CREATE INDEX IF NOT EXISTS subscriptions_stripe_customer_id_idx ON subscriptions (stripe_customer_id);
CREATE UNIQUE INDEX IF NOT EXISTS subscriptions_one_recoverable_per_user_idx
ON subscriptions (user_id)
WHERE status IN ('TRIALING', 'ACTIVE', 'PAST_DUE');

CREATE TABLE IF NOT EXISTS stripe_webhook_events (
  id TEXT PRIMARY KEY,
  stripe_event_id TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL,
  processed BOOLEAN NOT NULL DEFAULT FALSE,
  processed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Install the same erasure-time guards immediately for already-existing stores.
-- Optional store schemas must have the columns expected by their runtime creators.
DO $migration$
DECLARE store_name text;
BEGIN
  FOREACH store_name IN ARRAY ARRAY[
    'trading_accounts', 'protrebot_exchange_session_vault', 'assistant_usage',
    'assistant_proactive_state', 'analyst_credits', 'analyst_requests', 'analyst_cache',
    'assistant_calls', 'subscriptions', 'error_events'
  ]
  LOOP
    IF to_regclass(store_name) IS NOT NULL THEN
      EXECUTE format('DROP TRIGGER IF EXISTS commercial_erasure_guard ON %I', store_name);
      EXECUTE format(
        'CREATE TRIGGER commercial_erasure_guard BEFORE INSERT OR UPDATE ON %I '
        'FOR EACH ROW EXECUTE FUNCTION commercial_erasure_user_guard()', store_name
      );
    END IF;
  END LOOP;
  FOREACH store_name IN ARRAY ARRAY[
    'protrebot_cloud_state', 'protrebot_cloud_evidence', 'paper_account_snapshots'
  ]
  LOOP
    IF to_regclass(store_name) IS NOT NULL THEN
      EXECUTE format('DROP TRIGGER IF EXISTS commercial_erasure_guard ON %I', store_name);
      EXECUTE format(
        'CREATE TRIGGER commercial_erasure_guard BEFORE INSERT OR UPDATE ON %I '
        'FOR EACH ROW EXECUTE FUNCTION commercial_erasure_payload_guard()', store_name
      );
    END IF;
  END LOOP;
END;
$migration$;

COMMIT;
