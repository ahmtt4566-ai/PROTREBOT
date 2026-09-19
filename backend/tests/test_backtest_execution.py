import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest_execution import ExecutionConfig, HistoricalExecutionSimulator, run_realistic_backtest


class BacktestExecutionTests(unittest.TestCase):
    def decision(self, *, side="BUY", risk=100.0):
        return {
            "decision": side,
            "symbol": "BTCUSDT",
            "signal_timestamp": 900,
            "analysis": {"stop_loss": 99.0 if side == "BUY" else 101.0, "tp1": 103.0 if side == "BUY" else 97.0},
            "risk": {"notional_usdt": risk},
            "entry_eligible": True,
        }

    def test_market_fill_is_adverse_and_trade_ledger_is_deterministic(self):
        simulator = HistoricalExecutionSimulator(ExecutionConfig(spread_bps=10, slippage_bps=10, funding_bps_per_8h=0))
        self.assertTrue(simulator.open_from_decision(self.decision(), {"time": 900}, {"time": 1800, "open": 100}))
        trade = simulator.close_on_candle({"time": 2700, "open": 102, "high": 104, "low": 101, "close": 103})
        self.assertIsNotNone(trade)
        self.assertEqual(trade.exit_reason, "TAKE_PROFIT")
        self.assertGreater(trade.entry_fill_price, trade.entry_requested_price)
        self.assertLess(trade.exit_fill_price, trade.exit_requested_price)
        self.assertEqual(trade.trade_id, "BT-00000001")

    def test_long_uses_actual_fills_for_gross_and_actual_notional_for_fees(self):
        simulator = HistoricalExecutionSimulator(ExecutionConfig(spread_bps=10, slippage_bps=10, taker_fee_bps=5, funding_bps_per_8h=0))
        decision = self.decision()
        decision["analysis"]["tp1"] = 110.0
        simulator.open_from_decision(decision, {"time": 900}, {"time": 1800, "open": 100})
        trade = simulator.close_on_candle({"time": 2700, "open": 102, "high": 111, "low": 101, "close": 103})
        self.assertEqual(trade.entry_fill_price, Decimal("100.15"))
        self.assertEqual(trade.exit_fill_price, Decimal("109.83"))
        self.assertEqual(trade.gross_pnl, Decimal("9.68"))
        self.assertEqual(trade.fees, (Decimal("100.15") + Decimal("109.83")) * Decimal("0.0005"))
        self.assertEqual(trade.net_pnl, trade.gross_pnl - trade.fees - trade.funding_cost)

    def test_short_uses_actual_fills_for_gross_and_adverse_exit(self):
        simulator = HistoricalExecutionSimulator(ExecutionConfig(spread_bps=10, slippage_bps=10, taker_fee_bps=5, funding_bps_per_8h=0))
        decision = self.decision(side="SELL")
        decision["analysis"]["tp1"] = 100.0
        simulator.open_from_decision(decision, {"time": 900}, {"time": 1800, "open": 110})
        trade = simulator.close_on_candle({"time": 2700, "open": 102, "high": 100, "low": 96, "close": 99})
        self.assertEqual(trade.entry_fill_price, Decimal("109.83"))
        self.assertEqual(trade.exit_fill_price, Decimal("100.15"))
        self.assertEqual(trade.gross_pnl, Decimal("9.68") * Decimal("0.909"))
        self.assertEqual(trade.net_pnl, trade.gross_pnl - trade.fees - trade.funding_cost)

    def test_spread_and_slippage_are_attribution_only_not_double_counted(self):
        simulator = HistoricalExecutionSimulator(ExecutionConfig(spread_bps=10, slippage_bps=10, taker_fee_bps=0, funding_bps_per_8h=0))
        decision = self.decision()
        decision["analysis"]["tp1"] = 110.0
        simulator.open_from_decision(decision, {"time": 900}, {"time": 1800, "open": 100})
        trade = simulator.close_on_candle({"time": 2700, "open": 102, "high": 111, "low": 101, "close": 103})
        self.assertAlmostEqual(trade.net_pnl, trade.gross_pnl, places=8)
        self.assertGreater(trade.spread_cost, 0)
        self.assertGreater(trade.slippage_cost, 0)

    def test_higher_costs_cannot_improve_net_pnl(self):
        def run(fee, spread, slippage):
            sim = HistoricalExecutionSimulator(ExecutionConfig(taker_fee_bps=fee, spread_bps=spread, slippage_bps=slippage, funding_bps_per_8h=0))
            sim.open_from_decision(self.decision(), {"time": 900}, {"time": 1800, "open": 100})
            sim.close_on_candle({"time": 2700, "open": 102, "high": 104, "low": 101, "close": 103})
            return sim.result()["net_pnl"]
        self.assertGreaterEqual(run(5, 10, 10), run(10, 20, 20))

    def test_minimum_order_rejects_without_increasing_quantity(self):
        sim = HistoricalExecutionSimulator(ExecutionConfig(min_quantity=1, min_notional=1000, funding_bps_per_8h=0))
        self.assertFalse(sim.open_from_decision(self.decision(risk=10), {"time": 900}, {"time": 1800, "open": 100}))
        self.assertEqual(sim.rejections[0]["reason"], "MINIMUM_ORDER_SIZE")
        self.assertIsNone(sim.position)

    def test_same_candle_stop_and_target_uses_stop_first(self):
        sim = HistoricalExecutionSimulator(ExecutionConfig(funding_bps_per_8h=0))
        sim.open_from_decision(self.decision(), {"time": 900}, {"time": 1800, "open": 100})
        trade = sim.close_on_candle({"time": 2700, "open": 100, "high": 104, "low": 98, "close": 101})
        self.assertEqual(trade.exit_reason, "STOP_FIRST")

    def test_funding_is_chronological_and_reported(self):
        sim = HistoricalExecutionSimulator(ExecutionConfig(funding_bps_per_8h=10, spread_bps=0, slippage_bps=0))
        sim.open_from_decision(self.decision(), {"time": 900}, {"time": 1800, "open": 100})
        trade = sim.close_on_candle({"time": 1800 + 8 * 60 * 60, "open": 100, "high": 100, "low": 98, "close": 99})
        self.assertEqual(trade.exit_reason, "STOP")
        self.assertGreater(trade.funding_cost, 0)
        self.assertEqual(Decimal(str(sim.result()["total_funding_cost"])), trade.funding_cost)

    def test_equity_curve_and_net_pnl_reconcile(self):
        sim = HistoricalExecutionSimulator(ExecutionConfig(spread_bps=0, slippage_bps=0, funding_bps_per_8h=0))
        sim.open_from_decision(self.decision(), {"time": 900}, {"time": 1800, "open": 100})
        trade = sim.close_on_candle({"time": 2700, "open": 102, "high": 104, "low": 101, "close": 103})
        result = sim.result()
        self.assertAlmostEqual(result["ending_equity"], result["starting_equity"] + float(trade.net_pnl), places=8)
        timestamps = [point["timestamp"] for point in result["equity_curve"]]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_adapter_calls_canonical_decision_and_never_calls_exchange(self):
        candles = [{"time": index * 900, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000} for index in range(230)]
        wait = {"decision": "WAIT", "symbol": "BTCUSDT", "signal_timestamp": 0, "entry_eligible": False}
        from app import main as app_main
        with patch.object(app_main, "canonical_historical_decision", return_value=wait) as decision:
            result = run_realistic_backtest({"15m": candles}, "BTCUSDT", config=ExecutionConfig(funding_bps_per_8h=0))
        self.assertGreater(decision.call_count, 0)
        self.assertEqual(result["number_of_trades"], 0)
        self.assertTrue(result["data_quality"]["valid"])

    def test_long_mfe_mae_reflect_running_high_low_water_marks(self):
        sim = HistoricalExecutionSimulator(ExecutionConfig(spread_bps=0, slippage_bps=0, funding_bps_per_8h=0))
        sim.open_from_decision(self.decision(), {"time": 900}, {"time": 1800, "open": 100})
        # Intermediate candle: stays inside stop(99)/tp(103), only extends the water marks.
        self.assertIsNone(sim.close_on_candle({"time": 2700, "open": 100, "high": 102, "low": 99.5, "close": 101}))
        trade = sim.close_on_candle({"time": 3600, "open": 102, "high": 106, "low": 101, "close": 104})
        self.assertEqual(trade.exit_reason, "TAKE_PROFIT")
        self.assertEqual(trade.mfe_price, Decimal("6"))
        self.assertEqual(trade.mae_price, Decimal("0.5"))
        self.assertEqual(trade.mfe_r, Decimal("6"))
        self.assertEqual(trade.mae_r, Decimal("0.5"))

    def test_short_mfe_mae_reflect_running_high_low_water_marks(self):
        sim = HistoricalExecutionSimulator(ExecutionConfig(spread_bps=0, slippage_bps=0, funding_bps_per_8h=0))
        sim.open_from_decision(self.decision(side="SELL"), {"time": 900}, {"time": 1800, "open": 100})
        # Intermediate candle: stays inside stop(101)/tp(97), only extends the water marks.
        self.assertIsNone(sim.close_on_candle({"time": 2700, "open": 100, "high": 100.5, "low": 98.5, "close": 99}))
        trade = sim.close_on_candle({"time": 3600, "open": 98, "high": 99, "low": 96, "close": 97.5})
        self.assertEqual(trade.exit_reason, "TAKE_PROFIT")
        self.assertEqual(trade.mfe_price, Decimal("4"))
        self.assertEqual(trade.mae_price, Decimal("0.5"))
        self.assertEqual(trade.mfe_r, Decimal("4"))
        self.assertEqual(trade.mae_r, Decimal("0.5"))

    def test_stop_with_positive_pnl_forensic_flag_is_observational_only(self):
        self.assertEqual(HistoricalExecutionSimulator._exit_consistency_warning("STOP", Decimal("0.5")), "STOP_WITH_POSITIVE_PNL")
        self.assertIsNone(HistoricalExecutionSimulator._exit_consistency_warning("STOP", Decimal("-0.5")))
        self.assertIsNone(HistoricalExecutionSimulator._exit_consistency_warning("TAKE_PROFIT", Decimal("0.5")))


if __name__ == "__main__":
    unittest.main()
