"""Offline exchange fixtures only; these tests cannot submit real orders."""
import asyncio
import copy
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import account_settings, original_v2_live as original, v25_execution as live
from app import original_v2_demo_execution as demo_execution
import test_original_v2_demo as decisions
from app.strategies import original_v2_demo as strategy
from test_original_v2_demo_execution import EntryClient, result
from test_v25_partial_fills import PartialFillClient, make_plan


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setenv(original.FLAG, "true")
    monkeypatch.setattr(live, "persist_state", Mock())
    monkeypatch.setattr(live, "persist_state_awaited", AsyncMock(return_value="UPDATED"))
    state = live.initial_state()
    state.update(lock=asyncio.Lock(), recovery_ready=True, real_trading_locked=False,
                 connected=True, armed_until=time.time() + 3600, live_auto_trade=True)
    state["auto"].update(enabled=True, session_until=time.time() + 3600)
    state["original_v2_live"] = {
        "enabled": True, "owner_id": "owner", "fingerprint": "fingerprint",
        "risk_percent": 0.5, "daily_loss_percent": 5, "paused": False,
    }
    return state


def request_for(state):
    app = SimpleNamespace(state=SimpleNamespace(v25_execution=state, db_pool=object()))
    return SimpleNamespace(app=app, state=SimpleNamespace(member={"id": "owner", "role": "OWNER"}))


def native_plan(**changes):
    defaults = {"source": original.SOURCE, "initial_stop_loss": "49000", "initial_quantity": "0.010",
                "entry_order_status": "FILLED"}
    return make_plan(**{**defaults, **changes})


def test_default_disabled(monkeypatch):
    monkeypatch.delenv(original.FLAG, raising=False)
    assert original.available() is False
    assert original.running(live.initial_state()) is False


@pytest.mark.parametrize("field,value", [
    ("risk_percent", 1.001), ("risk_percent", 0), ("risk_percent", float("nan")),
    ("daily_loss_percent", 5.001), ("daily_loss_percent", 0), ("daily_loss_percent", float("inf")),
])
def test_hard_percentage_caps(field, value):
    with pytest.raises(ValidationError):
        original.Start(confirmation="ORIGINAL V2 CANLI", totp_code="123456", **{field: value})


def test_fixed_policy_and_demo_not_mutated(state):
    before = copy.deepcopy(state["policy"])
    fixed = strategy.fixed_profile()
    policy = original.execution_policy(state, {"wallet_balance": 200})
    assert len(policy["allowed_symbols"]) == 8
    assert policy["max_leverage"] == 3
    assert policy["max_stop_distance_pct"] == 6
    assert policy["max_loss_per_trade"] <= 1
    assert state["policy"] == before
    assert strategy.fixed_profile() == fixed


def test_native_sizing_with_costs_cannot_exceed_percentage(state):
    client = EntryClient()
    state["policy"]["max_margin_per_trade"] = 25
    spec = run(original.build_spec(client, result(), state, {"wallet_balance": 300}))
    assert spec["leverage"] == 3
    assert 0 < spec["estimated_stop_loss_usdt"] <= 1.5
    assert Decimal(spec["quantity"]) % spec["step"] == 0
    assert spec["initial_stop_loss"] == spec["stop_loss"]
    assert not any(method == "POST" for method, _, _ in client.calls)


def test_tiny_budget_rejected_without_order(state):
    client = EntryClient()
    with pytest.raises(HTTPException):
        run(original.build_spec(client, result(), state, {"wallet_balance": 0.01}))
    assert not any(method == "POST" for method, _, _ in client.calls)


def test_emergency_pause_never_closes_or_changes_plans(state):
    plan = native_plan()
    state["plans"][plan["id"]] = plan
    before = copy.deepcopy(state["plans"])
    original.pause(state, "test disconnect")
    assert not original.running(state)
    assert state["real_trading_locked"]
    assert not state["auto"]["enabled"]
    assert state["armed_until"] == 0
    assert state["plans"] == before
    assert state["original_v2_live"]["alerts"][0]["read"] is False


