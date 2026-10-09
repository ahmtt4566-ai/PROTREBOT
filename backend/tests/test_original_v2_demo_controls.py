"""Owner-bound Original controls, with fake market data and no private client."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest
import test_original_v2_demo_execution as execution
from app import original_v2_demo_execution as original
from app import v21_demo as control
from app.commercial_core import issue_token
from fastapi import HTTPException
from starlette.requests import Request


def context():
    app, runtime, state = execution.states()
    state["auto"].update({"enabled": False, "user_confirmed": False})
    secret = b"synthetic-original-controls-session-secret"
    app.state.v22_commercial = {"secret": secret}
    token = issue_token("owner", "CUSTOMER", secret)
    request = Request({
        "type": "http", "app": app, "headers": [],
        "state": {"member": {"id": "owner", "auth_version": 1}, "v22_authoritative_token": token},
    })
    return app, runtime, state, request


class FakeMarket:
    async def public_get(self, *_args):
        raise AssertionError("Candidates must use the provided synthetic decisions.")

    async def signed(self, *_args):
        raise AssertionError("Dry-run must never use a signed exchange client.")


def exercise_cycle():
    async def run():
        app, runtime, state, request = context()
        auto_before = deepcopy(state["auto"])
        data = execution.result()
        calls = []

        async def candidates(client, at, occupied):
            assert isinstance(client, FakeMarket)
            assert occupied == set()
            calls.append(at)
            values = {
                symbol: replace(data, signal=replace(data.signal, symbol=symbol, decision_time=at))
                for symbol in execution.decisions.SYMBOLS
            }
            return [({"symbol": symbol}, result) for symbol, result in values.items()], values

        app.state.http = object()
        # Give this application a fake market client without changing any module.
        market = FakeMarket()
        original_client = control.market_client_for(app)
        assert not original_client.api_key
        candidates_bound = execution.clone(candidates)
        function = execution.clone(original.dry_run_cycle, candidates=lambda _client, at, occupied: candidates_bound(market, at, occupied))
        response = await function(app, request=request, demo_state=runtime, state=state)
        again = await function(app, request=request, demo_state=runtime, state=state)
        assert again == response
        assert len(calls) == 1
        assert response["orders_sent"] == 0 and response["order"] is None
        assert len(response["plans"]) == 8
        assert len(state["journal"]) == 8
        assert len(state["original_v2"]["dry_run_plans"]) == 8
        assert state["auto"] == auto_before
        assert runtime["plans"] == {}
        for plan in response["plans"]:
            assert plan["status"] == "DRY_RUN"
            assert plan["strategy_id"] == original.strategy.STRATEGY_ID
            assert all(field in plan for field in original.IDENTITY_FIELDS)
            assert plan["quantity"] is None
            assert plan["order_authorized"] is False and plan["execution_connected"] is False
            row = next(row for row in state["journal"] if row["plan_id"] == plan["id"])
            assert row["kind"] == "ORIGINAL_DRY_RUN"
            assert all(row[field] == plan[field] for field in original.IDENTITY_FIELDS)
        serialized = control.serializable_state(state)
        restored = control._state_from_payload(serialized, "owner", app)
        assert restored["original_v2"] == state["original_v2"]
    asyncio.run(run())


@pytest.mark.parametrize(("enabled", "send"), [("false", "false"), ("true", "false"), ("true", "true"), ("false", "true")])
def test_explicit_dry_cycle_never_orders_under_any_flags(enabled, send):
    subprocess.check_call(
        [sys.executable, "-B", "-c",
         "import sys;sys.path[:0]=['backend','backend/tests'];import test_original_v2_demo_controls as t;t.exercise_cycle()"],
        cwd=execution.ROOT,
        env={**os.environ, original.strategy.FEATURE_FLAG: enabled, original.SEND_FLAG: send,
             "PYTHONDONTWRITEBYTECODE": "1"},
    )


@pytest.mark.parametrize("failure", ["owner", "arm", "maintenance", "enabled", "busy", "expired"])
def test_explicit_dry_cycle_preserves_guards_before_market_reads(failure):
    async def run():
        app, runtime, state, request = context()
        if failure == "owner":
            request.state.member["id"] = "someone-else"
        elif failure == "arm":
            runtime["armed_until"] = 0
        elif failure == "maintenance":
            app.state.maintenance["mode"] = "MAINTENANCE"
        elif failure in {"enabled", "busy"}:
            state["auto"][failure] = True
        else:
            request.state.v22_authoritative_token = issue_token(
                "owner", "CUSTOMER", app.state.v22_commercial["secret"], now=1, ttl_seconds=1,
            )
        with pytest.raises((HTTPException, execution.demo.BinanceDemoError)):
            await original.dry_run_cycle(app, request=request, demo_state=runtime, state=state)
        assert state["journal"] == [] and runtime["plans"] == {}
        assert "original_v2" not in state
        assert not hasattr(app.state, "http")
    asyncio.run(run())


@pytest.mark.parametrize(("strategy_id", "confirmation", "owner"), [
    ("unknown", "DEMO OTOMATİK", True),
    (original.strategy.STRATEGY_ID, " ", True),
    (original.strategy.STRATEGY_ID, "DEMO OTOMATİK", False),
])
def test_dry_run_route_rejects_bad_identity_confirmation_and_anonymous_owner(strategy_id, confirmation, owner):
    app, _runtime, state, request = context()
    if not owner:
        request.state.member = None
    with pytest.raises(HTTPException) as failure:
        asyncio.run(control.v21_original_dry_run(
            request, control.AutoStartRequest(strategy_id=strategy_id, confirmation=confirmation),
        ))
    assert failure.value.status_code == (422 if owner else 401)
    assert state["journal"] == [] and not hasattr(app.state, "http")


def test_status_is_read_only_and_does_not_modify_legacy_summary():
    _app, _runtime, state, _request = context()
    serialized = execution.clone(control.serializable_state, now_iso=lambda: "2025-03-01T00:00:00Z")
    before = deepcopy(serialized(state))
    certificate = execution.clone(control.certificate_payload, now_iso=lambda: "2025-03-01T00:00:00Z")
    summary_payload = execution.clone(control.summary_payload, certificate_payload=certificate)
    summary = deepcopy(summary_payload(state))
    payload = original.status_payload(state)
    assert payload["strategy_id"] == original.strategy.STRATEGY_ID
    assert payload["policy"]["allowed_symbols"] == list(execution.decisions.SYMBOLS)
    assert payload["dry_run_plans"] == []
    assert serialized(state) == before
    assert summary_payload(state) == summary
    assert "original_v2" not in state
    assert json.dumps(payload)


def exercise_api_cycle():
    async def run():
        app, runtime, state, request = context()
        app.state._binance_demo_user_state = {"owner": runtime}
        app.state._v21_demo_user_state = {"owner": state}
        calls = []
        dataset = execution.decisions.fixtures.open_dataset()
        at = int(datetime.now(timezone.utc).timestamp()) // 900 * 900
        shift = (at - execution.decisions.fixtures.AT) // 14400 * 14400

        def respond(req):
            calls.append((req.method, req.url.path))
            assert req.method == "GET" and "signature" not in req.url.params
            assert "x-mbx-apikey" not in req.headers
            if req.url.path == "/fapi/v1/exchangeInfo":
                info = deepcopy(dataset.metadata["exchange_info"])
                reference = next(row for row in info["symbols"] if row["symbol"] == "BTCUSDT")
                info["symbols"] = [{**deepcopy(reference), "symbol": symbol} for symbol in execution.decisions.SYMBOLS]
                return httpx.Response(200, json=info)
            assert req.url.path == "/fapi/v1/klines"
            interval = req.url.params["interval"]
            assert req.url.params["symbol"] in execution.decisions.SYMBOLS
            duration = {"15m": 900, "1h": 3600, "4h": 14400}[interval]
            rows = dataset.frames["BTCUSDT"][interval].closed(execution.decisions.fixtures.AT)[-259:]
            return httpx.Response(200, json=[[
                (row["time"] + shift) * 1000, row["open"], row["high"], row["low"],
                row["close"], row["volume"], (row["time"] + shift + duration) * 1000 - 1,
                row.get("quote_volume") or 1_000_000,
            ] for row in rows])

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            app.state.http = client
            response = await control.v21_original_dry_run(
                request, control.AutoStartRequest(strategy_id=original.strategy.STRATEGY_ID, confirmation="DEMO OTOMATİK"),
            )
        assert len(calls) == 25
        assert response["orders_sent"] == 0 and response["order"] is None
        assert len(response["plans"]) == len(state["journal"]) == 8
        assert runtime["plans"] == {} and state["auto"]["enabled"] is False
        assert {plan["symbol"] for plan in response["plans"]} == set(execution.decisions.SYMBOLS)
        assert control.v21_original_status
        status = await control.v21_original_status(request)
        assert status["flags"] == {"enabled": True, "send_orders": True}
        assert len(status["dry_run_plans"]) == 8
    asyncio.run(run())


def test_actual_owner_route_and_native_candles_still_never_send_with_both_flags_on():
    subprocess.check_call(
        [sys.executable, "-B", "-c",
         "import sys;sys.path[:0]=['backend','backend/tests'];import test_original_v2_demo_controls as t;t.exercise_api_cycle()"],
        cwd=execution.ROOT,
        env={**os.environ, original.strategy.FEATURE_FLAG: "true", original.SEND_FLAG: "true",
             "PYTHONDONTWRITEBYTECODE": "1"},
    )
