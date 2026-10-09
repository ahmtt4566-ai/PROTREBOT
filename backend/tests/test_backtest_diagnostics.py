import json
import math
import sys
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))

from app import execution_core as core  # noqa: E402
from app.backtest_baseline import Engine, Position, bootstrap, run  # noqa: E402
from app.backtest_diagnostics import (  # noqa: E402
    bootstrap_comparison, clustered_bootstrap, diagnostic_exposure_cap, distribution,
    excursions, exit_distribution, monthly_distribution, replay_cap, trade_set_changes, utc,
)
from backtest_diagnostics_cli import execute, precompute  # noqa: E402
from test_backtest_baseline import T, candle, config, dataset, signal, spec  # noqa: E402


def position(direction="LONG"):
    value = spec()
    value["direction"] = direction
    if direction == "SHORT":
        value.update(stop_loss="101", targets=["99", "98", "97"])
    return Position("BTCUSDT", direction, T, value, "signal", Decimal("0.0003"), Decimal(100))


def realized(identifier, *, opened=T, closed=None, net_r=1, direction="LONG"):
    value = position(direction)
    value.opened_at = opened
    value.fill(Decimal(str(value.spec["stop_loss"])), value.remaining,
               closed if closed is not None else opened + 899, "STOP")
    row = value.row()
    row.update(signal_id=identifier, net_r=net_r, net_r_ex_funding=net_r,
               net_pnl=net_r * value.initial_risk, net_pnl_ex_funding=net_r * value.initial_risk)
    row["gross_pnl"] = row["net_pnl"] + row["commission_usdt"]
    return row


class ExposureDiagnosisTests(unittest.TestCase):
    def test_only_exposure_threshold_changes_and_live_sanitizer_is_restored(self):
        original = core.sanitize_execution_policy
        baseline = original({})
        for cap in (350, 700, 1050, None):
            with self.subTest(cap=cap):
                with diagnostic_exposure_cap(cap):
                    actual = core.sanitize_execution_policy({})
                    self.assertEqual(actual["max_total_exposure_usdt"], cap if cap is not None else math.inf)
                    self.assertEqual({k: v for k, v in actual.items() if k != "max_total_exposure_usdt"},
                                     {k: v for k, v in baseline.items() if k != "max_total_exposure_usdt"})
                self.assertIs(core.sanitize_execution_policy, original)
                self.assertEqual(original({"max_total_exposure_usdt": 1050})["max_total_exposure_usdt"], 350)
        self.assertEqual(core.HARD_MAX_TOTAL_EXPOSURE_USDT, 350)

    def test_exception_and_invalid_cap_cannot_leave_live_adapter_installed(self):
        original = core.sanitize_execution_policy
        with self.assertRaisesRegex(RuntimeError, "failure"):
            with diagnostic_exposure_cap(None):
                raise RuntimeError("failure")
        self.assertIs(core.sanitize_execution_policy, original)
        with self.assertRaises(ValueError):
            with diagnostic_exposure_cap(999):
                pass
        self.assertIs(core.sanitize_execution_policy, original)

    def test_500_existing_plus_100_uses_same_native_gate_at_each_cap(self):
        data = dataset(("BTCUSDT", "ETHUSDT"))
        for cap in (350, 700, 1050, None):
            with self.subTest(cap=cap), diagnostic_exposure_cap(cap):
                engine = Engine(data, config())
                value = spec()
                value.update(symbol="ETHUSDT", quantity="5", notional_usdt=500)
                engine.positions["ETHUSDT"] = Position(
                    "ETHUSDT", "LONG", T, value, "existing", Decimal("0.0003"), Decimal(100))
                with patch.object(core, "evaluate_entry_gates", wraps=core.evaluate_entry_gates) as native:
                    failures = engine.gate_failures("BTCUSDT", signal(), T, 100)
                self.assertEqual(native.call_count, 1)
                self.assertEqual("exposure" in failures, cap == 350)

    def test_unlimited_does_not_bypass_direction_daily_or_unknown_exposure(self):
        with diagnostic_exposure_cap(None):
            engine = Engine(dataset(("BTCUSDT", "ETHUSDT", "SOLUSDT")), config())
            for symbol in ("ETHUSDT", "SOLUSDT"):
                value = spec()
                value["symbol"] = symbol
                engine.positions[symbol] = Position(
                    symbol, "LONG", T, value, symbol, Decimal("0.0003"), Decimal(100))
            self.assertIn("same_direction_positions", engine.gate_failures("BTCUSDT", signal(), T))
            engine.events = [{"kind": "LIVE_ENTRY", "created_at": "2025-04-01T00:00:00+00:00"}] * 3
            self.assertIn("daily_trades", engine.gate_failures("BTCUSDT", signal(), T))
            snapshot = engine.snapshot(T)
            snapshot["positions"][0]["mark_price"] = None
            with patch.object(engine, "snapshot", return_value=snapshot):
                self.assertIn("exposure", engine.gate_failures("BTCUSDT", signal(), T, 100))

    def test_350_matches_unmodified_engine_and_unlimited_json_is_finite(self):
        data = dataset()
        with patch.object(data, "canonical", return_value={"entry_eligible": True, "analysis": signal()}):
            original = run(data, config())
            actual = replay_cap(data, config(), 350)
            for key in original:
                self.assertEqual(actual[key], original[key])
            unlimited = replay_cap(data, config(), None)
        json.dumps(unlimited, allow_nan=False)
        self.assertIsNone(unlimited["policy"]["max_total_exposure_usdt"])
        self.assertEqual(len(actual["trades"]), 1)

    def test_added_removed_and_changed_sets_are_not_count_subtraction(self):
        first = [realized("A", net_r=2), realized("B", net_r=1)]
        second = [realized("B", net_r=1), realized("C", net_r=-0.5)]
        result = trade_set_changes(second, first, config())
        self.assertEqual(result["added"]["trade_count"], 1)
        self.assertEqual(result["added"]["net_expectancy_r"], -0.5)
        self.assertEqual(result["removed"]["trade_count"], 1)
        self.assertEqual(result["common_trade_count"], 1)
        self.assertEqual(result["changed_common_trade_count"], 0)
        second[0]["quantity"] *= 2
        self.assertEqual(trade_set_changes(second, first, config())["changed_common_trade_count"], 1)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            trade_set_changes(first + [first[0]], second, config())
        self.assertIsNone(trade_set_changes(first, first, config())["added"]["net_expectancy_r"])


