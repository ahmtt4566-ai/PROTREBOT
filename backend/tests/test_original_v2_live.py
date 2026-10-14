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
from app.public_market_data import PublicMarketData
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
        "symbol_count": 40,
        "universe": {
            "updated_day": datetime.now(timezone.utc).date().isoformat(),
            "minimum_age_days": original.ORIGINAL_MIN_SYMBOL_AGE_DAYS,
            "ranked_symbols": strategy.fixed_profile()["execution_policy"]["allowed_symbols"],
            "eligible_count": 8,
        },
    }
    return state


def request_for(state):
    app = SimpleNamespace(state=SimpleNamespace(v25_execution=state, db_pool=object()))
    return SimpleNamespace(
        app=app,
        headers={"x-protrebot-session": "owner-session"},
        state=SimpleNamespace(member={"id": "owner", "role": "OWNER", "auth_version": 1}),
    )


def native_plan(**changes):
    defaults = {"source": original.SOURCE, "initial_stop_loss": "49000", "initial_quantity": "0.010",
                "entry_order_status": "FILLED", "leverage":3, "original_risk_budget":100,
                "original_cost_bps":20}
    return make_plan(**{**defaults, **changes})


@pytest.mark.parametrize("hours", [1, 4, 12, 24])
def test_original_session_duration_choices(hours):
    body = original.Start(confirmation="ORIGINAL V2 CANLI", totp_code="123456", session_hours=hours)
    assert body.session_hours == hours
    assert original.Start(confirmation="ORIGINAL V2 CANLI", totp_code="123456").session_hours == 1
    assert original.Start(confirmation="ORIGINAL V2 CANLI", totp_code="123456").symbol_count == 40


@pytest.mark.parametrize("count", [10, 40, 100])
def test_original_live_symbol_count_bounds(count):
    body = original.Start(confirmation="ORIGINAL V2 CANLI", totp_code="123456", symbol_count=count)
    assert body.symbol_count == count


@pytest.mark.parametrize("count", [9, 101, 1.5, True, "40"])
def test_original_live_rejects_invalid_symbol_count(count):
    with pytest.raises(ValidationError):
        original.Start(confirmation="ORIGINAL V2 CANLI", totp_code="123456", symbol_count=count)


def test_original_live_execution_policy_changes_only_the_daily_symbol_scope(state):
    symbols = [f"TOKEN{index}USDT" for index in range(10)]
    state["original_v2_live"].update(
        symbol_count=10,
        universe={
            "updated_day": datetime.now(timezone.utc).date().isoformat(),
            "minimum_age_days": original.ORIGINAL_MIN_SYMBOL_AGE_DAYS,
            "ranked_symbols": symbols,
            "eligible_count": 10,
        },
    )

    policy = original.execution_policy(state, {"wallet_balance": 1000})

    assert policy["allowed_symbols"] == symbols
    assert policy["max_leverage"] == 3
    assert policy["max_positions"] == 5
    assert policy["max_same_direction_positions"] == 2
    assert policy["daily_trade_limit"] == 3
    assert policy["max_stop_distance_pct"] == 6


def test_original_live_universe_is_fetched_once_per_day_and_resliced_for_new_n():
    now_ms = int(time.time() * 1000)
    onboard_date = now_ms - 46 * 24 * 60 * 60 * 1000
    contracts = [
        {"symbol": f"TOKEN{index:02d}USDT", "baseAsset": f"TOKEN{index:02d}",
         "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT",
         "onboardDate": onboard_date}
        for index in range(12)
    ]
    tickers = [{"symbol": row["symbol"], "quoteVolume": str(1_000_000 + index)}
               for index, row in enumerate(contracts)]
    books = [{"symbol": row["symbol"], "bidPrice": "100", "askPrice": "100.04"}
             for row in contracts]
    calls = []

    class Client:
        async def public_get(self, path):
            calls.append(path)
            return {
                "/fapi/v1/exchangeInfo": {"symbols": contracts},
                "/fapi/v1/ticker/24hr": tickers,
                "/fapi/v1/ticker/bookTicker": books,
            }[path]

    service = PublicMarketData()
    application = SimpleNamespace(state=SimpleNamespace(public_market_data=service))
    config = {"symbol_count": 10}
    first = run(original.resolve_live_universe(application, Client(), config))
    config["symbol_count"] = 12
    second = run(original.resolve_live_universe(application, Client(), config))

    assert len(first) == 10
    assert len(second) == 12
    assert calls == ["/fapi/v1/exchangeInfo", "/fapi/v1/ticker/24hr", "/fapi/v1/ticker/bookTicker"]
    assert service.original_symbols == set(second)
    assert config["universe"]["eligible_count"] == 12
    assert config["universe"]["minimum_age_days"] >= 45


def test_pre_history_filter_cached_universe_is_not_reused(state):
    universe = state["original_v2_live"]["universe"]
    universe.pop("minimum_age_days")
    assert original.selected_symbols(state["original_v2_live"]) == []
    universe["minimum_age_days"] = 30
    assert original.selected_symbols(state["original_v2_live"]) == []
    universe["minimum_age_days"] = original.ORIGINAL_MIN_SYMBOL_AGE_DAYS
    assert original.selected_symbols(state["original_v2_live"])


def test_incomplete_history_pauses_session_before_its_scheduled_expiry(state, monkeypatch):
    expires = int(time.time()) + 24 * 3600
    state["original_v2_live"]["session_expires_at_epoch"] = expires
    monkeypatch.setattr(live, "auto_session_credentials", AsyncMock(return_value=("offline-api-key", "offline-secret")))
    monkeypatch.setattr(live, "readiness_for", Mock(return_value={"ready": True}))
    monkeypatch.setattr(live, "client_for_with_credentials", Mock(return_value=CandleClient()))
    monkeypatch.setattr(live, "account_snapshot", AsyncMock(return_value={
        "positions": [], "open_orders": [], "open_algo_orders": [],
    }))
    monkeypatch.setattr(original, "check_entry", AsyncMock())
    monkeypatch.setattr(original, "execution_policy", Mock(return_value={
        "max_loss_per_trade": "5", "max_margin_per_trade": "25",
        "fee_bps_per_side": "4", "slippage_bps_per_side": "4",
    }))
    monkeypatch.setattr(demo_execution, "live_candidates", AsyncMock(
        side_effect=live.BinanceDemoError("INCOMPLETE_ORIGINAL_LIVE_HISTORY", http_status=409),
    ))
    with pytest.raises(live.BinanceDemoError, match="INCOMPLETE_ORIGINAL_LIVE_HISTORY"):
        run(original.cycle(request_for(state).app, None))
    status = original.public_config(state)
    assert status["paused"] is True
    assert status["pause_reason"] == "INCOMPLETE_ORIGINAL_LIVE_HISTORY"
    assert status["session"]["remaining_seconds"] == 0
    assert datetime.fromisoformat(status["session"]["expires_at"]).timestamp() == expires
    assert state["real_trading_locked"] is True


