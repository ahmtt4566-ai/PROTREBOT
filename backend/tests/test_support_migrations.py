"""SQL contract only: no migration is applied to a database."""
from pathlib import Path

ROOT = Path(__file__).parents[1] / "migrations"


def test_support_schema_has_opaque_ownership_no_user_foreign_keys():
    sql = (ROOT / "20261010_004_support_cases.sql").read_text(encoding="utf-8")
    for field in ("legacy_ticket_id TEXT NOT NULL UNIQUE", "version INTEGER NOT NULL DEFAULT 1",
                  "user_id TEXT NOT NULL", "author_user_id TEXT NOT NULL",
                  "case_id TEXT NOT NULL REFERENCES support_cases(id)"):
        assert field in sql
    assert "REFERENCES commercial_auth_users" not in sql
    assert "application_state_snapshots" not in sql
    assert "BEFORE DELETE OR TRUNCATE ON support_notes" in sql
    assert "support_notes is append-only" in sql
    assert "BETWEEN 0 AND 2000" in sql
    assert "char_length(btrim(NEW.body)) = 0" in sql
    assert sql.strip().endswith("COMMIT;")


def test_erasure_exception_is_body_only_nested_and_tombstone_verified():
    sql = (ROOT / "20261010_004_support_cases.sql").read_text(encoding="utf-8")
    assert "pg_trigger_depth() > 1 AND NEW.body = ''" in sql
    assert "(to_jsonb(NEW) - 'body') = (to_jsonb(OLD) - 'body')" in sql
    assert "commercial_erased_users e" in sql
    assert "OLD.author_user_id" in sql and "OLD.case_id" in sql
    assert "AFTER INSERT ON commercial_erased_users" in sql
    assert "subject = '', message = '', legacy_response_note = ''" in sql
    assert "UPDATE support_notes n SET body = ''" in sql
    assert "PERFORM pg_advisory_xact_lock(61010004)" in sql
    assert "ENABLE ALWAYS TRIGGER" in sql
    from app.account_erasure import erase_database
    import inspect
    source = inspect.getsource(erase_database)
    assert "connection.transaction()" in source
    assert "INSERT INTO commercial_erased_users" in source


def test_support_audit_extension_preserves_existing_actions_and_target_types():
    sql = (ROOT / "20261010_005_support_audit_actions.sql").read_text(encoding="utf-8")
    for action in ("ROLE_CHANGED", "PERMISSION_GRANTED", "PERMISSION_REVOKED",
                   "customer.viewed", "customer.subscription.viewed", "customer.payments.viewed",
                   "support.case.viewed", "support.case.taken", "support.case.released",
                   "support.case.status_changed", "support.note.added"):
        assert f"'{action}'" in sql
    assert "'USER', 'MODERATOR_PERMISSION', 'SUPPORT_CASE'" in sql
    assert "UPDATE audit_log" not in sql and "DELETE FROM audit_log" not in sql
