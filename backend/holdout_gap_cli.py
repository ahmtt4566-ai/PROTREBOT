"""Gap-only amendment: register, signal-free dry verification, then one TEST bundle."""

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
            raise ValueError("Stage 6b runtime/results must remain outside Git")
        os.environ.update({"DATA_DIR": str(runtime / "runtime"),
                           "PROTREBOT_DATA_DIR": str(runtime / "runtime"), "DATABASE_URL": "",
                           "PROTREBOT_DURABLE_AUTH_REQUIRED": "0", "ASSISTANT_LIVE_TESTS": "0"})

from app import execution_core as core
from app.backtest_ablation import PRESETS, prepare_entries, verify_control
from app.backtest_baseline import Config, run
from app.backtest_data import Dataset, load_dataset
from app.backtest_diagnostics import trade_set_changes
from app.holdout_gap_model import GapDataset, annotate, blocked_decision, frozen_gap_replay
from app.holdout_gap_study import (
    claim_once, digest, exclusive_json, inherited_plan, register, source_hashes,
    validate_quality, verify_context, verify_dry_receipt,
)
from app.holdout_study import describe, period_scopes
from backtest_cli import cached_native_analysis, export_trades
from backtest_diagnostics_cli import write_json, write_table
from holdout_cli import cache_header, evaluate_candidates, scenario_name
from regime_cli import deny_network

LOGGER = logging.getLogger(__name__)


def load_verified(stage5: Path, locked: dict) -> Dataset:
    base = verify_context(stage5, locked["plan"])
    native = load_dataset(stage5 / "sealed", stage5 / "development" / "current-metadata.json",
                          base["test"]["start"], base["test"]["end"])
    validate_quality(native, base, locked["plan"])
    return native


def dry_verify(stage5: Path, output: Path) -> dict:
    deny_network()
    locked = register(stage5, output)
    if (output / "test-once.json").exists():
        raise ValueError("Stage 6b already claimed; dry verification cannot reset TEST")
    before = source_hashes()
    native = load_verified(stage5, locked)
    if before != source_hashes():
        raise ValueError("Stage 6b source changed during dry verification")
    verify_context(stage5, locked["plan"])
    write_json(output / "test-data-quality.json", native.report)
    record = {"state": "PASSED", "protocol_sha256": locked["sha256"],
              "source_sha256": before, "quality_sha256": digest(output / "test-data-quality.json"),
              "signals_calculated": 0, "replays": 0,
              "gap_rule": locked["plan"]["gap_rule"]}
    exclusive_json(output / "dry-verify.json", record, idempotent=True)
    print(f"STAGE6B_DRY_VERIFY_PASSED {locked['sha256']} signals=0 replays=0", flush=True)
    return record


def signal_worker(symbol: str, frames: dict, config: Config, policy: dict, cache: Path,
                  fingerprint: str, blocked: tuple[int, ...]) -> str:
    deny_network()
    native = Dataset({symbol: frames}, {}, {}, {}, {}, {})
    path = cache / f"{symbol}.jsonl.gz"
    if path.exists():
        raise ValueError("Stage 6b native cache already exists")
    temporary = path.with_suffix(".part")
    with gzip.open(temporary, "wt", encoding="utf-8") as stream, cached_native_analysis():
        stream.write(json.dumps(cache_header(symbol, config, policy, fingerprint)) + "\n")
        for at in range(config.start, config.end, 900):
            if not all(series.history_complete(at) for series in frames.values()):
                raise ValueError(f"Incomplete native history after dry verification: {symbol} / {at}")
            result = blocked_decision() if at in blocked else native.canonical(symbol, at, policy)
            stream.write(json.dumps({"at": at, "result": result}) + "\n")
            native.decisions.clear()
            if (at - config.start) % (10000 * 900) == 0:
                print(f"STAGE6B_NATIVE_PROGRESS {symbol} {at}", flush=True)
    temporary.replace(path)
    return symbol