def test_restart_retains_daily_lock_but_not_entry_authority(state):
    state["original_v2_live"]["daily"] = {"day": "2026-10-11", "opening_wallet": 1000, "locked": True}
    restored = live.sanitized_state(state)
    assert not original.running(restored)
    assert restored["original_v2_live"]["paused"]
    assert restored["original_v2_live"]["daily"]["locked"]


def test_stop_failure_original_keeps_position_legacy_closes(state, monkeypatch):
    plan = native_plan()
    client = PartialFillClient(plan, quantity="0.010")
    monkeypatch.setattr(live, "install_verified_stop", AsyncMock(side_effect=live.LiveExchangeError("stop rejected")))
    close = AsyncMock(return_value={"orderId": 55})
    monkeypatch.setattr(live, "close_tracked_symbol", close)
    monkeypatch.setattr(live, "log_event", AsyncMock())
    with pytest.raises(live.LiveExchangeError):
        run(live.install_protection(client, state, plan))
    close.assert_not_awaited()
    assert not original.running(state)
    assert client.quantity == "0.010"
    assert plan["stop_loss"] == "49000"
    legacy = make_plan(entry_order_status="FILLED")
    run(live.install_protection(PartialFillClient(legacy, quantity="0.010"), live.initial_state(), legacy))
    close.assert_awaited_once()


def test_exact_tp60_tp3_and_fixed_stop(state):
    plan = native_plan()
    client = PartialFillClient(plan, quantity="0.010")
    run(live.install_protection(client, state, plan))
    assert [row["type"] for row in client.algos] == ["STOP_MARKET", "TAKE_PROFIT_MARKET", "TAKE_PROFIT_MARKET"]
    assert client.algos[0]["triggerPrice"] == "49000"
    assert Decimal(client.algos[1]["quantity"]) == Decimal("0.006")
    assert client.algos[1]["triggerPrice"] == "51000"
    assert client.algos[2]["triggerPrice"] == "53000"
    assert client.algos[2]["closePosition"] == "true"
    assert "quantity" not in client.algos[2]


def test_partial_entry_tp60_uses_settled_initial_quantity(state):
    plan = native_plan(entry_order_status="PARTIALLY_FILLED")
    client = PartialFillClient(plan, quantity="0.005", final_quantity="0.005")
    run(live.install_protection(client, state, plan))
    assert plan["initial_quantity"] == "0.005"
    assert Decimal(client.algos[1]["quantity"]) == Decimal("0.003")


def test_tp_failure_stops_entries_and_never_closes(state, monkeypatch):
    plan = native_plan()
    client = PartialFillClient(plan, quantity="0.010")
    real_post = live.post_entry_protection_algo
    async def reject_tp(client, params, rows):
        if params["type"] == "TAKE_PROFIT_MARKET":
            raise live.LiveExchangeError("TP rejected")
        return await real_post(client, params, rows)
    monkeypatch.setattr(live, "post_entry_protection_algo", reject_tp)
    close = AsyncMock()
    monkeypatch.setattr(live, "close_tracked_symbol", close)
    with pytest.raises(live.LiveExchangeError):
        run(live.install_protection(client, state, plan))
    close.assert_not_awaited()
    assert not original.running(state)
    assert len(client.algos) == 1
    assert client.algos[0]["type"] == "STOP_MARKET"


def test_stop_mutation_rejected_before_exchange(state):
    plan = native_plan()
    plan["stop_loss"] = "49500"
    client = PartialFillClient(plan)
    with pytest.raises(live.LiveExchangeError):
        run(live.install_protection(client, state, plan))
    assert client.calls == []
    assert not original.running(state)


def test_tp1_not_reallocated_after_partial_exit(state, monkeypatch):
    plan = native_plan(original_tp_attempted=True)
    plan.update(tp12_algo_id=102, stop_algo_id=101)
    post = AsyncMock(return_value={"algoId": 103})
    monkeypatch.setattr(live, "post_entry_protection_algo", post)
    run(original.install_targets(object(), state, plan,
                                 {"quantity": "0.004", "mark_price": "50000"}, []))
    assert post.await_count == 1
    assert post.await_args.args[1]["triggerPrice"] == "53000"
    assert plan["initial_quantity"] == "0.010"


def income(amount, kind="REALIZED_PNL"):
    return {"asset": "USDT", "income": str(amount), "incomeType": kind, "tranId": kind,
            "time": int(datetime.now(timezone.utc).timestamp() * 1000) - 1}


