"""Offline risk consent, projections and transactional audit regression tests."""
import asyncio
import copy
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import account_settings, original_live_risk as risk, original_v2_live as original, v25_execution as live
from test_original_v2_demo_execution import EntryClient, result


@pytest.mark.parametrize("value,level", [(0.5, "NORMAL"), (1, "NORMAL"), (1.01, "YELLOW"), (2, "YELLOW"), (2.01, "ORANGE"), (5, "ORANGE"), (5.01, "RED"), (10, "RED")])
def test_trade_warning_boundaries(value, level):
    assert risk.trade_warning(value) == level


@pytest.mark.parametrize("value,level", [(1, "NORMAL"), (5, "NORMAL"), (5.01, "ORANGE"), (10, "ORANGE"), (10.01, "RED"), (25, "RED")])
def test_daily_warning_boundaries(value, level):
    assert risk.daily_warning(value) == level


@pytest.mark.parametrize("fields", [
    {"risk_percent": 0.49}, {"risk_percent": 10.01}, {"risk_percent": float("nan")},
    {"risk_percent": True}, {"risk_percent": "1"}, {"daily_loss_percent": 0.99},
    {"daily_loss_percent": 25.01}, {"daily_loss_percent": float("inf")},
    {"risk_percent": 2, "daily_loss_percent": 1},
    {"risk_percent": 2.01}, {"risk_percent": 5, "risk_acknowledged": "true"},
    {"risk_percent": 5.01, "daily_loss_percent": 10},
    {"risk_percent": 10, "daily_loss_percent": 25, "high_risk_confirmation": "YUKSEK RISKI KABUL EDIYORUM"},
])
def test_invalid_or_unconfirmed_risk_is_rejected(fields):
    with pytest.raises(ValidationError):
        risk.RiskChoice(**fields)


def test_projection_is_compound_and_keeps_one_percent_reference():
    values = risk.projection(1000, 10, 25, 1200)
    assert values["estimated_trade_loss_usdt"] == 100
    assert values["balance_after_5_losses_usdt"] == pytest.approx(590.49)
    assert values["balance_after_10_losses_usdt"] == pytest.approx(348.6784401)
    assert values["baseline_1_percent_after_5_losses_usdt"] == pytest.approx(950.9900499)
    assert values["baseline_1_percent_after_10_losses_usdt"] == pytest.approx(904.3820750088)
    assert values["daily_loss_limit_usdt"] == 300


class Connection:
    def __init__(self, state, *, failure=None):
        self.state = state
        self.failure = failure
        self.active = False
        self.committed = False
        self.rolled_back = False
        self.snapshot = None
        self.audits = []

    def is_in_transaction(self):
        return self.active

    @asynccontextmanager
    async def transaction(self):
        self.active = True
        try:
            yield
        except Exception:
            self.snapshot = None
            self.audits.clear()
            self.rolled_back = True
            raise
        else:
            self.committed = True
        finally:
            self.active = False

    async def fetchrow(self, query, key, payload, expected_revision):
        assert self.active
        assert query == live.V25_RISK_SNAPSHOT_UPSERT_SQL
        assert expected_revision == self.state["original_v2_live"].get("risk_revision", 0)
        assert key == live.V25_SNAPSHOT_KEY
        assert self.state["original_v2_live"]["risk_percent"] == 0.5
        if self.failure == "snapshot":
            raise RuntimeError("offline snapshot failure")
        if self.failure == "stale_database":
            return None
        self.snapshot = json.loads(payload)
        if self.failure == "concurrent_state":
            self.state["persistence_revision"] += 1
        return {"write_status": "UPDATED"}

    async def execute(self, query, *values):
        assert self.active
        if self.failure == "audit":
            raise RuntimeError("offline audit failure")
        assert "INSERT INTO audit_log" in query
        self.audits.append(values)
        return "INSERT 0 1"


