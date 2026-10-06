import asyncio
import copy
import gzip
import json
import os
import socket
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))

from app import analysis, execution_core as core, main
from app.backtest_baseline import iso
from app.backtest_data import Series
from app.regime_statistics import (
    cohort_periods, combination_comparisons, complete, evaluate_filters, joint_contrasts, summary,
)
from app.regime_study import (
    ResearchDataset, causal_percentiles, closed_features, common_period_dataset, compact_decision,
    development_manifest, filter_masks, lock_plan, native_pair, registered_plan,
    short_gate_disabled,
)
from regime_cli import deny_network, worker
from test_backtest_baseline import T, candle, config, dataset, signal


def row(identifier, value, day=0, direction="LONG"):
    return {"signal_id": identifier, "symbol": "BTCUSDT", "direction": direction,
            "opened_at": iso(T + day * 86400), "closed_at": iso(T + day * 86400 + 899),
            "status": "CLOSED", "net_r": value, "net_pnl": value * 3,
            "funding_usdt": 0, "ambiguous_bars": 0, "exits": [{"reason": "STOP"}]}


class OfflineNetworkTests(unittest.TestCase):
    def test_native_asyncio_loop_works_but_external_network_stays_blocked(self):
        async def replay():
            await asyncio.sleep(0)
            return "completed"

        with patch.object(socket.socket, "connect", socket.socket.connect), \
             patch.object(socket.socket, "connect_ex", socket.socket.connect_ex), \
             patch.object(socket, "getaddrinfo", socket.getaddrinfo):
            deny_network()
            deny_network()
            self.assertEqual(asyncio.run(replay()), "completed")
            with socket.socket() as connection:
                for method in (connection.connect, connection.connect_ex):
                    with self.subTest(method=method.__name__):
                        with self.assertRaisesRegex(OSError, "External network forbidden"):
                            method(("203.0.113.1", 443))
            with self.assertRaisesRegex(OSError, "External network forbidden"):
                socket.getaddrinfo("fapi.binance.com", 443)


class CombinationTests(unittest.TestCase):
    def controls(self):
        baseline = [row("a", -1, 0), row("b", 1, 1), row("c", -1, 2, "SHORT")]
        for value in baseline:
            value.update(actual_fill_price=100, quantity=1, initial_risk_usdt=3)
        be = copy.deepcopy(baseline)
        for value, delta in zip(be, (0.5, -0.25, 0.2)):
            value["net_r"] += delta
            value["net_pnl"] += delta * 3
        plan = registered_plan()
        plan["bootstrap"]["samples"] = 40
        return baseline, be, plan

    def test_native_pairing_and_hand_calculated_combination_deltas(self):
        from app.backtest_ablation import paired_difference

        baseline, be, plan = self.controls()
        with patch("app.backtest_ablation.paired_difference", wraps=paired_difference) as native:
            result = combination_comparisons(baseline, be, replace(config(), end=T + 3 * 86400), plan)
        self.assertEqual(native.call_count, 2)
        self.assertAlmostEqual(result["B_vs_A"]["mean_delta_net_r"], 0.15)
        self.assertAlmostEqual(result["B_NO_SHORT_vs_A_LONG"]["mean_delta_net_r"], 0.125)
        self.assertEqual(result["B_vs_A"]["matched_complete_pairs"], 3)
        self.assertEqual(result["B_NO_SHORT_vs_A_LONG"]["matched_complete_pairs"], 2)
        self.assertEqual(result["B_NO_SHORT_vs_A_LONG"]["unmatched_baseline"], 0)
        self.assertEqual(result["removed_short_count"], 1)
        self.assertEqual(result["B_vs_A"]["samples"], 40)
        self.assertIsNotNone(result["B_vs_A"]["trade_delta_r_95"])
        self.assertIsNotNone(result["B_vs_A"]["day_delta_r_95"])

    def test_changed_entry_cohort_and_initial_risk_are_rejected(self):
        baseline, be, plan = self.controls()
        with self.assertRaisesRegex(ValueError, "same frozen"):
            combination_comparisons(baseline, be[:-1], config(), plan)
        be[0]["initial_risk_usdt"] = 4
        with self.assertRaisesRegex(ValueError, "changed entry/risk"):
            combination_comparisons(baseline, be, config(), plan)

    def test_report_command_rejects_out_of_development_trade_before_pairing(self):
        from regime_report import execute

        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ), \
             patch("regime_cli.deny_network") as guard, \
             patch("app.regime_statistics.combination_comparisons") as paired:
            root = Path(temporary)
            locked = lock_plan(root)
            (root / "regime-study.json").write_text(json.dumps({
                "protocol_sha256": locked["sha256"],
                "test_candles_loaded": 0, "test_results_inspected": 0,
            }))
            invalid = row("outside", 1)
            invalid["opened_at"] = iso(locked["plan"]["splits"]["TEST"]["start"])
            (root / "cohorts-stop_first.json").write_text(json.dumps({
                "A": {"trades": [invalid]}, "B": {"trades": [invalid]},
            }))
            with self.assertRaisesRegex(ValueError, "outside development"):
                execute(root)
            guard.assert_called_once()
            paired.assert_not_called()


