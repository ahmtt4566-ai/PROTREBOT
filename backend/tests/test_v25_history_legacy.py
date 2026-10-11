"""Offline HTTP regressions using PostgreSQL JSONB-shaped LIVE snapshots."""
import asyncio
import copy
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import v25_execution as live


def trade(order_id=701, **changes):
    return {"id": 7, "symbol": "BTCUSDT", "orderId": order_id, "side": "SELL",
            "price": "50000", "qty": "0.001", "realizedPnl": "1.5",
            "commission": "0.01", "commissionAsset": "USDT", "time": 1791719400000, **changes}


@pytest.fixture
def history_app(monkeypatch):
    def build(plans, trades=None, income=None):
        payload = {"plans": copy.deepcopy(plans), "events": [{"symbol": "BTCUSDT"}]}
        pool = SimpleNamespace(fetchrow=AsyncMock(return_value={"payload": payload}))
        app = FastAPI()
        app.include_router(live.router)
        app.state.v25_execution = live.initial_state()
        app.state.db_pool = pool
        assert asyncio.run(live.restore_v25_state(app))
        async def signed(method, path, params):
            assert method == "GET"
            assert path in {"/fapi/v1/income", "/fapi/v1/userTrades"}
            assert params["limit"] == 1000
            return (trades if trades is not None else [trade()]) if path.endswith("userTrades") else (
                income if income is not None else [{
                    "symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "1.5",
                    "asset": "USDT", "time": 1791719400000,
                }]
            )
        exchange = SimpleNamespace(signed=AsyncMock(side_effect=signed))
        owner = Mock(return_value={"id": "owner", "role": "OWNER"})
        monkeypatch.setattr(live, "execution_owner", owner)
        monkeypatch.setattr(live, "client_for", Mock(return_value=exchange))
        return TestClient(app), app, exchange, owner
    return build


@pytest.mark.parametrize("legacy", [
    {"exchange_order_ids": None},
    {"close_client_order_ids": None},
    {"exchange_order_ids": None, "close_client_order_ids": None},
    {},
    {"exchange_order_ids": 21, "close_client_order_ids": "legacy-close"},
])
def test_legacy_and_current_jsonb_plans_read_without_migration(history_app, legacy):
    plans = {
        "old": {"symbol": "BTCUSDT", "entry_order_id": 20, **legacy},
        "current": {"symbol": "BTCUSDT", "entry_order_id": 11,
                    "exchange_order_ids": [11, "12"], "close_client_order_ids": ["current-close"]},
    }
    trades = [trade(11), trade(12), trade(20), trade(21), trade(701, clientOrderId="current-close"),
              trade(702, clientOrderId="legacy-close"), trade(703)]
    client, app, exchange, owner = history_app(plans, trades=trades)
    before = copy.deepcopy(app.state.v25_execution["plans"])
    response = client.get("/api/v25/history?limit=1000")
    assert response.status_code == 200
    body = response.json()
    expected = [703] if isinstance(legacy.get("exchange_order_ids"), int) else [21, 702, 703]
    assert [row["order_id"] for row in body["external_trades"]] == expected
    assert body["read_only"] is True and body["errors"] == []
    assert app.state.v25_execution["plans"] == before
    owner.assert_called_once()
    assert exchange.signed.await_count == 2
    app.state.db_pool.fetchrow.assert_awaited_once_with(
        "SELECT payload FROM application_state_snapshots WHERE state_key = $1", live.V25_SNAPSHOT_KEY,
    )


@pytest.mark.parametrize("malformed", [
    {"exchange_order_ids": {"secret-value": 1}},
    {"close_client_order_ids": [None, {"secret-value": 1}, "current-close"]},
    {"exchange_order_ids": ["²", "secret-value", 11, "12"]},
    {"entry_order_id": "²"},
])
def test_one_invalid_identity_cannot_collapse_history(history_app, caplog, malformed):
    plans = {"bad": {"symbol": "BTCUSDT", "exchange_order_ids": [11, "12"],
                     "close_client_order_ids": ["current-close"], **malformed},
             "current": {"symbol": "BTCUSDT", "entry_order_id": 20}}
    client, _, _, _ = history_app(plans, trades=[trade(20), trade(703)])
    with caplog.at_level(logging.WARNING):
        response = client.get("/api/v25/history?limit=1000")
    assert response.status_code == 200
    body = response.json()
    assert [row["order_id"] for row in body["external_trades"]] == [703]
    assert body["errors"]
    assert "history" in caplog.text.lower()
    assert "secret-value" not in caplog.text and "secret-value" not in response.text


@pytest.mark.parametrize("changes", [
    {"orderId": "²"}, {"orderId": {"secret-value": 1}},
    {"price": float("nan")}, {"time": {"secret-value": 1}},
])
def test_bad_exchange_row_is_reported_and_valid_rows_survive(history_app, changes):
    client, _, _, _ = history_app({}, trades=[trade(**changes), trade(703)])
    response = client.get("/api/v25/history?limit=1000")
    assert response.status_code == 200
    assert [row["order_id"] for row in response.json()["external_trades"]] == [703]
    assert response.json()["errors"]
    assert "secret-value" not in response.text


def test_missing_optional_legacy_exchange_fields_remain_unknown(history_app):
    client, _, _, _ = history_app({}, trades=[{"orderId": 703, "qty": "0.001", "price": "50000"}])
    response = client.get("/api/v25/history?limit=1000")
    assert response.status_code == 200
    row = response.json()["external_trades"][0]
    assert row["trade_id"] is None and row["realized_pnl"] is None and row["time"] is None
    assert response.json()["errors"] == []


def test_invalid_income_and_non_object_plan_do_not_hide_valid_history(history_app, caplog):
    income = [
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": float("inf")},
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "1.5", "time": 1791719400000},
    ]
    client, _, _, _ = history_app({"invalid-plan": None, "valid-plan": {
        "symbol": "BTCUSDT", "exchange_order_ids": [None, True, -1, 11],
    }}, trades=[trade(11), trade(703)], income=income)
    with caplog.at_level(logging.WARNING):
        response = client.get("/api/v25/history?limit=1000")
    assert response.status_code == 200
    body = response.json()
    assert [row["order_id"] for row in body["external_trades"]] == [703]
    assert [row["income"] for row in body["external_income"]] == ["1.5"]
    assert len(body["errors"]) == 3
    assert "LIVE history" in caplog.text


def test_symbol_filtered_history_uses_same_legacy_identity_reader(history_app):
    client, _, exchange, _ = history_app({"old": {
        "symbol": "BTCUSDT", "entry_order_id": "11",
        "exchange_order_ids": None, "close_client_order_ids": None,
    }}, trades=[trade(11), trade(703)])
    response = client.get("/api/v25/history?symbol=BTCUSDT&limit=1000")
    assert response.status_code == 200
    assert [row["order_id"] for row in response.json()["external_trades"]] == [703]
    assert response.json()["errors"] == []
    assert all(call.args[2]["symbol"] == "BTCUSDT" for call in exchange.signed.await_args_list)


def test_connection_setup_failure_has_explicit_http_status_not_500(history_app, monkeypatch):
    client, _, exchange, _ = history_app({})
    monkeypatch.setattr(live, "client_for", Mock(side_effect=live.LiveExchangeError(
        "Canlı API bağlantısı aktif değil.", http_status=412,
    )))
    response = client.get("/api/v25/history?limit=1000")
    assert response.status_code == 412
    assert response.json()["detail"] == "Canlı API bağlantısı aktif değil."
    exchange.signed.assert_not_awaited()