class ExcursionDiagnosisTests(unittest.TestCase):
    def test_tp1_then_stop_uses_frozen_full_risk_and_censors_stop_bar_high(self):
        data = dataset()
        data.marks["BTCUSDT"].rows[0].update(high=101.5, low=99.6)
        data.marks["BTCUSDT"].rows[1].update(open=100.5, high=102.7, low=98.7)
        value = position()
        value.fill(Decimal(101), value.tp1_quantity, T + 899, "TP1")
        value.fill(Decimal(99), value.remaining, T + 1799, "STOP")
        row = {**value.row(), **excursions(value.row(), data)}
        self.assertAlmostEqual(row["mae_r"], 1.03)
        self.assertAlmostEqual(row["mfe_r"], 1.47)
        self.assertAlmostEqual(row["mfe_r_upper"], 2.67)
        self.assertEqual(row["pre_stop_mfe_r"], row["mfe_r"])
        self.assertEqual(value.initial_risk, 3)
        result = exit_distribution([row])
        self.assertEqual(result["groups"]["TP1_THEN_STOP"]["trade_count"], 1)
        self.assertEqual(result["groups"]["TP1_THEN_STOP"]["mean_net_r"], row["net_r"])

    def test_stop_before_tp1_has_bounds_and_never_uses_post_close_candles(self):
        data = dataset()
        data.marks["BTCUSDT"].rows[0].update(high=100.9, low=98.5)
        value = position()
        value.fill(Decimal(99), value.remaining, T + 899, "STOP")
        first = excursions(value.row(), data)
        self.assertEqual(first["mfe_r"], 0)
        self.assertAlmostEqual(first["mfe_r_upper"], 0.87)
        self.assertAlmostEqual(first["mae_r"], 1.03)
        data.marks["BTCUSDT"].rows[1].update(high=100000, low=0.1)
        self.assertEqual(first, excursions(value.row(), data))
        result = exit_distribution([{**value.row(), **first}])
        self.assertEqual(result["groups"]["STOP_BEFORE_TP1"]["trade_count"], 1)

    def test_tp3_exit_censors_favorable_extreme_long_and_short(self):
        data = dataset()
        for direction, high, low in (("LONG", 104, 99.8), ("SHORT", 100.2, 96)):
            with self.subTest(direction=direction):
                data.marks["BTCUSDT"].rows[0].update(high=high, low=low)
                value = position(direction)
                value.fill(Decimal(str(value.spec["targets"][0])), value.tp1_quantity, T + 899, "TP1")
                value.fill(Decimal(str(value.spec["targets"][2])), value.remaining, T + 899, "TP3")
                result = excursions(value.row(), data)
                self.assertAlmostEqual(result["mfe_r"], 2.97)
                self.assertEqual(result["mfe_r_upper"], result["mfe_r"])
                self.assertAlmostEqual(result["mae_r"], 0.03)
                self.assertAlmostEqual(result["mae_r_upper"], 0.23)
                self.assertIsNone(result["pre_stop_mfe_r"])

    def test_opening_gap_exit_uses_only_open_and_prior_full_bars(self):
        data = dataset()
        data.marks["BTCUSDT"].rows[1].update(open=98.5, high=110, low=90)
        value = position()
        value.fill(Decimal("98.5"), value.remaining, T + 900, "STOP")
        result = excursions(value.row(), data)
        self.assertAlmostEqual(result["mae_r"], 1.53)
        self.assertAlmostEqual(result["mfe_r"], 0.47)
        self.assertEqual(result["mae_r"], result["mae_r_upper"])
        self.assertEqual(result["mfe_r"], result["mfe_r_upper"])

    def test_missing_risk_mark_or_unclosed_evidence_is_explicitly_null(self):
        row = realized("a", closed=T + 1799)
        data = dataset()
        row["initial_risk_usdt"] = None
        self.assertEqual(excursions(row, data)["excursion_status"], "INITIAL_RISK_MISSING")
        row["initial_risk_usdt"] = 3
        data.marks["BTCUSDT"].rows = data.marks["BTCUSDT"].rows[:1]
        data.marks["BTCUSDT"].times = [T]
        result = excursions(row, data)
        self.assertEqual(result["excursion_status"], "MARK_DATA_MISSING")
        self.assertIsNone(result["mfe_r"])
        row["status"] = "OPEN_AT_END"
        self.assertEqual(excursions(row, data)["excursion_status"], "UNCLOSED_OR_UNVERIFIED")

    def test_quantiles_missing_values_and_all_exit_groups(self):
        self.assertEqual(distribution([0, 1, 2, 3, 4, None]),
                         {"count": 5, "missing_count": 1, "p25": 1, "median": 2, "p75": 3})
        self.assertIsNone(distribution([])["median"])
        with self.assertRaises(ValueError):
            distribution([math.inf])
        data = dataset()
        rows = []
        for identifier, reason, partial in (("a", "STOP", False), ("b", "STOP", True), ("c", "TP3", True)):
            value = position()
            value.signal_identifier = identifier
            if partial:
                value.fill(Decimal(101), value.tp1_quantity, T + 899, "TP1")
            value.fill(Decimal(99 if reason == "STOP" else 103), value.remaining, T + 899, reason)
            rows.append({**value.row(), **excursions(value.row(), data)})
        groups = exit_distribution(rows)["groups"]
        self.assertEqual([groups[key]["trade_count"] for key in ("STOP_BEFORE_TP1", "TP1_THEN_STOP", "TP3")], [1, 1, 1])
        self.assertEqual(sum(group["trade_count"] for group in groups.values()), 3)


