"""Four preregistered descriptive features from saved trades; never replay."""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path
from statistics import fmean

from app.analysis import analyze
from app.backtest_baseline import percentile
from build_measurement_view import load_measurement_dataset, sha256_file
from measure_donchian_counts import (
    BACKEND,
    PHASES,
    VIEW,
    MeasurementError,
    deny_network,
    emit,
    quiet_native,
    validate_loaded_scope,
)
from measure_original_counts import METADATA
from measure_original_v2_counts import research_path_guard

PLAN = BACKEND / "studies" / "original-v2-feature-diagnosis-plan.json"
PLAN_SHA = "ba837036d96b77d78b6eff1789a35f1f78f32194e06851f950a7698d316c4934"
INPUT = Path.home() / "kaistrade-data" / "original-v2-diagnosis"
OUTPUT = Path.home() / "kaistrade-data" / "original-v2-feature-diagnosis"
FEATURES = ("F1", "F2", "F3", "F4")
BUCKETS = ("LOW", "MIDDLE", "HIGH")
SCHEMA = "original-v2-feature-diagnosis-v1"


def require(ok: bool, code: str) -> None:
    if not ok:
        raise MeasurementError(code)


def load_plan() -> dict:
    require(sha256_file(PLAN) == PLAN_SHA, "FEATURE_PLAN_HASH_MISMATCH")
    return json.loads(PLAN.read_text(encoding="utf-8"))


def validate_trades(rows: list[dict], phase: str, plan: dict) -> None:
    require(len(rows) == plan["phases"][phase]["fully_verified_N"], f"FEATURE_INPUT_N:{phase}")
    scope = next(value for value in PHASES if value.name == phase)
    ids = set()
    for row in rows:
        key = (row["symbol"], row["opened_at_epoch"], row["signal_id"])
        require(key not in ids, f"FEATURE_DUPLICATE_TRADE:{phase}")
        ids.add(key)
        native = row["native_row"]
        require(row["symbol"] in plan["symbols"] and row["direction"] in ("LONG", "SHORT")
                and scope.start <= row["opened_at_epoch"] <= row["closed_at_epoch"] < scope.end,
                f"FEATURE_TRADE_SCOPE:{phase}")
        require(native["status"] == "CLOSED" and native["funding_complete"]
                and native["commission_complete"] and native["remaining_quantity"] == 0
                and native["initial_risk_usdt"] > 0
                and row["net_r"] == native["net_r"] and row["path"] in (
                    "STOP_BEFORE_TP1", "STOP_AFTER_TP1", "TP3", "OTHER"),
                f"FEATURE_NOT_VERIFIED:{phase}")
        require(type(row["net_r"]) in (int, float) and math.isfinite(row["net_r"]),
                f"FEATURE_NONFINITE_NET_R:{phase}")


def load_inputs(plan: dict) -> dict:
    require(sha256_file(INPUT / "diagnosis.json") == plan["inputs"]["diagnosis_sha256"],
            "FEATURE_DIAGNOSIS_HASH_MISMATCH")
    rows = {}
    for phase in ("TRAIN", "VALIDATION"):
        path = INPUT / f"trades-{phase}.jsonl"
        require(sha256_file(path) == plan["inputs"][f"trades_{phase}_sha256"], f"FEATURE_RAW_HASH:{phase}")
        rows[phase] = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        validate_trades(rows[phase], phase, plan)
    return rows


def feature_values(series, row: dict) -> dict:
    at = row["opened_at_epoch"]
    candles = series.closed(at)
    require(len(candles) == 259 and series.history_complete(at), "FEATURE_INCOMPLETE_CLOSED_HISTORY")
    result = analyze(candles)
    require(result["entry"] == row["decision_price"] and result["ema"]["ema20"] == row["ema20_15m"]
            and result["atr"] == row["atr14_15m"] and result["confidence"] == row["confidence"]
            and result["direction"] == row["direction"], "FEATURE_NATIVE_CONTEXT_PARITY")
    atr, price = result["atr"], result["entry"]
    require(atr > 0 and price > 0, "FEATURE_INVALID_ATR_OR_PRICE")
    values = {
        "F1": result["adx"],
        "F2": abs(result["ema"]["ema20"] - result["ema"]["ema200"]) / atr,
        "F3": atr / price,
        "F4": (1 if row["direction"] == "LONG" else -1) * (price - candles[-96]["open"]) / atr,
    }
    require(all(type(value) in (int, float) and math.isfinite(value) for value in values.values()),
            "FEATURE_NONFINITE_NATIVE_VALUE")
    return values


