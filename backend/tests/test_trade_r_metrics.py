import asyncio
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch


sys.path.insert(0, str(Path(__file__).parents[1]))

from app import binance_demo, v21_demo, v25_execution as live  # noqa: E402
from app.trade_r_metrics import calculate_r_multiple, initial_entry_risk_usdt  # noqa: E402
import test_v25_auto_symbol_scope as scope_tests  # noqa: E402
from test_v25_partial_fills import PartialFillClient, make_plan  # noqa: E402


class OfflineLiveClient(live.BinanceLiveClient):
    def __init__(self, trades: list[dict[str, Any]] | None = None) -> None:
        self.trades = trades or []
        self.calls: list[tuple[str, str]] = []

    async def signed(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, path))
        if method == "GET" and path == "/fapi/v1/userTrades":
            return self.trades
        if method == "GET" and path in {"/fapi/v1/allOrders", "/fapi/v1/allAlgoOrders"}:
            return []
        raise AssertionError(f"Unexpected mock request: {method} {path}")


def r_row(identity: str, pnl: float, risk: float | None, *, created_at: str | None = None) -> dict[str, Any]:
    return {
        "id": identity, "kind": "LIVE_POSITION_CLOSED",
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "verified_realized": True, "realized_pnl": pnl, "initial_risk_usdt": risk,
        "r_multiple": calculate_r_multiple(pnl, risk),
    }


