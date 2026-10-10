from pathlib import Path
import re

ROOT = Path(__file__).parents[1] / "migrations"


def test_campaign_schema_no_email_fk_and_narrow_mutations():
    sql = (ROOT / "20261010_011_campaigns.sql").read_text(encoding="utf-8")
    assert "REFERENCES" not in sql and "FOREIGN KEY" not in sql
    assert "PRIMARY KEY (campaign_id,user_id)" in sql
    for table in ("campaigns", "campaign_recipients", "email_preferences"):
        columns = sql.split("CREATE TABLE " + table + " (")[1].split(");")[0]
        assert "email TEXT" not in columns and "email_address" not in columns
    for clause in ("OLD.status <> 'draft'", "Campaign content frozen", "NEW.version <> OLD.version + 1",
                   "BEFORE DELETE OR TRUNCATE", "pg_trigger_depth() > 1", "AFTER INSERT ON commercial_erased_users"):
        assert clause in sql


def test_typed_campaign_approval_preserves_account_contract_and_self_decision():
    sql = (ROOT / "20261010_012_campaign_approval_outbox.sql").read_text(encoding="utf-8")
    for clause in ("target_user_id IS NOT NULL", "target_user_id IS NULL", "target_snapshot='{}'::jsonb",
                   "target_snapshot->>'role'='CUSTOMER'", "payload - ARRAY['campaign_id','content_hash']",
                   "approval_one_pending_campaign", "kind='campaign.info' AND priority=10", "Terminal outbox is immutable"):
        assert clause in sql
    assert "approval_guard" not in sql and "decided_by" not in sql
    assert "OLD.kind='campaign.info'" in sql and "NEW.attempts=OLD.attempts" in sql


def test_audit_and_permissions_exact_extension():
    from app.audit_log import AUDIT_ACTIONS
    from app.moderator_access import PERMISSIONS
    sql = (ROOT / "20261010_013_campaign_permissions_audit.sql").read_text(encoding="utf-8")
    assert tuple(re.findall(r"'([^']+)'", sql.split("CHECK (action IN (")[1].split("));")[0])) == AUDIT_ACTIONS
    assert tuple(re.findall(r"'([^']+)'", sql.split("CHECK (permission IN (")[1].split("));")[0])) == PERMISSIONS
