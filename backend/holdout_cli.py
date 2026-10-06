"""One registered TEST bundle; no reset, resume or parameter overrides."""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import logging
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

if __name__ in ("__main__", "__mp_main__"):
    bootstrap_parser = argparse.ArgumentParser(add_help=False)
    bootstrap_parser.add_argument("--output", type=Path)
    bootstrap_args, _ = bootstrap_parser.parse_known_args()
    if bootstrap_args.output is not None:
        runtime = bootstrap_args.output.resolve()
        if runtime.is_relative_to(Path(__file__).resolve().parents[1]):
            raise ValueError("Holdout runtime/results must remain outside Git")
        os.environ.update({"DATA_DIR": str(runtime / "runtime"),
                           "PROTREBOT_DATA_DIR": str(runtime / "runtime"),
                           "DATABASE_URL": "", "PROTREBOT_DURABLE_AUTH_REQUIRED": "0",
                           "ASSISTANT_LIVE_TESTS": "0"})

from app import execution_core as core
from app.backtest_ablation import PRESETS, frozen_replay, prepare_entries, verify_control
from app.backtest_baseline import Config, run
from app.backtest_data import Dataset, load_dataset
from app.backtest_diagnostics import trade_set_changes
from app.holdout_statistics import bootstrap
from app.holdout_study import (
    candidate_masks, claim_once, describe, digest, period_scopes, register, scoped_rows,
    verify_inputs,
)
from backtest_cli import cached_native_analysis, export_trades
from backtest_diagnostics_cli import write_json, write_table
from regime_cli import deny_network

LOGGER = logging.getLogger(__name__)


class HoldoutDataset(Dataset):
    def canonical(self, symbol: str, at: int, policy: dict) -> dict:
        key = (symbol, at, policy["min_confidence"], policy["mtf_allow_either_timeframe"])
        if key not in self.decisions:
            raise ValueError(f"Missing verified one-shot native signal: {symbol} / {at}")
        return self.decisions[key]


def cache_header(symbol: str, config: Config, policy: dict, fingerprint: str) -> dict:
    return {"fingerprint": fingerprint, "symbol": symbol, "start": config.start, "end": config.end,
            "confidence": policy["min_confidence"], "either": policy["mtf_allow_either_timeframe"],
            "signal_mode": "NATIVE_SHORT_GATE_ON_ONLY"}


def signal_worker(symbol: str, frames: dict, config: Config, policy: dict, cache: Path,
                  fingerprint: str) -> str:
    deny_network()
    native = Dataset({symbol: frames}, {}, {}, {}, {}, {})
    path = cache / f"{symbol}.jsonl.gz"
    if path.exists():
        raise ValueError("One-shot native cache already exists")
    temporary = path.with_suffix(".part")
    with gzip.open(temporary, "wt", encoding="utf-8") as stream, cached_native_analysis():
        stream.write(json.dumps(cache_header(symbol, config, policy, fingerprint)) + "\n")
        for at in range(config.start, config.end, 900):
            if not all(series.history_complete(at) for series in frames.values()):
                raise ValueError(f"Incomplete native decision history: {symbol} / {at}")
            result = native.canonical(symbol, at, policy)
            stream.write(json.dumps({"at": at, "result": result}) + "\n")
            native.decisions.clear()
            if (at - config.start) % (10000 * 900) == 0:
                print(f"HOLDOUT_NATIVE_PROGRESS {symbol} {at}", flush=True)
    temporary.replace(path)
    return symbol


def precompute(data: HoldoutDataset, config: Config, workers: int, cache: Path,
               fingerprint: str) -> None:
    if not 1 <= workers <= 4:
        raise ValueError("Holdout allows one to four native workers")
    cache.mkdir(exist_ok=True)
    if any(cache.iterdir()):
        raise ValueError("TEST cache is not empty; refusing another evaluation")
    policy = core.sanitize_execution_policy(config.policy, preserve_empty_allowed_symbols=True)
    with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        jobs = [pool.submit(signal_worker, symbol, frames, config, policy, cache, fingerprint)
                for symbol, frames in data.frames.items()]
        for job in as_completed(jobs):
            symbol = job.result()
            with gzip.open(cache / f"{symbol}.jsonl.gz", "rt", encoding="utf-8") as stream:
                if json.loads(next(stream)) != cache_header(symbol, config, policy, fingerprint):
                    raise ValueError("One-shot native cache provenance differs")
                previous = config.start - 900
                for line in stream:
                    item = json.loads(line)
                    if item["at"] != previous + 900 or not config.start <= item["at"] < config.end:
                        raise ValueError("Invalid one-shot native timestamp")
                    previous = item["at"]
                    key = (symbol, previous, policy["min_confidence"], policy["mtf_allow_either_timeframe"])
                    data.decisions[key] = item["result"]
                if previous != config.end - 900:
                    raise ValueError("Incomplete one-shot native cache")
            print(f"HOLDOUT_NATIVE_DONE {symbol}", flush=True)