@pytest.mark.parametrize("hours", [0, 2, 25, -1, 1.5, True, "4"])
def test_original_rejects_unsupported_session_duration(hours):
    with pytest.raises(ValidationError):
        original.Start(confirmation="ORIGINAL V2 CANLI", totp_code="123456", session_hours=hours)


def test_default_disabled(monkeypatch):
    monkeypatch.delenv(original.FLAG, raising=False)
    assert original.available() is False
    assert original.running(live.initial_state()) is False


@pytest.mark.parametrize("row,diagnosis,exists", [
    (None, "FIRST_RECORD_MISSING", False),
    ({"revision": "20", "original_record_exists": True}, "DATABASE_AHEAD", True),
    ({"revision": "0", "original_record_exists": False}, "NO_REVISION_CONFLICT_OBSERVED", True),
    ({"revision": "private-invalid-value", "original_record_exists": True}, "INVALID_DATABASE_REVISION", True),
])
def test_persistence_diagnostics_reads_metadata_only(state, monkeypatch, row, diagnosis, exists):
    req = request_for(state)
    req.app.state.db_pool = SimpleNamespace(fetchrow=AsyncMock(return_value=row), execute=AsyncMock())
    monkeypatch.setattr(original, "authenticated_user", Mock(return_value={"id": "owner", "role": "OWNER"}))
    monkeypatch.setattr(live, "guard_execution_ownership", Mock())
    before = copy.deepcopy({key: value for key, value in state.items() if key != "lock"})
    response = run(original.persistence_diagnostics(req))
    assert response["diagnosis"] == diagnosis
    assert response["record_exists"] is exists
    assert response["read_only"] is True
    assert "private-invalid-value" not in str(response)
    assert {key: value for key, value in state.items() if key != "lock"} == before
    sql = req.app.state.db_pool.fetchrow.await_args.args[0]
    assert sql.lstrip().startswith("SELECT")
    assert "SELECT payload FROM" not in sql
    req.app.state.db_pool.execute.assert_not_awaited()
    live.persist_state.assert_not_called()
    live.persist_state_awaited.assert_not_awaited()


def test_persistence_diagnostics_denies_non_owner_before_database(state, monkeypatch):
    req = request_for(state)
    req.app.state.db_pool = SimpleNamespace(fetchrow=AsyncMock())
    def denied(*args, **kwargs):
        assert kwargs == {"owner": True}
        raise HTTPException(403, "OWNER required")
    monkeypatch.setattr(original, "authenticated_user", denied)
    with pytest.raises(HTTPException) as error:
        run(original.persistence_diagnostics(req))
    assert error.value.status_code == 403
    req.app.state.db_pool.fetchrow.assert_not_awaited()


@pytest.mark.parametrize("sqlstate,status", [("42P01", "TABLE_MISSING"), (None, "READ_FAILED")])
def test_persistence_diagnostics_redacts_database_errors(state, monkeypatch, sqlstate, status):
    req = request_for(state)
    error = RuntimeError("password=private-secret postgres://private-host")
    error.sqlstate = sqlstate
    req.app.state.db_pool = SimpleNamespace(fetchrow=AsyncMock(side_effect=error))
    monkeypatch.setattr(original, "authenticated_user", Mock(return_value={"id": "owner"}))
    monkeypatch.setattr(live, "guard_execution_ownership", Mock())
    response = run(original.persistence_diagnostics(req))
    assert response["database_status"] == status
    assert "private" not in str(response)


def test_persistence_diagnostics_is_get_only_and_start_keeps_its_handler():
    diagnostics = [route for route in original.router.routes if route.endpoint is original.persistence_diagnostics]
    assert len(diagnostics) == 1
    assert diagnostics[0].methods == {"GET"}
    starts = [route for route in original.router.routes if route.path.endswith("/start")]
    assert len(starts) == 1
    assert starts[0].endpoint is original.start


@pytest.mark.parametrize("field,value", [
    ("risk_percent", 1.001), ("risk_percent", 0), ("risk_percent", float("nan")),
    ("daily_loss_percent", 5.001), ("daily_loss_percent", 0), ("daily_loss_percent", float("inf")),
])
def test_hard_percentage_caps(field, value):
    with pytest.raises(ValidationError):
        original.Start(confirmation="ORIGINAL V2 CANLI", totp_code="123456", **{field: value})


def test_general_and_original_share_counter_and_delayed_write_cannot_overwrite_newer_state(state):
    stored = {"revision": 0}
    async def fetchrow(sql, key, body):
        import json
        assert key == "v25_live:state"
        assert "WHERE COALESCE" in sql
        assert ") >= COALESCE(" in sql
        payload = json.loads(body)
        if payload["persistence_revision"] < stored["revision"]:
            return None
        stored["revision"] = payload["persistence_revision"]
        return {"write_status": "UPDATED"}
    req = request_for(state)
    req.app.state.db_pool = SimpleNamespace(fetchrow=fetchrow)
    general_revision = live._advance_persistence_revision(state)
    original_revision = live._advance_persistence_revision(state)
    assert original_revision == general_revision + 1
    async def exercise():
        assert await live._persist_state_snapshot(req.app, {"persistence_revision": original_revision}) == "UPDATED"
        assert await live._persist_state_snapshot(req.app, {"persistence_revision": general_revision}) == "SKIPPED"
    run(exercise())
    assert stored["revision"] == original_revision
    assert state["persistence_revision"] == original_revision
    assert req.app.state.v25_last_persistence_write["submitted_revision"] == general_revision
    assert req.app.state.v25_last_persistence_write["result"] == "SKIPPED"


