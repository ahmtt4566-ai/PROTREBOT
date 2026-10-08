"""Locked component diagnosis from saved trades; no engine or replay."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from statistics import fmean
from typing import Any

from app.analysis import analyze
from app.backtest_baseline import percentile
from build_measurement_view import load_measurement_dataset, sha256_file
from diagnose_original_v2_features import cell, stats
from measure_donchian_counts import (
    BACKEND,
    PHASES,
    VIEW,
    MeasurementError,
    emit,
    epoch,
    validate_loaded_scope,
)
from measure_original_counts import METADATA

PLAN = BACKEND / "studies" / "original-component-diagnosis-plan.json"
PLAN_SHA = "0af053ea22a352f10dd63d424e6d12783472267402d11c741d8689593cba861f"
LEDGER = BACKEND / "studies" / "original-development-ledger.json"
OUTPUT = Path.home() / "kaistrade-data" / "original-component-diagnosis"
PHASE_NAMES = ("TRAIN", "VALIDATION", "NEW_WINDOW")
COMPONENTS = (
    ("C1_PRICE_EMA20", 10), ("C2_EMA20_EMA50", 12), ("C3_EMA50_EMA200", 14),
    ("C4_MACD", 10), ("C5_RSI", 9), ("C6_BOLLINGER_MID", 6),
    ("C7_ADX_EMA", 8), ("C8_VOLUME", 6),
)
IDS = tuple(name for name, _ in COMPONENTS)
SCORES = ("raw_total", "confidence")
BUCKETS = ("LOW", "MIDDLE", "HIGH")


def require(ok: bool, code: str) -> None:
    if not ok:
        raise MeasurementError(code)


def finite(value: Any) -> bool:
    return type(value) in (float, int) and math.isfinite(value)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def check_event(event: str, args: tuple, output: Path | None) -> None:
    if event in {"socket.connect", "socket.bind", "socket.getaddrinfo", "subprocess.Popen"}:
        raise MeasurementError("COMPONENT_OFFLINE_OPERATION_DENIED")
    if event == "sqlite3.connect" and args[0] != ":memory:":
        raise MeasurementError("COMPONENT_PERSISTENT_DATABASE_DENIED")
    if event not in {"open", "os.listdir", "os.scandir", "os.remove", "os.rename",
                     "os.mkdir", "os.rmdir"}:
        return
    for value in args[:2]:
        if isinstance(value, (str, bytes, os.PathLike)):
            text = os.fsdecode(value).lower().replace("\\", "/")
            require(not any(part in text for part in (
                "donchian-test-", "test-sealed.7z", "independent-block",
                "donchian-holdout-results", "protrebot-research",
            )) and not any(
                "sealed" in part or "registry" in part or "registries" in part
                for part in text.split("/") if not part.endswith((".py", ".pyc"))
            ), "COMPONENT_PROTECTED_PATH_DENIED")
    writing = event in {"os.remove", "os.rename", "os.mkdir", "os.rmdir"}
    if event == "open":
        mode, flags = args[1:3]
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or bool(
            flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
    if writing:
        require(output is not None, "COMPONENT_READ_ONLY")
        for value in args[:2] if event == "os.rename" else args[:1]:
            require(isinstance(value, (str, bytes, os.PathLike)), "COMPONENT_WRITE_DESCRIPTOR_DENIED")
            target = Path(os.fsdecode(value)).resolve()
            require(target == output or target.is_relative_to(output), "COMPONENT_WRITE_SCOPE_DENIED")


@contextmanager
def offline_guard(output: Path | None) -> Iterator[None]:
    active = True

    def audit(event: str, args: tuple) -> None:
        if active:
            check_event(event, args, output)

    sys.addaudithook(audit)
    try:
        yield
    finally:
        active = False


def locked_plan() -> dict:
    require(sha256_file(PLAN) == PLAN_SHA, "COMPONENT_PLAN_CHANGED")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    require(tuple((c["id"], c["points"]) for c in plan["components"]) == COMPONENTS,
            "COMPONENT_LIST_CHANGED")
    require(sha256_file(BACKEND / "app" / "analysis.py") == plan["analysis_source_sha256"],
            "COMPONENT_ANALYSIS_CHANGED")
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    require(ledger["trial_limit"] is None and [r["number"] for r in ledger["records"]] == list(range(1, 6))
            and ledger["records"][-1]["plan_sha256"] == PLAN_SHA, "COMPONENT_LEDGER_MISMATCH")
    return plan


def validate_trades(rows: list[dict], phase: str, plan: dict) -> None:
    scope = plan["phases"][phase]
    require(len(rows) == scope["fully_verified_N"], f"COMPONENT_N_MISMATCH:{phase}")
    identities = set()
    for row in rows:
        native = row["native_row"]
        identity = (row["symbol"], row["signal_id"])
        require(identity not in identities, f"COMPONENT_DUPLICATE:{phase}")
        identities.add(identity)
        require(row["symbol"] in plan["symbols"] and row["direction"] in ("LONG", "SHORT")
                and epoch(scope["start"]) <= row["opened_at_epoch"] <= row["closed_at_epoch"]
                < epoch(scope["end_exclusive"]), f"COMPONENT_PHASE_SCOPE:{phase}")
        require(native["status"] == "CLOSED" and native["funding_complete"]
                and native["commission_complete"] and native["remaining_quantity"] == 0
                and finite(native["initial_risk_usdt"]) and native["initial_risk_usdt"] > 0
                and row["net_r"] == native["net_r"] and finite(row["net_r"])
                and row["path"] in ("STOP_BEFORE_TP1", "STOP_AFTER_TP1", "TP3", "OTHER"),
                f"COMPONENT_NOT_VERIFIED:{phase}")


def directional_flags(result: dict, direction: str) -> tuple[bool, ...]:
    require(direction in ("LONG", "SHORT"), "COMPONENT_DIRECTION")
    price = result["entry"]
    e20, e50, e200 = (result["ema"][key] for key in ("ema20", "ema50", "ema200"))
    macd, rsi = result["macd"], result["rsi"]
    mid, adx, volume = result["bollinger"]["middle"], result["adx"], result["volume_ratio"]
    require(all(finite(v) for v in (price, e20, e50, e200, macd, rsi, mid, adx, volume)),
            "COMPONENT_NONFINITE_ANALYSIS")
    if direction == "LONG":
        return (price > e20, e20 > e50, e50 > e200, macd > 0, 52 <= rsi <= 72,
                price > mid, adx >= 20 and e20 > e50, volume >= 1.05)
    return (price < e20, e20 < e50, e50 < e200, macd < 0, 28 <= rsi <= 48,
            price < mid, adx >= 20 and e20 < e50, volume >= 1.05)


def component_record(row: dict, candles: list[dict]) -> dict:
    require(len(candles) == 259, "COMPONENT_WINDOW_NOT_259")
    times = [c["time"] for c in candles]
    require(all(finite(t) for t in times)
            and all(b - a == 900 for a, b in pairwise(times))
            and times[-1] + 900 == row["opened_at_epoch"], "COMPONENT_HISTORY_OR_LOOKAHEAD")
    totals: dict[str, int] = {}
    result = analyze(candles, observation_scores=totals)
    require(result["direction"] == row["direction"] and result["confidence"] == row["confidence"]
            and result["entry"] == row["decision_price"]
            and result["ema"]["ema20"] == row["ema20_15m"]
            and result["atr"] == row["atr14_15m"], "COMPONENT_NATIVE_PARITY")
    if "canonical_analysis" in row:
        require(all(result[k] == v for k, v in row["canonical_analysis"].items()),
                "COMPONENT_CANONICAL_PARITY")
    for direction in ("LONG", "SHORT"):
        flags = directional_flags(result, direction)
        total = 25 + sum(weight * flag for (_, weight), flag in zip(COMPONENTS, flags))
        require(total == totals[f"{direction.lower()}_score"], "COMPONENT_NATIVE_TOTAL_PARITY")
    flags = directional_flags(result, row["direction"])
    raw_total = totals[f"{row['direction'].lower()}_score"]
    require(raw_total == max(totals.values()) and min(95, raw_total) == result["confidence"],
            "COMPONENT_CONFIDENCE_PARITY")
    return {
        "symbol": row["symbol"], "direction": row["direction"], "signal_id": row["signal_id"],
        "opened_at": row["opened_at"], "net_r": row["net_r"], "path": row["path"],
        "binary": dict(zip(IDS, (int(flag) for flag in flags))),
        "points": {key: weight * int(flag) for (key, weight), flag in zip(COMPONENTS, flags)},
        "raw_total": raw_total, "confidence": result["confidence"],
        "native_long_score": totals["long_score"], "native_short_score": totals["short_score"],
        "context_sha256": digest(candles),
    }


def entry_context_digest(row: dict) -> str:
    return digest({k: row[k] for k in (
        "symbol", "direction", "signal_id", "opened_at_epoch",
        "canonical_analysis", "closed_contract_context",
    )})


def validate_verified_entry(row: dict) -> None:
    require(row["fully_verified"] is True and row["status"] == "CLOSED"
            and row["funding_known"] is True
            and isinstance(row["remaining_quantity_decimal"], str), "COMPONENT_ENTRY_NOT_VERIFIED")
    try:
        remaining = Decimal(row["remaining_quantity_decimal"])
    except InvalidOperation as exc:
        raise MeasurementError("COMPONENT_ENTRY_INVALID_DECIMAL") from exc
    require(remaining.is_finite() and remaining == 0, "COMPONENT_ENTRY_REMAINING_NOT_ZERO")


def load_records(plan: dict) -> dict[str, list[dict]]:
    for key in ("measurement_manifest_sha256", "metadata_sha256"):
        path = VIEW / "manifest.json" if key.startswith("measurement") else METADATA
        require(sha256_file(path) == plan["inputs"][key], f"COMPONENT_INPUT_CHANGED:{key}")
    for value in plan["inputs"].values():
        if isinstance(value, dict):
            require(sha256_file(Path(value["path"])) == value["sha256"], "COMPONENT_RAW_CHANGED")
    verified_entries = {}
    entries_path = Path(plan["inputs"]["entries_NEW_WINDOW"]["path"])
    accepted, unverified = 0, 0
    with entries_path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            accepted += 1
            if row["fully_verified"]:
                validate_verified_entry(row)
                key = (row["symbol"], row["signal_id"])
                require(key not in verified_entries, "COMPONENT_DUPLICATE_ENTRY")
                verified_entries[key] = entry_context_digest(row)
            else:
                unverified += 1
                require("net_r" not in row and "net_pnl" not in row, "COMPONENT_UNVERIFIED_METRICS")
    require(accepted == 557 and unverified == 2 and len(verified_entries) == 555,
            "COMPONENT_NEW_ENTRY_COUNTS")
    data = load_measurement_dataset(VIEW, METADATA, PHASES[0].start, PHASES[-1].end)
    validate_loaded_scope(data, plan["symbols"])
    records = {}
    for phase in PHASE_NAMES:
        path = Path(plan["inputs"][f"trades_{phase}"]["path"])
        with path.open(encoding="utf-8") as stream:
            rows = [json.loads(line) for line in stream]
        validate_trades(rows, phase, plan)
        records[phase] = []
        for row in rows:
            if phase == "NEW_WINDOW":
                key = (row["symbol"], row["signal_id"])
                require(key in verified_entries
                        and entry_context_digest(row) == verified_entries.pop(key),
                        "COMPONENT_ENTRY_TRADE_PARITY")
                candles = row["closed_contract_context"]["15m"]
            else:
                series = data.frames[row["symbol"]]["15m"]
                require(series.history_complete(row["opened_at_epoch"]), "COMPONENT_INCOMPLETE_HISTORY")
                candles = series.closed(row["opened_at_epoch"])
            records[phase].append(component_record(row, candles))
        emit(sys.stdout, {"status": "component_parity_verified", "phase": phase,
                          "N": len(records[phase]), "replay": False})
    require(not verified_entries, "COMPONENT_UNMATCHED_VERIFIED_ENTRIES")
    return records


def checked_vectors(left: Sequence[float], right: Sequence[float]) -> None:
    require(len(left) == len(right) and len(left) >= 2
            and all(finite(v) for v in (*left, *right)), "COMPONENT_CORRELATION_INPUT")


def pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    checked_vectors(left, right)
    xmean, ymean = fmean(left), fmean(right)
    x = [v - xmean for v in left]
    y = [v - ymean for v in right]
    variance = math.fsum(v * v for v in x) * math.fsum(v * v for v in y)
    if variance == 0:
        return None
    value = math.fsum(a * b for a, b in zip(x, y)) / math.sqrt(variance)
    require(-1 - 1e-12 <= value <= 1 + 1e-12, "COMPONENT_CORRELATION_RANGE")
    return max(-1.0, min(1.0, value))


def phi(left: Sequence[int], right: Sequence[int]) -> float | None:
    checked_vectors(left, right)
    require(all(v in (0, 1) for v in (*left, *right)), "COMPONENT_BINARY_INPUT")
    n11 = sum(a == b == 1 for a, b in zip(left, right))
    n00 = sum(a == b == 0 for a, b in zip(left, right))
    n10 = sum(a == 1 and b == 0 for a, b in zip(left, right))
    n01 = len(left) - n11 - n00 - n10
    denominator = (n11 + n10) * (n00 + n01) * (n11 + n01) * (n00 + n10)
    return (n11 * n00 - n10 * n01) / math.sqrt(denominator) if denominator else None


def ranks(values: Sequence[float]) -> list[float]:
    require(bool(values) and all(finite(v) for v in values), "COMPONENT_RANK_INPUT")
    order = sorted(range(len(values)), key=lambda i: values[i])
    result = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        for i in order[start:end]:
            result[i] = (start + 1 + end) / 2
        start = end
    return result


def spearman(left: Sequence[float], right: Sequence[float]) -> float | None:
    checked_vectors(left, right)
    return pearson(ranks(left), ranks(right))


def eigenvalues(matrix: list[list[float]]) -> list[float]:
    n = len(matrix)
    require(n > 0 and all(len(row) == n for row in matrix), "COMPONENT_EIGEN_SHAPE")
    require(all(finite(matrix[i][j]) and abs(matrix[i][j] - matrix[j][i]) <= 1e-12
                for i in range(n) for j in range(n)), "COMPONENT_EIGEN_SYMMETRY")
    a = [row[:] for row in matrix]
    for _ in range(200 * n * n):
        pairs = [(abs(a[i][j]), i, j) for i in range(n) for j in range(i + 1, n)]
        if not pairs or max(pairs)[0] <= 1e-12:
            values = sorted(a[i][i] for i in range(n))
            require(min(values) >= -1e-8, "COMPONENT_CORRELATION_NOT_PSD")
            values = [max(0.0, v) for v in values]
            require(math.isclose(math.fsum(values), math.fsum(matrix[i][i] for i in range(n)),
                                 abs_tol=1e-8)
                    and math.isclose(math.fsum(v * v for v in values),
                                     math.fsum(v * v for row in matrix for v in row),
                                     abs_tol=1e-8), "COMPONENT_EIGEN_INVARIANTS")
            return values
        _, p, q = max(pairs)
        angle = 0.5 * math.atan2(2 * a[p][q], a[q][q] - a[p][p])
        c, s = math.cos(angle), math.sin(angle)
        app, aqq, apq = a[p][p], a[q][q], a[p][q]
        for k in range(n):
            if k not in (p, q):
                akp, akq = a[k][p], a[k][q]
                a[k][p] = a[p][k] = c * akp - s * akq
                a[k][q] = a[q][k] = s * akp + c * akq
        a[p][p] = c * c * app - 2 * s * c * apq + s * s * aqq
        a[q][q] = s * s * app + 2 * s * c * apq + c * c * aqq
        a[p][q] = a[q][p] = 0.0
    raise MeasurementError("COMPONENT_EIGEN_DID_NOT_CONVERGE")


def participation_ratio(matrix: list[list[float]]) -> tuple[float, list[float]]:
    values = eigenvalues(matrix)
    denominator = math.fsum(v * v for v in values)
    require(denominator > 0, "COMPONENT_ZERO_SPECTRUM")
    return math.fsum(values) ** 2 / denominator, values


def metric_cell(value: float | None, n: int, reason: str | None = None) -> dict:
    result = cell(value, n)
    if value is None:
        require(reason is not None, "COMPONENT_UNEXPLAINED_NULL")
        result["undefined_reason"] = reason
    return result


def group_stats(rows: list[dict]) -> dict:
    result = stats(rows, sum(row["path"] == "TP3" for row in rows))
    result.pop("share_of_all_phase_tp3_pct")
    if not rows:
        for key in ("mean_net_r", "stop_before_tp1_share_pct", "tp3_share_pct"):
            result[key]["undefined_reason"] = "EMPTY_GROUP"
    return result


def train_cuts(records: dict[str, list[dict]], plan: dict) -> dict[str, list[float]]:
    require(bool(records.get("TRAIN")), "COMPONENT_TRAIN_REQUIRED")
    return {score: [percentile([row[score] for row in records["TRAIN"]], fraction)
                    for fraction in plan["scores"]["fractions"]] for score in SCORES}


def bucket(value: float, cuts: list[float]) -> str:
    require(len(cuts) == 2 and finite(value) and all(finite(v) for v in cuts)
            and cuts[0] <= cuts[1], "COMPONENT_CUT_INPUT")
    return "LOW" if value <= cuts[0] else "MIDDLE" if value <= cuts[1] else "HIGH"


def matrices(rows: list[dict]) -> tuple[dict, dict]:
    n = len(rows)
    binary = {key: [row["binary"][key] for row in rows] for key in IDS}
    points = {key: [row["points"][key] for row in rows] for key in IDS}
    values = {"agreement": {}, "phi": {}, "spearman": {}}
    for left in IDS:
        for matrix in values.values():
            matrix[left] = {}
        for right in IDS:
            agreement = sum(a == b for a, b in zip(binary[left], binary[right])) / n
            values["agreement"][left][right] = metric_cell(agreement, n)
            values["phi"][left][right] = metric_cell(phi(binary[left], binary[right]), n,
                                                    "CONSTANT_COMPONENT")
            values["spearman"][left][right] = metric_cell(spearman(points[left], points[right]), n,
                                                         "CONSTANT_COMPONENT")
    varying = [key for key in IDS if len(set(binary[key])) > 1]
    effective: dict[str, Any] = {
        "component_count": len(IDS), "nonconstant_components": varying,
        "constant_components": [key for key in IDS if key not in varying],
        "formula": "(sum(lambda))^2 / sum(lambda^2)",
    }
    if varying:
        matrix = [[values["phi"][a][b]["value"] for b in varying] for a in varying]
        ratio, spectrum = participation_ratio(matrix)
        effective.update(value=metric_cell(ratio, n), eigenvalues=spectrum)
    else:
        effective.update(value=metric_cell(None, n, "NO_NONCONSTANT_COMPONENTS"), eigenvalues=[])
    return values, effective


def count_scalar_cells(value: Any) -> int:
    if isinstance(value, dict):
        if {"value", "N", "reliability"} <= value.keys():
            return 1
        return sum(count_scalar_cells(v) for v in value.values())
    if isinstance(value, list):
        return sum(count_scalar_cells(v) for v in value)
    return 0


def summarize(records: dict[str, list[dict]], plan: dict) -> dict:
    require(set(records) == set(PHASE_NAMES), "COMPONENT_PHASES")
    for phase, rows in records.items():
        require(len(rows) == plan["phases"][phase]["fully_verified_N"], f"COMPONENT_N_MISMATCH:{phase}")
        for row in rows:
            require(set(row["binary"]) == set(row["points"]) == set(IDS)
                    and all(row["binary"][key] in (0, 1)
                            and row["points"][key] == weight * row["binary"][key]
                            for key, weight in COMPONENTS)
                    and finite(row["net_r"]) and row["path"] in (
                        "STOP_BEFORE_TP1", "STOP_AFTER_TP1", "TP3", "OTHER"),
                    "COMPONENT_RECORD_SCHEMA")
    cuts = train_cuts(records, plan)
    phases = {}
    for phase, rows in records.items():
        dependence, effective = matrices(rows)
        phases[phase] = {
            "N": len(rows), "mean_net_r": metric_cell(fmean(row["net_r"] for row in rows), len(rows)),
            "dependence": dependence, "effective_independent_confirmations": effective,
            "component_states": {
                key: {str(state): group_stats([row for row in rows if row["binary"][key] == state])
                      for state in (0, 1)} for key in IDS
            },
            "score_tertiles": {
                score: {name: group_stats([row for row in rows if bucket(row[score], cuts[score]) == name])
                        for name in BUCKETS} for score in SCORES
            },
        }
    require(count_scalar_cells(phases) == plan["counter"]["current_reported_scalar_cells"],
            "COMPONENT_CELL_COUNTER_MISMATCH")
    return {"train_cutpoints": cuts, "phases": phases}


def validate_history_counter(plan: dict) -> None:
    for item in plan["counter"]["historical_scope"]:
        path = Path.home() / "kaistrade-data" / item["report"].replace("/", "\\")
        require(sha256_file(path) == item["sha256"], "COMPONENT_HISTORY_REPORT_CHANGED")
        report = json.loads(path.read_text(encoding="utf-8"))
        tables = ({name: phase["tables"] for name, phase in report["phases"].items()}
                  if "phases" in report else report["diagnosis"]["tables"])
        require(count_scalar_cells(tables) == item["scalar_cells"], "COMPONENT_HISTORY_CELL_COUNT")


def execute(plan: dict, output: Path) -> dict:
    require(output.resolve() == OUTPUT.resolve() and not output.exists(), "COMPONENT_OUTPUT_EXISTS_OR_SCOPE")
    validate_history_counter(plan)
    records = load_records(plan)
    diagnosis = summarize(records, plan)
    output.mkdir(exist_ok=False)
    manifests = {}
    for phase, rows in records.items():
        path = output / f"components-{phase}.jsonl"
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                stream.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        with path.open(encoding="utf-8") as stream:
            require([json.loads(line) for line in stream] == rows, "COMPONENT_RAW_ROUNDTRIP")
        manifests[phase] = {"file": path.name, "N": len(rows), "sha256": sha256_file(path)}
    report = {
        "schema": "original-component-diagnosis-v1", "plan_sha256": PLAN_SHA,
        "ledger_sha256": sha256_file(LEDGER), "script_sha256": sha256_file(Path(__file__)),
        "profile_sha256": plan["profile_sha256"], "analysis_source_sha256": plan["analysis_source_sha256"],
        "inputs": plan["inputs"], "phase_definitions": plan["phases"],
        "component_definitions": plan["components"], "counter": plan["counter"],
        "mechanical_notes": [
            "FULLY_VERIFIED_ACCEPTED_TRADES_ONLY; CONDITIONAL_SELECTION_NOT_ALL_CANDIDATES",
            "DIRECTION_ALIGNED_COMPONENTS; PHASES_NOT_POOLED",
            "SPEARMAN_OF_0_OR_POSITIVE_WEIGHT_HAS_THE_SAME_RANKING_AS_THE_BINARY_COMPONENT",
            "CONSTANT_COMPONENT_CORRELATIONS_UNDEFINED; OMITTED_ONLY_FROM_THE_EIGENSPECTRUM",
            "PARTICIPATION_RATIO_IS_EFFECTIVE_DIMENSION_NOT_PROOF_OF_INDEPENDENCE_OR_CAUSALITY",
            "RAW_TOTALS_NOT_STORED_HISTORICALLY; RECONSTRUCTED_TOTALS_MATCH_NATIVE_OBSERVATION_SCORES",
            "TRAIN_ONLY_TERTILES; TIES_NOT_SPLIT; EMPTY_GROUPS_NOT_DROPPED",
            "HISTORICAL_COUNTER_IS_AN_EXPLICIT_LOWER_BOUND_NOT_A_COMPLETE_LIFETIME_COUNT",
            "NO_REPLAY_NO_RULE_CHANGE_NO_RECOMMENDATION_NO_CANDIDATE_SELECTION",
        ],
        "raw_components": manifests, "diagnosis": diagnosis,
    }
    path = output / "component-diagnosis.json"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, allow_nan=False, indent=2)
        stream.write("\n")
    with path.open(encoding="utf-8") as stream:
        require(json.load(stream) == report, "COMPONENT_REPORT_ROUNDTRIP")
    emit(sys.stdout, {"status": "completed", "output": str(path), "sha256": sha256_file(path),
                      "counter": plan["counter"], "raw_components": manifests})
    return report


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    sys.dont_write_bytecode = True
    with offline_guard(OUTPUT.resolve()):
        require(not OUTPUT.exists(), "COMPONENT_OUTPUT_EXISTS")
        execute(locked_plan(), OUTPUT)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MeasurementError, OSError) as exc:
        emit(sys.stdout, {"status": "failed", "code": str(exc), "error_class": type(exc).__name__})
        raise SystemExit(2) from exc
