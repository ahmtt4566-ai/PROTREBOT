import asyncio
import csv
import hashlib
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))

from app import analysis, execution_core as core, main, v25_execution as live  # noqa: E402
from app.backtest_baseline import Config, Engine, LocalSpecClient, Position, bootstrap, metrics, run  # noqa: E402
from app.backtest_data import Dataset, Series, gap_report, load_dataset, normalize_candles, timestamp  # noqa: E402
from backtest_download import BASE, fetch  # noqa: E402
from backtest_cli import cached_native_analysis, execute  # noqa: E402

T = 1743465600


def candle(at, *, opening=100, high=100.5, low=99.5, close=100):
    return {"time": at, "open": opening, "high": high, "low": low, "close": close,
            "volume": 100, "quote_volume": 20000000}


def metadata(symbols):
    return {"historical": False, "observed_at": "2026-10-06T09:00:00+00:00",
            "exchange_info": {"symbols": [
                {"symbol": symbol, "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT",
                 "filters": [{"filterType": "MARKET_LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "100000"},
                             {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                             {"filterType": "MIN_NOTIONAL", "notional": "5"}]} for symbol in symbols]},
            "brackets": {symbol: {"symbol": symbol, "brackets": [
                {"notionalFloor": 0, "notionalCap": 1000000, "maintMarginRatio": 0.004,
                 "cum": 0, "initialLeverage": 125}]} for symbol in symbols}}


def dataset(symbols=("BTCUSDT",), bars=16):
    frames, marks, funding, months = {}, {}, {}, {}
    for symbol in symbols:
        frames[symbol] = {}
        for interval, duration in (("15m", 900), ("1h", 3600), ("4h", 14400)):
            frames[symbol][interval] = Series(interval, [
                candle(T + step * duration, opening=99, low=98.5) if step < 0 else candle(T + step * duration)
                for step in range(-260, bars)])
        marks[symbol] = Series("15m", [candle(T + step * 900) for step in range(bars)])
        funding[symbol] = [{"time": T + step * 28800, "rate": 0, "interval_hours": 8}
                           for step in range(-1, 4)]
        months[symbol] = {"2025-03", "2025-04"}
    return Dataset(frames, marks, funding, metadata(symbols), {}, months)


def signal(stop=99):
    return {"direction": "LONG", "confidence": 95, "entry": 100, "stop_loss": stop,
            "tp1": 101, "tp2": 102, "tp3": 103, "atr": 2,
            "radar": {"trap_score": 1, "breakout_quality": 90}}


def spec():
    return {"symbol": "BTCUSDT", "direction": "LONG", "entry_price": "100", "quantity": "3",
            "stop_loss": "99", "targets": ["101", "102", "103"], "leverage": 30,
            "step": Decimal("0.01"), "min_qty": Decimal("0.01"), "min_notional": Decimal("5"),
            "margin_usdt": 10, "notional_usdt": 300}


def config(**kwargs):
    return Config(T, T + 1800, bootstrap_samples=50, conditional_current_metadata=True, **kwargs)


class DataAndCausalityTests(unittest.TestCase):
    def test_future_primary_and_higher_timeframes_do_not_change_decision(self):
        first, second = dataset(), dataset()
        for frame in second.frames["BTCUSDT"].values():
            for row in frame.rows:
                if row["time"] >= T:
                    row.update(high=10000, close=9999, volume=100000000)
        policy = core.sanitize_execution_policy({})
        self.assertEqual(first.canonical("BTCUSDT", T, policy), second.canonical("BTCUSDT", T, policy))

    def test_actual_canonical_calls_actual_analysis_with_only_closed_candles(self):
        data = dataset()
        with patch.object(main, "analyze", wraps=analysis.analyze) as native:
            data.canonical("BTCUSDT", T, core.sanitize_execution_policy({}))
        self.assertEqual(native.call_count, 5)
        for call in native.call_args_list:
            rows = call.args[0]
            interval = rows[-1]["time"] - rows[-2]["time"]
            self.assertTrue(all(row["time"] + interval <= T for row in rows))
            self.assertEqual(len(rows), 259)

    def test_cli_memo_adapter_is_exact_and_restores_native_binding(self):
        data = dataset()
        policy = core.sanitize_execution_policy({})
        frames = {name: series.closed(T) for name, series in data.frames["BTCUSDT"].items()}
        original = main.analyze
        baseline = main.canonical_historical_decision("BTCUSDT", frames, T, required_intervals=("15m", "1h", "4h"))
        with cached_native_analysis():
            actual = main.canonical_historical_decision("BTCUSDT", frames, T, required_intervals=("15m", "1h", "4h"))
            self.assertEqual(actual, baseline)
            self.assertEqual(data.canonical("BTCUSDT", T, policy)["decision"], baseline["decision"])
        self.assertIs(main.analyze, original)

    def test_missing_and_duplicate_candles_are_reported_not_filled(self):
        series, duplicates = normalize_candles([candle(T), candle(T), candle(T + 1800)], "15m")
        self.assertEqual(duplicates, 1)
        report = gap_report(series, T, T + 2700)
        self.assertEqual(report["missing_candles"], 1)
        self.assertEqual(report["gaps"][0]["start"], T + 900)
        self.assertIsNone(series.at(T + 900))

    def test_conflicting_duplicate_and_invalid_ohlc_are_explicit_errors(self):
        for rows in ([candle(T), candle(T, close=100.1)], [candle(T, high=90)]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                normalize_candles(rows, "15m")

    def test_higher_timeframe_gap_report_allows_partial_boundary_period(self):
        series = Series("1h", [candle(T + 3600)])
        self.assertEqual(gap_report(series, T + 900, T + 5400)["missing_candles"], 0)

    def test_subsecond_funding_timestamp_is_preserved(self):
        self.assertAlmostEqual(timestamp(T * 1000 + 3), T + 0.003)

    def test_current_metadata_requires_explicit_consent(self):
        with self.assertRaisesRegex(ValueError, "consent"):
            Engine(dataset(), replace(config(), conditional_current_metadata=False))

    def test_csv_manifest_import_checks_hash_and_reports_missing_frames(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "BTCUSDT-15m.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(candle(T)))
                writer.writeheader()
                writer.writerow(candle(T))
            archive = {"symbol": "BTCUSDT", "kind": "klines", "interval": "15m",
                       "month": "2025-04", "path": path.name, "status": "OK",
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            (root / "manifest.json").write_text(json.dumps({"archives": [archive]}), encoding="utf-8")
            meta = root / "metadata.json"
            meta.write_text(json.dumps(metadata(("BTCUSDT",))), encoding="utf-8")
            data = load_dataset(root, meta, T, T + 1800)
            self.assertEqual(data.frames["BTCUSDT"]["15m"].at(T)["close"], 100)
            self.assertEqual(data.report["symbols"]["BTCUSDT"]["klines_15m"]["missing_candles"], 1)
            self.assertEqual(data.report["symbols"]["BTCUSDT"]["funding"]["status"], "FUNDING YOK")
            path.write_text("altered", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                load_dataset(root, meta, T, T + 1800)

    def test_parquet_import_uses_identical_normalized_candles(self):
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:
            self.skipTest("Optional observation Parquet dependency is not installed")
        from app.backtest_data import read_rows
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "candles.parquet"
            rows = [candle(T), candle(T + 900)]
            pq.write_table(pa.Table.from_pylist(rows), path)
            actual, _ = normalize_candles(read_rows(path), "15m")
            expected, _ = normalize_candles(rows, "15m")
            self.assertEqual(actual.rows, expected.rows)

    def test_downloader_refuses_non_archive_and_signed_urls_before_io(self):
        self.assertTrue(BASE.startswith("https://data.binance.vision/"))
        for url in ("https://fapi.binance.com/fapi/v1/order", "https://fapi.binance.com/fapi/v1/leverageBracket"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "static"):
                fetch(url)


class FillAccountingTests(unittest.TestCase):
    def position(self, **kwargs):
        return Position("BTCUSDT", "LONG", T, spec(), "test-signal", Decimal("0.0003"), Decimal(100), **kwargs)

    def test_hand_calculated_entry_tp1_stop_commission_and_net_r(self):
        position = self.position()
        initial = position.initial_risk
        position.advance(candle(T, high=101.2), candle(T, high=101.2), "STOP_FIRST", opening_only=False)
        self.assertEqual(position.remaining, Decimal("1.20"))
        position.advance(candle(T + 900, low=98.5), candle(T + 900, low=98.5),
                         "STOP_FIRST", opening_only=False)
        row = position.row()
        self.assertEqual(row["actual_fill_price"], 100.03)
        self.assertEqual(row["tp1_quantity"], 1.8)
        self.assertAlmostEqual(row["gross_pnl"], 0.41982, places=10)
        self.assertAlmostEqual(row["commission_usdt"], 0.30029991, places=10)
        self.assertAlmostEqual(row["net_pnl"], 0.11952009, places=10)
        self.assertAlmostEqual(row["net_r"], 0.03984003, places=10)
        self.assertEqual(row["initial_risk_usdt"], 3)
        self.assertEqual(position.initial_risk, initial)
        self.assertEqual([fill["reason"] for fill in row["exits"]], ["TP1", "STOP"])
        self.assertEqual(position.spec["stop_loss"], "99")

    def test_actual_entry_is_unrounded_next_open_plus_slippage(self):
        position = self.position(market_open=Decimal("100.004"))
        self.assertEqual(position.actual_entry, Decimal("100.0340012"))
        self.assertEqual(position.initial_risk, 3)

    def test_ambiguous_bar_stop_first_vs_tp_first_differs(self):
        first, second = self.position(), self.position()
        bar = candle(T, high=104, low=98)
        first.advance(bar, bar, "STOP_FIRST", opening_only=False)
        second.advance(bar, bar, "TP_FIRST", opening_only=False)
        self.assertEqual([fill["reason"] for fill in first.row()["exits"]], ["STOP"])
        self.assertEqual([fill["reason"] for fill in second.row()["exits"]], ["TP1", "TP3"])
        self.assertGreater(second.row()["net_r"], first.row()["net_r"])
        self.assertEqual(first.ambiguous_bars, 1)

    def test_partial_quantity_floors_to_native_step_and_min_notional_can_disable_tp1(self):
        value = spec()
        value["quantity"], value["step"] = "3.01", Decimal("0.1")
        position = Position("BTCUSDT", "LONG", T, value, "test", Decimal(0), Decimal(100))
        self.assertEqual(position.tp1_quantity, Decimal("1.8"))
        value["min_notional"] = Decimal("200")
        position = Position("BTCUSDT", "LONG", T, value, "test", Decimal(0), Decimal(100))
        position.advance(candle(T, high=101.5), candle(T, high=101.5), "STOP_FIRST", opening_only=False)
        self.assertFalse(position.tp1_hit)
        self.assertEqual(position.remaining, Decimal("3.01"))
        self.assertEqual(position.row()["tp_protection_state"], "TP1_UNPROTECTED_MINIMUM")

    def test_gap_stop_fills_at_contract_open_not_trigger(self):
        position = self.position()
        bar = candle(T + 900, opening=97, high=98, low=96, close=97)
        position.advance(bar, bar, "STOP_FIRST", opening_only=True)
        self.assertEqual(position.exits[0]["expected_price"], 97)
        self.assertAlmostEqual(position.exits[0]["actual_price"], 96.9709)

    def test_mark_not_contract_controls_stop_trigger(self):
        position = self.position()
        position.advance(candle(T, low=98), candle(T, low=99.5), "STOP_FIRST", opening_only=False)
        self.assertEqual(position.status, "OPEN")

    def test_short_fills_and_funding_sign_are_symmetric(self):
        value = spec()
        value.update(direction="SHORT", stop_loss="101", targets=["99", "98", "97"])
        position = Position("BTCUSDT", "SHORT", T, value, "test", Decimal("0.0003"), Decimal(100))
        position.advance(candle(T, low=96), candle(T, low=96), "STOP_FIRST", opening_only=False)
        self.assertEqual(position.row()["status"], "CLOSED")
        self.assertGreater(position.row()["net_r"], 0)


class PipelineTests(unittest.TestCase):
    def test_empty_or_unlisted_policy_scope_cannot_open_offline_entry(self):
        for symbols in ([], ["ETHUSDT"]):
            with self.subTest(symbols=symbols):
                engine = Engine(dataset(), config(policy={"allowed_symbols": symbols}))
                asyncio.run(engine.enter("BTCUSDT", signal(), T))
                self.assertEqual(engine.first_rejections["allowed_symbols"], 1)
                self.assertEqual(len(engine.trades), 0)

    def test_native_spec_cost_filter_and_liquidation_are_called(self):
        data = dataset()
        engine = Engine(data, config())
        with patch.object(live, "build_live_spec", wraps=live.build_live_spec) as native, \
                patch("app.backtest_baseline.isolated_liquidation_risk",
                      wraps=__import__("app.liquidation_risk", fromlist=["isolated_liquidation_risk"]).isolated_liquidation_risk) as liquidation:
            asyncio.run(engine.enter("BTCUSDT", signal(), T))
        self.assertEqual(native.await_count, 1)
        self.assertEqual(liquidation.call_count, 1)
        self.assertEqual(len(engine.trades), 1)
        expensive = Engine(data, config(policy={"minimum_net_reward_usdt": 25}))
        asyncio.run(expensive.enter("BTCUSDT", signal(), T))
        self.assertEqual(expensive.first_rejections["cost_filter"], 1)

    def test_same_turn_third_long_is_blocked_by_native_direction_gate(self):
        data = dataset(("BTCUSDT", "ETHUSDT", "SOLUSDT"))
        engine = Engine(data, config())
        for symbol in data.frames:
            asyncio.run(engine.enter(symbol, signal(stop=98), T))
        self.assertEqual(len(engine.trades), 2)
        self.assertEqual(engine.gate_counts["same_direction_positions"], 1)

    def test_exposure_uses_normalized_quantity_times_mark(self):
        data = dataset(("BTCUSDT", "ETHUSDT"))
        engine = Engine(data, config())
        asyncio.run(engine.enter("BTCUSDT", signal(), T))
        self.assertNotIn("notional", engine.snapshot(T)["positions"][0])
        asyncio.run(engine.enter("ETHUSDT", signal(), T))
        self.assertEqual(len(engine.trades), 1)
        self.assertEqual(engine.gate_counts["exposure"], 1)

    def test_daily_and_loss_streak_gates_use_shared_metrics(self):
        engine = Engine(dataset(), config())
        engine.events = [{"kind": "LIVE_POSITION_CLOSED", "created_at": main.datetime.fromtimestamp(
            T - offset, main.timezone.utc).isoformat(), "realized_pnl": -4, "plan_id": str(offset)}
            for offset in (1, 2, 3)]
        # Keep all events on the replay UTC day.
        for event in engine.events:
            event["created_at"] = "2025-04-01T00:00:00+00:00"
        failures = engine.gate_failures("BTCUSDT", signal(), T)
        self.assertIn("daily_loss", failures)
        self.assertIn("consecutive_losses", failures)
        engine.events.extend({"kind": "LIVE_ENTRY", "created_at": "2025-04-01T00:00:00+00:00"} for _ in range(3))
        self.assertIn("daily_trades", engine.gate_failures("BTCUSDT", signal(), T))

    def test_liquidation_and_isolated_fail_closed(self):
        data = dataset()
        data.metadata["brackets"] = {}
        engine = Engine(data, config(policy={"require_isolated": False}))
        asyncio.run(engine.enter("BTCUSDT", signal(), T))
        self.assertEqual(engine.gate_counts["liquidation_buffer"], 1)
        self.assertEqual(engine.gate_counts["isolated"], 1)
        self.assertEqual(len(engine.trades), 0)

    def test_missing_funding_makes_net_r_null_not_zero(self):
        data = dataset()
        data.funding["BTCUSDT"] = []
        engine = Engine(data, config())
        asyncio.run(engine.enter("BTCUSDT", signal(), T))
        position = engine.trades[0]
        position.fill(Decimal(99), position.remaining, T + 899, "STOP")
        engine.finalize(position, T + 899)
        row = position.row()
        self.assertIsNone(row["funding_usdt"])
        self.assertIsNone(row["net_r"])
        self.assertIsNotNone(row["net_r_ex_funding"])
        self.assertEqual(metrics([row], 1000)["funding_null_count"], 1)

    def test_future_funding_does_not_affect_entry_balance(self):
        data = dataset()
        data.funding["BTCUSDT"] = [{"time": T + 0.003, "rate": 0.5, "interval_hours": 8}]
        engine = Engine(data, config())
        asyncio.run(engine.enter("BTCUSDT", signal(), T))
        before = engine.cash
        engine.advance(engine.trades[0], T, opening_only=True)
        self.assertEqual(engine.cash, before)
        engine.advance(engine.trades[0], T, opening_only=False)
        self.assertLess(engine.cash, before)

    def test_funding_after_tp1_uses_remaining_actual_quantity(self):
        data = dataset()
        data.funding["BTCUSDT"] = [{"time": T + 900, "rate": 0.0001, "interval_hours": 8}]
        engine = Engine(data, config())
        asyncio.run(engine.enter("BTCUSDT", signal(), T))
        position = engine.trades[0]
        position.fill(Decimal(101), position.tp1_quantity, T + 899, "TP1")
        position.tp1_hit = True
        self.assertEqual(position.remaining, Decimal("1.20"))
        engine.funding_at(position, T + 900, opening_only=True)
        self.assertEqual(position.funding_amount, Decimal("-0.012000"))
        self.assertEqual(position.initial_risk, 3)

    def test_short_receives_positive_historical_funding(self):
        data = dataset()
        data.funding["BTCUSDT"] = [{"time": T + 900, "rate": 0.0001, "interval_hours": 8}]
        value = spec()
        value.update(direction="SHORT", stop_loss="101", targets=["99", "98", "97"])
        position = Position("BTCUSDT", "SHORT", T, value, "test", Decimal("0.0003"), Decimal(100))
        engine = Engine(data, config())
        engine.funding_at(position, T + 900, opening_only=True)
        self.assertEqual(position.funding_amount, Decimal("0.0300"))

    def test_gap_closure_is_unverified_and_blocks_later_entries(self):
        data = dataset(("BTCUSDT", "ETHUSDT"))
        engine = Engine(data, config())
        asyncio.run(engine.enter("BTCUSDT", signal(), T))
        data.marks["BTCUSDT"] = Series("15m", [])
        engine.advance(engine.trades[0], T + 900, opening_only=True)
        self.assertEqual(engine.trades[0].status, "UNKNOWN_DATA_GAP")
        self.assertIsNone(engine.trades[0].row()["net_pnl"])
        self.assertIn("pnl_verified", engine.gate_failures("ETHUSDT", signal(), T + 900))

    def test_deterministic_replay_and_no_forced_end_close(self):
        decision = {"entry_eligible": True, "analysis": signal()}
        with patch.object(Dataset, "canonical", return_value=decision):
            first, second = run(dataset(), config()), run(dataset(), config())
        self.assertEqual(first, second)
        self.assertEqual(first["trades"][0]["status"], "OPEN_AT_END")
        self.assertIsNone(first["trades"][0]["net_r"])

    def test_offline_client_forbids_signed_and_unknown_public_endpoints(self):
        client = LocalSpecClient(metadata(("BTCUSDT",))["exchange_info"], 100)
        with self.assertRaises(AssertionError):
            asyncio.run(client.signed("POST", "/fapi/v1/order"))
        with self.assertRaises(AssertionError):
            asyncio.run(client.public_get("/fapi/v1/klines"))


class StatisticsAndCliTests(unittest.TestCase):
    def rows(self):
        return [{"status": "CLOSED", "net_pnl": value, "net_r": value / 2, "funding_usdt": 0,
                 "closed_at": f"2025-04-01T00:0{index}:00+00:00", "ambiguous_bars": 0}
                for index, value in enumerate((4, -2, -4, 6))]

    def test_summary_closed_curve_pf_and_expectancy(self):
        result = metrics(self.rows(), 1000)
        self.assertAlmostEqual(result["profit_factor"], 10 / 6)
        self.assertEqual(result["net_expectancy_r"], 0.5)
        self.assertEqual(result["win_rate"], 0.5)
        self.assertEqual(result["max_drawdown_usdt"], 6)
        self.assertEqual(result["max_drawdown_r"], 3)

    def test_bootstrap_is_seeded_and_exposes_unbounded_pf(self):
        self.assertEqual(bootstrap(self.rows(), 200, 42), bootstrap(self.rows(), 200, 42))
        result = bootstrap([self.rows()[0]], 20, 42)
        self.assertTrue(result["pf_upper_unbounded"])
        self.assertEqual(result["profit_factor_95"], [None, None])

    def test_empty_statistics_are_null_not_fake_performance(self):
        self.assertIsNone(metrics([], 1000)["net_expectancy_r"])
        self.assertIsNone(bootstrap([], 20, 42)["expectancy_r_95"])

    def test_cli_writes_all_seven_scenarios_and_stage2_columns_offline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = SimpleNamespace(output=root / "output", data=root, metadata=root / "metadata.json",
                                   start="2025-04-01T00:00:00+00:00", end="2025-04-01T00:30:00+00:00",
                                   initial_equity=1000, bootstrap_samples=20, conditional_current_metadata=True)
            with patch("backtest_cli.load_dataset", return_value=dataset()), \
                    patch.object(Dataset, "canonical", return_value={"entry_eligible": True, "analysis": signal()}):
                report = execute(args)
            self.assertEqual(len(report["comparison"]), 7)
            self.assertTrue((args.output / "comparison.json").exists())
            with (args.output / "stop_first-spread2-slip3-trades.csv").open(encoding="utf-8", newline="") as stream:
                row = next(csv.DictReader(stream))
            for key in ("signal_id", "intent_id", "gross_pnl", "commission_usdt", "funding_usdt", "net_pnl", "net_r", "initial_risk_usdt"):
                self.assertIn(key, row)


if __name__ == "__main__":
    unittest.main()