@pytest.mark.parametrize("status,diagnosis", [("SKIPPED", "WRITE_SKIPPED_REQUIRES_REVIEW"), ("DATABASE_ERROR", "PREVIOUS_WRITE_FAILED"), ("UPDATED", "NO_REVISION_CONFLICT_OBSERVED")])
def test_diagnostics_distinguishes_awaited_write_from_old_background_write(state, monkeypatch, status, diagnosis):
    req = request_for(state)
    req.app.state.db_pool = SimpleNamespace(fetchrow=AsyncMock(return_value={"revision": "0", "original_record_exists": True}))
    req.app.state.v25_last_persistence_write = {"result": "SKIPPED", "submitted_revision": 0}
    req.app.state.v25_last_awaited_persistence_write = {"result": status, "submitted_revision": 0}
    monkeypatch.setattr(original, "authenticated_user", Mock(return_value={"id": "owner"}))
    monkeypatch.setattr(live, "guard_execution_ownership", Mock())
    from fastapi import Response
    response = Response()
    result = run(original.persistence_diagnostics(req, response))
    assert result["diagnosis"] == diagnosis
    assert response.headers["Cache-Control"] == "no-store"


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
    with pytest.raises(live.MinimumTradeSizeError) as error:
        run(original.build_spec(client, result(), state, {"wallet_balance": 0.01}))
    assert error.value.check_key == "minimum_trade_size"
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

def test_partial_fill_can_leave_actual_margin_below_entry_minimum_without_moving_stop(state):
    plan = native_plan(quantity="0.000300", initial_quantity="0.000300",
                       step="0.000001", min_qty="0.000001", leverage=3)
    client = PartialFillClient(plan, quantity="0.000240", final_quantity="0.000240")
    client.order["origQty"] = "0.000300"
    run(live.install_protection(client, state, plan))
    assert Decimal(plan["initial_quantity"]) * Decimal("50000") / 3 == Decimal("4")
    assert Decimal(client.algos[1]["quantity"]) == Decimal("0.000144")
    assert plan["stop_loss"] == plan["initial_stop_loss"] == "49000"
    assert not original.running(state)
    assert original.fill_locked(state)
    assert "5 USDT" in state["original_v2_live"]["fill_lock"]["violations"][0]


def test_fill_slippage_does_not_move_original_stop_but_can_exceed_planned_stop_distance(state):
    plan = native_plan(entry_order_status="FILLED", entry_price="50000", stop_loss="47000",
                       initial_stop_loss="47000")
    client = PartialFillClient(plan, quantity="0.010")
    native_signed = client.signed
    async def signed(method, path, params=None):
        response = await native_signed(method, path, params)
        if (method, path) == ("GET", "/fapi/v3/positionRisk"):
            return [{**row, "entryPrice":"51000", "markPrice":"51000"} for row in response]
        return response
    client.signed = signed
    run(live.install_protection(client, state, plan))
    actual_entry = Decimal("51000")
    assert abs(actual_entry - Decimal(plan["stop_loss"])) / actual_entry * 100 > Decimal("6")
    assert client.algos[0]["triggerPrice"] == "47000"
    assert not original.running(state)
    assert original.fill_locked(state)
    assert "%6" in state["original_v2_live"]["fill_lock"]["violations"][0]


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

def test_installed_tp1_is_not_revalidated_at_new_market_notional(state, monkeypatch):
    plan = native_plan(original_tp_attempted=True, min_notional="400")
    plan.update(tp12_algo_id=102, stop_algo_id=101)
    post = AsyncMock(return_value={"algoId": 103})
    monkeypatch.setattr(live, "post_entry_protection_algo", post)
    run(original.install_targets(object(), state, plan,
                                 {"quantity": "0.004", "mark_price": "50000"}, []))
    assert post.await_count == 1
    assert "quantity" not in post.await_args.args[1]
    assert plan["initial_quantity"] == "0.010"


@pytest.mark.parametrize("index,field,value", [
    (1, "triggerPrice", "51100"), (1, "quantity", "0.005"), (1, "reduceOnly", "false"),
    (2, "triggerPrice", "53100"), (2, "closePosition", "false"), (2, "workingType", "CONTRACT_PRICE"),
])
def test_changed_original_targets_pause_without_closing(state, index, field, value):
    plan = native_plan()
    client = PartialFillClient(plan, quantity="0.010")
    run(live.install_protection(client, state, plan))
    client.algos[index][field] = value
    before = len(client.calls)
    with pytest.raises(live.LiveExchangeError):
        run(original.verify_targets(client, state, plan, client.snapshot()["open_algo_orders"]))
    assert not original.running(state)
    assert plan["tp_protection_state"] == "UNPROTECTED"
    assert len(client.calls) == before
    assert client.quantity == "0.010"
    assert plan["initial_stop_loss"] == "49000"


def test_original_protection_identity_survives_restart_without_reallocation(state):
    plan = native_plan()
    client = PartialFillClient(plan, quantity="0.010")
    run(live.install_protection(client, state, plan))
    state["plans"][plan["id"]] = plan
    restored = live.sanitized_state(state)
    restored_plan = restored["plans"][plan["id"]]
    before = len(client.calls)
    run(original.verify_targets(client, restored, restored_plan, client.snapshot()["open_algo_orders"]))
    assert len(client.calls) == before
    assert Decimal(restored_plan["initial_quantity"]) == Decimal("0.010")
    assert restored_plan["stop_loss"] == restored_plan["initial_stop_loss"] == "49000"
    assert not restored["auto"]["enabled"]


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


