"""Stage 3b offline exposure/exit/month/cluster diagnosis; no strategy tuning."""

from __future__ import annotations

import argparse
import csv
import json
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from app import execution_core as core
from app.backtest_baseline import Config
from app.backtest_data import Dataset, Series, load_dataset
from app.backtest_diagnostics import (
    CAPS, excursions, exit_distribution, monthly_distribution, replay_cap, trade_set_changes,
)
from backtest_cli import cached_native_analysis, epoch_iso, export_trades


def symbol_decisions(symbol: str, frames: dict[str, Series], config: Config, policy: dict) -> tuple[str, dict]:
    data = Dataset({symbol: frames}, {}, {}, {}, {}, {})
    with cached_native_analysis():
        for at in range(config.start, config.end, 900):
            if all(series.history_complete(at) for series in frames.values()):
                data.canonical(symbol, at, policy)
    return symbol, data.decisions


def precompute(data: Dataset, config: Config, workers: int) -> None:
    if not 1 <= workers <= 4:
        raise ValueError("Offline diagnosis allows 1 to 4 workers")
    policy = core.sanitize_execution_policy(config.policy, preserve_empty_allowed_symbols=True)
    symbols = sorted(set(policy["allowed_symbols"]) & set(data.frames))
    print(f"DECISIONS_START symbols={len(symbols)} workers={workers}", flush=True)
    if workers == 1:
        for symbol in symbols:
            _, decisions = symbol_decisions(symbol, data.frames[symbol], config, policy)
            data.decisions.update(decisions)
            print(f"DECISIONS_DONE {symbol} count={len(decisions)}", flush=True)
    else:
        with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
            jobs = [pool.submit(symbol_decisions, symbol, data.frames[symbol], config, policy) for symbol in symbols]
            for job in as_completed(jobs):
                symbol, decisions = job.result()
                data.decisions.update(decisions)
                print(f"DECISIONS_DONE {symbol} count={len(decisions)}", flush=True)


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")