class BootstrapAndPeriodTests(unittest.TestCase):
    def test_iid_reuses_stage3_method_and_day_blocks_preserve_correlated_trades(self):
        rows = [realized(str(i), net_r=1 if i < 20 else -1,
                         opened=T if i < 20 else T + 86400) for i in range(40)]
        cfg = replace(config(), end=T + 3 * 86400, bootstrap_samples=500)
        result = bootstrap_comparison(rows, cfg)
        self.assertEqual(result["TRADE"], bootstrap(rows, 500, cfg.seed))
        day = result["DAY"]
        self.assertEqual(day["nonempty_cluster_count"], 2)
        self.assertEqual(day["empty_calendar_cluster_count"], 1)
        self.assertGreater(day["empty_resamples"], 0)
        self.assertGreater(day["expectancy_r_95"][1] - day["expectancy_r_95"][0],
                           result["TRADE"]["expectancy_r_95"][1] - result["TRADE"]["expectancy_r_95"][0])
        self.assertTrue(day["pf_upper_unbounded"])
        self.assertEqual(result, bootstrap_comparison(rows, cfg))

    def test_week_blocks_use_monday_utc_and_keep_empty_calendar_weeks(self):
        rows = [realized("a", net_r=2), realized("b", opened=T + 7 * 86400, net_r=-1)]
        cfg = replace(config(), end=T + 21 * 86400)
        result = clustered_bootstrap(rows, cfg, "WEEK")
        self.assertEqual(result["cluster_count"], 4)
        self.assertEqual(result["nonempty_cluster_count"], 2)
        self.assertEqual(result["empty_calendar_cluster_count"], 2)
        self.assertEqual(result["cluster_basis"], "UTC_ENTRY_TIME")

    def test_missing_r_or_single_active_cluster_does_not_make_fake_ci(self):
        cfg = replace(config(), end=T + 3 * 86400)
        rows = [realized("a")]
        result = clustered_bootstrap(rows, cfg, "DAY")
        self.assertEqual(result["status"], "INSUFFICIENT_NONEMPTY_CLUSTERS")
        self.assertIsNone(result["expectancy_r_95"])
        rows[0]["net_r"] = None
        self.assertEqual(clustered_bootstrap(rows, cfg, "DAY")["valid_trade_count"], 0)
        with self.assertRaises(ValueError):
            clustered_bootstrap(rows, cfg, "MONTH")

    def test_no_loss_and_zero_pnl_bootstraps_are_not_fake_finite_pf(self):
        cfg = replace(config(), end=T + 2 * 86400)
        rows = [realized("a"), realized("b", opened=T + 86400)]
        positive = clustered_bootstrap(rows, cfg, "DAY")
        self.assertEqual(positive["profit_factor_95"], [None, None])
        self.assertTrue(positive["pf_lower_unbounded"])
        self.assertTrue(positive["pf_upper_unbounded"])
        for row in rows:
            row.update(net_r=0, net_pnl=0)
        zero = clustered_bootstrap(rows, cfg, "DAY")
        self.assertIsNone(zero["profit_factor_95"])
        self.assertEqual(zero["zero_pnl_resamples"], cfg.bootstrap_samples)
        self.assertEqual(zero["expectancy_r_95"], [0, 0])

    def test_monthly_uses_closure_month_retains_empty_month_and_directions(self):
        cfg = replace(config(), end=int(utc("2025-06-01T00:00:00+00:00").timestamp()))
        rows = [realized("a", closed=int(utc("2025-05-01T00:00:00+00:00").timestamp()), direction="SHORT", net_r=-1)]
        result = monthly_distribution(rows, cfg)
        self.assertEqual(len(result), 6)
        april = next(row for row in result if row["month"] == "2025-04" and row["direction"] == "ALL")
        self.assertEqual(april["trade_count"], 0)
        self.assertIsNone(april["net_expectancy_r"])
        self.assertIsNone(april["closed_curve_net_pnl"])
        may_short = next(row for row in result if row["month"] == "2025-05" and row["direction"] == "SHORT")
        self.assertEqual(may_short["trade_count"], 1)
        self.assertEqual(may_short["closed_curve_net_pnl"], -3)
        self.assertEqual(may_short["net_expectancy_r"], -1)


