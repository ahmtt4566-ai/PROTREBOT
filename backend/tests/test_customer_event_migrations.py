import re
from pathlib import Path

ROOT = Path(__file__).parents[1] / "migrations"


def test_event_schema_bounded_aggregation_and_narrow_mutation_retention_erasure():
    sql = (ROOT / "20261010_009_customer_events.sql").read_text(encoding="utf-8")
    for field in ("email", "message", "token", "stack", "context", "ip_address", "username", "FOREIGN KEY"):
        assert field not in sql.split("CREATE INDEX")[0].replace("auth.email_verification", "auth.verification").replace("auth.email_verified", "auth.verified").replace("'email_verified'", "'verified'")
    for clause in ("rows_in_hour >= 49", "INTERVAL '10 minutes'", "INTERVAL '90 days'",
                   "NEW.count = OLD.count + 1", "pg_trigger_depth() > 1", "RETURN NULL",
                   "BEFORE TRUNCATE", "AFTER INSERT ON commercial_erased_users"):
        assert clause in sql
    assert "REFERENCES" not in sql


def test_audit_extension_preserves_previous_actions_and_adds_events_view_only():
    old = (ROOT / "20261010_007_approval_audit_actions.sql").read_text(encoding="utf-8")
    new = (ROOT / "20261010_010_customer_event_audit.sql").read_text(encoding="utf-8")
    def actions(sql):
        return set(re.findall(r"'([^']+)'", sql))
    assert "customer.events.viewed" in actions(new)
    assert actions(old) - {"USER", "MODERATOR_PERMISSION", "SUPPORT_CASE", "APPROVAL_REQUEST"} < actions(new)
