"""Final gap18d count-only trial: VALIDATION first; TRAIN requires 150 verified closures."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import measure_original_v2_counts as previous
from app.strategies.original_gap_risk import PROFILE, REFERENCE_SHA256, exception_record
from measure_donchian_counts import (
    BACKEND,
    PHASES,
    STEP,
    VIEW,
    MeasurementError,
    Phase,
    deny_network,
    emit,
    locked_plan,
    primary_config,
    quiet_native,
    validate_loaded_scope,
)
from measure_original_counts import METADATA, VIEW_SHA, sha256_file

OUTPUT = Path.home() / "kaistrade-data" / "original-v2-measurement-results-run3"
REFERENCE = Path.home() / "kaistrade-data" / "original-v2-measurement-results-run2" / "VALIDATION" / "counts.json"
SCHEMA = "original-v2-gap18d-counts-v1"
EXTRA_SOURCES = {
    "app/strategies/offline_facade.py", "app/strategies/original_gap_risk.py",
    "app/strategies/original_gap_engine.py", "measure_original_gap_counts.py",
}
BASE_SOURCES = {
    "app/analysis.py", "app/main.py", "app/backtest_baseline.py", "app/execution_core.py",
    "app/v25_execution.py", "app/strategies/original_offline_risk.py",
    "app/strategies/original_offline_engine.py", "measure_original_v2_counts.py",
}


async def replay_counts(engine: Any, phase: Phase, progress=None) -> dict:
    result = await previous.replay_counts(
        engine, phase, progress, entry_data_exclusion=engine.entry_data_exclusion)
    result["rejections"].setdefault("gap_blackout", 0)
    result["gap_blackout"] = engine.blackout_counts(sorted(result["by_symbol"]))
    return result


async def measure_phases(data: Any, plan: dict, *, phases: tuple[Phase, ...], progress=None) -> dict:
    from app.strategies.original_gap_engine import OriginalGapRiskEngine

    if (not phases or len({phase.name for phase in phases}) != len(phases)
            or any(phase.name not in {"TRAIN", "VALIDATION"} or phase.start < PHASES[0].start
                   or phase.end > PHASES[-1].end or phase.start >= phase.end
                   or phase.start % STEP or phase.end % STEP for phase in phases)):
        raise MeasurementError("MEASUREMENT_SCOPE_DENIED")
    return {phase.name: await replay_counts(
        OriginalGapRiskEngine(data, primary_config(phase, plan)), phase, progress) for phase in phases}


def provenance(plan: dict) -> dict:
    record = previous.provenance(plan)
    record["parent_profile_hash"] = record["profile_hash"]
    record["profile_hash"] = PROFILE.profile_hash
    record["exception_record"] = exception_record()
    record["source_sha256"].update({
        name: sha256_file(BACKEND.joinpath(*name.split("/"))) for name in sorted(EXTRA_SOURCES)})
    return record


def validate_report(report: dict, *, expected_phases: set[str] | None = None) -> None:
    from app.strategies.original_offline_risk import PROFILE as original_profile

    def require(ok: bool, path: str) -> None:
        if not ok:
            raise MeasurementError(f"GAP_COUNTS_SCHEMA_REJECTED path={path}")

    def keys(value: Any, expected: set[str], path: str) -> None:
        require(isinstance(value, dict), path)
        if set(value) != expected:
            rejected = ",".join(sorted(set(value) ^ expected))
            raise MeasurementError(f"GAP_COUNTS_SCHEMA_REJECTED path={path} keys={rejected}")

    def counts(value: Any, expected: set[str], path: str) -> None:
        keys(value, expected, path)
        require(all(type(n) is int and n >= 0 for n in value.values()), path)

    keys(report, {"exception_record", "schema", "profile", "provenance", "phases", "elapsed_seconds"}, "report")
    require(report["schema"] == SCHEMA, "schema")
    require(report["profile"] == asdict(PROFILE), "profile")
    require(report["exception_record"] == exception_record(), "exception_record")
    record = report["provenance"]
    keys(record, {"profile_hash", "original_parameter_hash", "source_commit", "source_sha256",
                  "metadata_sha256", "measurement_manifest_sha256", "stage6b_plan_sha256",
                  "exception_record", "parent_profile_hash"}, "provenance")
    require(record["profile_hash"] == PROFILE.profile_hash, "provenance.profile_hash")
    require(record["parent_profile_hash"] == original_profile.profile_hash, "provenance.parent_profile_hash")
    require(record["exception_record"] == exception_record(), "provenance.exception_record")
    keys(record["source_sha256"], BASE_SOURCES | EXTRA_SOURCES, "provenance.source_sha256")
    for name, digest in record["source_sha256"].items():
        require(isinstance(digest, str) and len(digest) == 64
                and all(c in "0123456789abcdef" for c in digest), f"provenance.source_sha256.{name}")
    require(isinstance(report["phases"], dict), "phases")
    projected = copy.deepcopy(report)
    projected.pop("exception_record")
    projected["schema"] = previous.SCHEMA
    projected["profile"] = asdict(original_profile)
    projected["provenance"].pop("exception_record")
    projected["provenance"].pop("parent_profile_hash")
    projected["provenance"]["profile_hash"] = original_profile.profile_hash
    projected["provenance"]["source_sha256"] = {
        name: record["source_sha256"][name] for name in BASE_SOURCES}
    for name, phase in report["phases"].items():
        path = f"phases.{name}.gap_blackout"
        require(isinstance(phase, dict) and "gap_blackout" in phase, path)
        gap = phase["gap_blackout"]
        keys(gap, {"count", "by_symbol", "by_type", "by_symbol_and_type",
                   "inventory_sha256", "type_counts_overlap"}, path)
        counts({"count": gap["count"]}, {"count"}, f"{path}.count")
        require(isinstance(phase.get("by_symbol"), dict), f"phases.{name}.by_symbol")
        symbols = set(phase["by_symbol"])
        streams = {"contract", "mark", "funding"}
        counts(gap["by_symbol"], symbols, f"{path}.by_symbol")
        counts(gap["by_type"], streams, f"{path}.by_type")
        keys(gap["by_symbol_and_type"], symbols, f"{path}.by_symbol_and_type")
        for symbol, values in gap["by_symbol_and_type"].items():
            counts(values, streams, f"{path}.by_symbol_and_type.{symbol}")
            require(gap["by_symbol"][symbol] <= sum(values.values()) <= 3 * gap["by_symbol"][symbol],
                    f"{path}.by_symbol_and_type.{symbol}.sum")
        require(sum(gap["by_symbol"].values()) == gap["count"], f"{path}.by_symbol.sum")
        for stream in streams:
            require(sum(values[stream] for values in gap["by_symbol_and_type"].values()) == gap["by_type"][stream],
                    f"{path}.by_type.{stream}.sum")
        require(gap["type_counts_overlap"] is True, f"{path}.type_counts_overlap")
        digest = gap["inventory_sha256"]
        require(isinstance(digest, str) and len(digest) == 64
                and all(c in "0123456789abcdef" for c in digest), f"{path}.inventory_sha256")
        require(phase["rejections"].get("gap_blackout") == gap["count"], f"{path}.rejections")
        keys(phase["rejections_by_stage"], {"PREFILTER", "ENTER"}, f"phases.{name}.rejections_by_stage")
        require(all(isinstance(values, dict) for values in phase["rejections_by_stage"].values()),
                f"phases.{name}.rejections_by_stage")
        require(sum(values.get("gap_blackout", 0) for values in phase["rejections_by_stage"].values()) == gap["count"],
                f"{path}.stage_sum")
        # Remove only the registered exclusion fields; the strict native count schema still applies.
        target = projected["phases"][name]
        target.pop("gap_blackout")
        target["rejections"].pop("gap_blackout")
        for values in target["rejections_by_stage"].values():
            values.pop("gap_blackout", None)
    previous.validate_report(projected, expected_phases=expected_phases)
    json.dumps(report, allow_nan=False)


def reference_evidence() -> dict:
    if sha256_file(REFERENCE) != REFERENCE_SHA256:
        raise MeasurementError("FINAL_EXCEPTION_REFERENCE_MISMATCH")
    record = json.loads(REFERENCE.read_text(encoding="utf-8"))
    previous.validate_report(record, expected_phases={"VALIDATION"})
    return record


def guarded_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--phase", required=True, choices=("VALIDATION", "TRAIN"))
    args = parser.parse_args(argv)
    stream, loop = sys.stdout, None
    try:
        output_directory = OUTPUT / args.phase
        if output_directory.exists():
            raise MeasurementError("RESULTS_ALREADY_EXIST")
        if args.metadata.resolve() != METADATA.resolve():
            raise MeasurementError("METADATA_COPY_REQUIRED")
        validation = None
        if args.phase == "TRAIN":
            path = OUTPUT / "VALIDATION" / "counts.json"
            if not path.is_file():
                raise MeasurementError("TRAIN_REQUIRES_COMPLETED_VALIDATION")
            validation = json.loads(path.read_text(encoding="utf-8"))
            validate_report(validation, expected_phases={"VALIDATION"})
            if not previous.validation_allows_train(validation):
                raise MeasurementError("TRAIN_DENIED_VALIDATION_BELOW_150")
        reference_evidence()
        plan = locked_plan()
        if sha256_file(args.metadata) != plan["input_sha256"]["metadata"]:
            raise MeasurementError("LOCKED_METADATA_MISMATCH")
        if sha256_file(VIEW / "manifest.json") != VIEW_SHA:
            raise MeasurementError("LOCKED_VIEW_MISMATCH")
        output_directory.mkdir(parents=True, exist_ok=False)
        os.environ.update({"DATA_DIR": str(output_directory / "runtime"),
                           "PROTREBOT_DATA_DIR": str(output_directory / "runtime"),
                           "DATABASE_URL": "", "PROTREBOT_DURABLE_AUTH_REQUIRED": "0",
                           "ASSISTANT_LIVE_TESTS": "0"})
        loop = asyncio.new_event_loop()
        started = time.monotonic()
        emit(stream, {"status": "authorized_final_exception", "exception_record": exception_record()})
        with quiet_native(), deny_network():
            record = provenance(plan)
            if validation is not None and validation["provenance"] != record:
                raise MeasurementError("TRAIN_VALIDATION_PROVENANCE_MISMATCH")
            data = previous.load_measurement_dataset(VIEW, args.metadata, PHASES[0].start, PHASES[-1].end)
            validate_loaded_scope(data, plan["symbols"])
            phases = loop.run_until_complete(measure_phases(
                data, plan, phases=previous.selected_phases(args.phase),
                progress=lambda value: emit(stream, value)))
        report = {"exception_record": exception_record(), "schema": SCHEMA, "profile": asdict(PROFILE),
                  "provenance": record, "phases": phases, "elapsed_seconds": time.monotonic() - started}
        validate_report(report, expected_phases={args.phase})
        path = output_directory / "counts.json"
        with path.open("x", encoding="utf-8") as output:
            json.dump(report, output, indent=2, allow_nan=False)
            output.write("\n")
        emit(stream, {"status": "completed", "output": str(path), "sha256": sha256_file(path)})
        emit(stream, {"phase": args.phase, "counts": phases[args.phase]["counts"],
                      "stop_rejection": phases[args.phase]["stop_rejection"]})
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, ArithmeticError) as exc:
        emit(stream, {"status": "failed",
                      "code": str(exc) if isinstance(exc, MeasurementError) else "NATIVE_FAILURE",
                      "error_class": type(exc).__name__})
        return 2
    finally:
        if loop is not None:
            loop.close()


def main(argv: list[str] | None = None) -> int:
    with previous.research_path_guard():
        return guarded_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