def validate_quality(data: Dataset, plan: dict) -> None:
    if set(data.frames) != set(plan["symbols"]) or data.report["missing_archives"]:
        raise ValueError("Holdout symbols or archive coverage differ")
    for symbol, parts in data.report["symbols"].items():
        for kind in ("klines_15m", "klines_1h", "klines_4h", "markPriceKlines_15m"):
            part = parts[kind]
            if part["missing_candles"] or part["duplicates"] or part["gaps"]:
                raise ValueError(f"Holdout candle gaps/duplicates: {symbol} / {kind}")


def scenario_name(config: Config) -> str:
    return f"slip{config.slippage_bps}-spread{config.spread_bps}-{config.intrabar.lower()}"


def evaluate_candidates(rows: list[dict], data: Dataset, config: Config, plan: dict, scopes: dict) -> tuple[dict, dict]:
    masks, evidence = candidate_masks(rows, data, plan)
    result = {}
    for name, mask in masks.items():
        selected = [row for row, keep in zip(rows, mask) if keep]
        groups = scoped_rows(selected, scopes)
        reports = {}
        for scope, observations in groups.items():
            if scope == "partly_seen" and not scopes["partly_seen"]:
                reports[scope] = None
                continue
            if scope == "clean" and not scopes["partly_seen"]:
                reports[scope] = reports["full"]
                continue
            calendar = scopes["full"] if scope == "partly_seen" else scopes[scope]
            reports[scope] = {"summary": describe(observations, config, plan),
                              "bootstrap": bootstrap(observations, calendar, plan["bootstrap"]),
                              "bootstrap_calendar": "FULL_TEST_INCLUDING_CROSS_BOUNDARY_ENTRY_DAYS" if scope == "partly_seen" else scope,
                              "signal_ids": [row["signal_id"] for row in observations]}
        result[name] = {"scopes": reports, "removed_entry_count": len(rows) - len(selected),
                        "added_entry_count": 0}
        print(f"HOLDOUT_CANDIDATE {scenario_name(config)} {name} "
              f"N={len(selected)} passed={reports['full']['summary']['criteria']['passed']}", flush=True)
    return result, evidence