def precompute(data: GapDataset, config: Config, workers: int, cache: Path, fingerprint: str) -> None:
    cache.mkdir(exist_ok=True)
    if any(cache.iterdir()):
        raise ValueError("Stage 6b cache is not empty; refusing another evaluation")
    policy = core.sanitize_execution_policy(config.policy, preserve_empty_allowed_symbols=True)
    blocked = tuple(data.gap_rule["blocked_native_decision_times"])
    with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        jobs = [pool.submit(signal_worker, symbol, frames, config, policy, cache, fingerprint, blocked)
                for symbol, frames in data.frames.items()]
        for job in as_completed(jobs):
            symbol = job.result()
            with gzip.open(cache / f"{symbol}.jsonl.gz", "rt", encoding="utf-8") as stream:
                if json.loads(next(stream)) != cache_header(symbol, config, policy, fingerprint):
                    raise ValueError("Stage 6b cache provenance differs")
                previous = config.start - 900
                for line in stream:
                    item = json.loads(line)
                    if item["at"] != previous + 900 or not config.start <= item["at"] < config.end:
                        raise ValueError("Invalid Stage 6b native timestamp")
                    previous = item["at"]
                    if previous in blocked and item["result"] != blocked_decision():
                        raise ValueError("Gap cache contains an unauthorized native decision")
                    key = (symbol, previous, policy["min_confidence"], policy["mtf_allow_either_timeframe"])
                    data.decisions[key] = item["result"]
                if previous != config.end - 900:
                    raise ValueError("Incomplete Stage 6b cache")
            print(f"STAGE6B_NATIVE_DONE {symbol}", flush=True)


def candidate_reports(rows: list[dict], data: GapDataset, config: Config, base: dict, scopes: dict) -> tuple[dict, dict]:
    reports, evidence = evaluate_candidates(rows, data, config, base, scopes)
    retained = [row for row in rows if not row["gap_affected"]]
    without = evaluate_candidates(retained, data, config, base, scopes)[0] if len(retained) != len(rows) else reports
    by_id = {row["signal_id"]: row for row in rows}
    for name, report in reports.items():
        affected = [by_id[key] for key in report["scopes"]["full"]["signal_ids"] if by_id[key]["gap_affected"]]
        report["gap_affected_count"] = len(affected)
        report["gap_affected_signal_ids"] = [row["signal_id"] for row in affected]
        report["gap_affected_summary"] = describe(affected, config, base)
        report["excluding_gap_affected"] = without[name]["scopes"]
    return reports, evidence