@pytest.mark.parametrize("scan_failure", [False, True])
def test_original_scan_telemetry_records_only_completed_scans(state, monkeypatch, scan_failure):
    req = request_for(state)
    monkeypatch.setattr(live, "readiness_for", Mock(return_value={"ready": True}))
    monkeypatch.setattr(live, "client_for_with_credentials", Mock(return_value=object()))
    monkeypatch.setattr(live, "account_snapshot", AsyncMock(return_value={
        "wallet_balance": 1000, "positions": [], "open_orders": [],
    }))
    monkeypatch.setattr(original, "check_entry", AsyncMock())
    symbols = [f"TOKEN{index}USDT" for index in range(12)]
    monkeypatch.setattr(original, "resolve_live_universe", AsyncMock(return_value=symbols))
    scan = AsyncMock(return_value=([], {}), side_effect=RuntimeError("offline scan failed") if scan_failure else None)
    monkeypatch.setattr(original.original, "live_candidates", scan)
    orders = AsyncMock()
    monkeypatch.setattr(live, "execute_live_order", orders)
    if scan_failure:
        with pytest.raises(RuntimeError):
            run(original.cycle(req.app, ("offline-key", "offline-secret")))
    else:
        run(original.cycle(req.app, ("offline-key", "offline-secret")))
    scanner = original.public_config(state)["scanner"]
    assert scanner["last_cycle_at"]
    assert scanner["stage"] == ("BLOCKED" if scan_failure else "SCAN_COMPLETE")
    assert bool(scanner.get("last_scan_at")) is not scan_failure
    assert scan.await_args.args[3] == symbols
    assert scan.await_args.kwargs["require_complete"] is True
    assert isinstance(scan.await_args.kwargs["diagnostics"], dict)
    orders.assert_not_awaited()
    if not scan_failure:
        assert scanner["scanned_symbol_count"] == len(symbols)
        assert scanner["scanned_symbols"] == symbols
        assert scanner["candidate_count"] == 0
        assert scanner["last_24h"] == {"scans": 1, "candidates": 0, "trades": 0}
        run(original.cycle(req.app, ("offline-key", "offline-secret")))
        assert original.public_config(state)["scanner"]["stage"] == "WAITING_NEXT_CANDLE"
        scan.assert_awaited_once()
        live.account_snapshot.assert_awaited_once()
        original.check_entry.assert_awaited_once()


def test_original_live_counts_only_successful_candidate_orders_in_last_24h(state, monkeypatch):
    req = request_for(state)
    monkeypatch.setattr(live, "readiness_for", Mock(return_value={"ready": True}))
    monkeypatch.setattr(live, "client_for_with_credentials", Mock(return_value=object()))
    monkeypatch.setattr(live, "account_snapshot", AsyncMock(return_value={
        "wallet_balance": 1000, "positions": [], "open_orders": [],
    }))
    monkeypatch.setattr(original, "check_entry", AsyncMock())
    symbols = ["BTCUSDT"]
    monkeypatch.setattr(original, "resolve_live_universe", AsyncMock(return_value=symbols))
    candidate = {"symbol": "BTCUSDT", "opportunity_score": 1.0}
    decision = result()
    monkeypatch.setattr(original.original, "live_candidates", AsyncMock(return_value=(
        [(candidate, decision)], {"BTCUSDT": decision},
    )))
    monkeypatch.setattr(original, "build_spec", AsyncMock(return_value={"margin_usdt": 10.0}))
    execute = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(live, "execute_live_order", execute)

    run(original.cycle(req.app, ("offline-key", "offline-secret")))

    scanner = original.public_config(state)["scanner"]
    assert scanner["candidate_count"] == 1
    assert scanner["last_24h"] == {"scans": 1, "candidates": 1, "trades": 1}
    execute.assert_awaited_once()


def test_original_minimum_trade_size_rejection_skips_symbol_and_keeps_automation_running(state, monkeypatch):
    req = request_for(state)
    monkeypatch.setattr(live, "readiness_for", Mock(return_value={"ready": True}))
    monkeypatch.setattr(live, "client_for_with_credentials", Mock(return_value=object()))
    monkeypatch.setattr(live, "account_snapshot", AsyncMock(return_value={
        "wallet_balance": 1000, "positions": [], "open_orders": [],
    }))
    monkeypatch.setattr(original, "check_entry", AsyncMock())
    symbols = ["BTCUSDT", "ETHUSDT"]
    monkeypatch.setattr(original, "resolve_live_universe", AsyncMock(return_value=symbols))
    decision = result()
    candidates = [
        ({"symbol": symbol, "opportunity_score": 1.0}, decision)
        for symbol in symbols
    ]
    monkeypatch.setattr(
        original.original, "live_candidates",
        AsyncMock(return_value=(candidates, {"BTCUSDT": decision, "ETHUSDT": decision})),
    )
    size = {
        "total_quantity": "0.4", "total_notional_usdt": "4",
        "tp1_quantity": "0.2", "tp1_notional_usdt": "2",
        "remaining_quantity": "0.2", "remaining_notional_usdt": "2",
        "min_qty": "0.001", "min_notional_usdt": "5",
    }
    monkeypatch.setattr(original, "build_spec", AsyncMock(side_effect=[
        live.MinimumTradeSizeError("BTCUSDT", size),
        {"margin_usdt": 10.0},
    ]))
    execute = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(live, "execute_live_order", execute)

    run(original.cycle(req.app, ("offline-key", "offline-secret")))

    scanner = original.public_config(state)["scanner"]
    assert scanner["candidate_count"] == 1
    assert scanner["rejection_counts"]["minimum_trade_size"] == 1
    assert scanner["near_misses"][0]["symbol"] == "BTCUSDT"
    assert scanner["near_misses"][0]["total_notional_usdt"] == "4"
    assert scanner["near_misses"][0]["tp1_notional_usdt"] == "2"
    assert scanner["near_misses"][0]["remaining_notional_usdt"] == "2"
    assert scanner["last_24h"] == {"scans": 1, "candidates": 1, "trades": 1}
    assert original.running(state)
    assert not state["original_v2_live"]["paused"]
    execute.assert_awaited_once()
    assert execute.await_args.args[1].symbol == "ETHUSDT"