def cell(value, n: int) -> dict:
    return {"value": value, "N": n, "reliability": "guvenilmez" if n < 30 else "descriptive_only"}


def stats(rows: list[dict], all_tp3: int) -> dict:
    n = len(rows)
    stop = sum(row["path"] == "STOP_BEFORE_TP1" for row in rows)
    tp3 = sum(row["path"] == "TP3" for row in rows)
    return {
        "N": n, "mean_net_r": cell(fmean(row["net_r"] for row in rows) if n else None, n),
        "stop_before_tp1_share_pct": cell(stop / n * 100 if n else None, n),
        "tp3_share_pct": cell(tp3 / n * 100 if n else None, n),
        "share_of_all_phase_tp3_pct": cell(tp3 / all_tp3 * 100 if all_tp3 else None, all_tp3),
    }


def assess(groups: dict, phase_rows: dict) -> dict:
    available = [bucket for bucket in BUCKETS if groups["TRAIN"][bucket]]
    require(bool(available), "FEATURE_NO_TRAIN_BUCKET_MEAN")
    selected = min(available, key=lambda bucket: (
        fmean(row["net_r"] for row in groups["TRAIN"][bucket]), BUCKETS.index(bucket)))
    failed, details = [], {}
    for phase in ("TRAIN", "VALIDATION"):
        excluded = groups[phase][selected]
        remaining = [row for bucket in BUCKETS if bucket != selected for row in groups[phase][bucket]]
        overall = fmean(row["net_r"] for row in phase_rows[phase])
        mean = fmean(row["net_r"] for row in excluded) if excluded else None
        conditions = {
            "excluded_N_ge_30": len(excluded) >= 30,
            "excluded_mean_below_overall": mean is not None and mean < overall,
            "excluded_tp3_share_le_stop_before_tp1_share": (
                sum(row["path"] == "TP3" for row in excluded)
                <= sum(row["path"] == "STOP_BEFORE_TP1" for row in excluded)),
        }
        if phase == "VALIDATION":
            conditions["remaining_N_ge_100"] = len(remaining) >= 100
        failed.extend(f"{phase}:{key}" for key, passed in conditions.items() if not passed)
        details[phase] = {
            "excluded_N": len(excluded), "remaining_N": len(remaining),
            "overall_mean_net_r": overall, "excluded_mean_net_r": mean,
            "remaining_mean_net_r": fmean(row["net_r"] for row in remaining) if remaining else None,
            "conditions": conditions,
        }
    train = details["TRAIN"]
    score = train["remaining_mean_net_r"] - train["overall_mean_net_r"] if train["remaining_N"] else None
    return {"excluded_bucket": selected, "candidate": not failed, "failed_conditions": failed,
            "train_ranking_score": score, "phases": details}


def summarize(records: dict, plan: dict) -> dict:
    require(set(records) == {"TRAIN", "VALIDATION"} and all(records.values()), "FEATURE_PHASE_ROWS_REQUIRED")
    cuts, tables, assessments = {}, {}, {}
    for feature in FEATURES:
        cuts[feature] = [percentile([row["features"][feature] for row in records["TRAIN"]], fraction)
                         for fraction in plan["tertiles"]["fractions"]]
        groups = {}
        tables[feature] = {}
        for phase in ("TRAIN", "VALIDATION"):
            groups[phase] = {bucket: [] for bucket in BUCKETS}
            for row in records[phase]:
                value = row["features"][feature]
                bucket = "LOW" if value <= cuts[feature][0] else "MIDDLE" if value <= cuts[feature][1] else "HIGH"
                groups[phase][bucket].append(row)
            total_tp3 = sum(row["path"] == "TP3" for row in records[phase])
            tables[feature][phase] = {bucket: stats(rows, total_tp3) for bucket, rows in groups[phase].items()}
        assessments[feature] = assess(groups, records)
    eligible = [feature for feature in FEATURES if assessments[feature]["candidate"]]
    winner = max(eligible, key=lambda feature: (
        assessments[feature]["train_ranking_score"], -FEATURES.index(feature))) if eligible else None
    return {
        "phase_N": {phase: len(rows) for phase, rows in records.items()},
        "cutpoints": {"source": "TRAIN", "N": len(records["TRAIN"]),
                      "fractions": plan["tertiles"]["fractions"], "features": cuts},
        "tables": tables, "assessments": assessments,
        "mechanical_candidate": {
            "value": "ADAY" if winner else "ADAY YOK", "eligible_features": eligible,
            "selected_feature": winner, "excluded_bucket": assessments[winner]["excluded_bucket"] if winner else None,
            "ranking": plan["candidate_rule"]["ranking"], "strategy_changed": False,
        },
    }