class TradeRMetricTests(unittest.TestCase):
    def setUp(self):
        persistence = patch.object(live, "persist_state")
        persistence.start()
        self.addCleanup(persistence.stop)

    def test_verified_live_close_records_net_r_and_keeps_initial_risk(self):
        state = live.initial_state()
        plan = {
            "id": "r-plan", "symbol": "BTCUSDT", "direction": "LONG",
            "initial_risk_usdt": 10.0, "quantity": "0.005", "stop_loss": "50000",
        }
        result = {"realized_pnl": 18.0, "gross_realized_pnl": 20.0, "commission_usdt": 2.0}
        with patch.object(live, "verified_plan_pnl", new=AsyncMock(return_value=result)), \
                patch.object(live, "write_trade_review", return_value=None):
            asyncio.run(live.settle_closed_plan(OfflineLiveClient(), state, plan))
        self.assertEqual(plan["initial_risk_usdt"], 10)
        self.assertEqual(plan.get("r_multiple"), 1.8)
        self.assertEqual(state["events"][0].get("r_multiple"), 1.8)

    def test_empty_performance_returns_null_r_metrics(self):
        result = v21_demo.performance_payload({"journal": []})
        self.assertIn("avg_r", result)
        self.assertIsNone(result["avg_r"])
        self.assertIsNone(result["expectancy_r"])

    def test_legacy_live_plan_without_initial_risk_has_null_r(self):
        plan = {
            "id": "old-plan", "symbol": "BTCUSDT", "status": "KAPANDI",
            "pnl_verified": True, "realized_pnl": 20,
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "entry_price": 100, "stop_loss": 90, "quantity": 1,
        }
        rows = live.verified_live_journal({"plans": {plan["id"]: plan}})
        self.assertIn("r_multiple", rows[0])
        self.assertIsNone(rows[0]["r_multiple"])
        self.assertIsNone(v21_demo.performance_payload({"journal": rows})["avg_r"])

    def test_initial_risk_uses_planned_rounded_quantity_and_stop_distance(self):
        for entry, stop, quantity, expected in [
            ("50000", "49000", "0.010", 10),
            ("50000", "51000", "0.010", 10),
            ("100.01", "99.99", "0.125", 0.0025),
        ]:
            with self.subTest(entry=entry, stop=stop, quantity=quantity):
                self.assertEqual(initial_entry_risk_usdt({
                    "entry_price": entry, "stop_loss": stop, "quantity": quantity,
                }), expected)

    def test_missing_invalid_or_zero_risk_produces_null_not_a_fallback(self):
        for risk in [None, 0, -1, True, "invalid", float("nan"), float("inf"), "1e-500"]:
            with self.subTest(risk=risk):
                self.assertIsNone(calculate_r_multiple(10, risk))
        for pnl in [None, True, "invalid", float("nan"), float("inf")]:
            with self.subTest(pnl=pnl):
                self.assertIsNone(calculate_r_multiple(pnl, 10))
        self.assertIsNone(calculate_r_multiple(1e308, 1e-308))
        self.assertEqual(calculate_r_multiple(0, 10), 0)
        self.assertEqual(calculate_r_multiple(-5, 10), -0.5)

    def test_entry_plan_and_durable_intent_capture_same_risk(self):
        application, state = scope_tests.application_for(["BTCUSDT"])
        result = scope_tests.AutoSymbolScopeTests().execute(application, "BTCUSDT", source="MANUAL")
        self.assertEqual(result["plan"]["initial_risk_usdt"], 0.1)
        self.assertIsNone(result["plan"]["r_multiple"])
        intent_id, intent = next(iter(state["intents"].items()))
        self.assertEqual(intent["spec"]["initial_risk_usdt"], 0.1)
        recovered = live.recover_plan_from_intent(intent_id, intent, {"orderId": 42, "status": "PARTIALLY_FILLED"})
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["initial_risk_usdt"], 0.1)
        restored = live.sanitized_state({"plans": {recovered["id"]: recovered}, "intents": state["intents"]})
        self.assertEqual(restored["plans"][recovered["id"]]["initial_risk_usdt"], 0.1)

    def test_legacy_intent_does_not_infer_initial_risk_from_current_levels(self):
        application, state = scope_tests.application_for(["BTCUSDT"])
        scope_tests.AutoSymbolScopeTests().execute(application, "BTCUSDT", source="MANUAL")
        intent_id, intent = next(iter(state["intents"].items()))
        del intent["spec"]["initial_risk_usdt"]
        recovered = live.recover_plan_from_intent(intent_id, intent, {"orderId": 42, "status": "FILLED"})
        self.assertIsNone(recovered["initial_risk_usdt"])
        self.assertIsNone(recovered["r_multiple"])

    def test_partial_fill_and_stop_move_do_not_rewrite_initial_risk(self):
        plan = make_plan(initial_risk_usdt=10.0, r_multiple=None)
        client = PartialFillClient(plan, quantity="0.005")
        state = live.initial_state()
        state["plans"] = {plan["id"]: plan}
        with patch.object(live, "STOP_RETRY_BASE_SECONDS", 0):
            asyncio.run(live.install_protection(client, state, plan))
        self.assertEqual(plan["quantity"], "0.005")
        self.assertEqual(plan["initial_risk_usdt"], 10)
        plan["stop_loss"] = "50000"
        with patch.object(live, "verified_plan_pnl", new=AsyncMock(return_value={"realized_pnl": 2})), \
                patch.object(live, "write_trade_review", return_value=None):
            asyncio.run(live.settle_closed_plan(client, state, plan))
        self.assertEqual(plan["initial_risk_usdt"], 10)
        self.assertEqual(plan["r_multiple"], 0.2)

    def test_verified_net_pnl_includes_existing_entry_and_exit_usdt_commission(self):
        client = OfflineLiveClient([
            {"orderId": 1, "side": "BUY", "qty": "1", "price": "100", "time": 1000,
             "realizedPnl": "0", "commission": "1", "commissionAsset": "USDT"},
            {"orderId": 2, "side": "SELL", "qty": "1", "price": "120", "time": 2000,
             "realizedPnl": "20", "commission": "1", "commissionAsset": "USDT"},
        ])
        plan = {
            "id": "net-plan", "symbol": "BTCUSDT", "direction": "LONG",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "entry_order_id": 1, "exchange_order_ids": [1, 2], "initial_risk_usdt": 10,
        }
        state = live.initial_state()
        with patch.object(live, "write_trade_review", return_value=None):
            asyncio.run(live.settle_closed_plan(client, state, plan))
        self.assertEqual(plan["gross_realized_pnl"], 20)
        self.assertEqual(plan["commission_usdt"], 2)
        self.assertEqual(plan["realized_pnl"], 18)
        self.assertEqual(plan["r_multiple"], 1.8)
        self.assertTrue(all(method == "GET" for method, _ in client.calls))

    def test_unverified_close_has_no_r_and_retains_existing_entry_lock(self):
        plan = {"id": "unverified", "symbol": "BTCUSDT", "initial_risk_usdt": 10, "r_multiple": 99}
        state = live.initial_state()
        with patch.object(live, "verified_plan_pnl", new=AsyncMock(side_effect=live.LiveExchangeError("Mock missing close proof"))):
            asyncio.run(live.settle_closed_plan(OfflineLiveClient(), state, plan))
        self.assertFalse(plan["pnl_verified"])
        self.assertIsNone(plan["r_multiple"])
        self.assertEqual(plan["initial_risk_usdt"], 10)
        self.assertFalse(state["auto"]["enabled"])
        self.assertEqual(state["armed_until"], 0)
        self.assertEqual(live.verified_live_journal({"plans": {plan["id"]: plan}}), [])

    def test_repeated_verified_settlement_is_idempotent_and_restores_r(self):
        plan = {"id": "repeat", "symbol": "BTCUSDT", "initial_risk_usdt": 10}
        state = live.initial_state()
        pnl = AsyncMock(return_value={"realized_pnl": -5})
        with patch.object(live, "verified_plan_pnl", new=pnl), \
                patch.object(live, "write_trade_review", return_value=None):
            asyncio.run(live.settle_closed_plan(OfflineLiveClient(), state, plan))
            plan.update({"stop_loss": 0, "quantity": 0, "r_multiple": None})
            asyncio.run(live.settle_closed_plan(OfflineLiveClient(), state, plan))
        self.assertEqual(pnl.await_count, 1)
        self.assertEqual(plan["initial_risk_usdt"], 10)
        self.assertEqual(plan["r_multiple"], -0.5)
        self.assertEqual(sum(event["kind"] == "LIVE_POSITION_CLOSED" for event in state["events"]), 1)

    def test_legacy_settlement_records_explicit_null_without_backfilling_risk(self):
        plan = {"id": "legacy", "symbol": "BTCUSDT", "entry_price": 100, "stop_loss": 90, "quantity": 1}
        state = live.initial_state()
        with patch.object(live, "verified_plan_pnl", new=AsyncMock(return_value={"realized_pnl": 20})), \
                patch.object(live, "write_trade_review", return_value=None):
            asyncio.run(live.settle_closed_plan(OfflineLiveClient(), state, plan))
        self.assertNotIn("initial_risk_usdt", plan)
        self.assertIsNone(plan["r_multiple"])
        self.assertIsNone(state["events"][0]["initial_risk_usdt"])
        self.assertIsNone(state["events"][0]["r_multiple"])

    def test_r_average_and_expectancy_use_only_known_r_with_breakevens(self):
        rows = [r_row("win", 20, 10), r_row("loss", -5, 5), r_row("flat", 0, 10), r_row("legacy", 100, None)]
        result = v21_demo.performance_payload({"journal": rows})
        self.assertEqual(result["total_trades"], 4)
        self.assertEqual(result["net_profit"], 115)
        self.assertAlmostEqual(result["avg_r"], 1 / 3, places=6)
        self.assertAlmostEqual(result["expectancy_r"], 1 / 3, places=6)

    def test_r_statistics_reuse_existing_period_filter_and_deduplication(self):
        old = r_row("old", 90, 10, created_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat())
        win, loss = r_row("win", 20, 10), r_row("loss", -10, 10)
        result = v21_demo.performance_payload({"journal": [old, win, dict(win), loss]}, "daily")
        self.assertEqual(result["total_trades"], 2)
        self.assertEqual(result["avg_r"], 0.5)
        self.assertEqual(result["expectancy_r"], 0.5)

    def test_invalid_or_missing_risk_never_enters_r_statistics_even_with_cached_r(self):
        for risk in [None, 0, -1, True, float("nan"), float("inf")]:
            with self.subTest(risk=risk):
                row = r_row("bad", 20, risk)
                row["r_multiple"] = 99
                result = v21_demo.performance_payload({"journal": [row]})
                self.assertIsNone(result["avg_r"])
                self.assertIsNone(result["expectancy_r"])

    def test_unverified_rows_and_fill_level_r_do_not_enter_r_statistics(self):
        unverified = {**r_row("unknown", 999, 1), "verified_realized": False}
        fill = {**r_row("fill", 5, 1), "kind": "FILL"}
        result = v21_demo.performance_payload({"journal": [unverified, fill]})
        self.assertEqual(result["total_trades"], 1)
        self.assertIsNone(result["avg_r"])
        self.assertIsNone(result["expectancy_r"])

    def test_demo_fill_pnl_is_not_relabelled_as_net_r_and_does_not_mix_with_live(self):
        demo = v21_demo.initial_state()
        v21_demo.process_stream_event(demo, {
            "e": "ORDER_TRADE_UPDATE", "T": 123,
            "o": {"s": "BTCUSDT", "X": "FILLED", "x": "TRADE", "i": 7, "S": "SELL", "R": True, "rp": "20", "ap": "100"},
        })
        live_plan = {
            "id": "live-only", "symbol": "BTCUSDT", "status": "KAPANDI", "pnl_verified": True,
            "initial_risk_usdt": 10, "realized_pnl": -5, "closed_at": datetime.now(timezone.utc).isoformat(),
        }
        demo["plans"] = {"not-demo": live_plan}
        demo_result = v21_demo.performance_payload(demo)
        live_result = v21_demo.performance_payload({"journal": live.verified_live_journal({"plans": {"live-only": live_plan}})})
        self.assertEqual(demo_result["net_profit"], 20)
        self.assertIsNone(demo_result["avg_r"])
        self.assertIsNone(demo_result["expectancy_r"])
        self.assertEqual(live_result["net_profit"], -5)
        self.assertEqual(live_result["avg_r"], -0.5)

    def test_new_demo_entry_records_risk_before_submission_without_changing_accounting(self):
        application = SimpleNamespace(state=SimpleNamespace(
            http=object(), v21_demo={}, maintenance={"mode": "NORMAL"},
        ))
        demo = {"lock": asyncio.Lock(), "_user_id": "demo-user", "plans": {}, "events": [], "armed_until": time.time() + 300}
        v21 = {"_user_id": "demo-user", "settings": dict(v21_demo.DEFAULT_SETTINGS), "paper_positions": []}
        body = binance_demo.DemoOrderRequest(
            symbol="BTCUSDT", direction="LONG", order_type="LIMIT", limit_price=100,
            margin_usdt=10, leverage=1, stop_loss=98, tp1=102, tp2=104, tp3=106,
        )
        spec = {
            "symbol": "BTCUSDT", "direction": "LONG", "order_type": "LIMIT", "side": "BUY",
            "quantity": "0.100", "quantity_decimal": Decimal("0.100"),
            "entry_price": "100", "current_price": 100, "stop_loss": "98",
            "targets": ["102", "104", "106"], "leverage": 1, "margin_usdt": 10,
            "notional_usdt": 10, "step": Decimal("0.001"), "min_qty": Decimal("0.001"),
            "min_notional": 5,
        }
        snapshot = {"positions": [], "open_orders": [], "open_algo_orders": [],
                    "open_algo_orders_available": True, "algo_orders_quality": "VALID_EMPTY", "available_balance": 100}
        recorded_risk = []

        async def submit(*args, **kwargs):
            recorded_risk.append(next(iter(demo["plans"].values()))["initial_risk_usdt"])
            return {"orderId": 7, "clientOrderId": kwargs["client_id"], "status": "NEW"}

        with patch.multiple(
            binance_demo,
            client_for_state=lambda *args: SimpleNamespace(trace_request_id=None),
            ensure_one_way_position_mode=AsyncMock(return_value=0),
            account_snapshot=AsyncMock(return_value=snapshot),
            resolve_demo_symbol=AsyncMock(return_value="BTCUSDT"),
            build_order_spec=AsyncMock(return_value=spec),
            set_isolated_margin=AsyncMock(return_value={}),
            apply_verified_leverage=AsyncMock(return_value={
                "requested_leverage": 1, "applied_leverage": 1, "margin_type": "ISOLATED",
                "leverage_verified": True, "configuration_source": "TEST", "max_notional_value": 200,
            }),
            submit_entry=AsyncMock(side_effect=submit),
            persist_runtime=lambda *args: None,
            persist_runtime_and_wait=AsyncMock(),
        ):
            result = asyncio.run(binance_demo.execute_demo_order(
                application, body, source="MANUAL", demo_state=demo, v21_state=v21,
            ))
        self.assertEqual(recorded_risk, [0.2])
        self.assertEqual(result["plan"]["initial_risk_usdt"], 0.2)
        self.assertIsNone(result["plan"]["r_multiple"])
        self.assertEqual(result["risk_per_trade"], 0.2)
        self.assertEqual(result["plan"]["quantity"], "0.100")


if __name__ == "__main__":
    unittest.main()