class ProtocolTests(unittest.TestCase):
    def test_fixed_reverse_time_split_and_embargo(self):
        plan = registered_plan()
        splits = plan["splits"]
        lengths = [splits[key]["end"] - splits[key]["start"] for key in ("TEST", "TRAIN", "VALIDATION")]
        self.assertEqual(lengths, [274 * 86400, 548 * 86400, 274 * 86400])
        self.assertEqual(splits["TRAIN"]["evaluate_start"] - splits["TRAIN"]["start"], 93 * 86400)
        self.assertLess(splits["TEST"]["end"], 1743465600)
        self.assertFalse(plan["test_access_before_stage6"])
        self.assertEqual(len(plan["research_symbols"]), 8)
        self.assertEqual(plan["combination"], ["F1_ADX1H20", "F3_ATR30"])
        self.assertEqual(len(plan["filters"]), 8)
        self.assertEqual(plan["bootstrap"]["family_size"], 11)
        self.assertEqual(plan["bootstrap"]["samples"], 20000)

    def test_lock_is_idempotent_and_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            locked = lock_plan(root)
            self.assertEqual(locked, lock_plan(root))
            path = root / "study-lock.json"
            altered = copy.deepcopy(locked)
            altered["plan"]["filters"][0]["threshold"] = 19
            path.write_text(json.dumps(altered), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "refusing"):
                lock_plan(root)
            self.assertEqual(json.loads(path.read_text()), altered)

    def test_test_archive_rejected_before_native_loader_or_zip_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manifest.json").write_text(json.dumps({"archives": [
                {"month": "2024-06", "path": "poison.zip"}]}))
            with patch("app.backtest_data.read_rows", side_effect=AssertionError("TEST read")) as read:
                with self.assertRaisesRegex(ValueError, "Sealed TEST"):
                    development_manifest(root, registered_plan())
            read.assert_not_called()

    def test_archive_path_escape_and_future_month_fail_closed(self):
        for item in ({"month": "2024-07", "path": "..\\sealed\\poison.zip"},
                     {"month": "2026-10", "path": "future.zip"}):
            with self.subTest(item=item), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "manifest.json").write_text(json.dumps({"archives": [item]}))
                with self.assertRaises(ValueError):
                    development_manifest(root, registered_plan())


