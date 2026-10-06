"""Offline conditional baseline and mandatory execution-cost sensitivities."""

from __future__ import annotations

import argparse
import csv
import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from app.backtest_baseline import Config, run
from app.backtest_data import load_dataset


@contextmanager
def cached_native_analysis():
    """Only the standalone CLI uses this pure, exact-input memo adapter."""
    from app import main
    original = main.analyze

    @lru_cache(maxsize=128)
    def calculate(key):
        rows = [dict(zip(("time", "open", "high", "low", "close", "volume"), row)) for row in key]
        return original(rows)

    def adapter(rows):
        return calculate(tuple(tuple(row[name] for name in ("time", "open", "high", "low", "close", "volume"))
                               for row in rows))

    main.analyze = adapter
    try:
        yield
    finally:
        main.analyze = original


def epoch_iso(raw: str) -> int:
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        raise ValueError("Replay timestamps must include a timezone")
    instant = value.timestamp()
    if instant != int(instant):
        raise ValueError("Replay boundary must be an integral second")
    return int(instant)


def export_trades(rows: list[dict], path: Path) -> None:
    fields = sorted({key for row in rows for key in row}) or ["signal_id", "net_r"]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: json.dumps(value) if isinstance(value, (list, dict)) else value
                          for key, value in row.items()} for row in rows)


def delta(value, baseline):
    return value - baseline if value is not None and baseline is not None else None


def execute(args) -> dict:
    root = args.output.resolve()
    if root.is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("Research outputs must be outside Git")
    config = Config(epoch_iso(args.start), epoch_iso(args.end), initial_equity=args.initial_equity,
                    bootstrap_samples=args.bootstrap_samples,
                    conditional_current_metadata=args.conditional_current_metadata)
    data = load_dataset(args.data.resolve(), args.metadata.resolve(), config.start, config.end)
    root.mkdir(parents=True, exist_ok=True)
    (root / "data-quality.json").write_text(json.dumps(data.report, indent=2, allow_nan=False), encoding="utf-8")
    scenarios = [(2, 3, "STOP_FIRST"), (2, 3, "TP_FIRST"),
                 (1, 3, "STOP_FIRST"), (5, 3, "STOP_FIRST"),
                 (1, 6, "STOP_FIRST"), (2, 6, "STOP_FIRST"), (5, 6, "STOP_FIRST")]
    results = []
    with cached_native_analysis():
        for spread, slip, ordering in scenarios:
            name = f"{ordering.lower()}-spread{spread}-slip{slip}"
            print(f"REPLAY_START {name}", flush=True)
            result = run(data, replace(config, spread_bps=spread, slippage_bps=slip, intrabar=ordering))
            (root / f"{name}.json").write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
            export_trades(result["trades"], root / f"{name}-trades.csv")
            results.append({"scenario": name, **{key: value for key, value in result.items() if key != "trades"}})
            print(f"REPLAY_DONE {name} {json.dumps(result['summary'], allow_nan=False)}", flush=True)
    baseline = results[0]["summary"]
    comparison = [{"scenario": item["scenario"], **{key: item["summary"][key] for key in (
        "trade_count", "closed_trade_count", "net_expectancy_r", "profit_factor", "max_drawdown_usdt", "funding_null_count")},
        "expectancy_delta_r": delta(item["summary"]["net_expectancy_r"], baseline["net_expectancy_r"]),
        "profit_factor_delta": delta(item["summary"]["profit_factor"], baseline["profit_factor"])}
        for item in results]
    report = {"label": "CONDITIONAL BASELINE / KOŞULLU BASELINE", "assumptions": results[0]["assumptions"],
              "metadata_provenance": {key: data.metadata.get(key) for key in (
                  "observed_at", "historical", "exchange_info_source", "brackets_source")},
              "comparison": comparison, "runs": results, "data_quality_file": "data-quality.json"}
    (root / "comparison.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    with (root / "comparison.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(comparison[0]))
        writer.writeheader()
        writer.writerows(comparison)
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
    parser.add_argument("--conditional-current-metadata", action="store_true")
    args = parser.parse_args()
    print(json.dumps({"comparison": execute(args)["comparison"]}, allow_nan=False))


if __name__ == "__main__":
    main()
