import re
from pathlib import Path

ROOT = Path(__file__).parents[1] / "migrations"


def test_approval_schema_guards_identities_transitions_erasure_and_replay():
    sql = (ROOT / "20261010_006_approval_requests.sql").read_text(encoding="utf-8")
    assert "REFERENCES" not in sql and "FOREIGN KEY" not in sql
    assert "decided_by <> requester_user_id" in sql
    assert "WHERE status = 'pending'" in sql
    assert "NEW.created_at + INTERVAL '72 hours'" in sql
    assert "NEW.version <> OLD.version + 1" in sql
    immutable = re.search(r"IF \(to_jsonb\(NEW\) - ARRAY\[(.*?)\]\)", sql, re.S).group(1)
    for field in ("action_type", "target_user_id", "requester_user_id", "requester_role", "payload", "target_snapshot", "reason", "created_at", "expires_at"):
        assert f"'{field}'" not in immutable
    for transition in ("OLD.status = 'pending'", "OLD.status = 'approved'", "OLD.status = 'executing'",
                       "OLD.status = 'failed'", "OLD.result_code = 'agents_revoke_pending'"):
        assert transition in sql
    assert "BEFORE DELETE OR TRUNCATE ON approval_requests" in sql
    assert "pg_trigger_depth() > 1 AND NEW.reason = '' AND NEW.decision_note = ''" in sql
    assert "AFTER INSERT ON commercial_erased_users" in sql
    assert "application_state_snapshots" not in sql
    assert sql.strip().endswith("COMMIT;")


def test_approval_audit_extension_keeps_all_previous_actions():
    old = (ROOT / "20261010_005_support_audit_actions.sql").read_text(encoding="utf-8")
    new = (ROOT / "20261010_007_approval_audit_actions.sql").read_text(encoding="utf-8")
    actions = lambda sql: set(re.findall(r"'([^']+)'", re.search(r"CHECK \(action IN \((.*?)\)\)", sql, re.S).group(1)))
    assert actions(old) < actions(new)
    assert len(actions(new) - actions(old)) == 8
    assert "'APPROVAL_REQUEST'" in new
