from pathlib import Path


def test_outbox_schema_payload_allowlist_terminal_immutability_and_erasure():
    sql = (Path(__file__).parents[1] / "migrations" / "20261010_008_notification_outbox.sql").read_text(encoding="utf-8")
    assert "REFERENCES" not in sql
    for value in ("dedupe_key TEXT NOT NULL UNIQUE", "attempts BETWEEN 0 AND 8",
                  "OLD.status IN ('sent','dead')", "BEFORE DELETE OR TRUNCATE", "pg_trigger_depth() > 1",
                  "AFTER INSERT ON commercial_erased_users", "NEW.attempts = OLD.attempts + 1",
                  "payload - ARRAY['bucket','count']", "payload - ARRAY['approval_id','result']"):
        assert value in sql
    assert "email" not in sql.lower() and "reason" not in sql and "name" not in sql
    assert "application_state_snapshots" not in sql