def write_features(output: Path, phase: str, rows: list[dict]) -> dict:
    path = output / f"features-{phase}.jsonl"
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
    require([json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] == rows,
            f"FEATURE_OUTPUT_ROUNDTRIP:{phase}")
    return {"file": path.name, "N": len(rows), "sha256": sha256_file(path)}


def validate_report(report: dict, records: dict, plan: dict) -> None:
    require(set(report) == {"schema", "plan_sha256", "provenance", "raw_features", "diagnosis"}
            and report["schema"] == SCHEMA and report["plan_sha256"] == PLAN_SHA, "FEATURE_REPORT_REGISTRATION")
    require(report["diagnosis"] == summarize(records, plan), "FEATURE_REPORT_MECHANICAL_MISMATCH")
    require(report["diagnosis"]["phase_N"] == {
        phase: scope["fully_verified_N"] for phase, scope in plan["phases"].items()
    }, "FEATURE_REPORT_N_MISMATCH")
    require(set(report["raw_features"]) == {"TRAIN", "VALIDATION"}, "FEATURE_REPORT_PHASES")
    for phase, manifest in report["raw_features"].items():
        require(set(manifest) == {"file", "N", "sha256"} and manifest["file"] == f"features-{phase}.jsonl"
                and manifest["N"] == len(records[phase]) and isinstance(manifest["sha256"], str)
                and len(manifest["sha256"]) == 64
                and all(char in "0123456789abcdef" for char in manifest["sha256"]), f"FEATURE_OUTPUT_MANIFEST:{phase}")
    json.dumps(report, allow_nan=False)


def guarded_main() -> int:
    try:
        require(not OUTPUT.exists(), "RESULTS_ALREADY_EXIST")
        plan = load_plan()
        require(sha256_file(BACKEND / "app" / "analysis.py") == plan["analysis_source_sha256"],
                "FEATURE_ANALYSIS_SOURCE_CHANGED")
        require(sha256_file(METADATA) == plan["inputs"]["metadata_sha256"], "FEATURE_METADATA_CHANGED")
        require(sha256_file(VIEW / "manifest.json") == plan["inputs"]["measurement_manifest_sha256"],
                "FEATURE_VIEW_CHANGED")
        saved = load_inputs(plan)
        emit(sys.stdout, {"status": "started", "replay": False, "plan_sha256": PLAN_SHA})
        with quiet_native(), deny_network():
            data = load_measurement_dataset(VIEW, METADATA, PHASES[0].start, PHASES[-1].end)
            validate_loaded_scope(data, plan["symbols"])
            records = {}
            for phase, rows in saved.items():
                records[phase] = [{
                    "symbol": row["symbol"], "direction": row["direction"], "signal_id": row["signal_id"],
                    "opened_at": row["opened_at"], "net_r": row["net_r"], "path": row["path"],
                    "features": feature_values(data.frames[row["symbol"]]["15m"], row),
                } for row in rows]
                require(len(records[phase]) == plan["phases"][phase]["fully_verified_N"], f"FEATURE_N_CHANGED:{phase}")
            diagnosis = summarize(records, plan)
        OUTPUT.mkdir(parents=True, exist_ok=False)
        manifests = {phase: write_features(OUTPUT, phase, rows) for phase, rows in records.items()}
        report = {
            "schema": SCHEMA, "plan_sha256": PLAN_SHA,
            "provenance": {
                "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "script_sha256": sha256_file(Path(__file__)), "inputs": plan["inputs"],
                "analysis_source_sha256": plan["analysis_source_sha256"],
                "profile_sha256": plan["profile_sha256"],
                "original_parameter_sha256": plan["original_parameter_sha256"],
                "replay": False, "strategy_changed": False, "experiment_attempt_used": False,
            },
            "raw_features": manifests, "diagnosis": diagnosis,
        }
        validate_report(report, records, plan)
        path = OUTPUT / "feature-diagnosis.json"
        with path.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, allow_nan=False)
            handle.write("\n")
        emit(sys.stdout, {"status": "completed", "output": str(path), "sha256": sha256_file(path),
                          "mechanical_candidate": diagnosis["mechanical_candidate"], "raw_features": manifests})
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, ArithmeticError) as exc:
        emit(sys.stdout, {"status": "failed", "code": str(exc), "error_class": type(exc).__name__})
        return 2


def main() -> int:
    with research_path_guard():
        return guarded_main()


if __name__ == "__main__":
    raise SystemExit(main())
