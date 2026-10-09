import gzip
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))

from app import execution_core as core, main
from app.backtest_ablation import PRESETS, frozen_replay
from app.backtest_baseline import Engine
from app.backtest_data import Series, gap_report
from app.holdout_gap_model import GapDataset, annotate, frozen_gap_replay, gap_position
from app.holdout_gap_study import (
    amendment, claim_once, inherited_plan, protocol_digest, register, validate_quality,
)
from app.holdout_study import period_scopes
from holdout_gap_cli import bundle, candidate_reports, dry_verify, execute, precompute, signal_worker
from test_backtest_ablation import closed_dataset, entry
from test_backtest_baseline import T, config, dataset, signal
from test_regime_study import row


def fixture(bars=16):
    native = dataset(bars=bars)
    base = inherited_plan(amendment())
    base["test"] = {"start": T, "end": T + bars * 900}
    base["symbols"] = ["BTCUSDT"]
    base["seen_periods"] = []
    base["bootstrap"]["samples"] = 20
    extension = amendment()
    extension["gap_rule"]["symbols"] = ["BTCUSDT"]
    extension["gap_rule"]["mark_bar_open"] = T + 900
    extension["gap_rule"]["blocked_native_decision_times"] = [T + 900, T + 1800]
    native.marks["BTCUSDT"] = Series("15m", [
        candle for candle in native.marks["BTCUSDT"].rows if candle["time"] != T + 900])
    native.report = {"missing_archives": [], "symbols": {"BTCUSDT": {
        **{f"klines_{interval}": {**gap_report(series, T, T + bars * 900), "duplicates": 0}
           for interval, series in native.frames["BTCUSDT"].items()},
        "markPriceKlines_15m": {**gap_report(native.marks["BTCUSDT"], T, T + bars * 900), "duplicates": 0}}}}
    return native, base, extension