def test_daily_initial_balance_and_loss_latch(state):
    client = SimpleNamespace(signed=AsyncMock(return_value=[income(-50), income(100, "TRANSFER")]))
    with pytest.raises(HTTPException):
        run(original.daily_guard(client, state, {"wallet_balance": 1050, "unrealized_pnl": 0}))
    ledger = state["original_v2_live"]["daily"]
    assert ledger["opening_wallet"] == 1000
    assert ledger["loss"] == 50
    assert ledger["limit"] == 50
    assert ledger["locked"]
    client.signed.return_value = []
    with pytest.raises(HTTPException):
        run(original.daily_guard(client, state, {"wallet_balance": 1200, "unrealized_pnl": 0}))
    assert ledger["opening_wallet"] == 1000


def test_unrealized_loss_counts_toward_daily_limit(state):
    client = SimpleNamespace(signed=AsyncMock(return_value=[]))
    with pytest.raises(HTTPException):
        run(original.daily_guard(client, state, {"wallet_balance": 1000, "unrealized_pnl": -50}))


def test_unproven_daily_history_blocks(state):
    client = SimpleNamespace(signed=AsyncMock(return_value={"error": "not history"}))
    with pytest.raises(HTTPException):
        run(original.daily_guard(client, state, {"wallet_balance": 1000, "unrealized_pnl": 0}))


@pytest.mark.parametrize("disabled", ["feature", "2fa", "arm", "confirmation", "customer"])
def test_start_requires_every_owner_gate(state, monkeypatch, disabled):
    req = request_for(state)
    state["auto"]["enabled"] = False
    monkeypatch.setattr(original, "authenticated_user", Mock(return_value={"id": "owner", "role": "OWNER"}))
    monkeypatch.setattr(account_settings, "enabled", AsyncMock(return_value=disabled != "2fa"))
    totp = AsyncMock()
    monkeypatch.setattr(account_settings, "require_totp", totp)
    monkeypatch.setattr(live, "live_auto_start_gate", Mock(return_value=(disabled != "arm", "ARM required")))
    native = Mock()
    monkeypatch.setattr(live, "client_for", native)
    if disabled == "feature":
        monkeypatch.setenv(original.FLAG, "false")
    if disabled == "customer":
        original.authenticated_user.side_effect = HTTPException(403, "OWNER required")
    body = original.Start(confirmation="NO" if disabled == "confirmation" else "ORIGINAL V2 CANLI",
                          totp_code="123456")
    with pytest.raises(HTTPException):
        run(original.start(req, body))
    native.assert_not_called()
    totp.assert_not_awaited()
    original.authenticated_user.assert_called_once_with(req, owner=True)


def test_owner_notifications_hidden_from_customers(state, monkeypatch):
    original.pause(state, "exchange disconnected")
    req = request_for(state)
    monkeypatch.setattr(original, "authenticated_user", Mock(return_value={"id": "customer", "role": "CUSTOMER"}))
    assert original.merge_bell(req, []) == []
    alert_id = state["original_v2_live"]["alerts"][0]["id"]
    with pytest.raises(HTTPException) as error:
        original.merge_bell(req, [], read_id=alert_id)
    assert error.value.status_code == 404
    original.authenticated_user.return_value = {"id": "owner", "role": "OWNER"}
    assert original.merge_bell(req, [], read_id=alert_id)[0]["read"]


def test_original_cycle_dispatch_does_not_use_legacy_scanner(state, monkeypatch):
    req = request_for(state)
    cycle = AsyncMock()
    monkeypatch.setattr(original, "cycle", cycle)
    monkeypatch.setattr(live, "live_auto_symbol_scope", Mock(side_effect=AssertionError("legacy scanner")))
    run(live.automatic_cycle(req.app))
    cycle.assert_awaited_once_with(req.app, None)


def test_missing_credentials_pause_without_exchange(state, monkeypatch):
    req = request_for(state)
    monkeypatch.setattr(live, "auto_session_credentials", AsyncMock(return_value=("", "")))
    client = Mock()
    monkeypatch.setattr(live, "client_for_with_credentials", client)
    with pytest.raises(HTTPException):
        run(original.cycle(req.app, None))
    client.assert_not_called()
    assert not original.running(state)


