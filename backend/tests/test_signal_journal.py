import asyncio
import csv
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parents[1]))

from app import signal_journal as telemetry, v25_execution as live  # noqa: E402
from app.analysis import analyze  # noqa: E402
from app.execution_core import sanitize_execution_policy  # noqa: E402
from app.signal_observation import build_record  # noqa: E402
from app.signal_outcomes import fill_outcomes, hypothetical, import_data  # noqa: E402
from signal_journal_cli import export, summary  # noqa: E402
import test_v25_auto_symbol_scope as scope  # noqa: E402

DECISION = 18000


def round_data(**overrides):
    return {
        "policy": sanitize_execution_policy({"allowed_symbols": ["BTCUSDT"], "min_confidence": 85}),
        "snapshot": {**scope.SNAPSHOT, "multi_assets_mode": False}, "daily": {},
        "plans": [], "ts_scan": DECISION + 5, "ts_decision": DECISION, **overrides,
    }


def candidate(**overrides):
    return {
        "symbol": "BTCUSDT", "intent_id": "auto-BTCUSDT-15m-18000000",
        "canonical": {
            "signal_timestamp": DECISION - 900,
            "entry_eligible": False,
            "analysis": {
                "direction": "LONG", "confidence": 75, "entry": 100, "stop_loss": 99,
                "tp1": 101, "tp3": 103, "atr": 1, "radar": {"trap_score": 40, "breakout_quality": 20},
                "ema": {"ema20": 100, "ema50": 99, "ema200": 98},
                "macd": 0.5, "rsi": 60, "adx": 25, "volume_ratio": 1.2,
                "bollinger": {"upper": 102, "middle": 100, "lower": 98},
            },
            "mtf": {"higher_timeframe_confirmation": False, "blocked_by_short_filter": False, "alignment": 70,
                    "timeframes": {"15m": {"direction": "LONG", "confidence": 75},
                                   "1h": {"direction": "SHORT", "confidence": 60},
                                   "4h": {"direction": "LONG", "confidence": 80}}},
        },
        "first_reject": "CONFIDENCE_BELOW_MIN", **overrides,
    }


def bar(open_time, *, high=100.5, low=99.5, close=100):
    return {"time": open_time, "open": 100, "high": high, "low": low, "close": close, "volume": 10}


class StorageFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.journal = telemetry.Journal(Path(temporary.name) / "observations.sqlite3")

    def record(self, **overrides):
        record = build_record(round_data(), candidate(**overrides), self.journal)
        self.journal.record(record, "round-one")
        return record