def context(failure=None):
    state = live.initial_state()
    state["lock"] = asyncio.Lock()
    state["original_v2_live"] = {
        "owner_id": "owner", "fingerprint": "fp", "risk_percent": 0.5, "daily_loss_percent": 5,
        "enabled": True, "paused": False,
        "universe": {
            "updated_day": datetime.now(timezone.utc).date().isoformat(),
            "minimum_age_days": original.ORIGINAL_MIN_SYMBOL_AGE_DAYS,
            "ranked_symbols": ["BTCUSDT"], "eligible_count": 1,
        },
        "daily": {"day": datetime.now(timezone.utc).date().isoformat(), "opening_wallet": 1000, "loss": 60, "limit": 50, "locked": True},
    }
    state["plans"] = {"existing": {"initial_quantity": "1", "initial_stop_loss": "90", "stop_loss": "90", "tp1": "110", "tp3": "120"}}
    conn = Connection(state, failure=failure)
    @asynccontextmanager
    async def acquire():
        yield conn
    application = SimpleNamespace(state=SimpleNamespace(db_pool=SimpleNamespace(acquire=acquire), v25_execution=state))
    request = SimpleNamespace(app=application, state=SimpleNamespace(), client=None)
    return request, state, conn


def high_choice(**fields):
    return risk.RiskChoice(
        **{"risk_percent": 10, "daily_loss_percent": 25, "high_risk_confirmation": risk.HIGH_RISK_CONFIRMATION,
           "warning_balance_usdt": 1000, **fields},
    )


def save(request, state, choice, snapshot=None, fingerprint="fp"):
    return asyncio.run(risk.save_choice(
        request, {"id": "owner", "role": "OWNER"}, state, choice,
        snapshot if snapshot is not None else {"wallet_balance": 1000}, fingerprint,
    ))


def test_initial_default_risk_understanding_is_audited_once():
    request, state, conn = context()
    choice = risk.RiskChoice(understood=True, warning_balance_usdt=1000)
    config = save(request, state, choice)
    assert config["risk_percent"] == 0.5 and config["daily_loss_percent"] == 5
    assert config["risk_revision"] == 1 and config["understood"] is True
    assert len(conn.audits) == 1
    assert json.loads(conn.audits[0][8])["understood"] is False
    assert json.loads(conn.audits[0][9])["understood"] is True
    save(request, state, choice.model_copy(update={"expected_risk_revision": 1}))
    assert len(conn.audits) == 1


def test_risk_and_audit_commit_together_without_reopening_daily_lock_or_touching_positions():
    request, state, conn = context()
    old_plans = copy.deepcopy(state["plans"])
    old_authority = copy.deepcopy(state["auto_authorization"])
    config = save(request, state, high_choice())
    assert conn.committed
    assert config["risk_percent"] == 10
    assert config["risk_revision"] == 1
    assert config["daily"]["locked"] is True
    assert config["daily"]["limit"] == 250
    assert config["enabled"] is True
    assert state["plans"] == old_plans
    assert state["auto_authorization"] == old_authority
    assert conn.snapshot["original_v2_live"]["risk_percent"] == 10
    assert len(conn.audits) == 1
    row = conn.audits[0]
    assert row[:5] == ("owner", "OWNER", "original_live.risk_changed", "USER", "owner")
    before, after = json.loads(row[8]), json.loads(row[9])
    assert before["risk_percent"] == 0.5
    assert after["risk_percent"] == 10
    assert after["daily_loss_percent"] == 25
    assert after["warning_level"] == after["daily_warning_level"] == "RED"
    assert after["high_risk_confirmed"] is True
    assert after["warning_balance_usdt"] == 1000
    assert not any(key in after for key in ("totp_code", "api_key", "secret_key", "high_risk_confirmation"))


@pytest.mark.parametrize("failure,status", [("snapshot", 503), ("audit", 503), ("stale_database", 409), ("concurrent_state", 409)])
def test_failed_atomic_write_preserves_previous_risk_and_rolls_back(failure, status):
    request, state, conn = context(failure)
    before = copy.deepcopy(state["original_v2_live"])
    plans = copy.deepcopy(state["plans"])
    with pytest.raises(HTTPException) as error:
        save(request, state, high_choice())
    assert error.value.status_code == status
    assert conn.rolled_back
    assert state["original_v2_live"] == before
    assert state["plans"] == plans
    assert conn.snapshot is None
    assert conn.audits == []


