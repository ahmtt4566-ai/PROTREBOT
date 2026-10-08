"""N>=100, mean net R>0, PF>1.15 and mean>baseline; otherwise DUR.

Fixed fourth trial, partly clean period: passing is only a Demo observation
candidate, not verification. One process runs BASELINE then F2 once each.
Existing real daily mark supplements are retained; no interpolation is used.
Persist native entry analysis, closed contexts, specs and funding cashflow traces.
Accepted-entry records include unverified entries without performance metrics.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from collections.abc import Awaitable, Callable
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from statistics import fmean

import measure_original_f2_counts as driver
import prescreen_original_v2 as parent
from app.backtest_baseline import Position
from app.backtest_data import Dataset, load_dataset
from app.strategies.original_f2_engine import OriginalF2RiskEngine, f2_value
from app.strategies.original_f2_risk import PARENT_HASH, PROFILE
from app.strategies.original_gap_risk import PROFILE as GAP_PROFILE
from app.strategies.original_offline_engine import fully_verified
from build_measurement_view import sha256_file
from diagnose_original_v2_trades import DiagnosisEngine, paths_table, trade_record
from measure_donchian_counts import (
    BACKEND,
    MeasurementError,
    Phase,
    emit,
    epoch,
    month,
    primary_config,
    quiet_native,
)

MIN_N = 100
MIN_MEAN_NET_R = 0.0
MIN_USDT_PF = 1.15
RULE = {
    "minimum_fully_verified_completed": MIN_N,
    "mean_net_r_strictly_above": MIN_MEAN_NET_R,
    "usdt_pf_strictly_above": MIN_USDT_PF,
    "mean_net_r_strictly_above_baseline": True,
    "undefined_or_nonfinite": "FAIL",
    "all_conditions_required": True,
}
PLAN = BACKEND / "studies" / "original-v2-f2-test-plan.json"
PLAN_SHA = "1f02f9f3c8aee4a70df599338d201659bf1dd3cccda5b5ae7d32f2756ad6f052"
OUTPUT = Path.home() / "kaistrade-data" / "original-v2-f2-test"
SCHEMA = "original-v2-f2-test-v1"
PROTECTED = ("donchian-test-", "test-sealed.7z", "independent-block", "donchian-holdout-results")
SOURCES = (
    "app\\strategies\\original_f2_risk.py", "app\\strategies\\original_f2_engine.py",
    "measure_original_f2_counts.py", "prescreen_original_f2.py",
)


class FeatureDiagnosisEngine(DiagnosisEngine):
    def __init__(self, data, config):
        super().__init__(data, config)
        self.entry_f2: dict[int, dict[str, float | None]] = {}
        self.entry_context: dict[int, dict] = {}
        self.funding_trace: dict[int, list[dict]] = {}

    async def _enter(self, symbol: str, signal: dict, at: int) -> None:
        before = len(self.trades)
        await super()._enter(symbol, signal, at)
        if len(self.trades) == before + 1:
            value = f2_value(signal)
            longest = signal.get("ema", {}).get("ema200")
            self.entry_f2[id(self.trades[-1])] = {
                "f2": value,
                "ema200_15m": longest if type(longest) in (int, float) and math.isfinite(longest) else None,
            }
            self.entry_context[id(self.trades[-1])] = {
                "funding_initial_cashflow_decimal": str(self.trades[-1].funding_amount),
                "canonical_analysis": deepcopy(signal),
                "closed_contract_context": {
                    interval: deepcopy(series.closed(at))
                    for interval, series in self.data.frames[symbol].items()
                },
            }

    def advance(self, position: Position, at: int, *, opening_only: bool) -> None:
        before, known = position.funding_amount, position.funding_known
        super().advance(position, at, opening_only=opening_only)
        if position.funding_amount != before or position.funding_known != known:
            self.funding_trace.setdefault(id(position), []).append({
                "bar_open_epoch": at, "opening_only": opening_only,
                "cashflow_delta_decimal": str(position.funding_amount - before),
                "cashflow_total_decimal": str(position.funding_amount),
                "funding_known": position.funding_known,
            })


class F2DiagnosisEngine(OriginalF2RiskEngine, FeatureDiagnosisEngine):
    """Compose admission with the existing accepted-entry/raw-trade recorder."""


def emit_status(value: dict) -> None:
    emit(sys.stdout, value)


def decision(baseline: dict, filtered: dict) -> dict:
    parent.require(type(filtered["N"]) is int and filtered["N"] >= 0, "INVALID_FILTERED_N")
    failures = []
    if filtered["N"] < MIN_N:
        failures.append("F2:N_LT_100")
    mean, pf, reference = filtered["mean_net_r"], filtered["usdt_pf"], baseline["mean_net_r"]
    if mean is None or not math.isfinite(mean) or mean <= MIN_MEAN_NET_R:
        failures.append("F2:MEAN_NET_R_NOT_GT_ZERO")
    if pf is None or not math.isfinite(pf) or pf <= MIN_USDT_PF:
        failures.append("F2:USDT_PF_UNDEFINED_OR_NOT_GT_1.15")
    if (mean is None or reference is None or not math.isfinite(mean)
            or not math.isfinite(reference) or mean <= reference):
        failures.append("F2:MEAN_NET_R_NOT_GT_BASELINE")
    return {
        "value": "DUR" if failures else "DEVAM (Demo'da izlemeye aday)",
        "failed_conditions": failures, "verification_claim": False,
    }


def protected_path(path: Path) -> bool:
    text = str(path).lower()
    if any(token in text for token in PROTECTED):
        return True
    # Research stores are forbidden, not unrelated Python *_registry.py modules.
    return any("sealed" in part.lower() or "registry" in part.lower() for part in path.parts[:-1]) or (
        path.suffix.lower() not in {".py", ".pyc", ".pyd"}
        and ("sealed" in path.name.lower() or "registry" in path.name.lower())
    )


def validate_manifest(root: Path, manifest: dict, plan: dict) -> None:
    parent.require(not protected_path(root) and not root.is_symlink(), "FORBIDDEN_SOURCE_ROOT")
    root = root.resolve()
    parent.require(manifest.get("sealed") is not True, "SEALED_MANIFEST_DENIED")
    allowed = {("klines", interval) for interval in ("15m", "1h", "4h")}
    allowed |= {("markPriceKlines", "15m"), ("fundingRate", None)}
    for archive in manifest["archives"]:
        parent.require(archive["symbol"] in plan["symbols"], "SOURCE_SYMBOL_DENIED")
        parent.require((archive["kind"], archive["interval"]) in allowed, "SOURCE_STREAM_DENIED")
        parent.require(archive["status"] == "OK", "SOURCE_ARCHIVE_UNAVAILABLE")
        registered_month = archive["month"][:7]
        parent.require("2024-07" <= registered_month <= "2026-09", "SOURCE_MONTH_DENIED")
        relative = Path(archive["path"].replace("/", os.sep))
        parent.require(not relative.is_absolute() and not protected_path(relative), "SOURCE_PATH_DENIED")
        path = (root / relative).resolve()
        parent.require(path.is_relative_to(root) and not protected_path(path), "SOURCE_PATH_ESCAPE")
        # Prevent a safe month field from masking a different archive date.
        parent.require(path.name.endswith(f"-{archive['month']}.zip"), "SOURCE_FILENAME_MONTH_MISMATCH")


def locked_plan() -> dict:
    parent.require(sha256_file(PLAN) == PLAN_SHA, "F2_PLAN_CHANGED")
    plan = json.loads(PLAN.read_bytes())
    parent.require(PROFILE.profile_hash == plan["profile_sha256"], "F2_PROFILE_CHANGED")
    parent.require(GAP_PROFILE.profile_hash == PARENT_HASH == plan["parent_profile_sha256"],
                   "PARENT_PROFILE_CHANGED")
    for name, digest in plan["source_sha256"].items():
        parent.require(sha256_file(BACKEND / name) == digest, "INHERITED_SOURCE_CHANGED:" + name)
    return plan


def load_registered_dataset(plan: dict) -> Dataset:
    root = Path(plan["inputs"]["root"])
    parent.require(not protected_path(root) and not root.is_symlink(), "FORBIDDEN_SOURCE_ROOT")
    manifest_path, metadata = root / "manifest.json", root / "current-metadata.json"
    parent.require(sha256_file(manifest_path) == plan["inputs"]["manifest_sha256"], "SOURCE_MANIFEST_CHANGED")
    parent.require(sha256_file(metadata) == plan["inputs"]["metadata_sha256"], "SOURCE_METADATA_CHANGED")
    manifest = json.loads(manifest_path.read_bytes())
    validate_manifest(root, manifest, plan)
    start, end = epoch(plan["window"]["start"]), epoch(plan["window"]["end_exclusive"])
    data = load_dataset(root, metadata, start, end)
    parent.require(set(data.frames) == set(plan["symbols"]), "DATASET_SYMBOLS_CHANGED")
    parent.require(not data.report["missing_archives"], "DATASET_MISSING_ARCHIVES")
    for symbol, report in data.report["symbols"].items():
        for key in ("klines_15m", "klines_1h", "klines_4h", "markPriceKlines_15m"):
            parent.require(report[key]["missing_candles"] == 0, "DATASET_CANDLE_GAP:" + symbol + ":" + key)
        parent.require(report["funding"]["status"] == "PRESENT" and not report["funding"]["gaps"]
                       and report["funding"]["trailing_missing_events"] == 0
                       and data.funding_complete(symbol, start, end - 1), "DATASET_FUNDING_GAP:" + symbol)
        for series in (*data.frames[symbol].values(), data.marks[symbol]):
            parent.require(all(start <= at < end for at in series.times), "DATASET_OUTSIDE_WINDOW")
        parent.require(all(start <= row["time"] < end for row in data.funding[symbol]),
                       "FUNDING_OUTSIDE_WINDOW")
    return data


def install_offline_guard(output: Path) -> None:
    research = Path.home() / "kaistrade-data"
    session = Path.home() / ".copilot" / "session-state"
    source = Path(json.loads(PLAN.read_bytes())["inputs"]["root"]).resolve()
    output = output.resolve()

    def audit(event, args):
        if event.startswith("socket.") and event in {"socket.connect", "socket.getaddrinfo", "socket.bind"}:
            raise MeasurementError("NETWORK_DENIED")
        if event == "sqlite3.connect" and args[0] != ":memory:":
            raise MeasurementError("PERSISTENT_DATABASE_DENIED")
        if event in {"subprocess.Popen", "os.system"}:
            raise MeasurementError("SECOND_PROCESS_DENIED")
        if event in {"open", "os.listdir", "os.scandir", "os.mkdir", "os.remove", "os.rename", "os.rmdir"}:
            value = args[0]
            if isinstance(value, (str, bytes, os.PathLike)):
                path = Path(os.fsdecode(value))
                parent.require(not protected_path(path), "PROTECTED_PATH_DENIED")
                path = path.resolve()
                if path.is_relative_to(research) or path.is_relative_to(session):
                    parent.require(path.is_relative_to(source) or path.is_relative_to(output),
                                   "UNREGISTERED_RESEARCH_PATH_DENIED")
                write = event in {"os.mkdir", "os.remove", "os.rename", "os.rmdir"}
                if event == "open":
                    mode, flags = args[1:3]
                    write = (
                        isinstance(mode, str) and any(char in mode for char in "wax+")
                    ) or (
                        isinstance(flags, int)
                        and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
                    )
                if write:
                    parent.require(path.is_relative_to(output), "WRITE_OUTSIDE_NEW_OUTPUT_DENIED")
        if event in {"os.rename", "os.replace"}:
            parent.require(Path(args[1]).resolve().is_relative_to(output), "RENAME_OUTSIDE_OUTPUT_DENIED")

    sys.addaudithook(audit)


def halves(observations: list[parent.Observation], phase: Phase) -> dict:
    midpoint = (phase.start + phase.end) // 2
    return {
        name: {
            "N": len(selected := [row for row in observations if start <= row.opened_at < end]),
            "mean_net_r": fmean(row.net_r for row in selected) if selected else None,
        } for name, start, end in (("FIRST", phase.start, midpoint), ("SECOND", midpoint, phase.end))
    }


async def run_once(
    data: Dataset, plan: dict, phase: Phase, output: Path, progress: Callable[[dict], None],
) -> dict:
    parent.require(phase.name in {"BASELINE", "F2"}, "UNREGISTERED_RUN")
    parent.require(not data.decisions, "NONEMPTY_DECISION_CACHE")
    config = primary_config(phase, plan)
    engine = F2DiagnosisEngine(data, config) if phase.name == "F2" else FeatureDiagnosisEngine(data, config)
    admission = engine.apply_f2 if isinstance(engine, F2DiagnosisEngine) else None
    counts = await driver.replay_counts(engine, phase, progress, admission=admission)
    observations = parent.verified_observations(engine.trades)
    parent.require(len(observations) == counts["counts"]["fully_verified_completed"], "VERIFIED_N_MISMATCH")
    raw_path = output / f"trades-{phase.name}.jsonl"
    entry_path = output / f"entries-{phase.name}.jsonl"
    paths = []
    with (
        raw_path.open("x", encoding="utf-8", newline="\n") as stream,
        entry_path.open("x", encoding="utf-8", newline="\n") as entries,
    ):
        for position in engine.trades:
            context = {
                **engine.entry_f2[id(position)], **engine.entry_context[id(position)],
                "funding_cashflow_trace": engine.funding_trace.get(id(position), []),
                "native_spec": {
                    key: str(value) if isinstance(value, Decimal) else value
                    for key, value in position.spec.items()
                },
            }
            entries.write(json.dumps({
                "symbol": position.symbol, "direction": position.direction,
                "signal_id": position.signal_identifier, "opened_at_epoch": position.opened_at,
                "closed_at_epoch": position.closed_at, "status": position.status,
                "funding_known": position.funding_known, "fully_verified": fully_verified(position),
                "initial_quantity_decimal": str(position.quantity),
                "remaining_quantity_decimal": str(position.remaining),
                "native_exits": position.exits, **context,
            }, ensure_ascii=True, allow_nan=False) + "\n")
            if fully_verified(position):
                record = {
                    **trade_record(engine, position), **context,
                    "funding_source_events": [
                        row for row in data.funding[position.symbol]
                        if position.opened_at <= row["time"] <= position.closed_at
                    ],
                    "native_accounting_decimal": {
                        "gross_pnl": str(position.gross), "commission": str(position.commission),
                        "funding_cashflow": str(position.funding_amount),
                        "actual_entry": str(position.actual_entry), "tp1_quantity": str(position.tp1_quantity),
                        "mark_entry": str(position.mark_entry),
                    },
                }
                initial = Decimal(context["funding_initial_cashflow_decimal"])
                changes = sum((Decimal(row["cashflow_delta_decimal"])
                               for row in context["funding_cashflow_trace"]), Decimal(0))
                parent.require(initial + changes == position.funding_amount, "FUNDING_TRACE_PARITY_MISMATCH")
                stream.write(json.dumps(record, ensure_ascii=True, allow_nan=False) + "\n")
                paths.append({"path": record["path"], "net_r": record["net_r"]})
    parent.require(len(paths) == len(observations), "RAW_TRADE_N_MISMATCH")
    metrics, costs = parent.summarize(observations)
    if isinstance(engine, F2DiagnosisEngine):
        filters = engine.filter_counts()
        parent.require(filters["candidates_before"] == counts["filter_candidates"]["before"]
                       and filters["candidates_after"] == counts["filter_candidates"]["after"],
                       "FILTER_CANDIDATE_COUNT_MISMATCH")
        parent.require(filters["candidates_before"] - filters["candidates_after"]
                       == sum(filters["rejections_by_direction"].values())
                       == counts["rejections"]["f2_filter"], "FILTER_REJECTION_COUNT_MISMATCH")
    else:
        filters = {"enabled": False, **counts["filter_candidates"]}
    return {
        "profile_sha256": engine.profile.profile_hash,
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
        "by_symbol_counts": counts["by_symbol"], "paths": paths_table(paths),
        "bootstrap": parent.bootstrap_mean(observations, phase),
        "halves_by_entry_time": halves(observations, phase),
        "halves_midpoint_utc": parent.stamp((phase.start + phase.end) // 2),
        "f2_filter": filters,
        "raw_trades": {"path": str(raw_path), "N": len(paths), "sha256": sha256_file(raw_path)},
        "accepted_entry_records": {
            "path": str(entry_path), "N": len(engine.trades), "sha256": sha256_file(entry_path),
            "unverified_records_have_no_performance_metrics": True,
        },
    }


async def execute(
    plan: dict, output: Path, *,
    _load_dataset: Callable[[dict], Dataset] = load_registered_dataset,
    _run_phase: Callable[[Dataset, dict, Phase, Path, Callable[[dict], None]], Awaitable[dict]] = run_once,
) -> dict:
    data = _load_dataset(plan)
    results = {}
    start, end = epoch(plan["window"]["start"]), epoch(plan["window"]["end_exclusive"])
    with (output / "progress.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
        def progress(value):
            stream.write(json.dumps(value, allow_nan=False) + "\n")
            stream.flush()

        for name in plan["run_sequence"]:
            emit_status({"status": "run_started", "run": name})
            with quiet_native():
                result = await _run_phase(data, plan, Phase(name, start, end), output, progress)
            results[name] = result
            destination = output / f"report-{name}.json"
            with destination.open("x", encoding="utf-8", newline="\n") as report:
                json.dump(result, report, ensure_ascii=True, allow_nan=False, indent=2)
            emit_status({"status": "run_completed", "run": name, "report_sha256": sha256_file(destination)})
    for key in ("canonical_distribution", "config_sha256", "policy_sha256"):
        parent.require(results["BASELINE"]["native_counts"][key] == results["F2"]["native_counts"][key],
                       "COMPARATOR_PARITY_CHANGED:" + key)
    return {
        "schema": SCHEMA, "plan_sha256": PLAN_SHA, "provenance": plan["provenance"],
        "ledger": plan["ledger"], "window": plan["window"], "rule": RULE,
        "definitions": parent.DEFINITIONS,
        "raw_definitions": {
            "entry_context": "NATIVE_CANONICAL_ANALYSIS_AND_DEFAULT_CLOSED_CONTRACT_WINDOWS_NO_NEW_FEATURE_SCAN",
            "funding_source_events": "SOURCE_EVENTS_IN_HOLDING_INTERVAL_NOT_AN_APPLIED_EVENT_LEDGER",
            "funding_cashflow_trace": "PASSIVE_BEFORE_AFTER_NATIVE_ADVANCE_CASHFLOW_AND_KNOWN_STATE_CHANGES",
            "decimal_fields": "LOSSLESS_NATIVE_DECIMAL_STRINGS",
            "accepted_entry_records": "ALL_ACCEPTED_ENTRIES_NO_UNVERIFIED_PNL_OR_R",
            "unavailable_or_undefined_metrics": "EXPLICIT_NULL_NEVER_IMPUTED",
        },
        "source_sha256": {name: sha256_file(BACKEND / name) for name in SOURCES},
        "input_manifest_sha256": plan["inputs"]["manifest_sha256"],
        "input_metadata_snapshot": data.metadata,
        "runs": results, "decision": decision(results["BASELINE"]["metrics"], results["F2"]["metrics"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    parent.require(args.output.resolve() == OUTPUT.resolve(), "OUTPUT_SCOPE_DENIED")
    parent.require(not args.output.exists(), "OUTPUT_EXISTS_STOP")
    plan = locked_plan()
    sys.dont_write_bytecode = True
    with asyncio.Runner() as runner:
        runner.get_loop()
        install_offline_guard(args.output)
        args.output.mkdir()
        result = runner.run(execute(plan, args.output))
        destination = args.output / "f2-test.json"
        with destination.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, ensure_ascii=True, allow_nan=False, indent=2)
        emit_status({"status": "completed", "result": str(destination), "sha256": sha256_file(destination),
                     "decision": result["decision"]})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MeasurementError as exc:
        emit_status({"status": "failed", "code": str(exc)})
        raise SystemExit(2) from exc