def bundle(stage5: Path, output: Path, locked: dict, workers: int) -> dict:
    plan, test = locked["plan"], locked["plan"]["test"]
    config = Config(test["start"], test["end"], initial_equity=plan["initial_equity"],
                    conditional_current_metadata=True,
                    policy={"allowed_symbols": plan["symbols"],
                            "max_total_exposure_usdt": plan["max_total_exposure_usdt"]})
    source = stage5 / "sealed"
    native = load_dataset(source, stage5 / "development" / "current-metadata.json", config.start, config.end)
    write_json(output / "test-data-quality.json", native.report)
    validate_quality(native, plan)
    data = HoldoutDataset(native.frames, native.marks, native.funding, native.metadata,
                          native.report, native.funding_months)
    fingerprint = locked["sha256"] + ":" + digest(output / "test-once.json")
    precompute(data, config, workers, output / "native-cache", fingerprint)
    scopes = period_scopes(plan)
    reports, table = {}, []
    primary_rows = None
    for cost in plan["costs"]:
        current = replace(config, slippage_bps=cost["slippage_bps"], spread_bps=cost["spread_bps"])
        reference = run(data, current)
        entries = asyncio.run(prepare_entries(data, current, reference))
        control = frozen_replay(data, current, entries, PRESETS["A"])
        verify_control(control, reference)
        write_json(output / f"entry-cohort-slip{current.slippage_bps}-spread{current.spread_bps}.json",
                   {"native_entry_schedule": reference["trades"], "stage1_gate_rejections": reference["stage1_gate_rejections"]})
        for ordering in plan["orderings"]:
            variant_config = replace(current, intrabar=ordering)
            replay = frozen_replay(data, variant_config, entries, PRESETS["B"])
            rows = replay["trades"]
            if primary_rows is None:
                primary_rows = rows
            candidates, evidence = evaluate_candidates(rows, data, variant_config, plan, scopes)
            name = scenario_name(variant_config)
            reports[name] = {"config": {"slippage_bps": current.slippage_bps, "spread_bps": current.spread_bps,
                                      "intrabar": ordering}, "candidates": candidates, "filter_evidence": evidence,
                             "cohort_changes_vs_primary": trade_set_changes(rows, primary_rows, variant_config),
                             "gate_rejections": reference["stage1_gate_rejections"]}
            write_json(output / f"scenario-{name}.json", reports[name])
            export_trades(rows, output / f"B-{name}-trades.csv")
            for candidate, value in candidates.items():
                for scope, report in value["scopes"].items():
                    if report is None:
                        continue
                    s = report["summary"]
                    table.append({"scenario": name, "candidate": candidate, "scope": scope,
                                  **{key: s[key] for key in (
                                      "trade_count", "r_eligible_count", "net_expectancy_r", "profit_factor",
                                      "profit_factor_status", "win_rate", "max_drawdown_usdt",
                                      "closed_curve_net_pnl", "funding_null_count")},
                                  "label": s["criteria"]["label"], "failed_criteria": json.dumps(s["criteria"]["failed_criteria"])})
    result = {"protocol_sha256": locked["sha256"], "registered_candidates": plan["candidates"],
              "native_cache_fingerprint": fingerprint,
              "primary_scenario": "slip3-spread2-stop_first", "period_scopes": scopes,
              "test_bundle_runs": 1, "candidate_scenarios_inspected": len(reports) * len(plan["candidates"]),
              "bonferroni_family": 3, "primary_family_only_sensitivity_not_independent_confirmation": True,
              "no_parameter_tuning": True, "stage7_started": False, "results": reports,
              "limitations": [
                  "Reverse-time historical holdout, not forward walk-forward.",
                  "Stage5 current exchange metadata held fixed, not historical rules or universe.",
                  "Per-cost native A STOP_FIRST schedules; all three B candidates remove only, no capacity refill.",
                  "TP_FIRST shares its cost scenario's STOP_FIRST entries; it does not rescan daily gates.",
                  "Clean/partly-seen subsets derive from the same outcomes, never a second TEST replay.",
                  "Open/unverified/missing-R or funding accounting excluded from net-R; counts disclosed.",
                  "Nominal95 is unadjusted; family95 uses Bonferroni3 for the registered primary candidates.",
                  "Eight scenarios/24 candidate cells inspected; sensitivity cells are not independent validation.",
              ]}
    write_json(output / "holdout-results.json", result)
    write_table(output / "comparison.csv", table)
    return result


def execute(stage5: Path, output: Path, workers: int) -> dict:
    deny_network()
    if not 1 <= workers <= 4:
        raise ValueError("Holdout allows one to four native workers")
    stage5, output = stage5.resolve(), output.resolve()
    sources = {name: digest(Path(__file__).parent.joinpath(*name.split("/")))
               for name in ("holdout_cli.py", "app/holdout_study.py", "app/holdout_statistics.py", "studies/stage6-plan.json")}
    locked = claim_once(stage5, output, sources)
    try:
        result = bundle(stage5, output, locked, workers)
        verify_inputs(stage5, locked["plan"])
        current_sources = {name: digest(Path(__file__).parent.joinpath(*name.split("/"))) for name in sources}
        if current_sources != sources:
            raise ValueError("Holdout code changed during the one-shot run")
    except Exception as error:
        LOGGER.exception("One-shot holdout failed; rerun remains forbidden")
        record = json.loads((output / "test-once.json").read_text(encoding="utf-8"))
        write_json(output / "test-once.json", {**record, "state": "FAILED", "error_type": type(error).__name__,
                                             "error": str(error)})
        write_json(Path(record["repository_claim_path"]), {**record, "state": "FAILED",
                                                          "error_type": type(error).__name__, "error": str(error)})
        raise
    record = json.loads((output / "test-once.json").read_text(encoding="utf-8"))
    write_json(output / "test-once.json", {**record, "state": "COMPLETED", "results_sha256": digest(output / "holdout-results.json")})
    write_json(Path(record["repository_claim_path"]), {**record, "state": "COMPLETED",
                                                      "results_sha256": digest(output / "holdout-results.json")})
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("register", "run"))
    parser.add_argument("--stage5-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 5))
    args = parser.parse_args()
    if args.command == "register":
        register(args.stage5_root, args.output)
    else:
        execute(args.stage5_root, args.output, args.workers)
