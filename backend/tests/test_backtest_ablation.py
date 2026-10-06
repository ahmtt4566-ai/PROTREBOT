import asyncio
import copy
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

from app import analysis, v25_execution as live  # noqa: E402
from app.backtest_ablation import (  # noqa: E402
    EXIT_VARIANTS, TIME_VARIANTS, PRESETS, Entry, ExitPosition, Variant,
    frozen_replay, paired_difference, prepare_entries, scheduled_replay, verify_control,
)
from app.backtest_baseline import Position, run  # noqa: E402
from backtest_ablation_cli import execute  # noqa: E402
from test_backtest_baseline import T, candle, config, dataset, signal, spec  # noqa: E402


def closed_dataset():
    data = dataset()
    for series in (data.frames["BTCUSDT"]["15m"], data.marks["BTCUSDT"]):
        series.at(T).update(high=101.5)
        series.at(T + 900).update(low=98.5)
    return data


def entry(direction="LONG", at=T, identifier="signal"):
    value = spec()
    value["direction"] = direction
    if direction == "SHORT":
        value.update(stop_loss="101", targets=["99", "98", "97"])
    ref = Position("BTCUSDT", direction, at, value, identifier, Decimal("0.0003"), Decimal(100)).row()
    return Entry(ref, value, {**signal(), "direction": direction}, 300, Decimal("0.01"), at)


def exit_position(variant="A", direction="LONG"):
    frozen = entry(direction)
    return ExitPosition("BTCUSDT", direction, T, copy.deepcopy(frozen.spec), "signal",
                        Decimal("0.0003"), Decimal(100), variant=PRESETS[variant], tick=Decimal("0.01"))