def bundle(native: Dataset, output: Path, locked: dict, workers: int) -> dict:
    base = inherited_plan(locked["plan"])
    data = GapDataset(native, locked["plan"]["gap_rule"])
    config = Config(base["test"]["start"], base["test"]["end"], initial_equity=base["initial_equity"],
                    conditional_current_metadata=True,
                    policy={"allowed_symbols": base["symbols"],
                            "max_total_exposure_usdt": base["max_total_exposure_usdt"]})
    fingerprint = locked["sha256"] + ":" + digest(output / "test-once.json")
    precompute(data, config, workers, output / "native-cache", fingerprint)
    scopes, reports, table, primary_rows = period_scopes(base), {}, [], None
    for cost in base["costs"]:
        current = replace(config, slippage_bps=cost["slippage_bps"], spread_bps=cost["spread_bps"])
        reference = run(data, current)
        gap = data.gap_rule["mark_bar_open"]
        reference["trades"] = [annotate(row, gap) for row in reference["trades"]]
        entries = asyncio.run(prepare_entries(data, current, reference))
        control = frozen_gap_replay(data, current, entries, PRESETS["A"])
        verify_control(control, reference)
        write_json(output / f"entry-cohort-slip{current.slippage_bps}-spread{current.spread_bps}.json",
                   {"native_entry_schedule": reference["trades"],
                    "stage1_gate_rejections": reference["stage1_gate_rejections"],
                    "all_rejections": reference["all_rejections"]})
        for ordering in base["orderings"]:
            cfg = replace(current, intrabar=ordering)
            rows = frozen_gap_replay(data, cfg, entries, PRESETS["B"])["trades"]
            if primary_rows is None:
                primary_rows = rows
            candidates, evidence = candidate_reports(rows, data, cfg, base, scopes)
            name = scenario_name(cfg)
            reports[name] = {"config": {**cost, "intrabar": ordering}, "candidates": candidates,
                             "filter_evidence": evidence,
                             "cohort_changes_vs_primary": trade_set_changes(rows, primary_rows, cfg),
                             "gate_rejections": reference["stage1_gate_rejections"],
                             "gap_affected_b_trade_count": sum(row["gap_affected"] for row in rows)}
            write_json(output / f"scenario-{name}.json", reports[name])
            export_trades(rows, output / f"B-{name}-trades.csv")
            for candidate, value in candidates.items():
                for sensitivity, groups in (("ALL", value["scopes"]),
                                            ("EXCLUDE_GAP_AFFECTED", value["excluding_gap_affected"])):
                    for scope, report in groups.items():
                        if report is None:
                            continue
                        s = report["summary"]
                        table.append({"scenario": name, "candidate": candidate, "scope": scope,
                                      "gap_sensitivity": sensitivity, "gap_affected_count": value["gap_affected_count"],
                                      **{key: s[key] for key in (
                                          "trade_count", "r_eligible_count", "net_expectancy_r", "profit_factor",
                                          "profit_factor_status", "win_rate", "max_drawdown_usdt",
                                          "closed_curve_net_pnl", "funding_null_count")},
                                      "label": s["criteria"]["label"]})
    result = {"protocol_sha256": locked["sha256"], "base_plan_sha256": locked["plan"]["base_plan_sha256"],
              "gap_rule": data.gap_rule, "prior_failure_preserved": True,
              "registered_candidates": base["candidates"], "period_scopes": scopes,
              "primary_scenario": "slip3-spread2-stop_first", "results": reports,
              "test_bundle_runs": 1, "candidate_scenarios_inspected": 24, "bonferroni_family": 3,
              "gap_exclusion_is_same_outcome_subset_not_replay": True,
              "blocked_native_decision_count": len(data.gap_rule["blocked_native_decision_times"]) * len(base["symbols"]),
              "raw_mark_candles_inserted": 0, "no_parameter_tuning": True, "stage7_started": False,
              "limitations": [
                  "Reverse-time historical holdout, not forward walk-forward; current Stage5 metadata/universe.",
                  "Contract OHLC is a declared protective trigger approximation only for the exact archival mark gap.",
                  "Gap entry/closed-bar signal embargo and BE deferral may change subsequent native entry cohorts.",
                  "Per-cost A STOP_FIRST gates; B filters remove only, no capacity refill; TP_FIRST freezes those entries.",
                  "Gap exclusion removes affected outcomes, not their portfolio effects; no rescan or replay.",
                  "Bonferroni3 is the primary family; eight scenarios/24 cells and gap subsets are not independent validation.",
              ]}
    write_json(output / "holdout-results.json", result)
    write_table(output / "comparison.csv", table)
    return result


def execute(stage5: Path, output: Path, workers: int) -> dict:
    deny_network()
    if not 1 <= workers <= 4:
        raise ValueError("Stage 6b allows one to four workers")
    stage5, output = stage5.resolve(), output.resolve()
    locked = register(stage5, output)
    verify_dry_receipt(stage5, output, locked)
    if (output / "test-once.json").exists():
        raise ValueError("Stage 6b already claimed; no rerun")
    native = load_verified(stage5, locked)
    if native.report != json.loads((output / "test-data-quality.json").read_text(encoding="utf-8")):
        raise ValueError("Raw data differs from successful dry verification")
    locked = claim_once(stage5, output)
    record = json.loads((output / "test-once.json").read_text(encoding="utf-8"))
    try:
        result = bundle(native, output, locked, workers)
        verify_context(stage5, locked["plan"])
        if source_hashes() != record["source_sha256"]:
            raise ValueError("Stage 6b code changed during TEST")
    except Exception as error:
        LOGGER.exception("Stage 6b one-shot failed; previous failure preserved; no rerun")
        failed = {**record, "state": "FAILED", "error_type": type(error).__name__, "error": str(error)}
        write_json(output / "test-once.json", failed)
        write_json(Path(record["repository_claim_path"]), failed)
        raise
    completed = {**record, "state": "COMPLETED", "results_sha256": digest(output / "holdout-results.json")}
    write_json(output / "test-once.json", completed)
    write_json(Path(record["repository_claim_path"]), completed)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("register", "dry-verify", "run"))
    parser.add_argument("--stage5-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 5))
    args = parser.parse_args()
    if args.command == "register":
        register(args.stage5_root, args.output)
    elif args.command == "dry-verify":
        dry_verify(args.stage5_root, args.output)
    else:
        execute(args.stage5_root, args.output, args.workers)