def test_original_live_cooldown_is_reported_without_becoming_an_empty_scan(state, monkeypatch):
    req = request_for(state)
    monkeypatch.setattr(live, "readiness_for", Mock(return_value={"ready": True}))
    monkeypatch.setattr(live, "client_for_with_credentials", Mock(return_value=object()))
    monkeypatch.setattr(live, "account_snapshot", AsyncMock(return_value={
        "wallet_balance": 1000, "positions": [], "open_orders": [],
    }))
    monkeypatch.setattr(original, "check_entry", AsyncMock())
    monkeypatch.setattr(original, "resolve_live_universe", AsyncMock(return_value=["BTCUSDT"]))
    cooldown = live.LiveRateLimitError("Binance market-data cooldown", retry_after=300, exchange_code=-1003)
    monkeypatch.setattr(original.original, "live_candidates", AsyncMock(side_effect=cooldown))

    with pytest.raises(live.LiveRateLimitError):
        run(original.cycle(req.app, ("offline-key", "offline-secret")))

    scanner = original.public_config(state)["scanner"]
    assert scanner["stage"] == "RATE_LIMIT_WAIT"
    assert scanner["last_failure"]["reason_code"] == "binance_rate_limit"
    assert scanner["last_failure"]["message"] == "Binance market-data cooldown"
    assert scanner.get("candidate_count") is None
    assert state["original_v2_live"]["enabled"] is True


def test_original_live_24h_summary_ignores_expired_or_invalid_scan_records(state, monkeypatch):
    now = 2_000_000_000.0
    monkeypatch.setattr(original.time, "time", lambda: now)
    state["original_v2_live"]["scanner"] = {
        "history": [
            {"at": now - 86401, "candidates": 9, "trades": 8},
            {"at": now - 3600, "candidates": 3, "trades": 1},
            {"at": now - 100, "candidates": -1, "trades": 1},
            {"at": now + 1, "candidates": 20, "trades": 20},
        ],
    }

    scanner = original.public_config(state)["scanner"]

    assert scanner["last_24h"] == {"scans": 1, "candidates": 3, "trades": 1}


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
    def __init__(self, gap=False, short_history_symbol=None):
        self.data = decisions.fixtures.open_dataset()
        self.gap = gap
        self.short_history_symbol = short_history_symbol
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
        if params["symbol"] == self.short_history_symbol and params["interval"] == "4h":
            rows = rows[-180:]
        return [[row["time"] * 1000, row["open"], row["high"], row["low"], row["close"], row["volume"],
                 (row["time"] + duration) * 1000 - 1, row.get("quote_volume") or 1_000_000] for row in rows]


def test_live_scan_uses_stream_fetcher_for_every_strategy_interval():
    client = CandleClient()
    fetcher_client = CandleClient()
    calls = []

    async def streamed_fetch(symbol, interval, limit):
        calls.append((symbol, interval, limit))
        return await fetcher_client.public_get(
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
        )

    run(demo_execution.live_candidates(
        client,
        decisions.fixtures.AT,
        set(),
        list(decisions.SYMBOLS),
        require_complete=True,
        kline_fetch=streamed_fetch,
    ))

    assert len(calls) == len(decisions.SYMBOLS) * len(strategy.INTERVALS)
    assert {interval for _, interval, _ in calls} == set(strategy.INTERVALS)
    assert all(limit == 260 for _, _, limit in calls)
    assert [path for path, _ in client.calls] == ["/fapi/v1/exchangeInfo"]
    assert all(path == "/fapi/v1/klines" for path, _ in fetcher_client.calls)


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


def test_live_scan_diagnostics_account_for_every_symbol_without_changing_candidates():
    symbols = list(decisions.SYMBOLS)
    expected, expected_results = run(demo_execution.live_candidates(
        CandleClient(), decisions.fixtures.AT, set(), symbols, require_complete=True,
    ))
    diagnostics = {}

    actual, results = run(demo_execution.live_candidates(
        CandleClient(), decisions.fixtures.AT, set(), symbols,
        require_complete=True, diagnostics=diagnostics,
    ))

    assert [candidate["symbol"] for candidate, _ in actual] == [
        candidate["symbol"] for candidate, _ in expected
    ]
    assert {
        symbol: (value.signal.decision, value.signal.strategy_eligible, value.signal.reasons)
        for symbol, value in results.items()
    } == {
        symbol: (value.signal.decision, value.signal.strategy_eligible, value.signal.reasons)
        for symbol, value in expected_results.items()
    }
    assert diagnostics["scanned_symbol_count"] == len(symbols)
    assert diagnostics["data_complete_count"] == len(symbols)
    assert diagnostics["candidate_count"] == len(actual)
    assert sum(diagnostics["rejection_counts"].values()) + len(actual) == len(symbols)
    assert len(diagnostics["near_misses"]) == min(3, len(symbols))
    assert all(item["symbol"] in symbols and item["reason_code"] for item in diagnostics["near_misses"])


def test_live_scan_diagnostics_name_incomplete_candle_symbol_and_interval():
    symbols = list(decisions.SYMBOLS)
    diagnostics = {}

    eligible, results = run(demo_execution.live_candidates(
        CandleClient(gap=True), decisions.fixtures.AT, set(), symbols,
        require_complete=True, diagnostics=diagnostics,
    ))

    assert eligible == []
    assert results == {}
    assert diagnostics["failure"] is None
    assert diagnostics["rejection_counts"]["data_missing"] == len(symbols)
    assert diagnostics["data_complete_count"] == 0
    assert all("INCOMPLETE_ORIGINAL_LIVE_HISTORY" in item["reason"] for item in diagnostics["near_misses"])


def test_one_short_4h_history_is_excluded_without_stopping_other_symbols(monkeypatch):
    evaluate = Mock(wraps=strategy.evaluate_live_universe)
    monkeypatch.setattr(strategy, "evaluate_live_universe", evaluate)
    diagnostics = {}
    client = CandleClient(short_history_symbol="PONSUSDT")
    _, results = run(demo_execution.live_candidates(
        client, decisions.fixtures.AT, set(), ["PONSUSDT", "FILUSDT"],
        require_complete=True, diagnostics=diagnostics,
    ))
    assert set(results) == {"FILUSDT"}
    assert [call.args[0].symbol for call in evaluate.call_args_list] == ["FILUSDT"]
    assert diagnostics["failure"] is None
    assert diagnostics["data_complete_count"] == 1
    assert diagnostics["rejection_counts"]["data_missing"] == 1
    rejection = next(item for item in diagnostics["near_misses"] if item["symbol"] == "PONSUSDT")
    assert rejection["reason_code"] == "data_missing"
    assert "4h" in rejection["reason"]
    assert any(params and params["symbol"] == "FILUSDT" for _, params in client.calls)