def write_table(path: Path, rows: list[dict]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def execute(args: argparse.Namespace) -> dict[str, Any]:
    root = args.output.resolve()
    if root.is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("Diagnostic outputs must be outside Git")
    config = Config(epoch_iso(args.start), epoch_iso(args.end), initial_equity=args.initial_equity,
                    bootstrap_samples=args.bootstrap_samples,
                    conditional_current_metadata=args.conditional_current_metadata)
    data = load_dataset(args.data.resolve(), args.metadata.resolve(), config.start, config.end)
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / "data-quality.json", data.report)
    precompute(data, config, args.workers)
    results, comparison = [], []
    baseline_rows: list[dict] = []
    previous_rows: list[dict] = []
    for cap in CAPS:
        label = str(cap) if cap is not None else "unlimited"
        print(f"DIAGNOSIS_START cap={label}", flush=True)
        result = replay_cap(data, config, cap)
        raw_rows = result["trades"]
        if cap == 350:
            if args.reference_baseline is not None:
                reference = json.loads(args.reference_baseline.read_text(encoding="utf-8"))
                for key in ("period", "policy", "summary", "bootstrap", "trades",
                            "stage1_gate_rejections", "stage1_gate_evaluations", "all_rejections", "first_rejections"):
                    if reference[key] != result[key]:
                        raise ValueError(f"350 replay differs from Stage 3 reference: {key}")
            baseline_rows = raw_rows
        result["versus_350"] = trade_set_changes(raw_rows, baseline_rows, config)
        result["versus_previous_cap"] = trade_set_changes(raw_rows, previous_rows, config) if previous_rows else None
        previous_rows = raw_rows
        result["trades"] = [{**row, **excursions(row, data)} for row in raw_rows]
        result["exit_distribution"] = exit_distribution(result["trades"])
        result["monthly"] = monthly_distribution(raw_rows, config)
        write_json(root / f"cap-{label}.json", result)
        export_trades(result["trades"], root / f"cap-{label}-trades.csv")
        write_table(root / f"cap-{label}-monthly.csv", result["monthly"])
        summary = result["summary"]
        intervals = result["bootstrap_comparison"]
        table = {"cap": label, **{key: summary[key] for key in (
            "trade_count", "closed_trade_count", "net_expectancy_r", "profit_factor",
            "max_drawdown_usdt", "max_drawdown_r", "funding_null_count")},
            "added_vs_350_count": result["versus_350"]["added"]["trade_count"],
            "added_vs_350_expectancy_r": result["versus_350"]["added"]["net_expectancy_r"],
            "removed_vs_350_count": result["versus_350"]["removed"]["trade_count"],
            "changed_common_trade_count": result["versus_350"]["changed_common_trade_count"]}
        for unit in ("TRADE", "DAY", "WEEK"):
            for field in ("expectancy_r_95", "profit_factor_95"):
                ci = intervals[unit][field]
                table[f"{unit.lower()}_{field}_low"] = ci[0] if ci else None
                table[f"{unit.lower()}_{field}_high"] = ci[1] if ci else None
        comparison.append(table)
        results.append({key: value for key, value in result.items() if key != "trades"})
        print(f"DIAGNOSIS_DONE {json.dumps(table, allow_nan=False)}", flush=True)
    report = {
        "label": "CONDITIONAL STAGE 3b DIAGNOSIS / NO STRATEGY CHANGE",
        "period": results[0]["period"], "assumptions": results[0]["assumptions"],
        "diagnostic_notes": [
            "Only the total exposure threshold is varied; every other native gate remains active.",
            "The standalone process temporarily adapts only the native sanitizer exposure field; restores it after each replay.",
            "UNLIMITED is offline-only infinity internally, NULL in serialized policy; unknown exposure remains fail-closed.",
            "Relaxing a cap changes subsequent portfolio/daily-gate paths; added trades are not necessarily a strict superset.",
            "Added/removed sets use deterministic signal_id against 350 and the preceding cap, not count subtraction.",
            "MAE_R is positive adverse excursion, MFE_R is positive favorable excursion; MARK_PRICE, costs excluded.",
            "Price movement is scaled by original quantity / immutable initial risk, never by reduced TP1 remainder.",
            "Main MAE/MFE are exit-bar lower bounds; upper bounds include possible pre-exit OHLC extremes.",
            "STOP_FIRST censors terminal-bar favorable highs; TP3 censors highs beyond target; opening exits use only mark open.",
            "Pre-Stop MFE uses the same bounds and excludes all later candles; no tick-level pre-Stop path is claimed.",
            "Monthly PnL uses UTC closure month; empty months are retained with NULL expectancy/PF/PnL.",
            "Calendar day/week clusters use UTC entry date, Monday weeks, and include empty calendar blocks.",
            "Partial boundary days/weeks are included; empty resamples excluded explicitly; fewer than 2 active clusters gives NULL CI.",
            "Block bootstrap preserves within-cluster dependence, not dependence across days/weeks; no walk-forward inference.",
            "Monthly differences are descriptive, not proof of a causal ADX/trend/volatility regime effect.",
        ],
        "metadata_provenance": {key: data.metadata.get(key) for key in (
            "observed_at", "historical", "exchange_info_source", "brackets_source")},
        "comparison": comparison, "runs": results, "data_quality_file": "data-quality.json",
    }
    write_json(root / "diagnosis.json", report)
    write_table(root / "exposure-comparison.csv", comparison)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--initial-equity", type=float, default=1000)
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--workers", type=int, default=max(1, min(4, (os.cpu_count() or 2) // 2)))
    parser.add_argument("--conditional-current-metadata", action="store_true")
    parser.add_argument("--reference-baseline", type=Path)
    args = parser.parse_args()
    execute(args)


if __name__ == "__main__":
    main()