class DataAndModelTests(unittest.TestCase):
    def test_locked_extension_inherits_every_base_setting(self):
        extension = amendment()
        base = inherited_plan(extension)
        self.assertEqual(protocol_digest(base), extension["base_plan_sha256"])
        self.assertEqual(extension["base_plan_sha256"],
                         "853dfa4714e8f9bf348350c3616666bafa172da660b383c15f67b86dd63fcc66")
        self.assertEqual(protocol_digest(extension),
                         "fe0353656e86363e8d85cf68641b96c1200b9439a3f7d6cb51881b8be2a9dd00")
        self.assertEqual(base["bootstrap"]["samples"], 20000)
        self.assertEqual(base["success"]["complete_trades_min"], 60)
        self.assertEqual(len(base["costs"]) * len(base["orderings"]) * len(base["candidates"]), 24)

    def test_only_exact_mark_gap_passes_without_interpolation(self):
        native, base, extension = fixture()
        validate_quality(native, base, extension)
        adapted = GapDataset(native, extension["gap_rule"])
        series = adapted.marks["BTCUSDT"]
        self.assertIsNone(native.marks["BTCUSDT"].at(T + 900))
        self.assertIs(series.at(T + 900), native.frames["BTCUSDT"]["15m"].at(T + 900))
        self.assertIs(series.rows, native.marks["BTCUSDT"].rows)
        self.assertNotIn(T + 900, series.times)
        self.assertNotIn(T + 900, [c["time"] for c in series.closed(T + 1800)])
        self.assertIsNone(series.at(T + 99))

    def test_new_mark_or_contract_gap_duplicate_or_warmup_gap_rejected(self):
        for mutation in ("extra_mark", "duplicate", "contract", "warmup"):
            with self.subTest(mutation=mutation):
                native, base, extension = fixture()
                if mutation == "extra_mark":
                    native.report["symbols"]["BTCUSDT"]["markPriceKlines_15m"]["missing_candles"] += 1
                elif mutation == "duplicate":
                    native.report["symbols"]["BTCUSDT"]["markPriceKlines_15m"]["duplicates"] = 1
                elif mutation == "contract":
                    native.report["symbols"]["BTCUSDT"]["klines_15m"]["missing_candles"] = 1
                else:
                    source = native.frames["BTCUSDT"]["4h"]
                    native.frames["BTCUSDT"]["4h"] = Series("4h", source.rows[10:])
                with self.assertRaises(ValueError):
                    validate_quality(native, base, extension)

    def test_blocked_open_and_closed_gap_decision_never_call_native(self):
        native, _, extension = fixture()
        adapted = GapDataset(native, extension["gap_rule"])
        with patch.object(main, "canonical_historical_decision") as native_call:
            for at in (T + 900, T + 1800):
                decision = adapted.canonical("BTCUSDT", at, core.sanitize_execution_policy({}))
                self.assertFalse(decision["entry_eligible"])
                self.assertFalse(decision["native_signal_calculated"])
        native_call.assert_not_called()
        with self.assertRaisesRegex(ValueError, "Missing verified"):
            adapted.canonical("BTCUSDT", T, core.sanitize_execution_policy({}))

    def test_native_worker_skips_gap_both_boundaries_and_uses_only_closed_bars(self):
        native, _, extension = fixture()
        cfg = replace(config(), end=T + 3600)
        with tempfile.TemporaryDirectory() as temporary, patch("holdout_gap_cli.deny_network"), \
             patch.object(main, "canonical_historical_decision", wraps=main.canonical_historical_decision) as decision:
            signal_worker("BTCUSDT", native.frames["BTCUSDT"], cfg, core.sanitize_execution_policy({}),
                          Path(temporary), "fixture", tuple(extension["gap_rule"]["blocked_native_decision_times"]))
            self.assertEqual([call.args[2] for call in decision.call_args_list], [T, T + 2700])
            for call in decision.call_args_list:
                for interval, candles in call.args[1].items():
                    duration = {"15m": 900, "1h": 3600, "4h": 14400}[interval]
                    self.assertTrue(all(c["time"] + duration <= call.args[2] for c in candles))
            with gzip.open(Path(temporary) / "BTCUSDT.jsonl.gz", "rt") as stream:
                values = [json.loads(line) for line in stream]
            self.assertFalse(values[2]["result"]["native_signal_calculated"])
            self.assertFalse(values[3]["result"]["native_signal_calculated"])

    def test_spawn_pipeline_preserves_gap_placeholders_no_native_fallback(self):
        from guarded_process import run_isolated_if_needed

        if run_isolated_if_needed(self):
            return
        native, _, extension = fixture()
        adapted = GapDataset(native, extension["gap_rule"])
        cfg = replace(config(), end=T + 3600)
        with tempfile.TemporaryDirectory() as temporary:
            precompute(adapted, cfg, 1, Path(temporary) / "cache", "spawn-fixture")
            for at in (T + 900, T + 1800):
                self.assertFalse(adapted.canonical("BTCUSDT", at, core.sanitize_execution_policy({}))["entry_eligible"])
            with self.assertRaisesRegex(ValueError, "not empty"):
                precompute(adapted, cfg, 1, Path(temporary) / "cache", "spawn-fixture")

    def test_gap_stop_reuses_native_fill_commission_quantity_and_immutable_risk(self):
        native, _, extension = fixture()
        native.frames["BTCUSDT"]["15m"].at(T).update(high=101.5)
        native.marks["BTCUSDT"].at(T).update(high=101.5)
        native.frames["BTCUSDT"]["15m"].at(T + 900).update(low=98.5)
        data = GapDataset(native, extension["gap_rule"])
        result = frozen_gap_replay(data, replace(config(), end=T + 2700), [entry()], PRESETS["B"])
        value = result["trades"][0]
        self.assertEqual([exit["reason"] for exit in value["exits"]], ["TP1", "STOP"])
        self.assertEqual(value["data_quality"], "GAP_AFFECTED")
        self.assertEqual(value["gap_trigger_source"], "CONTRACT_OHLC")
        self.assertEqual(value["gap_protective_exits"][0]["reason"], "STOP")
        self.assertEqual(value["initial_risk_usdt"], 3)
        self.assertEqual(value["tp1_quantity"], 1.8)
        self.assertAlmostEqual(value["commission_usdt"], 0.30029991)
        self.assertAlmostEqual(value["net_pnl"], 0.11952009)
        self.assertAlmostEqual(value["net_r"], 0.03984003)
        self.assertEqual(value["stop_updates"], [])

    def test_gap_tp_and_ambiguous_stop_priority_native_for_long_and_short(self):
        for direction in ("LONG", "SHORT"):
            for ordering in ("STOP_FIRST", "TP_FIRST"):
                with self.subTest(direction=direction, ordering=ordering):
                    native, _, extension = fixture()
                    contract = native.frames["BTCUSDT"]["15m"].at(T + 900)
                    contract.update(high=104, low=96)
                    data = GapDataset(native, extension["gap_rule"])
                    result = frozen_gap_replay(data, replace(config(), end=T + 2700, intrabar=ordering),
                                               [entry(direction)], PRESETS["B"])["trades"][0]
                    expected = ["STOP"] if ordering == "STOP_FIRST" else ["TP1", "TP3"]
                    self.assertEqual([exit["reason"] for exit in result["exits"]], expected)
                    self.assertTrue(result["gap_affected"])

    def test_be_uses_next_real_closed_mark_after_gap_not_future_or_missing_bar(self):
        native, _, extension = fixture()
        data = GapDataset(native, extension["gap_rule"])
        position = gap_position(entry(), data, config(), PRESETS["B"])
        position.tp1_hit = True
        for at in (T + 900, T + 1800):
            position.closed_update(data, at)
            self.assertEqual(position.spec["stop_loss"], "99")
        data.frames["BTCUSDT"]["15m"].at(T + 900).update(high=1000000)
        position.closed_update(data, T + 2700)
        self.assertGreater(Decimal(position.spec["stop_loss"]), position.actual_entry)
        self.assertEqual(position.stop_updates[0]["source_bar_open"], "2025-04-01T00:30:00+00:00")
        self.assertEqual(position.gap_stop_updates_deferred, [T + 900, T + 1800])

    def test_unaffected_trades_identical_to_stage4_b(self):
        native = closed_dataset()
        extension = amendment()
        extension["gap_rule"]["mark_bar_open"] = T + 10800
        extension["gap_rule"]["blocked_native_decision_times"] = [T + 10800, T + 11700]
        expected = frozen_replay(native, config(), [entry()], PRESETS["B"])["trades"][0]
        actual = frozen_gap_replay(GapDataset(native, extension["gap_rule"]), config(),
                                   [entry()], PRESETS["B"])["trades"][0]
        for key, value in expected.items():
            self.assertEqual(actual[key], value, key)
        self.assertFalse(actual["gap_affected"])

    def test_gap_embargo_rejects_frozen_entry_even_if_native_schedule_tampered(self):
        native, _, extension = fixture()
        with self.assertRaisesRegex(ValueError, "entry embargo"):
            frozen_gap_replay(GapDataset(native, extension["gap_rule"]), config(),
                              [entry(at=T + 900)], PRESETS["B"])

    def test_gap_valuation_and_real_funding_use_contract_open_without_inventing_rate(self):
        native, _, extension = fixture()
        native.frames["BTCUSDT"]["15m"].at(T + 900)["open"] = 100.2
        native.funding["BTCUSDT"] = [{"time": T + 900, "rate": 0.001, "interval_hours": 8}]
        data = GapDataset(native, extension["gap_rule"])
        engine = Engine(data, config())
        position = gap_position(entry(), data, config(), PRESETS["B"])
        engine.positions["BTCUSDT"] = position
        self.assertEqual(engine.snapshot(T + 900)["positions"][0]["mark_price"], 100.2)
        engine.funding_at(position, T + 900, opening_only=True)
        self.assertEqual(position.funding_amount, Decimal("-0.3006"))
        self.assertTrue(position.row()["gap_affected"])
        self.assertIsNone(native.marks["BTCUSDT"].at(T + 900))

    def test_affected_lifetime_and_exclusion_subset_do_not_replay(self):
        native, base, extension = fixture()
        data = GapDataset(native, extension["gap_rule"])
        gap_row, clean_row = row("gap", -1), row("clean", 1)
        gap_row["exits"], clean_row["exits"] = [], []
        rows = [annotate(gap_row, T), annotate(clean_row, T - 900)]
        cfg = replace(config(), end=base["test"]["end"])
        with patch("app.holdout_study.closed_features", return_value=(20, 0, 0)), \
             patch("holdout_gap_cli.frozen_gap_replay", side_effect=AssertionError("second replay")) as replay:
            reports, _ = candidate_reports(rows, data, cfg, base, period_scopes(base))
        replay.assert_not_called()
        self.assertEqual(reports["B"]["gap_affected_count"], 1)
        self.assertEqual(reports["B"]["excluding_gap_affected"]["full"]["signal_ids"], ["clean"])
        self.assertEqual(reports["B"]["excluding_gap_affected"]["full"]["summary"]["net_expectancy_r"], 1)


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.stage5, self.output = root / "stage5", root / "stage6b"
        anchor = root / "anchor"
        anchor.mkdir()
        for target, kwargs in (
            ("app.holdout_gap_study.anchor_directory", {"return_value": anchor}),
            ("app.holdout_gap_study.verify_context", {"return_value": inherited_plan(amendment())}),
            ("holdout_gap_cli.verify_context", {"return_value": inherited_plan(amendment())}),
            ("holdout_gap_cli.deny_network", {}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        register(self.stage5, self.output)

    def test_dry_verify_calls_no_signal_replay_or_bootstrap_and_consumes_no_claim(self):
        native, _, _ = fixture()
        with patch("holdout_gap_cli.load_verified", return_value=native), \
             patch.object(main, "canonical_historical_decision", side_effect=AssertionError("signal")) as signal_call, \
             patch("holdout_gap_cli.bundle", side_effect=AssertionError("replay")) as replay:
            record = dry_verify(self.stage5, self.output)
        self.assertEqual(record["signals_calculated"], 0)
        self.assertEqual(record["replays"], 0)
        self.assertFalse((self.output / "test-once.json").exists())
        signal_call.assert_not_called()
        replay.assert_not_called()

    def test_bad_quality_before_claim_preserves_right_and_old_failed_record(self):
        previous = self.stage5.parent / "stage6"
        previous.mkdir()
        old = previous / "test-once.json"
        old.write_text('{"state":"FAILED","rerun_allowed":false}')
        before = old.read_bytes()
        with patch("holdout_gap_cli.load_verified", side_effect=ValueError("unregistered mark gap")):
            with self.assertRaisesRegex(ValueError, "unregistered"):
                dry_verify(self.stage5, self.output)
        self.assertFalse((self.output / "test-once.json").exists())
        self.assertFalse((self.output / "dry-verify.json").exists())
        self.assertEqual(old.read_bytes(), before)
        native, _, _ = fixture()
        with patch("holdout_gap_cli.load_verified", return_value=native):
            dry_verify(self.stage5, self.output)
        self.assertTrue((self.output / "dry-verify.json").exists())

    def test_without_dry_or_changed_source_no_claim(self):
        with self.assertRaises(FileNotFoundError):
            claim_once(self.stage5, self.output)
        native, _, _ = fixture()
        with patch("holdout_gap_cli.load_verified", return_value=native):
            dry_verify(self.stage5, self.output)
        with patch("app.holdout_gap_study.source_hashes", return_value={"changed": "source"}):
            with self.assertRaisesRegex(ValueError, "unchanged"):
                claim_once(self.stage5, self.output)
        self.assertFalse((self.output / "test-once.json").exists())

    def test_run_rechecks_raw_quality_before_consuming_claim(self):
        native, _, _ = fixture()
        with patch("holdout_gap_cli.load_verified", return_value=native):
            dry_verify(self.stage5, self.output)
        with patch("holdout_gap_cli.load_verified", side_effect=ValueError("new raw gap")), \
             patch("holdout_gap_cli.bundle") as replay:
            with self.assertRaisesRegex(ValueError, "new raw gap"):
                execute(self.stage5, self.output, 1)
        self.assertFalse((self.output / "test-once.json").exists())
        replay.assert_not_called()

    def test_relocation_and_registration_overwrite_refused(self):
        with self.assertRaisesRegex(ValueError, "fixed external"):
            register(self.stage5, self.output.parent / "stage6c")
        data = json.loads((self.output / "holdout-lock.json").read_text())
        data["sha256"] = "changed"
        (self.output / "holdout-lock.json").write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "Immutable"):
            register(self.stage5, self.output)

    def test_failed_postclaim_no_second_native_load_or_retry(self):
        native, _, _ = fixture()
        with patch("holdout_gap_cli.load_verified", return_value=native) as load:
            dry_verify(self.stage5, self.output)
            with patch("holdout_gap_cli.bundle", side_effect=RuntimeError("fixture failure")), \
                 self.assertLogs("holdout_gap_cli", level="ERROR"), \
                 self.assertRaisesRegex(RuntimeError, "fixture failure"):
                execute(self.stage5, self.output, 1)
            with self.assertRaisesRegex(ValueError, "already claimed"):
                execute(self.stage5, self.output, 1)
            self.assertEqual(load.call_count, 2)
        record = json.loads((self.output / "test-once.json").read_text())
        self.assertEqual(record["state"], "FAILED")
        self.assertFalse(record["rerun_allowed"])
        self.assertEqual(json.loads(Path(record["repository_claim_path"]).read_text()), record)

    def test_completed_attempt_also_irreversible(self):
        native, _, _ = fixture()
        with patch("holdout_gap_cli.load_verified", return_value=native), patch("holdout_gap_cli.bundle") as replay:
            dry_verify(self.stage5, self.output)

            def finish(*_args):
                (self.output / "holdout-results.json").write_text('{"fixture":true}')
                return {"fixture": True}

            replay.side_effect = finish
            self.assertEqual(execute(self.stage5, self.output, 1), {"fixture": True})
            with self.assertRaisesRegex(ValueError, "already claimed"):
                execute(self.stage5, self.output, 1)
            self.assertEqual(replay.call_count, 1)
        self.assertEqual(json.loads((self.output / "test-once.json").read_text())["state"], "COMPLETED")

    def test_nonempty_bundle_all_registered_cells_controls_and_gap_subsets(self):
        native, base, extension = fixture()
        native.frames["BTCUSDT"]["15m"].at(T).update(high=101.5)
        native.marks["BTCUSDT"].at(T).update(high=101.5)
        native.frames["BTCUSDT"]["15m"].at(T + 900).update(low=98.5)

        def signals(data, cfg, *_args):
            policy = core.sanitize_execution_policy(cfg.policy)
            for at in range(cfg.start, cfg.end, 900):
                key = ("BTCUSDT", at, policy["min_confidence"], policy["mtf_allow_either_timeframe"])
                data.decisions[key] = {"entry_eligible": at == T, "analysis": signal(), "reason": "SYNTHETIC_WAIT"}

        with patch("holdout_gap_cli.inherited_plan", return_value=base), \
             patch("holdout_gap_cli.precompute", side_effect=signals), \
             patch("app.holdout_study.closed_features", return_value=(20, 0, 0)), \
             patch("app.v25_execution.execute_live_order", side_effect=AssertionError("LIVE order")) as live:
            (self.output / "test-once.json").write_text('{"fixture":true}')
            result = bundle(native, self.output, {"plan": extension, "sha256": "fixture"}, 1)
        live.assert_not_called()
        self.assertEqual(result["candidate_scenarios_inspected"], 24)
        self.assertEqual(len(result["results"]), 8)
        for scenario in result["results"].values():
            b = scenario["candidates"]["B"]
            self.assertEqual(b["gap_affected_count"], 1)
            self.assertEqual(b["scopes"]["full"]["summary"]["trade_count"], 1)
            self.assertEqual(b["excluding_gap_affected"]["full"]["summary"]["trade_count"], 0)
            self.assertEqual(scenario["candidates"]["B_ADX4H20"]["gap_affected_count"], 1)
        self.assertTrue((self.output / "comparison.csv").exists())
