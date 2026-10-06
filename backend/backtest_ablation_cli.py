"""Offline, preregistered exit ablations; time stops are opt-in and run last."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

from app.backtest_ablation import (
    EXIT_VARIANTS, TIME_VARIANTS, frozen_replay, paired_difference, portfolio_changes,
    prepare_entries, scheduled_replay, verify_control,
)
from app.backtest_baseline import Config
from app.backtest_data import load_dataset
from backtest_cli import cached_native_analysis, epoch_iso, export_trades
from backtest_diagnostics_cli import write_json, write_table


def execute(args) -> dict:
    root = args.output.resolve()
    if root.is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("Ablation outputs must be outside Git")
    if args.include_time_stop and not args.all_variants:
        raise ValueError("Time stops must follow the full exit/direction experiment")
    reference_bytes = args.baseline.read_bytes()
    reference = json.loads(reference_bytes)
    if reference["intrabar"] != "STOP_FIRST" or reference["spread_bps"] != 2 or reference["slippage_bps"] != 3:
        raise ValueError("Ablation requires Stage 3 spread2/slip3/STOP_FIRST baseline")
    if reference["policy"]["max_total_exposure_usdt"] != 350:
        raise ValueError("Ablation requires exposure 350")
    config = Config(
        epoch_iso(reference["period"]["start_inclusive"]), epoch_iso(reference["period"]["end_exclusive"]),
        initial_equity=args.initial_equity, bootstrap_samples=args.bootstrap_samples,
        conditional_current_metadata=args.conditional_current_metadata, policy=reference["policy"])
    if not reference["trades"]:
        raise ValueError("Frozen baseline cohort is empty")
    data = load_dataset(args.data.resolve(), args.metadata.resolve(), config.start, config.end)
    root.mkdir(parents=True, exist_ok=True)
    groups = [("exits-and-direction", EXIT_VARIANTS if args.all_variants else EXIT_VARIANTS[:1])]
    if args.include_time_stop:
        groups.append(("time-stop-last", TIME_VARIANTS))
    orderings = ("STOP_FIRST", "TP_FIRST") if args.tp_first_sensitivity else ("STOP_FIRST",)
    plan = {
        "baseline_sha256": hashlib.sha256(reference_bytes).hexdigest(),
        "metadata_sha256": hashlib.sha256(args.metadata.read_bytes()).hexdigest(),
        "period": reference["period"], "configurations": [asdict(v) for _, group in groups for v in group],
        "orderings": orderings, "time_stop_last": True,
        "be_definition": "Actual entry + both 5bp fees + 3bp exit slip; funding only in net PnL.",
        "trailing_definition": "Post-TP1 closed MARK_PRICE high/low Chandelier; native contract ATR(14), 2/3 fixed.",
        "activation": "Prior closed-bar stop update active at following bar open; STOP_FIRST on active-bar ties.",
        "time_definition": "After N complete holding bars, close next open unless historical closed-mark MFE >= 0.3 initial R.",
        "bootstrap_samples": config.bootstrap_samples, "seed": config.seed,
        "registered_before_results": True,
    }
    write_json(root / "registered-plan.json", plan)
    write_json(root / "data-quality.json", data.report)
    print(f"ABLATION_PREPARE frozen_entries={len(reference['trades'])}", flush=True)
    with cached_native_analysis():
        entries = asyncio.run(prepare_entries(data, config, reference))
    controls = {}
    all_results, tables = {}, []
    for group_name, variants in groups:
        directory = root / group_name
        directory.mkdir(exist_ok=True)
        for ordering in orderings:
            current_config = replace(config, intrabar=ordering)
            for variant in variants:
                name = f"{variant.name}-{ordering.lower()}"
                print(f"ABLATION_START {name}", flush=True)
                frozen = frozen_replay(data, current_config, entries, variant)
                portfolio = scheduled_replay(data, current_config, entries, variant)
                if variant.name == "A":
                    if ordering == "STOP_FIRST":
                        verify_control(frozen, reference)
                        verify_control(portfolio, reference)
                    controls[ordering] = frozen["trades"]
                if variant.name == "E60":
                    control = all_results[f"A-{ordering.lower()}"]["frozen"]
                    verify_control(frozen, control)
                frozen["paired_vs_control"] = paired_difference(frozen["trades"], controls[ordering], current_config)
                portfolio["trade_set_vs_frozen"] = portfolio_changes(portfolio["trades"], frozen["trades"], current_config)
                portfolio["paired_vs_frozen"] = paired_difference(portfolio["trades"], frozen["trades"], current_config)
                result = {"experiment_group": group_name, "name": name, "frozen": frozen, "portfolio": portfolio}
                all_results[name] = result
                write_json(directory / f"{name}.json", result)
                export_trades(frozen["trades"], directory / f"{name}-trades.csv")
                export_trades(frozen["paired_vs_control"]["pairs"], directory / f"{name}-paired.csv")
                export_trades(portfolio["trades"], directory / f"{name}-portfolio-trades.csv")
                summary, paired = frozen["summary"], frozen["paired_vs_control"]
                table = {"variant": variant.name, "ordering": ordering, "group": group_name,
                         **{key: summary[key] for key in (
                             "trade_count", "net_expectancy_r", "profit_factor", "win_rate",
                             "max_drawdown_usdt", "funding_null_count")},
                         **frozen["exit_groups"], "matched_pairs": paired["matched_complete_pairs"],
                         "mean_delta_net_r": paired["mean_delta_net_r"],
                         "trade_delta_r_95": json.dumps(paired["trade_delta_r_95"]),
                         "day_delta_r_95": json.dumps(paired["day_delta_r_95"]),
                         "portfolio_trade_count": portfolio["summary"]["trade_count"],
                         "portfolio_rejected_count": len(portfolio["rejected_entries"])}
                tables.append(table)
                print(f"ABLATION_DONE {json.dumps(table, allow_nan=False)}", flush=True)
    sensitivities = []
    if args.tp_first_sensitivity:
        for _, variants in groups:
            for variant in variants:
                stop = all_results[f"{variant.name}-stop_first"]["frozen"]
                profit = all_results[f"{variant.name}-tp_first"]["frozen"]
                sensitivities.append({
                    "variant": variant.name, "stop_first": stop["summary"], "tp_first": profit["summary"],
                    "paired_tp_first_minus_stop_first": paired_difference(profit["trades"], stop["trades"], config),
                })
    count = sum(len(variants) for _, variants in groups)
    report = {
        "label": "CONDITIONAL FROZEN-ENTRY EXIT ABLATION / NOT LIVE STRATEGY",
        "plan": plan, "assumptions": reference["assumptions"],
        "multiple_comparisons": {
            "registered_configurations_including_controls": count,
            "noncontrol_configurations": count - 1 - int(args.all_variants),
            "primary_runs_inspected": len(all_results), "portfolio_audits_inspected": len(all_results),
            "parameters_changed_after_results": False,
            "warning": "Exploratory unadjusted 95% intervals; no family-wise correction, selection or out-of-sample proof.",
        },
        "limitations": [
            "Same native baseline inputs/specs/fills/initial risk; only offline exits change.",
            "Primary cohort is independent counterfactual trades, not a risk-gated executable portfolio.",
            "G deliberately excludes baseline SHORT entries; matched exit delta excludes selection effect.",
            "Portfolio audit retries only the ordered baseline entries with native gates, not previously rejected/new signals.",
            "Stops only tighten; updates use previous CLOSED data and are never retroactively filled against that source bar.",
            "Same active execution bar stop/TP overlap uses requested ordering; opening gaps fill contract open plus adverse slip.",
            "Fee BE covers remaining unit entry/exit fees and exit slip, not funding or unpredictable gaps.",
            "Chandelier peak starts in the closed TP1 bar; price-protection/cancel-replace/API failures are not simulated.",
            "Funding and current rules/tier/full-fill approximations are inherited; no actual orders/network calls.",
            "TIME_STOP is a separate fourth exit category, never disguised as an exchange Stop.",
            "No parameter tuning, train/test selection, Stage 5 filter or future-profit claim.",
        ],
        "comparison": tables,
        "runs": {key: {**value, "frozen": {k: v for k, v in value["frozen"].items() if k != "trades"},
                       "portfolio": {k: v for k, v in value["portfolio"].items() if k != "trades"}}
                 for key, value in all_results.items()},
        "tp_first_sensitivity": sensitivities,
    }
    write_json(root / "ablation.json", report)
    write_table(root / "comparison.csv", tables)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--initial-equity", type=float, default=1000)
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--conditional-current-metadata", action="store_true")
    parser.add_argument("--all-variants", action="store_true")
    parser.add_argument("--tp-first-sensitivity", action="store_true")
    parser.add_argument("--include-time-stop", action="store_true")
    execute(parser.parse_args())


if __name__ == "__main__":
    main()