def test_cycle_keeps_session_open_after_one_symbol_has_short_history(state, monkeypatch):
    application = request_for(state).app
    client = CandleClient(short_history_symbol="PONSUSDT")
    expires = time.time() + 24 * 3600
    state["original_v2_live"]["session_expires_at_epoch"] = expires
    monkeypatch.setattr(original.time, "time", lambda: decisions.fixtures.AT)
    monkeypatch.setattr(live, "readiness_for", Mock(return_value={"ready": True}))
    monkeypatch.setattr(live, "client_for_with_credentials", Mock(return_value=client))
    monkeypatch.setattr(live, "account_snapshot", AsyncMock(return_value={
        "positions": [{"symbol": "FILUSDT"}], "open_orders": [], "open_algo_orders": [],
    }))
    monkeypatch.setattr(original, "check_entry", AsyncMock())
    monkeypatch.setattr(original, "resolve_live_universe", AsyncMock(return_value=["PONSUSDT", "FILUSDT"]))
    monkeypatch.setattr(original, "execution_policy", Mock(return_value={
        "max_loss_per_trade": "5", "max_margin_per_trade": "25",
        "fee_bps_per_side": "4", "slippage_bps_per_side": "4",
    }))
    orders = AsyncMock()
    monkeypatch.setattr(live, "execute_live_order", orders)
    run(original.cycle(application, ("offline-api-key", "offline-secret")))
    scanner = state["original_v2_live"]["scanner"]
    assert scanner["stage"] == "SCAN_COMPLETE"
    assert scanner["rejection_counts"]["data_missing"] == 1
    assert scanner["data_complete_count"] == 1
    assert scanner["last_failure"] is None
    assert original.running(state)
    assert state["real_trading_locked"] is False
    assert state["original_v2_live"]["session_expires_at_epoch"] == expires
    orders.assert_not_awaited()


def test_live_scan_excludes_symbols_that_cannot_meet_minimum_order_parts(monkeypatch):
    decision = result()
    monkeypatch.setattr(
        strategy, "evaluate_live_universe",
        Mock(return_value=decision),
    )
    monkeypatch.setattr(
        live, "rank_market_tickers",
        Mock(return_value=[{
            "symbol": "BTCUSDT", "opportunity_score": 1.0,
            "volume": 1_000_000, "change": 1.0,
        }]),
    )
    diagnostics = {}

    eligible, results = run(demo_execution.live_candidates(
        CandleClient(), decisions.fixtures.AT, set(), ["BTCUSDT"],
        require_complete=True,
        diagnostics=diagnostics,
        sizing_context={
            "max_loss_per_trade": "0.01", "max_margin_per_trade": "25",
            "fee_bps_per_side": "4", "slippage_bps_per_side": "4",
        },
    ))

    assert eligible == []
    assert results["BTCUSDT"] is decision
    assert diagnostics["candidate_count"] == 0
    assert diagnostics["rejection_counts"]["minimum_trade_size"] == 1
    near_miss = diagnostics["near_misses"][0]
    assert near_miss["symbol"] == "BTCUSDT"
    assert near_miss["reason_code"] == "minimum_trade_size"
    assert Decimal(near_miss["total_notional_usdt"]) < Decimal(near_miss["min_notional_usdt"])


@pytest.mark.parametrize("hours", [1, 4, 12, 24])
@pytest.mark.parametrize("authority_hours", [None, 0.5, 2])
@pytest.mark.parametrize("limit_source", ["login", "consent"])
def test_start_success_consumes_fresh_2fa_then_existing_auto_authority(state, monkeypatch, hours, authority_hours, limit_source):
    now = 2_000_000_000.0
    monkeypatch.setattr(original.time, "time", lambda: now)
    req = request_for(state)
    state["auto"]["enabled"] = False
    state["original_v2_live"]["enabled"] = False
    state["original_v2_live"]["daily"] = {"day": "saved-day", "loss": 12, "locked": False}
    state["original_v2_live"]["session_id"] = "expired-session"
    state["original_v2_live"]["session_expires_at_epoch"] = now - 1
    state["original_v2_live"]["session_warning_sent"] = True
    trace = []
    monkeypatch.setattr(original, "authenticated_user", Mock(return_value={"id": "owner", "role": "OWNER"}))
    monkeypatch.setattr(live, "guard_execution_ownership", Mock())
    monkeypatch.setattr(account_settings, "enabled", AsyncMock(return_value=True))
    async def code(*args, **kwargs):
        assert kwargs == {"required": True}
        trace.append("2fa")
    async def start(*args, **kwargs):
        assert kwargs == {"mfa_verified": True, "state_lock_held": True}
        trace.append("existing-auto")
        state["auto"]["enabled"] = True
        state["auto_authorization"]["auth_expires_at_epoch"] = now + ((authority_hours or 48) if limit_source == "login" else 48) * 3600
    monkeypatch.setattr(account_settings, "require_totp", code)
    monkeypatch.setattr(live, "_start_live_auto_session", start)
    monkeypatch.setattr(live, "live_auto_start_gate", Mock(return_value=(True, "ready")))
    monkeypatch.setattr(live, "live_credentials_status", Mock(return_value=("key", "secret", "fingerprint")))
    monkeypatch.setattr(live, "client_for", Mock(return_value=object()))
    monkeypatch.setattr(live, "account_snapshot", AsyncMock(return_value={"wallet_balance": 1000, "unrealized_pnl": 0}))
    monkeypatch.setattr(original, "daily_guard", AsyncMock())
    consent_hours = (authority_hours or 48) if limit_source == "consent" else 48
    monkeypatch.setattr(live, "consent_status", Mock(return_value={"active": True, "expires_at": datetime.fromtimestamp(now + consent_hours * 3600, timezone.utc).isoformat()}))
    response = run(original.start(req, original.Start(confirmation="ORIGINAL V2 CANLI", totp_code="123456", session_hours=hours)))
    assert response["enabled"]
    expected_expiry = now + min(hours, consent_hours) * 3600
    assert state["auto"]["session_until"] == expected_expiry
    assert state["auto_authorization"]["expires_at_epoch"] == expected_expiry
    assert response["session"]["hours"] == hours
    assert response["session"]["remaining_seconds"] == expected_expiry - now
    assert response["session"]["authority_limited"] is (expected_expiry < now + hours * 3600)
    assert not response["session"]["warning_sent"]
    assert state["original_v2_live"]["session_id"] != "expired-session"
    assert response["daily"]["loss"] == 12
    assert trace == ["2fa", "existing-auto"]
    live.persist_state_awaited.assert_awaited_once_with(state)