@pytest.mark.parametrize("wallet", [None, 0, -1, True, "1000", float("nan"), float("inf")])
def test_unverified_balance_cannot_be_used_for_risk_consent(wallet):
    request, state, conn = context()
    with pytest.raises(HTTPException) as error:
        save(request, state, high_choice(), {"wallet_balance": wallet})
    assert error.value.status_code == 423
    assert not conn.committed


@pytest.mark.parametrize("fields,snapshot,fingerprint,status", [
    ({"expected_risk_revision": 1}, {"wallet_balance": 1000}, "fp", 409),
    ({}, {"wallet_balance": 1001}, "fp", 409),
    ({"warning_balance_usdt": None}, {"wallet_balance": 1000}, "fp", 422),
    ({}, {"wallet_balance": 1000}, "other-account", 423),
])
def test_stale_revision_balance_and_account_binding_reject_change(fields, snapshot, fingerprint, status):
    request, state, conn = context()
    with pytest.raises(HTTPException) as error:
        save(request, state, high_choice(**fields), snapshot, fingerprint)
    assert error.value.status_code == status
    assert not conn.committed


def test_no_change_creates_no_duplicate_audit():
    request, state, conn = context()
    save(request, state, risk.RiskChoice())
    assert not conn.committed
    assert conn.audits == []


def test_legacy_daily_record_without_opening_wallet_can_save_without_postcommit_failure():
    request, state, conn = context()
    state["original_v2_live"]["daily"] = {"day": "saved-day", "loss": 12, "locked": False}
    config = save(request, state, high_choice())
    assert conn.committed
    assert config["risk_percent"] == 10
    assert config["daily"] == {"day": "saved-day", "loss": 12, "locked": False}


@pytest.mark.parametrize("loss", [None, True, "10", -1, float("nan")])
def test_invalid_daily_loss_is_rejected_before_persistence(loss):
    request, state, conn = context()
    state["original_v2_live"]["daily"]["loss"] = loss
    with pytest.raises(HTTPException) as error:
        save(request, state, high_choice())
    assert error.value.status_code == 423
    assert not conn.committed


def test_risk_change_requires_durable_database():
    request, state, _ = context()
    request.app.state.db_pool = None
    before = copy.deepcopy(state["original_v2_live"])
    with pytest.raises(HTTPException) as error:
        save(request, state, high_choice())
    assert error.value.status_code == 503
    assert state["original_v2_live"] == before


def test_tightening_daily_limit_latches_without_touching_open_plans():
    request, state, conn = context()
    state["original_v2_live"]["daily"].update(loss=20, locked=False)
    plans = copy.deepcopy(state["plans"])
    choice = risk.RiskChoice(risk_percent=1, daily_loss_percent=1, warning_balance_usdt=1000)
    config = save(request, state, choice)
    assert conn.committed
    assert config["daily"]["limit"] == 10
    assert config["daily"]["locked"] is True
    assert state["plans"] == plans


def test_effective_trade_risk_reports_existing_limits_without_changing_demo_policy():
    request, state, _ = context()
    save(request, state, high_choice())
    fixed = copy.deepcopy(original.strategy.fixed_profile())
    before_plans = copy.deepcopy(state["plans"])
    client = EntryClient()
    spec = asyncio.run(original.build_spec(client, result(), state, {"wallet_balance": 1000}))
    assert spec["margin_usdt"] <= 25
    assert spec["leverage"] == 3
    assert spec["estimated_stop_loss_usdt"] <= 3
    assert spec["risk_adjustment"]["selected_percent"] == 10
    assert spec["risk_adjustment"]["effective_percent"] <= 0.3
    assert spec["risk_adjustment"]["reasons"]
    assert original.strategy.fixed_profile() == fixed
    assert state["plans"] == before_plans
    assert not any(method == "POST" for method, _, _ in client.calls)


def test_unapproved_stored_high_risk_blocks_new_entry():
    _, state, _ = context()
    state["original_v2_live"].update(risk_percent=10, daily_loss_percent=25)
    with pytest.raises(HTTPException):
        original.execution_policy(state, {"wallet_balance": 1000})