class JournalTests(StorageFixture):
    def test_deterministic_signal_identity_uses_symbol_direction_and_close(self):
        identifier = telemetry.signal_id("BTCUSDT", "LONG", DECISION)
        self.assertEqual(identifier, telemetry.signal_id("btcusdt", "long", DECISION))
        self.assertNotEqual(identifier, telemetry.signal_id("BTCUSDT", "SHORT", DECISION))
        self.assertNotEqual(identifier, telemetry.signal_id("BTCUSDT", "LONG", DECISION + 900))
        self.assertEqual(self.record()["signal_id"], identifier)

    def test_repeated_write_is_one_signal_and_one_round_observation(self):
        record = self.record()
        self.journal.record(record, "round-one")
        self.journal.record(record, "round-two")
        with self.journal.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM signals").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM rounds").fetchone()[0], 2)

    def test_repeat_does_not_rewrite_initial_features_or_downgrade_acceptance(self):
        record = self.record(accepted=True)
        self.journal.record({**record, "confidence": 1, "decision": "REJECTED"}, "repeat")
        row = self.journal.rows()[0]
        self.assertEqual(row["decision"], "ACCEPTED")
        self.assertEqual(row["confidence"], 75)

    def test_early_rejection_still_evaluates_all_pure_later_gates(self):
        data = round_data(
            snapshot={**scope.SNAPSHOT, "positions": [{"symbol": "ETHUSDT", "direction": "LONG", "quantity": 5, "mark_price": 100}]},
            daily={"entries": 100, "realized_pnl": -100},
        )
        record = build_record(data, candidate(spread_bp=20), self.journal)
        self.assertEqual(record["reject_reason"], "CONFIDENCE_BELOW_MIN")
        for gate in ("TRAP_SCORE_ABOVE_MAX", "BREAKOUT_QUALITY_BELOW_MIN", "MTF_HIGHER_TIMEFRAME_MISMATCH",
                     "EXPOSURE", "SPREAD", "DAILY_TRADES", "DAILY_LOSS"):
            self.assertIn(gate, record["reject_reasons"])
        self.assertIn("LIQUIDATION_BUFFER", record["unknown_gates"])
        self.assertIsNone(record["funding_rate"])

    def test_missing_spread_is_unknown_not_zero_or_a_pass(self):
        record = self.record()
        gate = next(gate for gate in record["gate_results"] if gate["key"] == "SPREAD")
        self.assertIsNone(record["spread_bp"])
        self.assertIsNone(gate["passed"])
        self.assertEqual(record["bb_width"], 0.04)

    def test_short_alignment_rejection_is_separate(self):
        value = candidate()
        value["canonical"]["analysis"]["direction"] = "SHORT"
        value["canonical"]["mtf"]["blocked_by_short_filter"] = True
        record = build_record(round_data(), value, self.journal)
        self.assertIn("MTF_SHORT_ALIGNMENT_FILTER", record["reject_reasons"])

    def test_storage_is_separate_and_refuses_foreign_database(self):
        with self.journal.connect() as db:
            db.execute("CREATE TABLE foreign_persistence(secret TEXT)")
        with self.assertRaisesRegex(ValueError, "share"):
            with self.journal.connect():
                pass

    def test_trade_risk_is_immutable_and_unknown_funding_produces_no_fabricated_net_r(self):
        record = self.record(accepted=True)
        self.journal.trade(record["intent_id"], {"initial_risk_usdt": 2, "gross_pnl": 4,
                                                "commission_usdt": 0.2, "commission_complete": True})
        self.assertIsNone(self.journal.rows()[0]["net_r"])
        self.journal.trade(record["intent_id"], {"initial_risk_usdt": 999, "funding_usdt": -0.1, "funding_complete": True})
        row = self.journal.rows()[0]
        self.assertEqual(row["initial_risk_usdt"], 2)
        self.assertAlmostEqual(row["net_pnl"], 3.7)
        self.assertAlmostEqual(row["net_r"], 1.85)

    def test_missing_risk_never_generates_r(self):
        record = self.record(accepted=True)
        self.journal.trade(record["intent_id"], {"gross_pnl": 4, "commission_usdt": 0,
                                                "commission_complete": True, "funding_usdt": 0, "funding_complete": True})
        self.assertEqual(self.journal.rows()[0]["net_pnl"], 4)
        self.assertIsNone(self.journal.rows()[0]["net_r"])

    def test_weighted_fill_slippage_and_commission_are_captured_without_touching_legacy_r(self):
        record = self.record(accepted=True)
        observer = telemetry.Observer(self.journal)
        observer.handle("fills", None, {
            "intent_id": record["intent_id"], "entries": [{"qty": "1", "price": "101"}, {"qty": "3", "price": "102"}],
            "expected_entry": 100, "initial_risk_usdt": 2, "gross_pnl": 4,
            "commission_usdt": 0.2, "commission_complete": True,
        })
        row = self.journal.rows()[0]
        self.assertEqual(row["actual_fill_price"], 101.75)
        self.assertEqual(row["actual_slippage_price"], 1.75)
        self.assertIsNone(row["net_r"])

    def test_csv_export_shape_and_rejection_summary(self):
        record = self.record()
        output = self.journal.path.with_suffix(".csv")
        self.assertEqual(export(self.journal, output, "csv"), 1)
        with output.open(encoding="utf-8", newline="") as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row["signal_id"], record["signal_id"])
        self.assertEqual(row["decision"], "REJECTED")
        self.assertEqual(row["funding_rate"], "")
        self.assertEqual(summary(self.journal)["decisions"], {"REJECTED": 1})
        self.assertEqual(summary(self.journal)["first_reject_reasons"], {"CONFIDENCE_BELOW_MIN": 1})

    def test_unclosed_candle_is_not_stored_or_used(self):
        self.journal.candles("BTCUSDT", "15m", [bar(DECISION), bar(DECISION + 900, high=900)], DECISION + 900)
        rows = self.journal.closed_candles("BTCUSDT", "15m", DECISION + 10000)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["high"], 100.5)

    def test_invalid_candle_is_explicit_error(self):
        with self.assertRaises(ValueError):
            self.journal.candles("BTCUSDT", "15m", [bar(DECISION, high=99)], DECISION + 900)

    def test_return_horizons_wait_for_closed_contiguous_windows(self):
        self.record()
        bars = [bar(DECISION + step * 900, high=102, close=101) for step in range(16)]
        self.journal.candles("BTCUSDT", "15m", bars, DECISION + 14400)
        fill_outcomes(self.journal, DECISION + 3599)
        self.assertIsNone(self.journal.rows()[0]["return_1h"])
        fill_outcomes(self.journal, DECISION + 3600)
        row = self.journal.rows()[0]
        self.assertAlmostEqual(row["return_1h"], 0.01)
        self.assertIsNone(row["return_2h"])
        self.assertIsNone(row["hypothetical_result"])
        fill_outcomes(self.journal, DECISION + 14400)
        self.assertAlmostEqual(self.journal.rows()[0]["return_4h"], 0.01)

    def test_candle_gap_does_not_synthesize_missing_horizon(self):
        self.record()
        bars = [bar(DECISION + step * 900) for step in range(16) if step != 2]
        self.journal.candles("BTCUSDT", "15m", bars, DECISION + 14400)
        fill_outcomes(self.journal, DECISION + 14400)
        self.assertIsNone(self.journal.rows()[0]["return_1h"])
        self.assertIsNone(self.journal.rows()[0]["hypothetical_result"])

    def test_future_candles_do_not_change_decision_features_or_early_returns(self):
        record = self.record()
        self.journal.candles("BTCUSDT", "15m",
                             [bar(DECISION + step * 900) for step in range(4)] + [bar(DECISION + 3600, high=999)],
                             DECISION + 14400)
        fill_outcomes(self.journal, DECISION + 3600)
        row = self.journal.rows()[0]
        self.assertEqual(row["confidence"], record["confidence"])
        self.assertEqual(row["return_1h"], 0)
        self.assertIsNone(row["mfe_r_gross"])

    def test_offline_enrichment_rejects_future_market_evidence(self):
        record = self.record()
        result = import_data(self.journal, {"market": [{
            "signal_id": record["signal_id"], "observed_at": DECISION + 100, "spread_bp": 1,
        }]}, DECISION + 1000)
        self.assertEqual(result["future_market_ignored"], 1)
        self.assertIsNone(self.journal.rows()[0]["spread_bp"])
        import_data(self.journal, {"market": [{
            "signal_id": record["signal_id"], "observed_at": DECISION, "spread_bp": 20, "funding_rate": 0.001,
        }]}, DECISION + 1000)
        row = self.journal.rows()[0]
        self.assertEqual(row["spread_bp"], 20)
        self.assertEqual(row["decision"], "REJECTED")
        self.assertEqual(row["reject_reason"], "CONFIDENCE_BELOW_MIN")
        self.assertIn("SPREAD", row["enriched_reject_reasons"])

    def test_outcomes_without_verified_decision_candle_stay_missing(self):
        record = self.record()
        self.journal.enrich(record["signal_id"], {"decision_time_verified": False})
        self.journal.candles("BTCUSDT", "15m", [bar(DECISION + step * 900) for step in range(16)], DECISION + 14400)
        self.assertEqual(fill_outcomes(self.journal, DECISION + 14400)["incomplete"], 1)

    def test_outcome_job_rejects_backwards_as_of_without_changing_output(self):
        self.record()
        self.journal.candles("BTCUSDT", "15m", [bar(DECISION + step * 900) for step in range(16)], DECISION + 14400)
        fill_outcomes(self.journal, DECISION + 14400)
        before = self.journal.rows()
        with self.assertRaisesRegex(ValueError, "backwards"):
            fill_outcomes(self.journal, DECISION + 3600)
        self.assertEqual(self.journal.rows(), before)

    def test_provider_future_close_is_excluded_even_if_open_looks_closed(self):
        self.journal.candles("BTCUSDT", "15m",
                             [[DECISION * 1000, "100", "101", "99", "100", "10", (DECISION + 1800) * 1000]],
                             DECISION + 900)
        self.assertEqual(self.journal.closed_candles("BTCUSDT", "15m", DECISION + 10000), [])

    def test_offline_enrichment_preserves_native_mtf_and_known_features(self):
        record = self.record()
        import_data(self.journal, {"market": [{"signal_id": record["signal_id"],
                                              "observed_at": DECISION, "spread_bp": 1}]}, DECISION + 100)
        row = self.journal.rows()[0]
        gate = next(item for item in row["enriched_gate_results"] if item["key"] == "MTF_HIGHER_TIMEFRAME_MISMATCH")
        self.assertIs(gate["passed"], False)
        self.assertEqual(row["confidence"], record["confidence"])

    def test_offline_indicator_scores_ignore_future_candles(self):
        record = self.record()
        past = [bar(DECISION - (220 - step) * 900) for step in range(220)]
        future = [bar(DECISION + step * 900, high=999, close=998) for step in range(16)]
        scores = {}
        analyze(past, observation_scores=scores)
        import_data(self.journal, {
            "candles": [{"symbol": "BTCUSDT", "interval": "15m", "rows": [*past, *future]}],
            "market": [{"signal_id": record["signal_id"], "observed_at": DECISION, "spread_bp": 1}],
        }, DECISION + 14400)
        row = self.journal.rows()[0]
        self.assertEqual(row["long_score"], scores["long_score"])
        self.assertEqual(row["short_score"], scores["short_score"])
        self.assertEqual(row["confidence"], record["confidence"])
        self.assertEqual(row["ema20"], record["ema20"])

    def test_trade_import_requires_causal_timestamp_and_cannot_replace_risk(self):
        record = self.record(accepted=True)
        self.journal.trade(record["intent_id"], {"initial_risk_usdt": 2})
        trade = {"intent_id": record["intent_id"], "initial_risk_usdt": 999, "gross_pnl": 4,
                 "commission_usdt": 0.2, "commission_complete": True,
                 "funding_usdt": -0.1, "funding_complete": True}
        for observed in (None, DECISION - 1, DECISION + 1001):
            with self.subTest(observed=observed), self.assertRaises(ValueError):
                import_data(self.journal, {"trades": [{**trade, "observed_at": observed}]}, DECISION + 1000)
        import_data(self.journal, {"trades": [{**trade, "observed_at": DECISION + 100}]}, DECISION + 1000)
        row = self.journal.rows()[0]
        self.assertEqual(row["initial_risk_usdt"], 2)
        self.assertAlmostEqual(row["net_r"], 1.85)

    def test_malformed_market_measurement_is_an_explicit_error(self):
        record = self.record()
        for value in ("invalid", float("nan"), -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                import_data(self.journal, {"market": [{"signal_id": record["signal_id"],
                                                      "observed_at": DECISION, "spread_bp": value}]}, DECISION + 100)

    def test_fill_uses_original_expected_price_and_keeps_complete_funding(self):
        record = self.record(accepted=True)
        self.journal.trade(record["intent_id"], {"expected_entry": 100, "initial_risk_usdt": 2,
                                                "funding_usdt": -0.1, "funding_complete": True})
        observer = telemetry.Observer(self.journal)
        observer.handle("fills", None, {
            "intent_id": record["intent_id"], "expected_entry": 102, "entries": [{"qty": "1", "price": "102"}],
            "gross_pnl": 4, "commission_usdt": 0.2, "commission_complete": True,
        })
        row = self.journal.rows()[0]
        self.assertEqual(row["expected_entry"], 100)
        self.assertEqual(row["actual_slippage_price"], 2)
        self.assertAlmostEqual(row["net_r"], 1.85)

    def test_missing_actual_commission_is_not_invented_as_complete_zero(self):
        record = self.record(accepted=True)
        observer = telemetry.Observer(self.journal)
        observer.handle("fills", None, {
            "intent_id": record["intent_id"], "entries": [{"qty": "1", "price": "100"}],
            "gross_pnl": 4, "commission_usdt": 0, "commission_complete": True,
            "commission_rows": [{"commissionAsset": "USDT"}], "close_rows": [{"realizedPnl": "4"}],
            "funding_usdt": 0, "funding_complete": True,
        })
        row = self.journal.rows()[0]
        self.assertFalse(row["commission_complete"])
        self.assertIsNone(row["commission_usdt"])
        self.assertIsNone(row["net_r"])

    def test_real_parquet_retains_accepted_only_columns_and_handles_empty_database(self):
        try:
            import pyarrow.parquet as parquet
        except ImportError:
            self.skipTest("Install requirements-observation.txt to test optional Parquet export")
        output = self.journal.path.with_suffix(".parquet")
        self.assertEqual(export(self.journal, output, "parquet"), 0)
        self.assertEqual(parquet.read_table(output).num_rows, 0)
        self.record()
        value = candidate(accepted=True)
        value["symbol"] = "ETHUSDT"
        value["intent_id"] = "auto-ETHUSDT-15m-18000000"
        accepted = build_record(round_data(), value, self.journal)
        self.journal.record(accepted, "accepted-round")
        self.journal.trade(accepted["intent_id"], {"plan_id": "accepted-only", "initial_risk_usdt": 2})
        self.assertEqual(export(self.journal, output, "parquet"), 2)
        rows = parquet.read_table(output).to_pylist()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["plan_id"] for row in rows}, {None, "accepted-only"})
        self.assertTrue(all("return_4h" in row for row in rows))

    def test_cli_summary_export_and_fill_are_offline_commands(self):
        self.record()
        script = Path(__file__).parents[1] / "signal_journal_cli.py"
        base = [sys.executable, str(script), "--db", str(self.journal.path)]
        result = subprocess.run([*base, "summary"], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["decisions"], {"REJECTED": 1})
        output = self.journal.path.with_suffix(".csv")
        result = subprocess.run([*base, "export", "--output", str(output)], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {"exported": 1})
        with output.open(encoding="utf-8", newline="") as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 1)
        result = subprocess.run([*base, "fill", "--as-of", "1970-01-01T05:00:00+00:00"],
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {"updated": 0, "incomplete": 1})