class FrozenInputTests(unittest.TestCase):
    def test_registered_parameters_default_old_and_time_stop_is_isolated(self):
        self.assertEqual(Variant(), EXIT_VARIANTS[0])
        self.assertEqual([v.time_bars for v in TIME_VARIANTS], [8, 16, 32])
        self.assertEqual({v.atr_multiplier for v in EXIT_VARIANTS if v.atr_multiplier}, {2, 3})
        for kwargs in ({"atr_multiplier": 2.5}, {"tp1_fraction": 0.45}, {"time_bars": 10},
                       {"time_bars": 8, "be": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Variant(**kwargs)

    def test_native_spec_signal_and_frozen_fill_risk_are_reused(self):
        data = closed_dataset()
        with patch.object(data, "canonical", return_value={"entry_eligible": True, "analysis": signal()}) as canonical:
            reference = run(data, config())
            with patch.object(live, "build_live_spec", wraps=live.build_live_spec) as native:
                entries = asyncio.run(prepare_entries(data, config(), reference))
            self.assertEqual(native.await_count, len(reference["trades"]))
            self.assertGreater(canonical.call_count, 0)
            result = frozen_replay(data, config(), entries, Variant())
            verify_control(result, reference)
            verify_control(scheduled_replay(data, config(), entries, Variant()), reference)
            for name in ("B", "C_ATR2", "D_ATR3", "E40", "F8"):
                changed = frozen_replay(data, config(), entries, PRESETS[name])
                for row, original in zip(changed["trades"], reference["trades"]):
                    for key in ("signal_id", "quantity", "actual_fill_price", "initial_risk_usdt", "opened_at"):
                        self.assertEqual(row[key], original[key])
        self.assertEqual(entries[0].spec["stop_loss"], "99")

    def test_tampered_frozen_quantity_or_control_pnl_is_rejected(self):
        data = closed_dataset()
        with patch.object(data, "canonical", return_value={"entry_eligible": True, "analysis": signal()}):
            reference = run(data, config())
            changed = copy.deepcopy(reference)
            changed["trades"][0]["quantity"] *= 2
            with self.assertRaisesRegex(ValueError, "mismatch.*quantity"):
                asyncio.run(prepare_entries(data, config(), changed))
            entries = asyncio.run(prepare_entries(data, config(), reference))
        result = frozen_replay(data, config(), entries, Variant())
        result["trades"][0]["net_pnl"] += 1
        with self.assertRaisesRegex(ValueError, "control trade differs"):
            verify_control(result, reference)

    def test_g_removes_only_short_and_paired_delta_has_no_selection_effect(self):
        data = closed_dataset()
        cfg = config()
        both = [entry(), entry("SHORT", identifier="short")]
        original = frozen_replay(data, cfg, both, Variant())
        changed = frozen_replay(data, cfg, both, PRESETS["G_NO_SHORT"])
        self.assertEqual(len(changed["trades"]), 1)
        self.assertEqual(changed["intentional_direction_exclusions"], ["short"])
        paired = paired_difference(changed["trades"], original["trades"], cfg)
        self.assertEqual(paired["matched_complete_pairs"], 1)
        self.assertEqual(paired["unmatched_baseline"], 1)
        self.assertEqual(paired["mean_delta_net_r"], 0)

    def test_e60_is_a_control_despite_different_experiment_label(self):
        data = closed_dataset()
        original = frozen_replay(data, config(), [entry()], Variant())
        changed = frozen_replay(data, config(), [entry()], PRESETS["E60"])
        verify_control(changed, original)


class ExitRuleTests(unittest.TestCase):
    def test_be_covers_per_unit_roundtrip_fee_and_exit_slip_long_and_short(self):
        for direction in ("LONG", "SHORT"):
            with self.subTest(direction=direction):
                value = exit_position("B", direction)
                price = value.fee_break_even()
                actual = price * (1 - value.sign * value.slip)
                net_unit = value.sign * (actual - value.actual_entry) - Decimal("0.0005") * (value.actual_entry + actual)
                self.assertGreaterEqual(net_unit, 0)
                previous = price
                value.funding_amount = Decimal("-100")
                self.assertEqual(value.fee_break_even(), previous)
                self.assertEqual(value.initial_risk, 3)

    def test_be_activates_only_after_tp1_on_next_bar_not_retroactively(self):
        data = dataset()
        value = exit_position("B")
        value.closed_update(data, T + 900)
        self.assertEqual(value.spec["stop_loss"], "99")
        value.advance(candle(T, high=101.5, low=99.5), candle(T, high=101.5, low=99.5),
                      "STOP_FIRST", opening_only=False)
        self.assertTrue(value.tp1_hit)
        self.assertEqual(value.spec["stop_loss"], "99")
        value.closed_update(data, T + 900)
        self.assertGreater(Decimal(value.spec["stop_loss"]), value.actual_entry)
        self.assertEqual(value.stop_updates[0]["applied_at"], "2025-04-01T00:15:00+00:00")
        self.assertEqual(value.stop_updates[0]["source_bar_open"], "2025-04-01T00:00:00+00:00")

    def test_trailing_uses_native_closed_atr_and_does_not_see_future_extremes(self):
        first, second = dataset(), dataset()
        first.marks["BTCUSDT"].at(T).update(high=105)
        second.marks["BTCUSDT"].at(T).update(high=105)
        for series in second.frames["BTCUSDT"].values():
            for row in series.rows:
                if row["time"] >= T + 900:
                    row.update(high=100000, low=0.1)
        second.marks["BTCUSDT"].at(T + 900).update(high=100000, low=0.1)
        results = []
        for data in (first, second):
            value = exit_position("C_ATR2")
            value.fill(Decimal(101), value.tp1_quantity, T + 899, "TP1")
            value.tp1_hit = True
            with patch.object(analysis, "atr", wraps=analysis.atr) as native:
                value.closed_update(data, T + 900)
            self.assertEqual(native.call_count, 1)
            closed = data.frames["BTCUSDT"]["15m"].closed(T + 900)
            self.assertEqual(native.call_args.args[0], [row["high"] for row in closed])
            self.assertEqual(closed[-1]["time"], T)
            results.append(value.spec["stop_loss"])
        self.assertEqual(results[0], results[1])

    def test_updated_stop_and_tp3_same_active_bar_stop_first_is_pessimistic(self):
        data = dataset()
        data.marks["BTCUSDT"].at(T).update(high=105)
        outcomes = []
        for ordering in ("STOP_FIRST", "TP_FIRST"):
            value = exit_position("C_ATR2")
            value.spec["targets"] = ["101", "105", "110"]
            value.fill(Decimal(101), value.tp1_quantity, T + 899, "TP1")
            value.tp1_hit = True
            with patch.object(analysis, "atr", return_value=1):
                value.closed_update(data, T + 900)
            self.assertEqual(Decimal(value.spec["stop_loss"]), Decimal(103))
            bar = candle(T + 900, opening=104, high=111, low=102, close=104)
            value.advance(bar, bar, ordering, opening_only=False)
            self.assertEqual(value.ambiguous_bars, 1)
            outcomes.append(value)
        self.assertEqual(outcomes[0].exits[-1]["reason"], "STOP")
        self.assertEqual(outcomes[1].exits[-1]["reason"], "TP3")
        self.assertLess(outcomes[0].row()["net_r"], outcomes[1].row()["net_r"])

    def test_trailing_never_loosens_and_combination_chooses_tighter_stop(self):
        data = dataset()
        for name in ("C_ATR2", "D_ATR2"):
            value = exit_position(name)
            value.fill(Decimal(101), value.tp1_quantity, T + 899, "TP1")
            value.tp1_hit = True
            with patch.object(analysis, "atr", return_value=10):
                value.closed_update(data, T + 900)
            first = Decimal(value.spec["stop_loss"])
            if name == "C_ATR2":
                self.assertEqual(first, 99)
            else:
                self.assertEqual(first, value.fee_break_even())
            with patch.object(analysis, "atr", return_value=100):
                value.closed_update(data, T + 1800)
            self.assertEqual(Decimal(value.spec["stop_loss"]), first)
            self.assertEqual(value.initial_risk, 3)

    def test_short_chandelier_ratchets_down_and_never_back_up(self):
        data = dataset()
        data.marks["BTCUSDT"].at(T).update(low=98.8)
        value = exit_position("C_ATR2", "SHORT")
        value.fill(Decimal(99), value.tp1_quantity, T + 899, "TP1")
        value.tp1_hit = True
        with patch.object(analysis, "atr", return_value=0.5):
            value.closed_update(data, T + 900)
        self.assertEqual(Decimal(value.spec["stop_loss"]), Decimal("99.8"))
        with patch.object(analysis, "atr", return_value=2):
            value.closed_update(data, T + 1800)
        self.assertEqual(Decimal(value.spec["stop_loss"]), Decimal("99.8"))
        self.assertEqual(value.initial_risk, 3)

    def test_step_rounding_tp1_40_50_60_and_minimum_are_preserved(self):
        quantities = []
        for name in ("E40", "E50", "E60"):
            value = exit_position(name)
            quantities.append(value.tp1_quantity)
        self.assertEqual(quantities, [Decimal("1.20"), Decimal("1.50"), Decimal("1.80")])
        value = spec()
        value.update(entry_price="100000", quantity="0.003", stop_loss="99000",
                     targets=["101000", "102000", "103000"], step=Decimal("0.001"),
                     min_qty=Decimal("0.001"), min_notional=Decimal(50))
        for name in ("E40", "E50", "E60"):
            pos = ExitPosition("BTCUSDT", "LONG", T, copy.deepcopy(value), "small",
                               Decimal("0.0003"), Decimal(100000), variant=PRESETS[name])
            self.assertEqual(pos.tp1_quantity, Decimal("0.001"))
        value["min_notional"] = Decimal(200)
        pos = ExitPosition("BTCUSDT", "LONG", T, value, "small", Decimal("0.0003"),
                           Decimal(100000), variant=PRESETS["E40"])
        self.assertEqual(pos.tp1_quantity, 0)
        self.assertEqual(pos.row()["tp_protection_state"], "TP1_UNPROTECTED_MINIMUM")

    def test_gap_after_be_can_still_lose_and_fill_is_next_contract_open(self):
        data = dataset()
        value = exit_position("B")
        value.fill(Decimal(101), value.tp1_quantity, T + 899, "TP1")
        value.tp1_hit = True
        value.closed_update(data, T + 900)
        bar = candle(T + 900, opening=90, high=91, low=89, close=90)
        value.advance(bar, bar, "STOP_FIRST", opening_only=True)
        self.assertEqual(value.exits[-1]["expected_price"], 90)
        self.assertLess(value.row()["net_r"], 0)


class TimeAndPairTests(unittest.TestCase):
    def test_time_stop_runs_after_exact_n_closed_bars_and_at_next_open(self):
        for bars in (8, 16, 32):
            with self.subTest(bars=bars):
                data = dataset(bars=40)
                for row in data.marks["BTCUSDT"].rows:
                    row.update(high=100.2, low=99.8)
                cfg = replace(config(), end=T + 40 * 900)
                result = frozen_replay(data, cfg, [entry()], PRESETS[f"F{bars}"])
                row = result["trades"][0]
                self.assertEqual(row["exits"][-1]["reason"], "TIME_STOP")
                self.assertEqual(row["exits"][-1]["time"], T + bars * 900)
                self.assertEqual(row["initial_risk_usdt"], 3)
                self.assertEqual(result["exit_groups"]["TIME_STOP"], 1)

    def test_time_progress_threshold_includes_equality_and_does_not_use_future_bar(self):
        data = dataset(bars=12)
        for row in data.marks["BTCUSDT"].rows:
            row.update(high=100.2, low=99.8)
        data.marks["BTCUSDT"].at(T).update(high=100.33)
        value = exit_position("F8")
        for at in range(T + 900, T + 9 * 900, 900):
            value.closed_update(data, at)
        self.assertEqual(value.best_progress_r, Decimal("0.3"))
        self.assertFalse(value.time_due)
        data.marks["BTCUSDT"].at(T).update(high=100.2)
        data.marks["BTCUSDT"].at(T + 8 * 900).update(high=100000)
        cfg = replace(config(), end=T + 12 * 900)
        row = frozen_replay(data, cfg, [entry()], PRESETS["F8"])["trades"][0]
        self.assertEqual(row["exits"][-1]["time"], T + 8 * 900)
        self.assertEqual(row["exits"][-1]["reason"], "TIME_STOP")

    def test_time_trigger_does_not_override_original_stop_on_same_open(self):
        value = exit_position("F8")
        value.time_due = True
        bar = candle(T + 7200, opening=98, high=99, low=97, close=98)
        value.advance(bar, bar, "STOP_FIRST", opening_only=True)
        self.assertEqual(value.exits[-1]["reason"], "STOP")

    def test_paired_bootstrap_uses_actual_same_entry_delta_not_unpaired_means(self):
        old, new = [], []
        for index, delta in enumerate((1, -0.5, 0.25)):
            frozen = entry(at=T + index * 86400, identifier=str(index))
            row = copy.deepcopy(frozen.reference)
            row.update(status="CLOSED", closed_at=iso_time(T + index * 86400 + 899),
                       net_r=-1, net_pnl=-3, funding_usdt=0, ambiguous_bars=0)
            old.append(row)
            changed = {**row, "net_r": -1 + delta, "net_pnl": -3 + delta * 3}
            new.append(changed)
        cfg = replace(config(), end=T + 4 * 86400)
        result = paired_difference(new, old, cfg)
        self.assertAlmostEqual(result["mean_delta_net_r"], 0.25)
        self.assertEqual(result["matched_complete_pairs"], 3)
        self.assertIsNotNone(result["trade_delta_r_95"])
        self.assertIsNotNone(result["day_delta_r_95"])
        self.assertEqual(result, paired_difference(new, old, cfg))
        new[0]["quantity"] *= 2
        with self.assertRaisesRegex(ValueError, "changed entry/risk"):
            paired_difference(new, old, cfg)

    def test_missing_net_r_pair_is_null_not_fabricated(self):
        row = entry().reference
        result = paired_difference([row], [row], config())
        self.assertEqual(result["matched_complete_pairs"], 0)
        self.assertIsNone(result["mean_delta_net_r"])
        self.assertEqual(result["incomplete_pair_ids"], ["signal"])


def iso_time(at):
    from app.backtest_baseline import iso
    return iso(at)


class CliAndPortfolioTests(unittest.TestCase):
    def test_portfolio_gates_are_separate_from_frozen_entries(self):
        data = dataset()
        first = entry()
        second = entry(at=T + 900, identifier="overlap")
        original = second.reference
        # Two overlapping same-symbol inputs intentionally test the counterfactual audit.
        primary = frozen_replay(data, config(), [first, second], Variant())
        portfolio = scheduled_replay(data, config(), [first, second], Variant())
        self.assertEqual(len(primary["trades"]), 2)
        self.assertEqual(len(portfolio["trades"]), 1)
        self.assertIn("duplicate", portfolio["rejected_entries"][0]["reasons"])
        self.assertEqual(portfolio["rejected_entries"][0]["signal_id"], original["signal_id"])

    def test_cli_controls_all_26_runs_time_last_and_no_post_result_parameter_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = closed_dataset()
            with patch.object(data, "canonical", return_value={"entry_eligible": True, "analysis": signal()}):
                reference = run(data, config())
                baseline = root / "baseline.json"
                baseline.write_text(json.dumps(reference), encoding="utf-8")
                args = SimpleNamespace(output=root / "output", data=root, metadata=root / "meta.json",
                                       baseline=baseline, initial_equity=1000, bootstrap_samples=50,
                                       conditional_current_metadata=True, all_variants=True,
                                       include_time_stop=True, tp_first_sensitivity=True)
                args.metadata.write_text(json.dumps(data.metadata), encoding="utf-8")
                with patch("backtest_ablation_cli.load_dataset", return_value=data):
                    report = execute(args)
            self.assertEqual(report["multiple_comparisons"]["primary_runs_inspected"], 26)
            self.assertEqual(report["multiple_comparisons"]["noncontrol_configurations"], 11)
            self.assertFalse(report["multiple_comparisons"]["parameters_changed_after_results"])
            self.assertEqual(len(report["tp_first_sensitivity"]), 13)
            rows = report["comparison"]
            self.assertTrue(all(row["group"] == "exits-and-direction" for row in rows[:20]))
            self.assertTrue(all(row["group"] == "time-stop-last" for row in rows[20:]))
            self.assertTrue((args.output / "registered-plan.json").exists())
            self.assertTrue((args.output / "time-stop-last" / "F32-tp_first-paired.csv").exists())
            self.assertEqual(report["runs"]["A-stop_first"]["frozen"]["paired_vs_control"]["mean_delta_net_r"], 0)

    def test_repository_output_or_time_without_prior_exits_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside Git"):
            execute(SimpleNamespace(output=Path(__file__).parents[2] / "ablation-output"))
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "follow"):
                execute(SimpleNamespace(output=Path(temporary), include_time_stop=True, all_variants=False))


if __name__ == "__main__":
    unittest.main()