def test_risk_endpoint_uses_understanding_without_repeating_mfa_or_starting_automation(monkeypatch):
    request, state, conn = context()
    state["recovery_ready"] = True
    monkeypatch.setenv(original.FLAG, "true")
    monkeypatch.setattr(original, "authenticated_user", Mock(return_value={"id": "owner", "role": "OWNER"}))
    monkeypatch.setattr(live, "execution_owner", Mock())
    mfa = AsyncMock()
    monkeypatch.setattr(account_settings, "require_totp", mfa)
    monkeypatch.setattr(account_settings, "enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(live, "live_credentials_status", Mock(return_value=("offline-key", "offline-secret", "fp")))
    monkeypatch.setattr(live, "client_for", Mock(return_value=object()))
    monkeypatch.setattr(live, "account_snapshot", AsyncMock(return_value={"wallet_balance": 1000}))
    monkeypatch.setattr(original, "status", AsyncMock(side_effect=lambda req: original.public_config(state)))
    start = AsyncMock()
    monkeypatch.setattr(live, "_start_live_auto_session", start)
    body = risk.RiskUpdate(**{**high_choice().model_dump(), "understood": True})
    response = asyncio.run(original.update_risk(request, body))
    assert response["risk_revision"] == 1
    assert conn.committed
    mfa.assert_not_awaited()
    start.assert_not_awaited()


@pytest.mark.parametrize("gate,status", [("owner", 403), ("recovery", 503), ("understanding", 422), ("credentials", 423)])
def test_risk_update_denied_gates_preserve_risk_and_positions(monkeypatch, gate, status):
    request, state, conn = context()
    before = copy.deepcopy(state["original_v2_live"])
    plans = copy.deepcopy(state["plans"])
    state["recovery_ready"] = gate != "recovery"
    monkeypatch.setenv(original.FLAG, "true")
    user = Mock(return_value={"id": "owner", "role": "OWNER"})
    if gate == "owner":
        user.side_effect = HTTPException(403, "OWNER required")
    monkeypatch.setattr(original, "authenticated_user", user)
    monkeypatch.setattr(live, "execution_owner", Mock())
    monkeypatch.setattr(account_settings, "enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(account_settings, "require_totp", AsyncMock(side_effect=HTTPException(401, "Invalid MFA") if gate == "mfa" else None))
    monkeypatch.setattr(live, "live_credentials_status", Mock(return_value=("", "", None)))
    with pytest.raises(HTTPException) as error:
        asyncio.run(original.update_risk(request, risk.RiskUpdate(**{**high_choice().model_dump(), "understood": gate != "understanding"})))
    assert error.value.status_code == status
    assert state["original_v2_live"] == before
    assert state["plans"] == plans
    assert not conn.committed


def test_risk_snapshot_exchange_error_is_explicit_and_cannot_apply_choice(monkeypatch):
    request, state, conn = context()
    before = copy.deepcopy(state["original_v2_live"])
    state["recovery_ready"] = True
    monkeypatch.setenv(original.FLAG, "true")
    monkeypatch.setattr(original, "authenticated_user", Mock(return_value={"id": "owner", "role": "OWNER"}))
    monkeypatch.setattr(live, "execution_owner", Mock())
    monkeypatch.setattr(account_settings, "require_totp", AsyncMock())
    monkeypatch.setattr(account_settings, "enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(live, "live_credentials_status", Mock(return_value=("offline-key", "offline-secret", "fp")))
    monkeypatch.setattr(live, "client_for", Mock(return_value=object()))
    exchange_error = live.LiveExchangeError("Binance hesap bakiyesi alınamadı.", http_status=503)
    monkeypatch.setattr(live, "account_snapshot", AsyncMock(side_effect=exchange_error))
    with pytest.raises(HTTPException) as error:
        asyncio.run(original.update_risk(request, risk.RiskUpdate(**{**high_choice().model_dump(), "understood": True})))
    assert error.value.status_code == 503
    assert state["original_v2_live"] == before
    assert not conn.committed