class HypotheticalTests(unittest.TestCase):
    def record(self, direction="LONG"):
        return {"direction": direction, "entry": 100, "stop": 99 if direction == "LONG" else 101,
                "tp1": 101 if direction == "LONG" else 99, "tp3": 103 if direction == "LONG" else 97,
                "fee_bps_per_side": 5, "slippage_bps_per_side": 3}

    def test_same_bar_stop_and_target_is_conservative_and_flagged(self):
        result = hypothetical(self.record(), [bar(DECISION, high=104, low=98)])
        self.assertEqual(result["hypothetical_result"], "STOP")
        self.assertEqual(result["hypothetical_r_gross"], -1)
        self.assertTrue(result["intrabar_ambiguous"])
        self.assertLess(result["hypothetical_r_costs"], result["hypothetical_r_gross"])

    def test_tp1_sixty_percent_then_tp3_is_1_8_r(self):
        result = hypothetical(self.record(), [bar(DECISION, high=101.1), bar(DECISION + 900, high=103.1)])
        self.assertEqual(result["hypothetical_result"], "TP3")
        self.assertAlmostEqual(result["hypothetical_r_gross"], 1.8)
        self.assertIn("mae_r_costs", result)
        self.assertIn("mfe_r_costs", result)

    def test_short_tp3_and_stop_are_symmetric(self):
        result = hypothetical(self.record("SHORT"), [bar(DECISION, high=100.5, low=96.9)])
        self.assertEqual(result["hypothetical_result"], "TP3")
        self.assertAlmostEqual(result["hypothetical_r_gross"], 1.8)
        self.assertEqual(hypothetical(self.record("SHORT"), [bar(DECISION, high=101.1)])["hypothetical_result"], "STOP")

    def test_timeout_and_tp1_timeout_are_distinct(self):
        self.assertEqual(hypothetical(self.record(), [bar(DECISION)])["hypothetical_result"], "TIMEOUT")
        self.assertEqual(hypothetical(self.record(), [bar(DECISION, high=101.1)])["hypothetical_result"], "TP1")