def test_original_start_does_not_reacquire_live_state_lock(state, monkeypatch):
    req = request_for(state)
    state["auto"]["enabled"] = False
    state["original_v2_live"]["enabled"] = False
    state["policy"]["interval"] = "15m"
    owner = {"id": "owner", "role": "OWNER", "auth_version": 1}
    monkeypatch.setattr(original, "authenticated_user", Mock(return_value=owner))
    monkeypatch.setattr(live, "execution_owner", Mock(return_value=owner))
    monkeypatch.setattr(live, "guard_execution_ownership", Mock())
    monkeypatch.setattr(account_settings, "enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(account_settings, "require_totp", AsyncMock())
    monkeypatch.setattr(live, "live_auto_start_gate", Mock(return_value=(True, "ready")))
    monkeypatch.setattr(live, "live_credentials_status", Mock(return_value=("key", "secret", "fingerprint")))
    monkeypatch.setattr(live, "client_for", Mock(return_value=object()))
    monkeypatch.setattr(live, "account_snapshot", AsyncMock(return_value={"wallet_balance": 1000, "unrealized_pnl": 0}))
    monkeypatch.setattr(original, "daily_guard", AsyncMock())
    monkeypatch.setattr(live, "consent_status", Mock(return_value={
        "active": True,
        "expires_at": datetime.fromtimestamp(time.time() + 3600, timezone.utc).isoformat(),
    }))

    response = run(asyncio.wait_for(
        original.start(req, original.Start(confirmation="ORIGINAL V2 CANLI", totp_code="123456")),
        timeout=1,
    ))

    assert response["enabled"] is True
    assert state["auto"]["enabled"] is True
    live.account_snapshot.assert_awaited_once()
    live.persist_state_awaited.assert_awaited_once_with(state)


@pytest.mark.parametrize("renewal", [False, True])
def test_2fa_failure_never_starts_or_fetches_exchange(state, monkeypatch, renewal):
    req = request_for(state)
    state["auto"]["enabled"] = False
    state["original_v2_live"]["enabled"] = False
    if renewal:
        state["original_v2_live"]["session_expires_at_epoch"] = time.time() - 1
    monkeypatch.setattr(original, "authenticated_user", Mock(return_value={"id": "owner", "role": "OWNER"}))
    monkeypatch.setattr(account_settings, "enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(account_settings, "require_totp", AsyncMock(side_effect=HTTPException(401, "invalid 2fa")))
    monkeypatch.setattr(live, "live_auto_start_gate", Mock(return_value=(True, "ready")))
    start = AsyncMock()
    monkeypatch.setattr(live, "_start_live_auto_session", start)
    client = Mock()
    monkeypatch.setattr(live, "client_for", client)
    with pytest.raises(HTTPException) as error:
        run(original.start(req, original.Start(confirmation="ORIGINAL V2 CANLI", totp_code="123456")))
    assert error.value.status_code == 401
    start.assert_not_awaited()
    client.assert_not_called()


def test_original_session_warning_is_once_and_expiry_keeps_positions_and_latches(state, monkeypatch):
    now = 2_000_000_000.0
    monkeypatch.setattr(original.time, "time", lambda: now)
    config = state["original_v2_live"]
    config.update(session_expires_at_epoch=now + 1801, session_warning_sent=False,
                  daily={"locked": True}, fill_lock={"active": True})
    state["plans"] = {"saved": {"symbol": "BTCUSDT", "status": "OPEN"}}
    state["intents"] = {"saved-intent": {"status": "UNKNOWN"}}
    original.check_session_time(state)
    assert not config.get("alerts")
    monkeypatch.setattr(original.time, "time", lambda: now + 1)
    original.check_session_time(state)
    original.check_session_time(state)
    assert len(config["alerts"]) == 1
    assert config["alerts"][0]["type"] == "ORIGINAL_SESSION_EXPIRING"
    assert config["alerts"][0]["read"] is False
    assert state["events"][0]["recipient_user_id"] == "owner"
    monkeypatch.setattr(original.time, "time", lambda: now + 1801)
    original.check_session_time(state)
    assert not original.running(state)
    assert not state["auto"]["enabled"] and not state["live_auto_trade"]
    assert state["real_trading_locked"] and state["armed_until"] == 0
    assert config["daily"]["locked"] and config["fill_lock"]["active"]
    assert state["plans"]["saved"]["status"] == "OPEN"
    assert state["intents"]["saved-intent"]["status"] == "UNKNOWN"
    assert original.public_config(state)["session"]["remaining_seconds"] == 0
    count = len(config["alerts"])
    original.check_session_time(state)
    assert len(config["alerts"]) == count


def test_original_session_clock_does_not_change_legacy_v25(state):
    del state["original_v2_live"]
    before = copy.deepcopy({key: value for key, value in state.items() if key != "lock"})
    original.check_session_time(state)
    assert {key: value for key, value in state.items() if key != "lock"} == before
    assert live.LIVE_AUTO_SESSION_SECONDS == 3600
    assert original.public_config(state)["session"]["expires_at"] is None


@pytest.mark.parametrize("invalid_authority", ["missing-consent", "expired-consent"])
def test_original_start_fails_closed_without_duration_authority(state, monkeypatch, invalid_authority):
    state["auto"]["enabled"] = False
    state["original_v2_live"].update(enabled=False, daily={"locked": True, "loss": 12})
    monkeypatch.setattr(original, "authenticated_user", Mock(return_value={"id": "owner", "role": "OWNER"}))
    monkeypatch.setattr(account_settings, "enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(account_settings, "require_totp", AsyncMock())
    monkeypatch.setattr(live, "live_auto_start_gate", Mock(return_value=(True, "ready")))
    monkeypatch.setattr(live, "live_credentials_status", Mock(return_value=("key", "secret", "fingerprint")))
    monkeypatch.setattr(live, "client_for", Mock(return_value=object()))
    monkeypatch.setattr(live, "account_snapshot", AsyncMock(return_value={"wallet_balance": 1000}))
    monkeypatch.setattr(original, "daily_guard", AsyncMock())
    async def start(*args, **kwargs):
        state["auto"]["enabled"] = True
        state["auto_authorization"]["auth_expires_at_epoch"] = time.time() - 1
    monkeypatch.setattr(live, "_start_live_auto_session", start)
    consent = {"active": True, "expires_at": datetime.fromtimestamp(time.time() + 86400, timezone.utc).isoformat()}
    if invalid_authority == "missing-consent":
        consent["expires_at"] = None
    if invalid_authority == "expired-consent":
        consent["expires_at"] = datetime.fromtimestamp(time.time() - 1, timezone.utc).isoformat()
    monkeypatch.setattr(live, "consent_status", Mock(return_value=consent))
    with pytest.raises(HTTPException) as error:
        run(original.start(request_for(state), original.Start(confirmation="ORIGINAL V2 CANLI", totp_code="123456", session_hours=24)))
    assert error.value.status_code == 423
    assert not original.running(state) and not state["auto"]["enabled"]
    assert state["real_trading_locked"] and state["armed_until"] == 0
    assert state["original_v2_live"]["daily"] == {"locked": True, "loss": 12}


@pytest.mark.parametrize("hours", [1, 4, 12, 24])
def test_original_active_session_cannot_be_extended(state, monkeypatch, hours):
    monkeypatch.setattr(original, "authenticated_user", Mock(return_value={"id": "owner", "role": "OWNER"}))
    monkeypatch.setattr(account_settings, "enabled", AsyncMock(return_value=True))
    code = AsyncMock()
    start = AsyncMock()
    monkeypatch.setattr(account_settings, "require_totp", code)
    monkeypatch.setattr(live, "_start_live_auto_session", start)
    expiry = state["auto"]["session_until"]
    with pytest.raises(HTTPException) as error:
        run(original.start(request_for(state), original.Start(confirmation="ORIGINAL V2 CANLI", totp_code="123456", session_hours=hours)))
    assert error.value.status_code == 409
    assert state["auto"]["session_until"] == expiry
    code.assert_not_awaited()
    start.assert_not_awaited()


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
    snapshot = {
        "wallet_balance": 300, "available_balance": 300,
        "positions": [], "open_orders": [], "open_algo_orders": [],
        "open_algo_orders_available": True, "algo_orders_quality": "VALID_EMPTY",
        "unrealized_pnl": 0, "hedge_mode": False, "multi_assets_mode": False,
    }
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
    isolated_update = AsyncMock(return_value="ISOLATED")
    leverage_update = AsyncMock(return_value={"margin_type": "ISOLATED", "applied_leverage": 3})
    monkeypatch.setattr(live, "set_live_isolated_margin", isolated_update)
    monkeypatch.setattr(live, "apply_live_verified_leverage", leverage_update)
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
    isolated_update.assert_awaited_once_with(client, value.signal.symbol)
    leverage_update.assert_awaited_once_with(
        client, value.signal.symbol, 3, expected_margin_type="ISOLATED",
    )
    assert live.persist_state_awaited.await_count >= 2


def test_original_minimum_trade_size_error_does_not_pause_executor_before_submission(state, monkeypatch):
    req = request_for(state)
    req.app.state.maintenance = {"mode": "NORMAL"}
    value = result()
    signal = value.legacy["analysis"]
    monkeypatch.setattr(live.time, "time", lambda: value.signal.decision_time)
    state["armed_until"] = 0
    state["auto_authorization"] = {"user_id": "owner", "fingerprint": "fingerprint", "auth_version": 1}
    client = EntryClient()
    snapshot = {
        "wallet_balance": 300, "available_balance": 300,
        "positions": [], "open_orders": [], "open_algo_orders": [],
        "open_algo_orders_available": True, "algo_orders_quality": "VALID_EMPTY",
        "unrealized_pnl": 0, "hedge_mode": False, "multi_assets_mode": False,
    }
    monkeypatch.setattr(live, "client_for_with_credentials", Mock(return_value=client))
    monkeypatch.setattr(live, "account_snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(live, "readiness_for", Mock(return_value={"ready": True}))
    monkeypatch.setattr(live, "background_authorization_active", AsyncMock(return_value=True))
    from app import v22_commercial
    monkeypatch.setattr(v22_commercial, "validate_authoritative_session", AsyncMock(return_value={"role": "OWNER"}))
    monkeypatch.setattr(original, "check_entry", AsyncMock())
    monkeypatch.setattr(original, "daily_guard", AsyncMock())
    monkeypatch.setattr(live, "spread_bps", AsyncMock(return_value=1))
    monkeypatch.setattr(live, "evaluate_entry_gates", Mock(return_value={"passed": True, "reason": "pass", "gates": []}))
    metrics = {
        "total_quantity": "0.4", "total_notional_usdt": "4",
        "tp1_quantity": "0.2", "tp1_notional_usdt": "2",
        "remaining_quantity": "0.2", "remaining_notional_usdt": "2",
        "min_qty": "0.001", "min_notional_usdt": "5",
    }
    monkeypatch.setattr(original, "build_spec", AsyncMock(
        side_effect=live.MinimumTradeSizeError(value.signal.symbol, metrics),
    ))
    body = live.LiveOrderRequest(
        symbol=value.signal.symbol, direction=signal["direction"], margin_usdt=5, leverage=3,
        stop_loss=signal["stop_loss"], tp1=signal["tp1"], tp2=signal["tp2"], tp3=signal["tp3"],
    )

    with pytest.raises(HTTPException) as error:
        run(live.execute_live_order(
            req.app, body, source=original.SOURCE,
            credentials=("safe-key-123456", "safe-secret-123456"), strategy_result=value,
        ))

    assert error.value.status_code == 409
    assert original.running(state)
    assert not state["original_v2_live"]["paused"]
    assert state["real_trading_locked"] is False
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