def test_existing_live_lock_pauses_original_only(state):
    live.lock_live_execution(state, "NETWORK_FAILURE")
    assert not original.running(state)
    legacy = live.initial_state()
    live.lock_live_execution(legacy, "NETWORK_FAILURE")
    assert "original_v2_live" not in legacy


class CandleClient:
    def __init__(self, gap=False):
        self.data = decisions.fixtures.open_dataset()
        self.gap = gap
        self.calls = []

    async def public_get(self, path, params=None):
        self.calls.append((path, params))
        if path == "/fapi/v1/exchangeInfo":
            info = copy.deepcopy(self.data.metadata["exchange_info"])
            reference = next(row for row in info["symbols"] if row["symbol"] == "BTCUSDT")
            info["symbols"] = [{**copy.deepcopy(reference), "symbol": symbol} for symbol in decisions.SYMBOLS]
            return info
        assert path == "/fapi/v1/klines"
        duration = {"15m": 900, "1h": 3600, "4h": 14400}[params["interval"]]
        rows = self.data.frames["BTCUSDT"][params["interval"]].closed(decisions.fixtures.AT)[-259:]
        if self.gap:
            rows = rows[:100] + rows[101:]
        return [[row["time"] * 1000, row["open"], row["high"], row["low"], row["close"], row["volume"],
                 (row["time"] + duration) * 1000 - 1, row.get("quote_volume") or 1_000_000] for row in rows]


def test_live_scan_signal_and_ranking_exactly_equal_demo():
    demo_selected, demo_results = run(demo_execution.candidates(CandleClient(), decisions.fixtures.AT, set()))
    live_selected, live_results = run(demo_execution.candidates(
        CandleClient(), decisions.fixtures.AT, set(), require_complete=True))
    assert live_selected == demo_selected
    assert live_results == demo_results
    assert len(live_results) == 8


def test_live_rejects_data_gap_while_demo_behavior_is_unchanged():
    with pytest.raises(live.BinanceDemoError, match="INCOMPLETE_ORIGINAL_LIVE_HISTORY"):
        run(demo_execution.candidates(CandleClient(gap=True), decisions.fixtures.AT, set(), require_complete=True))
    eligible, results = run(demo_execution.candidates(CandleClient(gap=True), decisions.fixtures.AT, set()))
    assert eligible == []
    assert len(results) == 8


def test_start_success_consumes_fresh_2fa_then_existing_auto_authority(state, monkeypatch):
    req = request_for(state)
    state["auto"]["enabled"] = False
    trace = []
    monkeypatch.setattr(original, "authenticated_user", Mock(return_value={"id": "owner", "role": "OWNER"}))
    monkeypatch.setattr(live, "guard_execution_ownership", Mock())
    monkeypatch.setattr(account_settings, "enabled", AsyncMock(return_value=True))
    async def code(*args, **kwargs):
        assert kwargs == {"required": True}
        trace.append("2fa")
    async def start(*args):
        trace.append("existing-auto")
        state["auto"]["enabled"] = True
    monkeypatch.setattr(account_settings, "require_totp", code)
    monkeypatch.setattr(live, "v25_auto_start", start)
    monkeypatch.setattr(live, "live_auto_start_gate", Mock(return_value=(True, "ready")))
    monkeypatch.setattr(live, "live_credentials_status", Mock(return_value=("key", "secret", "fingerprint")))
    monkeypatch.setattr(live, "client_for", Mock(return_value=object()))
    monkeypatch.setattr(live, "account_snapshot", AsyncMock(return_value={"wallet_balance": 1000, "unrealized_pnl": 0}))
    monkeypatch.setattr(original, "daily_guard", AsyncMock())
    response = run(original.start(req, original.Start(confirmation="ORIGINAL V2 CANLI", totp_code="123456")))
    assert response["enabled"]
    assert trace == ["2fa", "existing-auto"]
    live.persist_state_awaited.assert_awaited_once_with(state)