class ObserverIsolationTests(StorageFixture):
    def setUp(self):
        super().setUp()
        self.observer = telemetry.Observer(self.journal)
        self.patch = patch.object(telemetry, "_observer", self.observer)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.observer.close)
        flag = patch.dict(os.environ, {"PROTREBOT_SIGNAL_JOURNAL_ENABLED": "true"})
        flag.start()
        self.addCleanup(flag.stop)

    def test_default_enabled_and_disabled_is_noop(self):
        with patch.dict(os.environ, {}, clear=True):
            token = telemetry.start_round(round_data()["policy"], scope.SNAPSHOT, {}, {})
            telemetry.emit("candidate", **candidate())
            telemetry.finish_round(token, None)
        self.observer.queue.join()
        self.assertEqual(len(self.journal.rows()), 1)
        with patch.dict(os.environ, {"PROTREBOT_SIGNAL_JOURNAL_ENABLED": "false"}):
            self.assertIsNone(telemetry.start_round({}, {}, {}, {}))
            telemetry.emit("candidate", symbol="ETHUSDT")
        self.assertEqual(len(self.journal.rows()), 1)

    def test_rejected_native_cycle_is_written_without_extra_requests(self):
        application, _ = scope.application_for(["BTCUSDT"])
        application.state.v25_execution["policy"]["min_confidence"] = 85
        value = candidate()["canonical"]
        _, candles, submit = scope.AutoSymbolScopeTests().cycle(
            application, [{"symbol": "BTCUSDT", "opportunity_score": 1}], decision=value,
        )
        self.observer.queue.join()
        row = self.journal.rows()[0]
        self.assertEqual(row["decision"], "REJECTED")
        self.assertEqual(row["reject_reason"], "CONFIDENCE_BELOW_MIN")
        submit.assert_not_awaited()
        candles.assert_awaited_once()

    def test_native_universe_reject_logs_all_observable_universe_gates(self):
        async def public_get(path, params=None):
            if path == "/fapi/v1/exchangeInfo":
                return {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING",
                                     "contractType": "PERPETUAL", "quoteAsset": "USDT"}]}
            if path == "/fapi/v1/ticker/24hr":
                return [{"symbol": "BTCUSDT", "quoteVolume": "1000",
                         "priceChangePercent": "0.1", "lastPrice": "100"}]
            raise AssertionError(f"Unexpected offline request: {path}")

        client = SimpleNamespace(public_get=AsyncMock(side_effect=public_get))
        token = telemetry.start_round(round_data()["policy"], scope.SNAPSHOT, {}, {})
        self.assertEqual(asyncio.run(live.scan_market_candidates(client, scope.SNAPSHOT, ["BTCUSDT"])), [])
        telemetry.finish_round(token, None)
        self.observer.queue.join()
        row = self.journal.rows()[0]
        self.assertEqual(row["reject_reason"], "VOLUME_BELOW_MIN")
        self.assertIn("MOVE_BELOW_MIN", row["reject_reasons"])
        self.assertEqual(row["quote_volume_24h"], 1000)
        self.assertEqual(row["decision"], "REJECTED")
        self.assertIsNone(row["long_score"])
        self.assertEqual(client.public_get.await_count, 2)

    def test_storage_failure_is_logged_and_order_still_succeeds(self):
        application, _ = scope.application_for(["BTCUSDT"])
        with patch.object(self.journal, "record", side_effect=OSError("offline synthetic disk failure")), \
                self.assertLogs("app.signal_journal", level="ERROR"):
            token = telemetry.start_round(round_data()["policy"], scope.SNAPSHOT, {}, {})
            telemetry.emit("candidate", **candidate())
            telemetry.finish_round(token, None)
            result = scope.AutoSymbolScopeTests().execute(application, "BTCUSDT")
            self.observer.queue.join()
        self.assertTrue(result["ok"])
        self.assertGreater(self.observer.failures, 0)

    def test_blocked_writer_does_not_delay_order_boundary(self):
        entered, release = threading.Event(), threading.Event()
        original = self.journal.record

        def blocked(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args)

        try:
            with patch.object(self.journal, "record", side_effect=blocked):
                token = telemetry.start_round(round_data()["policy"], scope.SNAPSHOT, {}, {})
                telemetry.emit("candidate", **candidate())
                telemetry.finish_round(token, None)
                self.assertTrue(entered.wait(2))
                application, _ = scope.application_for(["BTCUSDT"])
                result = scope.AutoSymbolScopeTests().execute(application, "BTCUSDT")
                self.assertTrue(result["ok"])
                self.assertFalse(release.is_set())
        finally:
            release.set()
            self.observer.queue.join()

    def test_queue_overflow_is_fail_open(self):
        observer = telemetry.Observer(self.journal, capacity=1)
        with patch.object(observer, "handle", side_effect=OSError("offline synthetic failure")):
            for _ in range(10):
                observer.publish("trade", None, {"intent_id": "test", "update": {}})
            observer.close()
        self.assertGreater(observer.dropped + observer.failures, 0)

    def test_accepted_native_cycle_has_one_submission_and_immutable_risk(self):
        application, _ = scope.application_for(["BTCUSDT"])
        value = candidate()["canonical"]
        value["entry_eligible"] = True
        value["decision"] = "BUY"
        value["analysis"].update(confidence=95, tp2=102)
        value["analysis"]["radar"] = {"trap_score": 1, "breakout_quality": 90}
        value["mtf"]["higher_timeframe_confirmation"] = True

        async def accepted(*args, **kwargs):
            return {"ok": True, "plan": {"id": "native-plan", "entry_price": "100",
                                        "quantity": "3", "initial_risk_usdt": 3}}

        _, candles, submit = scope.AutoSymbolScopeTests().cycle(
            application, [{"symbol": "BTCUSDT", "opportunity_score": 99}],
            execution_effect=accepted, decision=value,
        )
        self.observer.queue.join()
        submit.assert_awaited_once()
        candles.assert_awaited_once()
        rows = self.journal.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision"], "ACCEPTED")
        self.assertEqual(rows[0]["initial_risk_usdt"], 3)
        self.assertIsNone(rows[0]["net_r"])

    def test_producer_failure_and_first_rejection_do_not_escape_or_get_replaced(self):
        token = telemetry.start_round(round_data()["policy"], scope.SNAPSHOT, {}, {})
        telemetry.emit("candidate", **candidate())
        telemetry.emit("candidate", symbol="BTCUSDT", first_reject="ENTRY_SUBMISSION")
        telemetry.finish_round(token, None)
        self.observer.queue.join()
        self.assertEqual(self.journal.rows()[0]["reject_reason"], "CONFIDENCE_BELOW_MIN")
        with patch.object(self.observer, "publish", side_effect=OSError("synthetic enqueue failure")):
            token = telemetry.start_round(round_data()["policy"], scope.SNAPSHOT, {}, {})
            telemetry.finish_round(token, None)
        self.assertIsNone(telemetry.ROUND.get())
        self.assertGreater(self.observer.dropped, 0)

    def test_verified_partial_position_records_actual_fill_before_closure(self):
        record = self.record(accepted=True)
        plan = {"symbol": "BTCUSDT", "direction": "LONG", "source": "V25_AUTO",
                "intent_id": record["intent_id"], "quantity": "0.010",
                "entry_order_id": 42, "entry_client_order_id": "owned-entry",
                "entry_price": "100", "initial_risk_usdt": 2}
        self.assertTrue(live.confirm_live_plan_provenance(plan, {
            "symbol": "BTCUSDT", "direction": "LONG", "quantity": "0.005", "entry_price": 101,
        }))
        self.observer.queue.join()
        row = self.journal.rows()[0]
        self.assertEqual(row["actual_fill_price"], 101)
        self.assertEqual(row["actual_slippage_price"], 1)
        self.assertEqual(plan["initial_risk_usdt"], 2)
        self.assertIsNone(row["gross_pnl"])
        self.assertIsNone(row["net_r"])

    def test_invalid_configured_path_is_logged_by_worker_without_touching_native_file(self):
        native_path = self.journal.path.with_suffix(".json")
        native_path.write_text('{"untouched":true}', encoding="utf-8")
        observer = telemetry.Observer(telemetry.Journal(native_path))
        with self.assertLogs("app.signal_journal", level="ERROR"):
            observer.publish("candles", None, {"symbol": "BTCUSDT", "interval": "15m",
                                              "rows": [], "as_of": DECISION})
            observer.queue.join()
        observer.close()
        self.assertEqual(native_path.read_text(encoding="utf-8"), '{"untouched":true}')

    def test_analysis_default_payload_and_decision_are_unchanged(self):
        baseline = subprocess.check_output(["git", "show", "9dd89b4:backend/app/analysis.py"], text=True, encoding="utf-8")
        namespace = {}
        exec(compile(baseline, "<stage-one-analysis>", "exec"), namespace)
        for slope in (0.1, -0.1, 0):
            candles = [bar(step * 900, high=130, low=70, close=100 + slope * step) for step in range(230)]
            scores = {}
            expected = namespace["analyze"](candles)
            self.assertEqual(analyze(candles), expected)
            self.assertEqual(analyze(candles, observation_scores=scores), expected)
            self.assertEqual(set(scores), {"long_score", "short_score"})


if __name__ == "__main__":
    unittest.main()
