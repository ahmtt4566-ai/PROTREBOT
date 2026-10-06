import asyncio
import sys
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


sys.path.insert(0, str(Path(__file__).parents[1]))

from app import v25_execution as execution  # noqa: E402


def application_for(symbols):
    state = execution.initial_state()
    state.update({
        "recovery_ready": True, "real_trading_locked": False,
        "live_auto_trade": True, "armed_until": time.time() + 300,
        "lock": asyncio.Lock(),
    })
    state["policy"]["allowed_symbols"] = symbols
    state["auto"].update({"enabled": True, "session_until": time.time() + 300})
    application = SimpleNamespace(state=SimpleNamespace(
        v25_execution=state, db_pool=None, maintenance={"mode": "NORMAL"},
    ))
    return application, state


def order_for(symbol):
    return execution.LiveOrderRequest(
        symbol=symbol, direction="LONG", margin_usdt=5, leverage=1,
        stop_loss=98, tp1=102, tp2=104, tp3=106,
    )


def spec_for(symbol):
    return {
        "symbol": symbol, "direction": "LONG", "side": "BUY",
        "order_type": "MARKET", "entry_price": "100", "quantity": "0.050",
        "margin_usdt": 5, "notional_usdt": 5, "leverage": 1,
        "stop_loss": "98", "targets": ["102", "104", "106"],
        "step": Decimal("0.001"), "min_qty": Decimal("0.001"),
        "min_notional": Decimal("5"), "estimated_stop_loss_usdt": 0.1,
    }


SNAPSHOT = {
    "available_balance": 100, "wallet_balance": 100,
    "positions": [], "open_orders": [], "hedge_mode": False,
}
CREDENTIALS = ("TEST_KEY_PLACEHOLDER", "TEST_SECRET_PLACEHOLDER")


class ScopeChangingClient(execution.BinanceLiveClient):
    def __init__(self, state, new_scope):
        self.state = state
        self.new_scope = new_scope
        self.calls = []

    async def signed(self, method, path, params=None):
        self.calls.append((method, path, dict(params or {})))
        if method == "GET":
            self.state["policy"]["allowed_symbols"] = self.new_scope
            return {}
        if (method, path) == ("POST", "/fapi/v1/order"):
            return {"orderId": 42, "status": "FILLED"}
        raise AssertionError(f"Unexpected mock request: {method} {path}")


