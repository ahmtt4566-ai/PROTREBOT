import copy
import gzip
import json
import math
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))

from app import execution_core as core, main
from app.backtest_baseline import iso, metrics
from app.holdout_statistics import bootstrap, pf_interval
from app.holdout_study import (
    candidate_masks, claim_once, criteria, describe, period_scopes, register, registered_plan,
    scoped_rows, verify_inputs,
)
from holdout_cli import HoldoutDataset, bundle, evaluate_candidates, execute, precompute, signal_worker
from test_backtest_ablation import closed_dataset
from test_backtest_baseline import T, config, dataset
from test_backtest_baseline import signal
from test_regime_study import row


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.anchor = tempfile.TemporaryDirectory()
        self.addCleanup(self.anchor.cleanup)
        patcher = patch("app.holdout_study.anchor_directory", return_value=Path(self.anchor.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_registered_candidates_criteria_matrix_and_old_test_are_exact(self):
        plan = registered_plan()
        self.assertEqual([x["name"] for x in plan["candidates"]], ["B", "B_ADX4H20", "B_NO_SHORT"])
        self.assertEqual(plan["success"], {
            "net_expectancy_r_strict_gt": 0, "usdt_profit_factor_strict_gt": 1.2,
            "complete_trades_min": 60,
        })
        self.assertEqual((plan["test"]["start"], plan["test"]["end"]), (1696118400, 1719792000))
        self.assertEqual(len(plan["costs"]) * len(plan["orderings"]) * len(plan["candidates"]), 24)
        self.assertEqual(plan["bootstrap"]["family_size"], 3)
        self.assertEqual(plan["bootstrap"]["samples"], 20000)
        self.assertFalse(plan["rerun_after_result_or_failure"])
        self.assertFalse(plan["stage7_authorized"])
        self.assertEqual(period_scopes(plan)["overlap_seconds"], 0)

    def test_registration_idempotent_cannot_overwrite_or_change_output_root(self):
        with tempfile.TemporaryDirectory() as temporary, patch("app.holdout_study.verify_inputs"):
            stage5 = Path(temporary) / "stage5"
            output = Path(temporary) / "stage6"
            locked = register(stage5, output)
            self.assertEqual(register(stage5, output), locked)
            with self.assertRaisesRegex(ValueError, "fixed external"):
                register(stage5, Path(temporary) / "another-output")
            altered = copy.deepcopy(locked)
            altered["plan"]["success"]["complete_trades_min"] = 59
            (output / "holdout-lock.json").write_text(json.dumps(altered))
            with self.assertRaisesRegex(ValueError, "refusing"):
                register(stage5, output)
            self.assertEqual(json.loads((output / "holdout-lock.json").read_text()), altered)

    def test_input_mismatch_rejected_without_reading_any_zip(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage5 = Path(temporary)
            (stage5 / "study-lock.json").write_text("{}")
            with patch("app.backtest_data.read_rows", side_effect=AssertionError("TEST opened")) as read:
                with self.assertRaisesRegex(ValueError, "Registered input changed"):
                    verify_inputs(stage5, registered_plan())
            read.assert_not_called()

    def test_no_claim_before_registration_and_second_claim_refused(self):
        with tempfile.TemporaryDirectory() as temporary, patch("app.holdout_study.verify_inputs"):
            stage5, output = Path(temporary) / "stage5", Path(temporary) / "stage6"
            with self.assertRaisesRegex(ValueError, "Preregister"):
                claim_once(stage5, output, {})
            register(stage5, output)
            claim_once(stage5, output, {"module": "hash"})
            with self.assertRaisesRegex(ValueError, "already claimed"):
                claim_once(stage5, output, {"module": "hash"})
            self.assertFalse(json.loads((output / "test-once.json").read_text())["rerun_allowed"])

    def test_relocation_of_identical_inputs_cannot_create_second_registration(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second, \
             patch("app.holdout_study.verify_inputs"):
            register(Path(first) / "stage5", Path(first) / "stage6")
            with self.assertRaisesRegex(ValueError, "no relocation rerun"):
                register(Path(second) / "stage5", Path(second) / "stage6")

    def test_failed_attempt_is_consumed_and_no_second_loader_call(self):
        with tempfile.TemporaryDirectory() as temporary, patch("app.holdout_study.verify_inputs"), \
             patch("holdout_cli.deny_network"), patch("holdout_cli.load_dataset", side_effect=OSError("fixture failure")) as loader:
            stage5, output = Path(temporary) / "stage5", Path(temporary) / "stage6"
            register(stage5, output)
            with self.assertLogs("holdout_cli", level="ERROR"), self.assertRaisesRegex(OSError, "fixture failure"):
                execute(stage5, output, 1)
            self.assertEqual(json.loads((output / "test-once.json").read_text())["state"], "FAILED")
            with self.assertRaisesRegex(ValueError, "already claimed"):
                execute(stage5, output, 1)
            self.assertEqual(loader.call_count, 1)

    def test_successful_attempt_also_refuses_rerun(self):
        with tempfile.TemporaryDirectory() as temporary, patch("app.holdout_study.verify_inputs"), \
             patch("holdout_cli.verify_inputs") as verify, patch("holdout_cli.deny_network"), \
             patch("holdout_cli.bundle") as run:
            stage5, output = Path(temporary) / "stage5", Path(temporary) / "stage6"
            register(stage5, output)

            def finish(*_args):
                (output / "holdout-results.json").write_text('{"completed": true}')
                return {"completed": True}

            run.side_effect = finish
            self.assertEqual(execute(stage5, output, 1), {"completed": True})
            self.assertEqual(json.loads((output / "test-once.json").read_text())["state"], "COMPLETED")
            with self.assertRaisesRegex(ValueError, "results already"):
                execute(stage5, output, 1)
            self.assertEqual(run.call_count, 1)
            verify.assert_called_once()


class CriteriaTests(unittest.TestCase):
    def value(self, expectancy=0.1, pf=1.3, count=60):
        return {"net_expectancy_r": expectancy, "profit_factor": pf, "r_eligible_count": count,
                "profit_factor_status": "FINITE", "closed_curve_net_pnl": 10}

    def test_exact_strict_thresholds_and_count_boundary(self):
        plan = registered_plan()
        self.assertTrue(criteria(self.value(), plan)["passed"])
        for overrides in ({"expectancy": 0}, {"pf": 1.2}, {"count": 59},
                          {"expectancy": None}, {"pf": None}, {"expectancy": math.nan}):
            with self.subTest(overrides=overrides):
                result = criteria(self.value(**overrides), plan)
                self.assertFalse(result["passed"])
                self.assertEqual(result["label"], "BA\u015eARISIZ")
                self.assertTrue(result["failed_criteria"])

    def test_positive_no_loss_pf_unbounded_not_zero_gain_undefined(self):
        value = self.value(pf=None)
        value["profit_factor_status"] = "NO_LOSSES"
        self.assertTrue(criteria(value, registered_plan())["passed"])
        value["closed_curve_net_pnl"] = 0
        self.assertFalse(criteria(value, registered_plan())["passed"])

    def test_missing_r_not_fabricated_or_counted(self):
        value = row("missing", 1)
        value.update(net_r=None, net_pnl=None, funding_usdt=None)
        summary = describe([value], config(), registered_plan())
        self.assertIsNone(summary["net_expectancy_r"])
        self.assertEqual(summary["r_eligible_count"], 0)
        self.assertEqual(summary["excluded_from_r_count"], 1)
        self.assertFalse(summary["criteria"]["passed"])

    def test_nonfinite_accounting_rejected_explicitly(self):
        with self.assertRaisesRegex(ValueError, "Non-finite"):
            describe([row("nan", math.nan)], config(), registered_plan())


class ScopeTests(unittest.TestCase):
    def plan(self):
        plan = registered_plan()
        plan["test"] = {"start": T, "end": T + 4 * 86400}
        plan["seen_periods"] = [
            {"start": iso(T + 86400), "end": iso(T + 3 * 86400)},
            {"start": iso(T + 2 * 86400), "end": iso(T + 3 * 86400)},
        ]
        return plan

    def test_overlap_union_and_clean_intervals_without_second_replay(self):
        scopes = period_scopes(self.plan())
        self.assertEqual(scopes["partly_seen"], [(T + 86400, T + 3 * 86400)])
        self.assertEqual(scopes["clean"], [(T, T + 86400), (T + 3 * 86400, T + 4 * 86400)])
        self.assertEqual(scopes["overlap_seconds"], 2 * 86400)
        rows = [row("clean1", 1), row("seen", -1, 1), row("clean2", 1, 3)]
        groups = scoped_rows(rows, scopes)
        self.assertEqual([x["signal_id"] for x in groups["clean"]], ["clean1", "clean2"])
        self.assertIs(groups["full"], rows)

    def test_trade_crossing_into_seen_period_not_labelled_clean(self):
        trade = row("crossing", 1)
        trade["closed_at"] = iso(T + 86400)
        groups = scoped_rows([trade], period_scopes(self.plan()))
        self.assertEqual(groups["clean"], [])
        self.assertEqual(groups["partly_seen"], [trade])

    def test_cross_boundary_partly_seen_bootstrap_keeps_actual_entry_day(self):
        plan = self.plan()
        plan["bootstrap"]["samples"] = 20
        crossing = row("crossing", 1)
        crossing["closed_at"] = iso(T + 86400)
        rows = [crossing, row("seen", -1, 1)]
        with patch("app.holdout_study.closed_features", return_value=(20, 0, 0)):
            result, _ = evaluate_candidates(rows, dataset(), replace(config(), end=T + 4 * 86400),
                                             plan, period_scopes(plan))
        partly_seen = result["B"]["scopes"]["partly_seen"]
        self.assertEqual(partly_seen["summary"]["trade_count"], 2)
        self.assertEqual(partly_seen["bootstrap"]["DAY"]["active_clusters"], 2)

    def test_unseen_holdout_clean_reuses_same_observations(self):
        plan = self.plan()
        plan["seen_periods"] = []
        scopes = period_scopes(plan)
        rows = [row("fixture", 1)]
        groups = scoped_rows(rows, scopes)
        self.assertIs(groups["full"], groups["clean"])
        self.assertEqual(groups["partly_seen"], [])


class BootstrapTests(unittest.TestCase):
    def settings(self):
        return {**registered_plan()["bootstrap"], "samples": 300}

    def test_deterministic_nominal_and_corrected_intervals_both_units(self):
        rows = [row("a", -1), row("b", 2, 1), row("c", 0.5, 2), row("d", -0.2, 3)]
        intervals = [(T, T + 10 * 86400)]
        result = bootstrap(rows, intervals, self.settings())
        self.assertEqual(result, bootstrap(rows, intervals, self.settings()))
        for unit in ("TRADE", "DAY"):
            value = result[unit]
            nominal = value["nominal_95"]["expectancy_r"]
            adjusted = value["bonferroni_family_95"]["expectancy_r"]
            self.assertLessEqual(adjusted[0], nominal[0])
            self.assertGreaterEqual(adjusted[1], nominal[1])
            self.assertEqual(value["family_size"], 3)
        self.assertEqual(result["DAY"]["calendar_or_trade_clusters"], 10)

    def test_usdt_pf_is_not_r_profit_factor(self):
        first, second = row("a", 2), row("b", -1, 1)
        first["net_pnl"], second["net_pnl"] = 1, -2
        self.assertEqual(metrics([first, second], 1000)["profit_factor"], 0.5)
        self.assertEqual(bootstrap([first, second], [(T, T + 2 * 86400)], self.settings())["TRADE"]["pf_unit"], "USDT")

    def test_unbounded_pf_not_serialized_as_false_zero_or_nan(self):
        result = pf_interval([1.0, math.inf], 0.05)
        self.assertEqual(result["bounds"], [1.0, None])
        self.assertTrue(result["upper_unbounded"])
        json.dumps(result, allow_nan=False)

    def test_empty_single_day_and_nonfinite_inputs(self):
        self.assertIsNone(bootstrap([], [], self.settings())["TRADE"]["nominal_95"]["expectancy_r"])
        one = bootstrap([row("one", 1)], [(T, T + 86400)], self.settings())
        self.assertIsNone(one["DAY"]["nominal_95"]["expectancy_r"])
        self.assertEqual(one["DAY"]["status"], "INSUFFICIENT_COMPLETE_TRADES_OR_DAY_CLUSTERS")
        with self.assertRaisesRegex(ValueError, "Non-finite"):
            bootstrap([row("bad", math.nan)], [(T, T + 86400)], self.settings())
        with self.assertRaisesRegex(ValueError, "outside declared"):
            bootstrap([row("outside", 1, 2)], [(T, T + 86400)], self.settings())


class NativeCohortTests(unittest.TestCase):
    def test_spawn_cache_pipeline_matches_native_and_has_no_fallback(self):
        original = dataset()
        data = HoldoutDataset(original.frames, original.marks, original.funding, original.metadata,
                              original.report, original.funding_months)
        cfg = config(policy={"allowed_symbols": ["BTCUSDT"]})
        policy = core.sanitize_execution_policy(cfg.policy)
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            precompute(data, cfg, 1, cache, "synthetic-spawn")
            for at in (T, T + 900):
                self.assertEqual(data.canonical("BTCUSDT", at, policy), original.canonical("BTCUSDT", at, policy))
            with self.assertRaisesRegex(ValueError, "not empty"):
                precompute(data, cfg, 1, cache, "synthetic-spawn")

    def test_worker_calls_same_native_canonical_with_only_closed_frames(self):
        data = dataset()
        policy = core.sanitize_execution_policy({})
        with tempfile.TemporaryDirectory() as temporary, patch("holdout_cli.deny_network"), \
             patch.object(main, "canonical_historical_decision", wraps=main.canonical_historical_decision) as native:
            signal_worker("BTCUSDT", data.frames["BTCUSDT"], config(), policy, Path(temporary), "fixture")
            self.assertEqual(native.call_count, 2)
            for call in native.call_args_list:
                at = call.args[2]
                for interval, rows in call.args[1].items():
                    duration = {"15m": 900, "1h": 3600, "4h": 14400}[interval]
                    self.assertTrue(all(x["time"] + duration <= at for x in rows))
            with gzip.open(Path(temporary) / "BTCUSDT.jsonl.gz", "rt") as stream:
                values = [json.loads(line) for line in stream]
            self.assertEqual(values[0]["signal_mode"], "NATIVE_SHORT_GATE_ON_ONLY")
            self.assertEqual([x["at"] for x in values[1:]], [T, T + 900])

    def test_missing_cache_fails_instead_of_recomputing_test_signal(self):
        original = dataset()
        data = HoldoutDataset(original.frames, original.marks, original.funding, original.metadata,
                              original.report, original.funding_months)
        with patch.object(main, "canonical_historical_decision") as native:
            with self.assertRaisesRegex(ValueError, "Missing verified"):
                data.canonical("BTCUSDT", T, core.sanitize_execution_policy({}))
        native.assert_not_called()

    def test_masks_remove_only_with_adx_exact_boundary_and_unknown_reason(self):
        rows = [row("a", 1), row("b", -1, direction="SHORT"), row("c", 1)]
        with patch("app.holdout_study.closed_features", side_effect=[(20, 0, 0), (19.99, 0, 0), (None, None, None)]):
            masks, evidence = candidate_masks(rows, dataset(), registered_plan())
        self.assertEqual(masks, {"B": [True, True, True], "B_ADX4H20": [True, False, False],
                                 "B_NO_SHORT": [True, False, True]})
        self.assertEqual(evidence["unknown_adx_rejected_signal_ids"], ["c"])
        self.assertEqual(evidence["added_entries"], 0)

    def test_forming_4h_bar_cannot_change_filter(self):
        data = dataset()
        first = candidate_masks([row("a", 1)], data, registered_plan())
        data.frames["BTCUSDT"]["4h"].at(T).update(high=10000, close=9999)
        self.assertEqual(candidate_masks([row("a", 1)], data, registered_plan()), first)

    def test_full_and_clean_share_bootstrap_when_no_overlap(self):
        plan = registered_plan()
        plan["test"] = {"start": T, "end": T + 86400}
        plan["seen_periods"] = []
        rows = [row("a", 1)]
        with patch("app.holdout_study.closed_features", return_value=(20, 0, 0)), \
             patch("holdout_cli.bootstrap", return_value={"fixture": True}) as draw:
            results, _ = evaluate_candidates(rows, dataset(), replace(config(), end=T + 86400), plan, period_scopes(plan))
        self.assertEqual(draw.call_count, 3)
        self.assertIs(results["B"]["scopes"]["full"], results["B"]["scopes"]["clean"])

    def test_synthetic_complete_bundle_24_cells_same_run_no_live_order(self):
        native = dataset()
        plan = registered_plan()
        plan["test"] = {"start": T, "end": T + 1800}
        plan["symbols"] = ["BTCUSDT"]
        plan["seen_periods"] = []
        plan["bootstrap"]["samples"] = 20
        native.report = {"missing_archives": [], "symbols": {"BTCUSDT": {
            kind: {"missing_candles": 0, "duplicates": 0, "gaps": []}
            for kind in ("klines_15m", "klines_1h", "klines_4h", "markPriceKlines_15m")}}}

        def signals(data, cfg, *_args):
            policy = core.sanitize_execution_policy(cfg.policy)
            for at in range(cfg.start, cfg.end, 900):
                key = ("BTCUSDT", at, policy["min_confidence"], policy["mtf_allow_either_timeframe"])
                data.decisions[key] = {"entry_eligible": False, "reason": "SYNTHETIC_WAIT"}

        with tempfile.TemporaryDirectory() as temporary, patch("holdout_cli.load_dataset", return_value=native), \
             patch("holdout_cli.precompute", side_effect=signals), \
             patch("app.v25_execution.execute_live_order", side_effect=AssertionError("LIVE order")) as live:
            output = Path(temporary)
            (output / "test-once.json").write_text('{"fixture": true}')
            result = bundle(output, output, {"plan": plan, "sha256": "fixture"}, 1)
            self.assertEqual(result["test_bundle_runs"], 1)
            self.assertEqual(result["candidate_scenarios_inspected"], 24)
            self.assertEqual(len(result["results"]), 8)
            self.assertTrue((output / "holdout-results.json").exists())
            self.assertTrue((output / "comparison.csv").exists())
        live.assert_not_called()

    def test_nonempty_cost_bundle_reuses_native_spec_and_immutable_risk(self):
        native = closed_dataset()
        plan = registered_plan()
        plan["test"] = {"start": T, "end": T + 1800}
        plan["symbols"] = ["BTCUSDT"]
        plan["seen_periods"] = []
        plan["bootstrap"]["samples"] = 20
        native.report = {"missing_archives": [], "symbols": {"BTCUSDT": {
            kind: {"missing_candles": 0, "duplicates": 0, "gaps": []}
            for kind in ("klines_15m", "klines_1h", "klines_4h", "markPriceKlines_15m")}}}

        def signals(data, cfg, *_args):
            policy = core.sanitize_execution_policy(cfg.policy)
            for at in range(cfg.start, cfg.end, 900):
                key = ("BTCUSDT", at, policy["min_confidence"], policy["mtf_allow_either_timeframe"])
                data.decisions[key] = {"entry_eligible": True, "analysis": signal()}

        from app import v25_execution as live
        with tempfile.TemporaryDirectory() as temporary, patch("holdout_cli.load_dataset", return_value=native), \
             patch("holdout_cli.precompute", side_effect=signals), \
             patch("app.holdout_study.closed_features", return_value=(20, 0, 0)), \
             patch.object(live, "build_live_spec", wraps=live.build_live_spec) as spec, \
             patch.object(live, "execute_live_order", side_effect=AssertionError("LIVE order")) as send:
            output = Path(temporary)
            (output / "test-once.json").write_text('{"fixture": true}')
            result = bundle(output, output, {"plan": plan, "sha256": "fixture"}, 1)
            self.assertGreater(spec.await_count, 0)
            self.assertGreater(result["results"]["slip3-spread2-stop_first"]["candidates"]["B"]["scopes"]["full"]["summary"]["r_eligible_count"], 0)
            for scenario in result["results"].values():
                ids = scenario["candidates"]["B"]["scopes"]["full"]["signal_ids"]
                self.assertEqual(scenario["candidates"]["B_ADX4H20"]["scopes"]["full"]["signal_ids"], ids)
                self.assertEqual(scenario["candidates"]["B_NO_SHORT"]["scopes"]["full"]["signal_ids"], ids)
                self.assertFalse(scenario["candidates"]["B"]["scopes"]["full"]["summary"]["criteria"]["passed"])
            for path in output.glob("entry-cohort-*.json"):
                entries = json.loads(path.read_text())["native_entry_schedule"]
                self.assertTrue(all(row["initial_risk_usdt"] > 0 for row in entries))
        send.assert_not_called()