class NativeGateTests(unittest.TestCase):
    def test_native_functions_called_with_closed_frames_and_no_future_effect(self):
        data = dataset()
        policy = core.sanitize_execution_policy({})
        with patch.object(main, "canonical_historical_decision", wraps=main.canonical_historical_decision) as native:
            first = native_pair("BTCUSDT", data.frames["BTCUSDT"], T, policy)
        self.assertGreater(native.call_count, 0)
        for call in native.call_args_list:
            for interval, rows in call.args[1].items():
                duration = {"15m": 900, "1h": 3600, "4h": 14400}[interval]
                self.assertTrue(all(value["time"] + duration <= T for value in rows))
        changed = copy.deepcopy(data.frames["BTCUSDT"])
        for series in changed.values():
            for value in series.rows:
                if value["time"] >= T:
                    value.update(high=10000, close=9999)
        self.assertEqual(first, native_pair("BTCUSDT", changed, T, policy))

    def test_short_toggle_disables_only_original_80_gate(self):
        data = dataset()
        policy = core.sanitize_execution_policy({})
        short = {**signal(), "direction": "SHORT", "trend": "SHORT"}
        binding = main.shared_mtf_decision
        with patch.object(main, "analyze", return_value=short):
            on, off = native_pair("BTCUSDT", data.frames["BTCUSDT"], T, policy)
        self.assertFalse(on["entry_eligible"])
        self.assertTrue(off["entry_eligible"])
        self.assertEqual(off["analysis"]["direction"], "SHORT")
        self.assertIs(main.shared_mtf_decision, binding)

    def test_short_toggle_preserves_confidence_trap_and_long_behavior(self):
        data = dataset()
        policy = core.sanitize_execution_policy({})
        for changed in ({**signal(), "confidence": 10},
                        {**signal(), "radar": {"trap_score": 99, "breakout_quality": 90}}):
            with self.subTest(changed=changed), patch.object(main, "analyze", return_value={
                    **changed, "direction": "SHORT", "trend": "SHORT"}):
                on, off = native_pair("BTCUSDT", data.frames["BTCUSDT"], T, policy)
                self.assertFalse(on["entry_eligible"])
                self.assertFalse(off["entry_eligible"])
        with patch.object(main, "analyze", return_value={**signal(), "trend": "LONG"}):
            on, off = native_pair("BTCUSDT", data.frames["BTCUSDT"], T, policy)
        self.assertEqual(on, off)
        self.assertTrue(on["entry_eligible"])

    def test_adapter_restores_binding_after_exception(self):
        original = main.shared_mtf_decision
        with self.assertRaisesRegex(RuntimeError, "failure"):
            with short_gate_disabled():
                raise RuntimeError("failure")
        self.assertIs(main.shared_mtf_decision, original)

    def test_research_cache_modes_cannot_contaminate_or_fall_back(self):
        original = dataset()
        data = ResearchDataset(original.frames, original.marks, original.funding,
                               original.metadata, original.report, original.funding_months)
        policy = core.sanitize_execution_policy({})
        key = ("BTCUSDT", T, policy["min_confidence"], policy["mtf_allow_either_timeframe"])
        data.decisions[(*key, True)] = {"entry_eligible": False}
        data.decisions[(*key, False)] = {"entry_eligible": True}
        self.assertFalse(data.canonical("BTCUSDT", T, policy)["entry_eligible"])
        data.short_gate_on = False
        self.assertTrue(data.canonical("BTCUSDT", T, policy)["entry_eligible"])
        with self.assertRaisesRegex(ValueError, "Missing verified"):
            data.canonical("BTCUSDT", T + 900, policy)

    def test_worker_cache_fingerprint_and_atomic_output(self):
        data = dataset()
        with tempfile.TemporaryDirectory() as temporary, patch("regime_cli.deny_network"):
            root = Path(temporary)
            policy = core.sanitize_execution_policy({})
            worker("BTCUSDT", data.frames["BTCUSDT"], config(), policy, root, "hash1")
            with gzip.open(root / "BTCUSDT.jsonl.gz", "rt") as stream:
                values = [json.loads(line) for line in stream]
            self.assertEqual(values[0]["fingerprint"], "hash1")
            self.assertEqual([value["at"] for value in values[1:]], [T, T + 900])
            self.assertFalse(list(root.glob("*.part")))
            with self.assertRaisesRegex(ValueError, "fingerprint"):
                worker("BTCUSDT", data.frames["BTCUSDT"], config(), policy, root, "hash2")

    def test_compact_native_rejection_has_no_synthetic_analysis(self):
        result = compact_decision({"entry_eligible": False, "analysis": {"atr": 9}, "reason": "blocked"})
        self.assertNotIn("analysis", result)
        self.assertEqual(result["reason"], "blocked")

    def test_common_scope_reuses_expanded_rows_and_checks_archive_hashes(self):
        original = dataset(("BTCUSDT", "ETHUSDT"))
        original.report["sources"] = [{"path": "archive.zip", "sha256": "verified"}]
        data = ResearchDataset(original.frames, original.marks, original.funding,
                               original.metadata, original.report, original.funding_months)
        metadata = copy.deepcopy(original.metadata)
        metadata["brackets"].pop("ETHUSDT")
        manifest = {"archives": [{"path": "archive.zip", "sha256": "verified",
                                  "month": "2025-04", "status": "OK"}]}
        common = common_period_dataset(data, manifest, metadata)
        self.assertEqual(set(common.frames), {"BTCUSDT"})
        self.assertEqual(common.frames["BTCUSDT"]["15m"].times[0], T)
        self.assertIs(common.frames["BTCUSDT"]["15m"].rows[0], data.frames["BTCUSDT"]["15m"].at(T))
        manifest["archives"][0]["sha256"] = "changed"
        with self.assertRaisesRegex(ValueError, "differs"):
            common_period_dataset(data, manifest, metadata)


