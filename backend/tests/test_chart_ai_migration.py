"""Read-only schema contracts; the migration is never executed."""
import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]
SQL_PATH = ROOT / "migrations" / "20261015_001_chart_ai.sql"


def test_chart_schema_is_manual_only_with_isolated_tables_and_exact_limits():
    sql = SQL_PATH.read_text(encoding="utf-8")
    for table in ("quota_periods", "requests", "acknowledgements", "monthly_budget"):
        assert f"CREATE TABLE IF NOT EXISTS chart_ai_{table}" in sql
    assert "PREPARE ONLY" in sql and "Manual application requires separate approval" in sql
    assert "quota_limit=3" in sql and "quota_limit=20" in sql and "quota_limit IS NULL" in sql
    assert "25000000" in sql and "cost_microusd BETWEEN 0 AND 20000" in sql
    assert "PRIMARY KEY (user_hash,request_key)" in sql
    assert "chart_ai_one_processing_per_user" in sql
    assert "period_key='trial'" in sql and "AT TIME ZONE 'UTC'" in sql
    assert "result_expires_at=completed_at + INTERVAL '24 hours'" in sql
    assert "period_end IS NOT NULL" in sql and "result_expires_at IS NOT NULL" in sql
    assert not any(value in sql.lower() for value in ("base64", "image_data", "image_url", "api_key", "secret"))


def test_notification_extension_keeps_existing_contracts_and_guards_unchanged():
    sql = SQL_PATH.read_text(encoding="utf-8")
    assert "pg_get_expr(conbin,conrelid) INTO original_kind" in sql
    assert "pg_get_expr(conbin,conrelid) INTO original_payload" in sql
    assert sql.count("CHECK ((%s) OR (") == 2
    assert "kind=''chart_ai.budget_warning'' AND priority=0" in sql
    assert "ARRAY[''month'',''spent_microusd'',''budget_microusd'']" in sql
    assert "BETWEEN 20000000 AND 25000000" in sql
    assert "CREATE OR REPLACE FUNCTION" not in sql
    assert "UPDATE commercial_auth_users" not in sql
    assert "ALTER TABLE member_free_trials" not in sql
    assert "ALTER TABLE assistant_" not in sql and "ALTER TABLE analyst_" not in sql
    assert "IF has_kind OR has_payload THEN" in sql
    assert "IF NOT (has_kind AND has_payload) THEN" in sql
    assert "hashtextextended('chart-ai-schema-v1',0)" in sql


def test_store_has_no_schema_installation_sqlite_api_or_provider_side_effects():
    source = (ROOT / "app" / "chart_ai_store.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
    assert not any(name.startswith(("anthropic", "httpx", "requests", "sqlite3", "fastapi")) for name in modules)
    assert "CREATE TABLE" not in source and "ALTER TABLE" not in source
    assert "pg_advisory_xact_lock" in source and "FOR UPDATE" in source
    assert "clock_timestamp()" in source
    assert "datetime.now" not in source
    assert "async with conn.transaction()" in source


def test_stage_nine_checklist_records_approved_privacy_sentence():
    readme = (ROOT.parent / "README.md").read_text(encoding="utf-8")
    assert "Adim 9 yayin kontrol listesi - gizlilik" in readme
    assert "Resmin sitemizde saklanmaz; analiz için yapay zeka sağlayıcımıza gönderilir." in readme
    assert "Ayni cumle yayin oncesinde sitenin gizlilik metnine eklenecek." in readme
    assert "ZDR dogrulamasi\n  canli kullanim onkosulu degildir" in readme


def test_retry_migration_is_manual_repeatable_and_preserves_old_state_contracts():
    sql = (ROOT / "migrations" / "20261015_002_chart_ai_retry.sql").read_text(encoding="utf-8")
    assert "PREPARE ONLY" in sql and "Manual application requires separate approval" in sql
    assert "ADD COLUMN IF NOT EXISTS generation_attempts" in sql
    assert "ADD COLUMN IF NOT EXISTS accounted_microusd" in sql
    assert "generation_attempts BETWEEN 0 AND 2" in sql
    assert "accounted_microusd BETWEEN 0 AND 20000" in sql
    assert "cost_microusd=accounted_microusd" in sql
    assert "WHERE generation_attempts=0 AND provider_started_at IS NOT NULL" in sql
    assert "WHERE accounted_microusd=0 AND cost_microusd IS NOT NULL" in sql
    assert "pg_constraint" in sql and "IF NOT EXISTS" in sql
    assert "DROP " not in sql and "CREATE TABLE" not in sql
    assert "hashtextextended('chart-ai-schema-v1',0)" in sql
    assert "BEGIN;" in sql and "COMMIT;" in sql
    assert not any(word in sql for word in ("assistant_", "binance_", "api_key", "base64", "result_text"))