def test_2fa_failure_never_starts_or_fetches_exchange(state, monkeypatch):
    req = request_for(state)
    state["auto"]["enabled"] = False
    monkeypatch.setattr(original, "authenticated_user", Mock(return_value={"id": "owner", "role": "OWNER"}))
    monkeypatch.setattr(account_settings, "enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(account_settings, "require_totp", AsyncMock(side_effect=HTTPException(401, "invalid 2fa")))
    monkeypatch.setattr(live, "live_auto_start_gate", Mock(return_value=(True, "ready")))
    start = AsyncMock()
    monkeypatch.setattr(live, "v25_auto_start", start)
    client = Mock()
    monkeypatch.setattr(live, "client_for", client)
    with pytest.raises(HTTPException):
        run(original.start(req, original.Start(confirmation="ORIGINAL V2 CANLI", totp_code="123456")))
    start.assert_not_awaited()
    client.assert_not_called()


@pytest.mark.parametrize("write_status", ["UPDATED", "SKIPPED"])
def test_original_order_uses_fixed_builder_and_preserves_legacy_policy(state, monkeypatch, write_status):
    req = request_for(state)
    req.app.state.maintenance = {"mode": "NORMAL"}
    state["armed_until"] = 0
    state["auto_authorization"] = {"user_id": "owner", "fingerprint": "fingerprint", "auth_version": 1}
    value = result()
    signal = value.legacy["analysis"]
    monkeypatch.setattr(live.time, "time", lambda: value.signal.decision_time)
    client = EntryClient()
    snapshot = {"wallet_balance": 300, "available_balance": 300, "positions": [], "open_orders": [],
                "unrealized_pnl": 0, "hedge_mode": False, "multi_assets_mode": False}
    monkeypatch.setattr(live, "client_for_with_credentials", Mock(return_value=client))
    monkeypatch.setattr(live, "account_snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(live, "readiness_for", Mock(return_value={"ready": True}))
    monkeypatch.setattr(live, "background_authorization_active", AsyncMock(return_value=True))
    from app import v22_commercial
    monkeypatch.setattr(v22_commercial, "validate_authoritative_session", AsyncMock(return_value={"role": "OWNER"}))
    monkeypatch.setattr(original, "daily_guard", AsyncMock())
    monkeypatch.setattr(live, "spread_bps", AsyncMock(return_value=1))
    monkeypatch.setattr(live, "evaluate_entry_gates", Mock(return_value={"passed": True, "reason": "pass", "gates": []}))
    legacy_builder = AsyncMock(side_effect=AssertionError("Original must not use legacy dynamic stops"))
    monkeypatch.setattr(live, "build_live_spec", legacy_builder)
    monkeypatch.setattr(live, "set_live_isolated_margin", AsyncMock(return_value="ISOLATED"))
    monkeypatch.setattr(live, "apply_live_verified_leverage", AsyncMock(return_value={"margin_type": "ISOLATED", "applied_leverage": 3}))
    monkeypatch.setattr(live, "live_liquidation_risk", AsyncMock(return_value={"passed": True}))
    monkeypatch.setattr(live, "fresh_auto_submission_credentials", AsyncMock(return_value=("safe-key-123456", "safe-secret-123456")))
    monkeypatch.setattr(live, "directional_entry_gates", Mock(return_value=[]))
    async def submit(client, spec, client_id, **kwargs):
        kwargs["before_submit"]()
        return {"orderId": 10, "status": "FILLED"}
    submit_mock = AsyncMock(side_effect=submit)
    monkeypatch.setattr(live, "submit_entry", submit_mock)
    monkeypatch.setattr(live, "install_protection", AsyncMock())
    live.persist_state_awaited.return_value = write_status
    before = copy.deepcopy(state["policy"])
    body = live.LiveOrderRequest(symbol=value.signal.symbol, direction=signal["direction"], margin_usdt=5, leverage=3,
                                stop_loss=signal["stop_loss"], tp1=signal["tp1"], tp2=signal["tp2"], tp3=signal["tp3"])
    call = live.execute_live_order(req.app, body, source=original.SOURCE,
                                  credentials=("safe-key-123456", "safe-secret-123456"), strategy_result=value)
    if write_status == "SKIPPED":
        with pytest.raises(HTTPException) as error:
            run(call)
        assert error.value.status_code == 503
        submit_mock.assert_not_awaited()
        assert not original.running(state)
        return
    response = run(call)
    assert response["ok"]
    assert response["plan"]["source"] == original.SOURCE
    assert response["plan"]["initial_stop_loss"] == response["plan"]["stop_loss"]
    assert response["plan"]["leverage"] == 3
    assert state["policy"] == before
    legacy_builder.assert_not_awaited()
    assert submit_mock.await_count == 1
    assert submit_mock.await_args.args[1]["estimated_stop_loss_usdt"] <= 300 * 0.005
    assert live.persist_state_awaited.await_count >= 2


def test_reading_original_alert_preserves_demo_and_binance_notifications(state, monkeypatch):
    from app import binance_notifications, v21_demo
    req = request_for(state)
    original.pause(state, "disconnect")
    alert_id = state["original_v2_live"]["alerts"][0]["id"]
    monkeypatch.setattr(original, "authenticated_user", Mock(return_value={"id": "owner", "role": "OWNER"}))
    monkeypatch.setattr(v21_demo, "state_for", Mock(return_value=v21_demo.initial_state()))
    monkeypatch.setattr(v21_demo, "notification_payload", Mock(return_value=[
        {"id": "demo-alert", "timestamp": None, "read": False},
    ]))
    async def binance(request, items, **kwargs):
        return [*items, {"id": "binance:payment", "timestamp": None, "read": False}]
    monkeypatch.setattr(binance_notifications, "merge_bell", binance)
    response = run(v21_demo.v21_notification_read(req, alert_id))
    assert {row["id"] for row in response["items"]} == {alert_id, "demo-alert", "binance:payment"}
    assert next(row for row in response["items"] if row["id"] == alert_id)["read"]
    assert response["unread"] == 2


def test_recovered_original_intent_cannot_reallocate_tp1(state):
    intent = {
        "source": original.SOURCE, "client_order_id": "entry",
        "spec": {"symbol": "BTCUSDT", "direction": "LONG", "order_type": "MARKET",
                 "entry_price": "50000", "quantity": "0.010", "stop_loss": "49000",
                 "initial_stop_loss": "49000", "initial_quantity": "0.010",
                 "original_risk_budget": 1, "targets": ["51000", "52000", "53000"], "step": "0.001", "min_qty": "0.001"},
    }
    recovered = live.recover_plan_from_intent("intent", intent, {"orderId": 42, "status": "FILLED"})
    assert recovered["original_tp_attempted"]
    assert recovered["initial_stop_loss"] == "49000"
    assert recovered["initial_quantity"] == "0.010"


def test_tp1_cannot_submit_before_durable_attempt_marker(state):
    plan = native_plan()
    client = PartialFillClient(plan, quantity="0.010")
    live.persist_state_awaited.return_value = "SKIPPED"
    with pytest.raises(live.LiveExchangeError):
        run(live.install_protection(client, state, plan))
    assert [row["type"] for row in client.algos] == ["STOP_MARKET"]
    assert not original.running(state)


def test_required_mfa_checks_enabled_inside_atomic_security_edit(state, monkeypatch):
    req = request_for(state)
    @asynccontextmanager
    async def edit(*args):
        yield {"two_factor_enabled": False}
    monkeypatch.setattr(account_settings.store, "read", AsyncMock(return_value={"two_factor_enabled": False}))
    monkeypatch.setattr(account_settings.store, "edit", edit)
    limiter = AsyncMock()
    monkeypatch.setattr(account_settings.auth, "enforce_auth_limit", limiter)
    check = Mock()
    monkeypatch.setattr(account_settings, "check_limited_totp", check)
    run(account_settings.require_totp(req, {"id": "owner"}, "123456"))
    limiter.assert_not_awaited()
    with pytest.raises(HTTPException) as error:
        run(account_settings.require_totp(req, {"id": "owner"}, "123456", required=True))
    assert error.value.status_code == 403
    check.assert_not_called()


def test_legacy_notification_request_without_live_runtime_is_unchanged():
    items = [{"id": "legacy", "read": False}]
    assert original.merge_bell(SimpleNamespace(), items) is items
    with pytest.raises(HTTPException) as error:
        original.merge_bell(SimpleNamespace(), items, read_id="original-live:unknown")
    assert error.value.status_code == 404