class FeatureTests(unittest.TestCase):
    def test_indicator_definitions_equal_native_analysis(self):
        series = dataset().frames["BTCUSDT"]["15m"]
        native = analysis.analyze(series.closed(T))
        adx, atr_ratio, width = closed_features(series, T)
        self.assertEqual(adx, native["adx"])
        self.assertEqual(atr_ratio, native["atr"] / series.closed(T)[-1]["close"])
        self.assertAlmostEqual(width, (native["bollinger"]["upper"] - native["bollinger"]["lower"])
                               / native["bollinger"]["middle"], places=14)

    def test_forming_higher_frame_does_not_change_adx(self):
        series = dataset().frames["BTCUSDT"]["4h"]
        original = closed_features(series, T)
        series.at(T).update(high=10000, close=9999)
        self.assertEqual(original, closed_features(series, T))

    def test_missing_history_is_unknown_not_zero(self):
        series = dataset().frames["BTCUSDT"]["15m"]
        series.rows.pop(-18)
        series.__post_init__()
        self.assertEqual(closed_features(series, T), (None, None, None))

    def test_percentile_90_day_coverage_ties_and_no_lookahead(self):
        series = Series("15m", [candle(T + index * 900) for index in range(9000)])
        start, end = T + 8898 * 900, T + 8900 * 900
        first = causal_percentiles(series, start, end)
        self.assertEqual(first[start], {"atr_percentile": 100, "bb_percentile": 100})
        for value in series.rows:
            if value["time"] >= start:
                value.update(high=10000, close=9999)
        changed = causal_percentiles(series, start, start + 900)
        self.assertEqual(first[start], changed[start])

    def test_percentiles_without_90_days_are_null(self):
        series = dataset().frames["BTCUSDT"]["15m"]
        values = causal_percentiles(series, T, T + 900)
        self.assertEqual(values[T], {"atr_percentile": None, "bb_percentile": None})

    def test_filter_masks_can_only_remove_and_use_fixed_combo(self):
        plan = registered_plan()
        rows = [row("pass", 1), row("missing", -1, direction="SHORT")]
        features = {"pass": {"adx_1h": 22, "adx_4h": 26, "atr_percentile": 31, "bb_percentile": 55},
                    "missing": {"adx_1h": None, "adx_4h": 19, "atr_percentile": 29, "bb_percentile": 10}}
        masks = filter_masks(rows, features, plan)
        self.assertEqual(len(masks), 10)
        self.assertEqual(masks["F1_ADX1H20"], [True, False])
        self.assertEqual(masks["F1_ADX1H25"], [False, False])
        self.assertEqual(masks["F1_F3_COMBINED"], [True, False])
        self.assertEqual(masks["B_NO_SHORT"], [True, False])


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        self.rows = [row("p1", 1), row("n1", -1), row("p2", 2, 1), row("n2", -2, 1)]
        self.cfg = replace(config(), end=T + 3 * 86400)
        self.masks = {"positive": [True, False, True, False], "all": [True] * 4}

    def test_hand_calculated_selection_difference_and_common_exit_delta(self):
        plan = registered_plan()
        plan["bootstrap"]["samples"] = 100
        result = evaluate_filters(self.rows, self.masks, self.cfg, plan)
        positive = result["positive"]
        self.assertEqual(positive["retained"]["net_expectancy_r"], 1.5)
        self.assertEqual(positive["eliminated"]["net_expectancy_r"], -1.5)
        self.assertEqual(positive["selection_contrast"]["mean_r_difference"], 1.5)
        self.assertEqual(positive["paired_common_exit_delta_r"], 0)
        self.assertEqual(positive["remaining_ratio"], 0.5)
        self.assertEqual(positive["added_entry_count"], 0)

    def test_deterministic_joint_bootstrap_and_bonferroni_contains_nominal(self):
        first = joint_contrasts(self.rows, self.masks, self.cfg, samples=400, family_size=11)
        self.assertEqual(first, joint_contrasts(self.rows, self.masks, self.cfg, samples=400, family_size=11))
        for method in ("TRADE", "DAY"):
            interval = first["positive"][method]
            self.assertLessEqual(interval["bonferroni_family_95"][0], interval["nominal_95"][0])
            self.assertGreaterEqual(interval["bonferroni_family_95"][1], interval["nominal_95"][1])
            self.assertEqual(first["all"][method]["nominal_95"], [0, 0])
        self.assertEqual(first["positive"]["DAY"]["calendar_clusters"], 3)
        self.assertEqual(first["positive"]["DAY"]["active_clusters"], 2)

    def test_under_60_complete_trades_forbids_interpretation(self):
        result = summary(self.rows, self.cfg, 60)
        self.assertEqual(result["sample_status"], "YETERSIZ_ORNEKLEM")
        self.assertFalse(result["interpretation_allowed"])
        enough = [row(str(index), 1) for index in range(60)]
        self.assertTrue(summary(enough, self.cfg, 60)["interpretation_allowed"])
        enough[-1]["funding_usdt"] = None
        enough[-1]["net_r"] = None
        enough[-1]["net_pnl"] = None
        self.assertFalse(summary(enough, self.cfg, 60)["interpretation_allowed"])

    def test_zero_selected_and_single_day_have_explicit_undefined_status(self):
        result = joint_contrasts(self.rows[:2], {"none": [False, False]},
                                self.cfg, samples=20, family_size=11)
        self.assertIsNone(result["none"]["mean_r_difference"])
        self.assertIsNone(result["none"]["DAY"]["nominal_95"])
        self.assertEqual(result["none"]["DAY"]["active_clusters"], 1)

    def test_selected_single_day_has_no_day_ci_even_with_multiple_reference_days(self):
        result = joint_contrasts(self.rows, {"one_day": [True, True, False, False]},
                                self.cfg, samples=50, family_size=11)
        self.assertIsNone(result["one_day"]["DAY"]["nominal_95"])
        self.assertEqual(result["one_day"]["DAY"]["selected_active_clusters"], 1)
        self.assertEqual(result["one_day"]["DAY"]["reference_active_clusters"], 2)

    def test_union_contrast_resamples_changed_common_signal_jointly(self):
        changed = [3, -2, 2, -1]
        result = joint_contrasts(self.rows, {"off": [True] * 4}, self.cfg, samples=100,
                                family_size=11, target_values={"off": changed})
        self.assertEqual(result["off"]["mean_r_difference"], 0.5)
        self.assertEqual(result["off"]["TRADE"]["valid_resamples"], 100)

    def test_invalid_masks_nan_and_unsupported_family_raise(self):
        for masks, family in (({"bad": [True]}, 11), (self.masks, 1)):
            with self.subTest(masks=masks), self.assertRaises(ValueError):
                joint_contrasts(self.rows, masks, self.cfg, samples=10, family_size=family)
        poisoned = row("nan", float("nan"))
        with self.assertRaisesRegex(ValueError, "Non-finite"):
            complete(poisoned)

    def test_train_boundary_crossing_is_not_leaked_into_train_only_metrics(self):
        plan = registered_plan()
        plan["splits"]["VALIDATION"]["start"] = T + 86400
        crossing = row("crossing", 9)
        crossing["closed_at"] = iso(T + 2 * 86400)
        result = cohort_periods([*self.rows, crossing], self.cfg, plan)
        self.assertEqual(result["TRAIN"]["trade_count"], 2)
        self.assertEqual(result["VALIDATION"]["trade_count"], 2)
        self.assertEqual(result["train_boundary_crossing_excluded_from_train_only"], ["crossing"])


if __name__ == "__main__":
    unittest.main()
