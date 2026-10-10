"""Offline append-only audit contract; no PostgreSQL migration is executed."""
import re
from pathlib import Path

from app.audit_log import AUDIT_ACTIONS


def test_audit_migration_is_append_only_and_survives_user_erasure():
    sql = (Path(__file__).parents[1] / "migrations" / "20261010_002_audit_log.sql").read_text(encoding="utf-8")
    statements = re.sub(r"--[^\n]*", "", sql)
    for column in (
        "id", "created_at", "actor_user_id", "actor_role", "action", "target_type", "target_id",
        "request_id", "approval_request_id", "reason", '"before"', '"after"', "ip_masked",
    ):
        assert re.search(rf"^\s*{re.escape(column)}\s+", statements, re.M)
    assert not re.search(r"\b(REFERENCES|FOREIGN\s+KEY|CASCADE)\b", statements, re.I)
    assert "commercial_erasure" not in statements
    check = re.search(r"CHECK \(action IN \((.*?)\)\)", statements, re.S)
    assert check is not None
    assert tuple(re.findall(r"'([^']+)'", check.group(1))) == AUDIT_ACTIONS
    assert re.search(
        r"CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_log"
        r"\s+FOR EACH STATEMENT EXECUTE FUNCTION audit_log_reject_mutation\(\)",
        statements,
    )
    assert "RAISE EXCEPTION 'audit_log is append-only' USING ERRCODE = '55000'" in statements
    assert "NEW.created_at := clock_timestamp()" in statements
    assert "DEFAULT clock_timestamp()" in statements
    assert "BEFORE INSERT ON audit_log" in statements
    for trigger in ("audit_log_insert_time", "audit_log_append_only"):
        assert f"ALTER TABLE audit_log ENABLE ALWAYS TRIGGER {trigger}" in statements
    for column in ("created_at", "actor_user_id", "target_id", "action"):
        assert re.search(rf"CREATE INDEX IF NOT EXISTS audit_log_{column} ON audit_log \({column}", statements)
    assert "application_state_snapshots" not in statements
    assert statements.strip().endswith("COMMIT;")
