"""Disclosed, non-clean VALIDATION trial; N>=100, mean net R>0, USDT PF>1.15.

VALIDATION runs once first. Only a passing report permits one fresh TRAIN run.
All monetary accounting, R, costs and informational bootstrap reuse the preceding
pre-screen. No original module, default 259-bar window or native gate is changed.
Permission percentages sample all 15m points, not just tradable candidates.
Full warmup days have unavailable contiguous SMA600 history at every observed
point; the all-symbol count additionally requires no entry that UTC day.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from statistics import fmean

import measure_original_trend_counts as driver
import prescreen_original_v2 as parent
from app.strategies.original_trend_risk import PARENT_HASH, PROFILE, trial_record
from build_measurement_view import load_measurement_dataset, sha256_file
from measure_donchian_counts import (
    BACKEND,
    PHASES,
    VIEW,
    MeasurementError,
    deny_network,
    emit,
    locked_plan,
    month,
    primary_config,
    quiet_native,
    validate_loaded_scope,
)
from measure_original_counts import METADATA, NATIVE_REASONS, VIEW_SHA

MIN_N = 100
MIN_MEAN_NET_R = 0.0
MIN_USDT_PF = 1.15
RULE = {
    "minimum_fully_verified_completed": MIN_N, "mean_net_r_strictly_above": MIN_MEAN_NET_R,
    "usdt_pf_strictly_above": MIN_USDT_PF, "undefined_pf": "FAIL", "both_phases_required": True,
}
SCHEMA = "original-v2-trend100d-prescreen-v1"
OUTPUT = Path.home() / "kaistrade-data" / "original-v2-trend100d-prescreen"
SOURCES = {
    "app/strategies/original_trend_risk.py", "app/strategies/original_trend_engine.py",
    "measure_original_trend_counts.py", "prescreen_original_trend100d.py",
}
DEFINITIONS = {
    **parent.DEFINITIONS,
    "trend": "LAST_600_CONTIGUOUS_CLOSED_CONTRACT_4H_CLOSES_INCLUDING_DECISION_CANDLE_STRICT_GT_LT",
    "trend_stage": "AFTER_CANONICAL_BEFORE_STOP_PREFILTER_FINAL_SIGNAL_RANK_AND_TOP3",
    "permission_pct": "ALL_PHASE_15M_POINTS_PER_SYMBOL_TREND_ONLY_INCLUDES_POSITION_AND_GAP_TIMES",
    "warmup_days": "ALL_OBSERVED_UTC_DAY_POINTS_WITH_INSUFFICIENT_CONTIGUOUS_4H_HISTORY",
}


def decision(phases: dict) -> dict:
    parent.require(set(phases) in ({"VALIDATION"}, {"TRAIN", "VALIDATION"}), "INVALID_DECISION_PHASES")
    failed = []
    for name in ("VALIDATION", "TRAIN"):
        if name not in phases:
            continue
        metrics = phases[name]["metrics"]
        parent.require(type(metrics["N"]) is int and metrics["N"] >= 0, f"INVALID_N:{name}")
        if metrics["N"] < MIN_N:
            failed.append(f"{name}:N_LT_100")
        mean, pf = metrics["mean_net_r"], metrics["usdt_pf"]
        if mean is None or not math.isfinite(mean) or mean <= MIN_MEAN_NET_R:
            failed.append(f"{name}:MEAN_NET_R_NOT_GT_ZERO")
        if pf is None or not math.isfinite(pf) or pf <= MIN_USDT_PF:
            failed.append(f"{name}:USDT_PF_UNDEFINED_OR_NOT_GT_1.15")
    return {
        "value": "DUR" if failed else "DEVAM", "failed_conditions": failed,
        "scope": "BOTH_PHASES" if set(phases) == {"TRAIN", "VALIDATION"} else "VALIDATION_ONLY",
    }


async def phase_result(data, plan, phase, reference, progress=None) -> dict:
    from app.strategies.original_trend_engine import OriginalTrendRiskEngine

    parent.require(PHASES[0].start <= phase.start < phase.end <= PHASES[-1].end
                   and phase.start % 900 == phase.end % 900 == 0, "MEASUREMENT_SCOPE_DENIED")
    engine = OriginalTrendRiskEngine(data, primary_config(phase, plan))
    counts = await driver.replay_counts(engine, phase, progress)
    parent.require(counts["canonical_distribution"] == reference["canonical_distribution"],
                   "ORIGINAL_CANONICAL_DISTRIBUTION_CHANGED")
    parent.require(counts["config_sha256"] == reference["config_sha256"]
                   and counts["policy_sha256"] == reference["policy_sha256"], "PARENT_RISK_CONFIG_CHANGED")
    observations = parent.verified_observations(engine.trades)
    metrics, costs = parent.summarize(observations)
    parent.require(metrics["N"] == counts["counts"]["fully_verified_completed"], "VERIFIED_N_MISMATCH")
    return {
        "native_counts": counts, "metrics": metrics, "costs": costs,
        "by_direction": {
            side: {"N": len(selected := [row for row in observations if row.direction == side]),
                   "mean_net_r": fmean(row.net_r for row in selected) if selected else None}
            for side in ("LONG", "SHORT")
        },
        "by_entry_month": {
            key: {"N": len(selected := [row for row in observations if month(row.opened_at) == key]),
                  "net_pnl_usdt": float(sum((row.net for row in selected), Decimal(0)))}
            for key in counts["by_entry_month"]
        },
        "by_symbol_counts": counts["by_symbol"], "bootstrap": parent.bootstrap_mean(observations, phase),
    }


def provenance(plan, references) -> dict:
    return {
        "profile_hash": PROFILE.profile_hash, "parent_profile_hash": PARENT_HASH,
        "trial_record": trial_record(),
        "parent_prescreen_provenance": parent.provenance(plan, references),
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=BACKEND.parent, text=True).strip(),
        "source_sha256": {name: sha256_file(BACKEND.joinpath(*name.split("/"))) for name in sorted(SOURCES)},
    }


def validate_counts(counts: dict, source: dict, name: str) -> None:
    """Validate the added gate, then project only its delta into the strict parent schema."""
    def require(ok, field):
        parent.require(ok, f"TREND_SCHEMA phase={name} field={field}")

    trend = counts["trend_filter"]
    require(set(trend) == {
        "candidates_before", "candidates_after", "rejections_by_direction", "rejections_by_cause",
        "all_symbols_full_warmup_no_entry_days", "by_symbol",
    }, "trend_filter.keys")
    for key in ("candidates_before", "candidates_after", "all_symbols_full_warmup_no_entry_days"):
        require(type(trend[key]) is int and trend[key] >= 0, key)
    require(set(trend["rejections_by_direction"]) == {"LONG", "SHORT"}, "direction.keys")
    from app.strategies.original_trend_engine import CAUSES, PERMISSIONS

    require(set(trend["rejections_by_cause"]) == set(CAUSES) - {"ALLOWED"}, "causes.keys")
    for table in ("rejections_by_direction", "rejections_by_cause"):
        require(all(type(value) is int and value >= 0 for value in trend[table].values()), table)
    rejected = counts["rejections"]["trend_filter"]
    require(rejected == sum(trend["rejections_by_direction"].values())
            == sum(trend["rejections_by_cause"].values()), "rejections.sum")
    require(trend["candidates_before"] - trend["candidates_after"] == rejected, "candidate.partition")
    native = counts["counts"]
    pre = counts["rejections_by_stage"]["PREFILTER"]
    quality_wait = sum(pre.get(key, 0) for key in NATIVE_REASONS if key.isupper() or key == "canonical_wait")
    require(trend["candidates_before"] == native["native_canonical_evaluations"] - quality_wait,
            "candidates.before")
    require(trend["candidates_after"] == native["risk_stage_arrivals"], "candidates.after")
    require(rejected == sum(table.get("trend_filter", 0) for table in counts["rejections_by_stage"].values()),
            "stage.sum")
    symbols = set(counts["by_symbol"])
    require(set(trend["by_symbol"]) == symbols, "symbols.keys")
    require(native["grid_points"] % len(symbols) == 0, "symbol.grid")
    expected_points = native["grid_points"] // len(symbols)
    for symbol, value in trend["by_symbol"].items():
        require(set(value) == {"decision_points", "permission_points", "permission_pct",
                               "full_warmup_days", "partial_warmup_days"}, f"{symbol}.keys")
        require(value["decision_points"] == expected_points, f"{symbol}.points")
        require(set(value["permission_points"]) == set(PERMISSIONS)
                == set(value["permission_pct"]), f"{symbol}.permissions.keys")
        require(all(type(n) is int and n >= 0 for n in value["permission_points"].values()),
                f"{symbol}.permission.count")
        require(sum(value["permission_points"].values()) == expected_points, f"{symbol}.permission.sum")
        for direction in PERMISSIONS:
            require(value["permission_pct"][direction]
                    == value["permission_points"][direction] / expected_points * 100, f"{symbol}.{direction}.pct")
        for key in ("full_warmup_days", "partial_warmup_days"):
            require(type(value[key]) is int and value[key] >= 0, f"{symbol}.{key}")
    projected = copy.deepcopy(counts)
    projected.pop("trend_filter")
    projected["rejections"].pop("trend_filter")
    for table in projected["rejections_by_stage"].values():
        table.pop("trend_filter", None)
    # Parent's risk denominator begins after quality; in this profile it also begins after trend.
    projected["counts"]["native_canonical_evaluations"] -= rejected
    parent.gap.validate_report({
        "schema": parent.gap.SCHEMA, "exception_record": parent.gap.exception_record(),
        "profile": asdict(parent.PROFILE), "provenance": source["run3_provenance"],
        "phases": {name: projected}, "elapsed_seconds": 0,
    }, expected_phases={name})


def validate_report(report: dict) -> None:
    def keys(value, expected, field):
        parent.require(isinstance(value, dict) and set(value) == expected, f"TREND_SCHEMA keys:{field}")

    def finite(value, field, nullable=False):
        parent.require((nullable and value is None) or (type(value) in (int, float) and math.isfinite(value)),
                       f"TREND_SCHEMA finite:{field}")

    keys(report, {"trial_record", "schema", "decision_rule", "definitions", "profile", "provenance",
                  "phases", "decision", "elapsed_seconds"}, "report")
    parent.require(report["schema"] == SCHEMA and report["decision_rule"] == RULE
                   and report["definitions"] == DEFINITIONS and report["profile"] == asdict(PROFILE)
                   and report["trial_record"] == trial_record(), "TREND_SCHEMA registration")
    source = report["provenance"]
    keys(source, {"profile_hash", "parent_profile_hash", "trial_record", "parent_prescreen_provenance",
                  "source_commit", "source_sha256"}, "provenance")
    parent.require(source["profile_hash"] == PROFILE.profile_hash and source["parent_profile_hash"] == PARENT_HASH
                   and source["trial_record"] == trial_record(), "TREND_SCHEMA provenance")
    keys(source["source_sha256"], SOURCES, "source_sha256")
    for name, digest in {**source["source_sha256"], "commit": source["source_commit"]}.items():
        parent.require(isinstance(digest, str) and len(digest) == (40 if name == "commit" else 64)
                       and all(char in "0123456789abcdef" for char in digest), f"TREND_SCHEMA digest:{name}")
    parent.require(isinstance(report["phases"], dict) and set(report["phases"]) in (
        {"VALIDATION"}, {"TRAIN", "VALIDATION"}), "TREND_SCHEMA phase_selection")
    for name, phase in report["phases"].items():
        keys(phase, {"native_counts", "metrics", "costs", "by_direction", "by_entry_month",
                     "by_symbol_counts", "bootstrap"}, f"phase.{name}")
        counts, metrics, costs = phase["native_counts"], phase["metrics"], phase["costs"]
        validate_counts(counts, source["parent_prescreen_provenance"], name)
        keys(metrics, {"N", "mean_net_r", "median_net_r", "usdt_pf", "pf_status", "win_rate",
                       "total_net_pnl_usdt", "longest_net_loss_streak", "max_drawdown_usdt", "mean_gross_r"}, "metrics")
        parent.require(type(metrics["N"]) is int and metrics["N"] == counts["counts"]["fully_verified_completed"],
                       "TREND_SCHEMA verified_N")
        for key, value in metrics.items():
            if key != "pf_status":
                finite(value, key, nullable=key not in {"N", "total_net_pnl_usdt", "longest_net_loss_streak"})
        parent.require(metrics["pf_status"] in {"DEFINED", "NO_LOSSES", "NO_COMPLETE_TRADES"}
                       and (metrics["usdt_pf"] is not None) == (metrics["pf_status"] == "DEFINED"),
                       "TREND_SCHEMA PF")
        keys(phase["by_symbol_counts"], set(counts["by_symbol"]), "symbols")
        for value in phase["by_symbol_counts"].values():
            keys(value, parent.SYMBOL_COUNT_KEYS, "symbol.counts_only")
        parent.require(phase["by_symbol_counts"] == counts["by_symbol"], "TREND_SCHEMA symbol.counts")
        keys(phase["by_direction"], {"LONG", "SHORT"}, "directions")
        keys(phase["by_entry_month"], set(counts["by_entry_month"]), "months")
        for group, keys_expected, value_key in (
            ("by_direction", {"N", "mean_net_r"}, "mean_net_r"),
            ("by_entry_month", {"N", "net_pnl_usdt"}, "net_pnl_usdt"),
        ):
            for label, value in phase[group].items():
                keys(value, keys_expected, f"{group}.{label}")
                parent.require(type(value["N"]) is int and value["N"] >= 0, "TREND_SCHEMA grouped_N")
                parent.require(value["N"] == counts[
                    "by_direction" if group == "by_direction" else "by_entry_month"][label]["fully_verified_completed"],
                    "TREND_SCHEMA grouped_N_parity")
                finite(value[value_key], value_key, nullable=group == "by_direction" and value["N"] == 0)
            parent.require(sum(value["N"] for value in phase[group].values()) == metrics["N"],
                           "TREND_SCHEMA grouped_N_sum")
        parent.require(math.isclose(sum(value["net_pnl_usdt"] for value in phase["by_entry_month"].values()),
                                    metrics["total_net_pnl_usdt"], rel_tol=1e-12, abs_tol=1e-10),
                       "TREND_SCHEMA monthly_accounting")
        keys(costs, {"commission_usdt", "slippage_usdt", "funding_cashflow_usdt", "funding_cost_usdt",
                     "mean_cost_r"}, "costs")
        keys(costs["mean_cost_r"], {"commission", "slippage", "funding"}, "cost_r")
        for key, value in costs.items():
            if key != "mean_cost_r":
                finite(value, key)
        for value in costs["mean_cost_r"].values():
            finite(value, "cost_r", nullable=metrics["N"] == 0)
        parent.require(costs["funding_cost_usdt"] == -costs["funding_cashflow_usdt"], "TREND_SCHEMA funding_sign")
        if metrics["N"]:
            parent.require(math.isclose(metrics["mean_net_r"],
                                        metrics["mean_gross_r"] - sum(costs["mean_cost_r"].values()),
                                        rel_tol=1e-10, abs_tol=1e-9), "TREND_SCHEMA cost_identity")
        bootstrap = phase["bootstrap"]
        keys(bootstrap, {"samples", "seed", "confidence", "informational_only", "methods"}, "bootstrap")
        parent.require((bootstrap["samples"], bootstrap["seed"], bootstrap["confidence"], bootstrap["informational_only"])
                       == (20000, 2026, 0.95, True), "TREND_SCHEMA bootstrap_settings")
        keys(bootstrap["methods"], {"TRADE", "DAY"}, "bootstrap.methods")
        for method in bootstrap["methods"].values():
            keys(method, {"status", "mean_net_r_95", "active_clusters", "calendar_or_trade_clusters",
                          "valid_resamples", "empty_resamples"}, "bootstrap.method")
            bounds = method["mean_net_r_95"]
            if bounds is not None:
                parent.require(isinstance(bounds, list) and len(bounds) == 2, "TREND_SCHEMA bootstrap.bounds")
                for value in bounds:
                    finite(value, "bootstrap.bound")
                parent.require(bounds[0] <= bounds[1], "TREND_SCHEMA bootstrap.order")
    parent.require(report["decision"] == decision(report["phases"]), "TREND_SCHEMA decision")
    finite(report["elapsed_seconds"], "elapsed")
    parent.require(report["elapsed_seconds"] >= 0, "TREND_SCHEMA elapsed")
    json.dumps(report, allow_nan=False)


def guarded_main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--phase", required=True, choices=("VALIDATION", "TRAIN"))
    args = parser.parse_args(argv)
    loop, stream = None, sys.stdout
    try:
        output = OUTPUT / args.phase
        parent.require(not (OUTPUT.exists() if args.phase == "VALIDATION" else output.exists()), "RESULTS_ALREADY_EXIST")
        parent.require(args.metadata.resolve() == METADATA.resolve(), "METADATA_COPY_REQUIRED")
        previous = None
        if args.phase == "TRAIN":
            path = OUTPUT / "VALIDATION" / "prescreen.json"
            parent.require(path.is_file(), "TRAIN_REQUIRES_COMPLETED_VALIDATION")
            previous = json.loads(path.read_text(encoding="utf-8"))
            validate_report(previous)
            parent.require(set(previous["phases"]) == {"VALIDATION"}
                           and previous["decision"]["value"] == "DEVAM", "TRAIN_DENIED_VALIDATION_RULE_FAILED")
        plan, references = locked_plan(), parent.load_references()
        parent.require(sha256_file(args.metadata) == plan["input_sha256"]["metadata"], "LOCKED_METADATA_MISMATCH")
        parent.require(sha256_file(VIEW / "manifest.json") == VIEW_SHA, "LOCKED_VIEW_MISMATCH")
        output.mkdir(parents=True, exist_ok=False)
        os.environ.update({"DATA_DIR": str(output / "runtime"), "PROTREBOT_DATA_DIR": str(output / "runtime"),
                           "DATABASE_URL": "", "PROTREBOT_DURABLE_AUTH_REQUIRED": "0", "ASSISTANT_LIVE_TESTS": "0"})
        loop = asyncio.new_event_loop()
        started = time.monotonic()
        emit(stream, {"status": "registered_relaxations", "trial_record": trial_record()})
        with quiet_native(), deny_network():
            record = provenance(plan, references)
            if previous is not None:
                parent.require(previous["provenance"] == record, "VALIDATION_PROVENANCE_CHANGED")
            data = load_measurement_dataset(VIEW, args.metadata, PHASES[0].start, PHASES[-1].end)
            validate_loaded_scope(data, plan["symbols"])
            phase = next(value for value in PHASES if value.name == args.phase)
            result = loop.run_until_complete(phase_result(
                data, plan, phase, references[args.phase]["phases"][args.phase],
                progress=lambda value: emit(stream, value)))
        phases = {**(previous["phases"] if previous is not None else {}), args.phase: result}
        report = {"trial_record": trial_record(), "schema": SCHEMA, "decision_rule": RULE, "definitions": DEFINITIONS,
                  "profile": asdict(PROFILE), "provenance": record, "phases": phases,
                  "decision": decision(phases), "elapsed_seconds": time.monotonic() - started}
        validate_report(report)
        path = output / "prescreen.json"
        with path.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, allow_nan=False)
            handle.write("\n")
        emit(stream, {"status": "completed", "output": str(path), "sha256": sha256_file(path),
                      "decision": report["decision"]})
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, ArithmeticError) as exc:
        emit(stream, {"status": "failed", "code": str(exc) if isinstance(exc, MeasurementError) else "NATIVE_FAILURE",
                      "error_class": type(exc).__name__})
        return 2
    finally:
        if loop is not None:
            loop.close()


def main(argv=None) -> int:
    with parent.gap.previous.research_path_guard():
        return guarded_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