class DiagnosticCliTests(unittest.TestCase):
    def test_real_native_decisions_are_identical_serial_and_spawn_workers(self):
        from guarded_process import run_isolated_if_needed

        if run_isolated_if_needed(self):
            return
        first = dataset(("BTCUSDT", "ETHUSDT"))
        second = dataset(("BTCUSDT", "ETHUSDT"))
        precompute(first, config(), 1)
        precompute(second, config(), 2)
        self.assertEqual(first.decisions, second.decisions)
        self.assertEqual(len(first.decisions), 4)
        with self.assertRaises(ValueError):
            precompute(first, config(), 5)

    def test_cli_exports_four_caps_bounds_and_tables_and_checks_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = dataset()
            args = SimpleNamespace(
                data=root, metadata=root / "metadata.json", output=root / "output",
                start="2025-04-01T00:00:00+00:00", end="2025-04-01T00:30:00+00:00",
                initial_equity=1000, bootstrap_samples=50,
                conditional_current_metadata=True, workers=1, reference_baseline=None)
            with patch.object(data, "canonical", return_value={"entry_eligible": True, "analysis": signal()}), \
                    patch("backtest_diagnostics_cli.load_dataset", return_value=data), \
                    patch("backtest_diagnostics_cli.precompute"):
                report = execute(args)
                self.assertEqual([row["cap"] for row in report["comparison"]], ["350", "700", "1050", "unlimited"])
                for cap in ("350", "700", "1050", "unlimited"):
                    result = json.loads((args.output / f"cap-{cap}.json").read_text())
                    self.assertIn("mfe_r_upper", result["trades"][0])
                    self.assertTrue((args.output / f"cap-{cap}-trades.csv").exists())
                    self.assertTrue((args.output / f"cap-{cap}-monthly.csv").exists())
                    self.assertEqual(set(result["bootstrap_comparison"]), {"TRADE", "DAY", "WEEK"})
                self.assertTrue((args.output / "exposure-comparison.csv").exists())
                self.assertTrue((args.output / "diagnosis.json").exists())
                baseline = run(data, config())
                args.reference_baseline = root / "reference.json"
                args.reference_baseline.write_text(json.dumps(baseline), encoding="utf-8")
                execute(args)
                baseline["summary"]["trade_count"] = 999
                args.reference_baseline.write_text(json.dumps(baseline), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "differs.*summary"):
                    execute(args)
        self.assertEqual(core.sanitize_execution_policy({"max_total_exposure_usdt": 700})["max_total_exposure_usdt"], 350)

    def test_repository_output_is_rejected_before_data_loading(self):
        args = SimpleNamespace(output=Path(__file__).parents[2] / "diagnostic-output")
        with self.assertRaisesRegex(ValueError, "outside Git"):
            execute(args)


if __name__ == "__main__":
    unittest.main()