class AutoSymbolScopeTests(unittest.TestCase):
    def setUp(self):
        persistence = patch.object(execution, "persist_state")
        persistence.start()
        self.addCleanup(persistence.stop)

    def execute(self, application, symbol, *, source="V25_AUTO", override=None, submit=None, credential_refresh=None):
        submit = submit or AsyncMock(return_value={"orderId": 42, "status": "FILLED"})
        with patch.multiple(
            execution,
            readiness_for=lambda *args, **kwargs: {"ready": True},
            client_for_with_credentials=lambda *args, **kwargs: SimpleNamespace(),
            account_snapshot=AsyncMock(return_value=SNAPSHOT),
            spread_bps=AsyncMock(return_value=1),
            evaluate_entry_gates=lambda **kwargs: {"passed": True, "gates": []},
            build_live_spec=AsyncMock(return_value=spec_for(symbol)),
            set_live_isolated_margin=AsyncMock(return_value="ISOLATED"),
            apply_live_verified_leverage=AsyncMock(return_value={"applied_leverage": 1, "margin_type": "isolated"}),
            fresh_auto_submission_credentials=credential_refresh or AsyncMock(return_value=CREDENTIALS),
            submit_entry=submit,
            install_protection=AsyncMock(),
        ):
            return asyncio.run(execution.execute_live_order(
                application, order_for(symbol), source=source,
                allowed_symbols=override, credentials=CREDENTIALS,
            ))

    def cycle(self, application, candidates, *, scan_effect=None, execution_effect=None, decision=None):
        scan = AsyncMock(side_effect=scan_effect) if scan_effect else AsyncMock(return_value=candidates)
        candles = AsyncMock(return_value=([{"close": 100}] * 220, 123))
        submit = AsyncMock(side_effect=execution_effect) if execution_effect else AsyncMock()
        with patch.multiple(
            execution,
            client_for_with_credentials=lambda *args, **kwargs: SimpleNamespace(last_scan_eligible_count=len(candidates)),
            readiness_for=lambda *args, **kwargs: {"ready": True},
            consent_status=lambda *args, **kwargs: {"grace_active": False},
            account_snapshot=AsyncMock(return_value=SNAPSHOT),
            scan_market_candidates=scan,
            live_candles=candles,
            canonical_live_decision=AsyncMock(return_value=decision or {"decision": "WAIT", "analysis": {"direction": "WAIT"}}),
            spread_bps=AsyncMock(return_value=1),
            execute_live_order=submit,
        ):
            asyncio.run(execution.automatic_cycle(application, credentials=CREDENTIALS))
        return scan, candles, submit

    def test_scan_excludes_unlisted_symbol_even_when_its_volume_is_higher(self):
        async def public_get(path, params=None):
            if path == "/fapi/v1/exchangeInfo":
                return {"symbols": [
                    {"symbol": symbol, "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"}
                    for symbol in ["BTCUSDT", "ETHUSDT"]
                ]}
            if path == "/fapi/v1/ticker/24hr":
                return [
                    {"symbol": "BTCUSDT", "quoteVolume": "2000000", "priceChangePercent": "1", "lastPrice": "100"},
                    {"symbol": "ETHUSDT", "quoteVolume": "90000000", "priceChangePercent": "10", "lastPrice": "100"},
                ]
            raise AssertionError(f"Unexpected mock public request: {path}")

        client = SimpleNamespace(public_get=AsyncMock(side_effect=public_get))
        candidates = asyncio.run(execution.scan_market_candidates(client, SNAPSHOT, ["BTCUSDT"]))
        self.assertEqual([item["symbol"] for item in candidates], ["BTCUSDT"])
        self.assertEqual(client.last_scan_eligible_count, 1)

    def test_empty_scan_scope_makes_no_public_requests(self):
        for scope in [[], None]:
            with self.subTest(scope=scope):
                client = SimpleNamespace(public_get=AsyncMock())
                self.assertEqual(asyncio.run(execution.scan_market_candidates(client, SNAPSHOT, scope)), [])
                client.public_get.assert_not_awaited()

    def test_cycle_filters_candidates_even_when_scanner_returns_foreign_symbol(self):
        application, state = application_for(["BTCUSDT"])
        scan, candles, submit = self.cycle(application, [
            {"symbol": "BTCUSDT", "opportunity_score": 1},
            {"symbol": "ETHUSDT", "opportunity_score": 99},
        ])
        self.assertEqual([call.args[1] for call in candles.await_args_list], ["BTCUSDT"])
        self.assertEqual(scan.await_args.kwargs["allowed_symbols"], ["BTCUSDT"])
        self.assertEqual(state["auto"]["last_scan_stats"]["candidate_symbols"], ["BTCUSDT"])
        submit.assert_not_awaited()

    def test_empty_cycle_scope_is_logged_and_exposed_without_scanning(self):
        application, state = application_for([])
        with patch.object(execution, "automation_telemetry") as telemetry:
            scan, candles, submit = self.cycle(application, [])
        scan.assert_not_awaited()
        candles.assert_not_awaited()
        submit.assert_not_awaited()
        self.assertEqual(state["auto"]["last_skip_reason"], "allowed_symbols_empty")
        self.assertIn("parite", state["auto"]["last_decision"])
        self.assertTrue(any(item["kind"] == "LIVE_AUTO_SYMBOL_BLOCKED" for item in state["events"]))
        telemetry.assert_called()

    def test_policy_cleared_during_scan_stops_before_deep_analysis(self):
        application, state = application_for(["BTCUSDT"])
        candidates = [{"symbol": "BTCUSDT", "opportunity_score": 99}]

        async def clear_scope(*args, **kwargs):
            state["policy"]["allowed_symbols"] = []
            return candidates

        _scan, candles, submit = self.cycle(application, candidates, scan_effect=clear_scope)
        candles.assert_not_awaited()
        submit.assert_not_awaited()
        self.assertEqual(state["auto"]["last_skip_reason"], "allowed_symbols_empty")
        self.assertFalse(state["auto"]["busy"])

    def test_late_execution_scope_rejection_keeps_specific_cycle_reason(self):
        decision = {
            "decision": "BUY", "entry_eligible": True,
            "analysis": {
                "direction": "LONG", "confidence": 95, "radar": {"trap_score": 1},
                "entry": 100, "stop_loss": 99.5, "tp1": 102, "tp2": 104, "tp3": 106,
            },
        }
        for scope, reason in [([], "allowed_symbols_empty"), (["ETHUSDT"], "symbol_not_allowed")]:
            with self.subTest(scope=scope):
                application, state = application_for(["BTCUSDT"])

                async def reject_entry(*args, **kwargs):
                    state["policy"]["allowed_symbols"] = scope
                    try:
                        execution.ensure_live_auto_symbol_allowed(state, "BTCUSDT")
                    except execution.LiveExchangeError as exc:
                        raise execution.safe_exchange_error(exc) from exc
                    raise AssertionError("Revoked symbol was not rejected")

                _scan, _candles, submit = self.cycle(
                    application, [{"symbol": "BTCUSDT", "opportunity_score": 99}],
                    execution_effect=reject_entry, decision=decision,
                )
                submit.assert_awaited_once()
                self.assertEqual(state["auto"]["last_skip_reason"], reason)
                self.assertEqual(state["auto"]["last_error"], state["auto"]["last_decision"])
                self.assertFalse(any(item["kind"] == "AUTO_ERROR" for item in state["events"]))

    def test_execute_cannot_bypass_saved_whitelist_with_call_scope(self):
        application, state = application_for(["BTCUSDT"])
        submit = AsyncMock(return_value={"orderId": 42, "status": "FILLED"})
        with self.assertRaises(execution.HTTPException) as rejected:
            self.execute(application, "ETHUSDT", override=["ETHUSDT"], submit=submit)
        self.assertEqual(rejected.exception.status_code, 422)
        submit.assert_not_awaited()
        self.assertEqual(state["plans"], {})
        self.assertEqual(state["auto"]["last_skip_reason"], "symbol_not_allowed")

    def test_empty_registered_scope_cannot_be_overridden_at_execution(self):
        application, state = application_for([])
        submit = AsyncMock(return_value={"orderId": 42, "status": "FILLED"})
        with self.assertRaises(execution.HTTPException) as rejected:
            self.execute(application, "BTCUSDT", override=["BTCUSDT"], submit=submit)
        self.assertEqual(rejected.exception.status_code, 423)
        submit.assert_not_awaited()
        self.assertEqual(state["plans"], {})
        self.assertEqual(state["auto"]["last_skip_reason"], "allowed_symbols_empty")

    def test_allowed_auto_symbol_succeeds_and_manual_scope_is_unchanged(self):
        application, _state = application_for(["BTCUSDT"])
        self.assertTrue(self.execute(application, "BTCUSDT")["ok"])
        for scope in [["BTCUSDT"], []]:
            with self.subTest(scope=scope):
                application, _state = application_for(scope)
                self.assertTrue(self.execute(application, "ETHUSDT", source="MANUAL")["ok"])

    def test_latest_policy_is_rechecked_after_credential_refresh_before_submit(self):
        for updated_scope, status_code in [([], 423), (["ETHUSDT"], 422)]:
            with self.subTest(updated_scope=updated_scope):
                application, state = application_for(["BTCUSDT"])
                submit = AsyncMock()

                async def refresh(*args):
                    state["policy"]["allowed_symbols"] = updated_scope
                    return CREDENTIALS

                with self.assertRaises(execution.HTTPException) as rejected:
                    self.execute(
                        application, "BTCUSDT", submit=submit,
                        credential_refresh=AsyncMock(side_effect=refresh),
                    )
                self.assertEqual(rejected.exception.status_code, status_code)
                submit.assert_not_awaited()

    def test_policy_change_during_order_lookup_cannot_reach_entry_post(self):
        for updated_scope in [[], ["ETHUSDT"]]:
            with self.subTest(updated_scope=updated_scope):
                _application, state = application_for(["BTCUSDT"])
                client = ScopeChangingClient(state, updated_scope)
                with self.assertRaises(execution.LiveExchangeError):
                    asyncio.run(execution.submit_entry(
                        client, spec_for("BTCUSDT"), "offline-entry",
                        test_only=False,
                        before_submit=lambda: execution.ensure_live_auto_symbol_allowed(state, "BTCUSDT"),
                    ))
                self.assertEqual([call[0] for call in client.calls], ["GET"])

    def test_submit_boundary_allows_current_whitelist_and_preserves_manual_default(self):
        _application, state = application_for(["BTCUSDT"])
        client = ScopeChangingClient(state, ["BTCUSDT"])
        result = asyncio.run(execution.submit_entry(
            client, spec_for("BTCUSDT"), "offline-entry", test_only=False,
            before_submit=lambda: execution.ensure_live_auto_symbol_allowed(state, "BTCUSDT"),
        ))
        self.assertEqual(result["orderId"], 42)
        self.assertEqual([call[0] for call in client.calls], ["GET", "POST"])
        client = ScopeChangingClient(state, [])
        result = asyncio.run(execution.submit_entry(client, spec_for("ETHUSDT"), "offline-manual", test_only=False))
        self.assertEqual(result["orderId"], 42)
        self.assertEqual([call[0] for call in client.calls], ["GET", "POST"])

    def test_saved_and_restored_empty_live_policy_is_not_replaced_by_defaults(self):
        application, state = application_for(["BTCUSDT"])
        request = SimpleNamespace(app=application)
        with patch.object(execution, "execution_owner", return_value={"id": "offline-owner"}) as owner, \
                patch.object(execution, "public_status", side_effect=lambda app, req: {"policy": app.state.v25_execution["policy"]}):
            result = asyncio.run(execution.v25_policy(request, execution.PolicyUpdate(allowed_symbols=[])))
        owner.assert_called_once_with(request)
        self.assertEqual(result["policy"]["allowed_symbols"], [])
        self.assertEqual(state["policy"]["allowed_symbols"], [])
        restored = execution.sanitized_state({"policy": state["policy"]})
        self.assertEqual(restored["policy"]["allowed_symbols"], [])

    def test_empty_scope_reason_reaches_public_automation_status(self):
        application, state = application_for([])
        self.cycle(application, [])
        with patch.object(execution, "readiness", return_value={"ready": True}), \
                patch.object(execution, "consent_status", return_value={"fingerprint": None}):
            status = execution.public_status(application)
        self.assertEqual(status["auto"]["last_decision"], state["auto"]["last_decision"])
        self.assertEqual(status["scanner"]["last_skip_reason"], "allowed_symbols_empty")
        self.assertEqual(status["policy"]["allowed_symbols"], [])

    def test_empty_scope_preservation_is_opt_in_and_normalization_still_applies(self):
        from app.execution_core import DEFAULT_EXECUTION_POLICY, sanitize_execution_policy

        self.assertEqual(
            sanitize_execution_policy({"allowed_symbols": []})["allowed_symbols"],
            DEFAULT_EXECUTION_POLICY["allowed_symbols"],
        )
        self.assertEqual(
            sanitize_execution_policy({"allowed_symbols": []}, preserve_empty_allowed_symbols=True)["allowed_symbols"], [],
        )
        self.assertEqual(
            execution.live_auto_symbol_scope({"policy": {"allowed_symbols": ["btc/usdt", "BAD-USD", "BTCUSDT"]}}),
            ["BTCUSDT"],
        )
        for scope in [None, [], ["BAD-USD"]]:
            with self.subTest(scope=scope):
                _application, state = application_for(scope)
                with self.assertRaises(execution.LiveExchangeError):
                    execution.ensure_live_auto_symbol_allowed(state, "BTCUSDT")
                self.assertEqual(state["auto"]["last_skip_reason"], "allowed_symbols_empty")


if __name__ == "__main__":
    unittest.main()
