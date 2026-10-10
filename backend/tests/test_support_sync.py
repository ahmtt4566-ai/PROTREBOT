import asyncio
import copy
from contextlib import asynccontextmanager

import pytest

from app.support_sync import sync_support_cases


class SyncStore:
    def __init__(self):
        self.tickets = [{"id": "legacy-1", "user_id": "customer", "subject": "Bağlantı sorusu",
                         "message": "Müşteri mesajı", "priority": "NORMAL", "status": "OPEN",
                         "created_at": "2026-10-10T10:00:00+00:00"}]
        self.cases = {}
        self.fresh = False
        self.queries = []
        self.transaction_active = True
        self.erased = set()
        self.roles = {"customer": "CUSTOMER"}

    def is_in_transaction(self):
        return self.transaction_active

    def check(self, sql):
        self.queries.append(sql)
        if "application_state_snapshots" in sql:
            assert sql.strip().startswith("SELECT"), "Snapshot write attempted"

    async def fetchrow(self, sql, *args):
        self.check(sql)
        if "support_sync_state" in sql:
            return {"fresh": self.fresh}
        if "application_state_snapshots" in sql:
            return {"payload": {"support_tickets": copy.deepcopy(self.tickets)}}
        if "commercial_auth_users" in sql:
            return {"user_id": args[0]} if self.roles.get(args[0]) == "CUSTOMER" and args[0] not in self.erased else None
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        self.check(sql)
        if "INSERT INTO support_cases" in sql:
            keys = ("id", "legacy_ticket_id", "user_id", "subject", "message", "priority", "legacy_status", "legacy_response_note", "created_at_legacy")
            incoming = dict(zip(keys, args))
            row = self.cases.get(args[1])
            if row:
                assert row["user_id"] == incoming["user_id"]
                for key in ("subject", "message", "priority", "legacy_status", "legacy_response_note"):
                    row[key] = incoming[key]
            else:
                self.cases[args[1]] = {**incoming, "case_status": "NEW", "assignee_user_id": None, "version": 1, "notes": []}
            return "INSERT 0 1"
        if "UPDATE support_sync_state" in sql:
            self.fresh = True
        return "SELECT 1"

    @asynccontextmanager
    async def transaction(self):
        before = copy.deepcopy((self.cases, self.fresh))
        try:
            yield
        except BaseException:
            self.cases, self.fresh = before
            raise


def test_sync_is_idempotent_preserves_team_fields_and_never_writes_snapshot():
    conn = SyncStore()
    asyncio.run(sync_support_cases(conn))
    row = conn.cases["legacy-1"]
    row.update(case_status="WAITING", assignee_user_id="moderator", version=8, notes=["note-id"])
    before_snapshot = copy.deepcopy(conn.tickets)
    conn.tickets[0].update(subject="Güncel başlık", status="RESOLVED", response_note="Eski cevap")
    conn.fresh = False
    asyncio.run(sync_support_cases(conn))
    assert len(conn.cases) == 1
    assert row["subject"] == "Güncel başlık" and row["legacy_status"] == "RESOLVED"
    assert row["case_status"] == "WAITING" and row["assignee_user_id"] == "moderator"
    assert row["version"] == 8 and row["notes"] == ["note-id"]
    assert conn.tickets[0]["message"] == before_snapshot[0]["message"]
    assert all(sql.strip().startswith("SELECT") for sql in conn.queries if "application_state_snapshots" in sql)
    update = next(sql.split("DO UPDATE SET")[1] for sql in conn.queries if "INSERT INTO support_cases" in sql)
    assert not any(field in update for field in ("case_status", "assignee_user_id", "version", "support_notes"))


def test_sync_cache_avoids_a_second_snapshot_scan():
    conn = SyncStore()
    asyncio.run(sync_support_cases(conn))
    asyncio.run(sync_support_cases(conn))
    assert sum("SELECT payload FROM application_state_snapshots" in sql for sql in conn.queries) == 1


@pytest.mark.parametrize("role,erased", [("CUSTOMER", True), ("OWNER", False), ("MODERATOR", False)])
def test_sync_ignores_erased_and_staff_customers(role, erased):
    conn = SyncStore()
    conn.roles["customer"] = role
    if erased:
        conn.erased.add("customer")
    asyncio.run(sync_support_cases(conn))
    assert conn.cases == {}


def test_sync_invalid_legacy_data_rolls_back_and_cannot_prime_cache():
    conn = SyncStore()
    conn.tickets.append({**conn.tickets[0], "id": "legacy-2", "priority": "INVALID"})
    async def attempt():
        async with conn.transaction():
            await sync_support_cases(conn)
    with pytest.raises(ValueError):
        asyncio.run(attempt())
    assert conn.cases == {} and conn.fresh is False


def test_sync_requires_transaction():
    conn = SyncStore()
    conn.transaction_active = False
    with pytest.raises(RuntimeError):
        asyncio.run(sync_support_cases(conn))
    assert conn.queries == []
