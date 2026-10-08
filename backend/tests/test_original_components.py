from __future__ import annotations

from copy import deepcopy

import diagnose_original_components as diagnosis
import pytest
from app.analysis import analyze
from measure_donchian_counts import MeasurementError, epoch


def saved_trade():
    at = epoch("2024-08-01")
    candles = [
        {"time": at - (259 - i) * 900, "open": 100 + i * 0.1,
         "close": 100.05 + i * 0.1, "high": 101 + i * 0.1,
         "low": 99 + i * 0.1, "volume": 100 if i < 258 else 110}
        for i in range(259)
    ]
    result = analyze(candles)
    row = {
        "symbol": "BTCUSDT", "direction": result["direction"], "signal_id": "synthetic",
        "opened_at": "2024-08-01", "opened_at_epoch": at, "closed_at_epoch": at + 900,
        "net_r": 1.0, "path": "TP3", "confidence": result["confidence"],
        "decision_price": result["entry"], "ema20_15m": result["ema"]["ema20"],
        "atr14_15m": result["atr"], "canonical_analysis": {
            key: value for key, value in result.items() if key != "series"
        },
        "native_row": {"status": "CLOSED", "funding_complete": True, "commission_complete": True,
                       "remaining_quantity": 0, "initial_risk_usdt": 3.0, "net_r": 1.0},
    }
    return row, candles


def small_plan(n=4):
    return {
        "phases": {name: {"start": "2024-07-01", "end_exclusive": "2026-10-01",
                         "fully_verified_N": n} for name in diagnosis.PHASE_NAMES},
        "symbols": ["BTCUSDT"], "scores": {"fractions": [0.333, 0.667]},
        "counter": {"current_reported_scalar_cells": 780},
    }


def records():
    rows = []
    for i in range(4):
        flags = {key: (i // 2 if j % 2 else i % 2) for j, key in enumerate(diagnosis.IDS)}
        rows.append({
            "binary": flags, "points": {key: weight * flags[key] for key, weight in diagnosis.COMPONENTS},
            "raw_total": 25 + sum(weight * flags[key] for key, weight in diagnosis.COMPONENTS),
            "confidence": min(95, 25 + sum(weight * flags[key] for key, weight in diagnosis.COMPONENTS)),
            "net_r": i - 1.0, "path": "TP3" if i % 2 else "STOP_BEFORE_TP1",
        })
    return {name: deepcopy(rows) for name in diagnosis.PHASE_NAMES}


@pytest.mark.parametrize("right,expected", [
    ([0, 0, 1, 1], 1.0), ([1, 1, 0, 0], -1.0), ([0, 1, 0, 1], 0.0),
])
def test_phi_known_contingency_tables(right, expected):
    assert diagnosis.phi([0, 0, 1, 1], right) == expected
    assert diagnosis.pearson([0, 0, 1, 1], right) == expected


def test_spearman_average_tie_ranks_and_numeric_weights():
    assert diagnosis.ranks([10, 10, 20, 30]) == [1.5, 1.5, 3, 4]
    assert diagnosis.spearman([0, 0, 12, 12], [0, 14, 0, 14]) == 0
    assert diagnosis.spearman([0, 0, 12, 12], [0, 0, 14, 14]) == 1
    assert diagnosis.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1


@pytest.mark.parametrize("method", [diagnosis.phi, diagnosis.pearson, diagnosis.spearman])
def test_constant_correlations_are_undefined_not_zero(method):
    assert method([1, 1, 1, 1], [0, 0, 1, 1]) is None
    assert method([1, 1], [1, 1]) is None


@pytest.mark.parametrize("left,right", [([0], [1]), ([0, 1], [0]), ([float("nan"), 1], [0, 1])])
def test_invalid_correlation_input_fails_explicitly(left, right):
    with pytest.raises(MeasurementError, match="CORRELATION_INPUT"):
        diagnosis.pearson(left, right)


@pytest.mark.parametrize("matrix,expected", [
    ([[1]], 1), ([[1, 0], [0, 1]], 2), ([[1, 1], [1, 1]], 1),
    ([[1, -1], [-1, 1]], 1), ([[1, 0.5], [0.5, 1]], 1.6),
    ([[1, 0, 0], [0, 1, 0], [0, 0, 1]], 3),
])
def test_effective_count_from_spectrum(matrix, expected):
    ratio, values = diagnosis.participation_ratio(matrix)
    assert ratio == pytest.approx(expected)
    assert sum(values) == pytest.approx(len(matrix))
    assert sum(v * v for v in values) == pytest.approx(sum(v * v for row in matrix for v in row))


def test_invalid_spectrum_fails_without_fallback():
    with pytest.raises(MeasurementError, match="SYMMETRY"):
        diagnosis.eigenvalues([[1, 0.5], [0, 1]])
    with pytest.raises(MeasurementError, match="NOT_PSD"):
        diagnosis.eigenvalues([[1, 2], [2, 1]])
    with pytest.raises(MeasurementError, match="SHAPE"):
        diagnosis.eigenvalues([])


def test_constant_components_excluded_only_from_spectrum():
    rows = records()["TRAIN"]
    key = diagnosis.IDS[0]
    for row in rows:
        row["binary"][key] = 1
        row["points"][key] = 10
    matrices, effective = diagnosis.matrices(rows)
    assert key in effective["constant_components"]
    assert effective["component_count"] == 8
    assert len(effective["eigenvalues"]) == 7
    assert matrices["agreement"][key][key]["value"] == 1
    assert matrices["phi"][key][key]["value"] is None
    assert matrices["phi"][key][key]["undefined_reason"] == "CONSTANT_COMPONENT"
    assert matrices["phi"][key][key]["N"] == 4


def test_all_constant_effective_count_is_explicitly_undefined():
    row = records()["TRAIN"][0]
    _, effective = diagnosis.matrices([row, row])
    assert effective["value"]["value"] is None
    assert effective["value"]["undefined_reason"] == "NO_NONCONSTANT_COMPONENTS"
    assert effective["eigenvalues"] == []


def test_train_only_cuts_validation_and_new_window_cannot_change_them():
    data = records()
    original = diagnosis.train_cuts(data, small_plan())
    for phase in ("VALIDATION", "NEW_WINDOW"):
        for row in data[phase]:
            row["raw_total"] += 1000
            row["confidence"] += 1000
    assert diagnosis.train_cuts(data, small_plan()) == original
    assert diagnosis.bucket(original["confidence"][0], original["confidence"]) == "LOW"
    assert diagnosis.bucket(original["confidence"][1], original["confidence"]) in ("LOW", "MIDDLE")


def test_score_ties_keep_empty_buckets_and_small_cells():
    data = records()
    for rows in data.values():
        for row in rows:
            row["raw_total"] = row["confidence"] = 80
    report = diagnosis.summarize(data, small_plan())
    assert report["train_cutpoints"]["confidence"] == [80, 80]
    for phase in diagnosis.PHASE_NAMES:
        groups = report["phases"][phase]["score_tertiles"]["confidence"]
        assert groups["LOW"]["N"] == 4
        assert groups["MIDDLE"]["N"] == groups["HIGH"]["N"] == 0
        assert groups["HIGH"]["mean_net_r"]["undefined_reason"] == "EMPTY_GROUP"
        assert groups["LOW"]["mean_net_r"]["reliability"] == "guvenilmez"


def test_exact_cell_counter_and_phase_separation():
    data = records()
    data["VALIDATION"][0]["net_r"] = 100
    report = diagnosis.summarize(data, small_plan())
    assert diagnosis.count_scalar_cells(report["phases"]) == 780
    assert report["phases"]["TRAIN"]["mean_net_r"]["value"] == 0.5
    assert report["phases"]["VALIDATION"]["mean_net_r"]["value"] == 25.75
    assert report["phases"]["NEW_WINDOW"]["mean_net_r"]["value"] == 0.5


@pytest.mark.parametrize("phase", diagnosis.PHASE_NAMES)
def test_wrong_N_stops_no_silent_drop(phase):
    data = records()
    data[phase].pop()
    with pytest.raises(MeasurementError, match=f"N_MISMATCH:{phase}"):
        diagnosis.summarize(data, small_plan())


def test_native_component_totals_and_full_context_match():
    row, candles = saved_trade()
    record = diagnosis.component_record(row, candles)
    assert record["raw_total"] == 25 + sum(record["points"].values())
    assert record["confidence"] == row["confidence"]
    assert record["raw_total"] == record["native_long_score"]
    assert len(record["context_sha256"]) == 64


@pytest.mark.parametrize("change,error", [
    ("confidence", "NATIVE_PARITY"), ("canonical", "CANONICAL_PARITY"),
    ("future", "HISTORY_OR_LOOKAHEAD"), ("short", "WINDOW_NOT_259"),
])
def test_native_context_mismatch_or_lookahead_stops(change, error):
    row, candles = saved_trade()
    if change == "confidence":
        row["confidence"] -= 1
    elif change == "canonical":
        row["canonical_analysis"]["adx"] += 1
    elif change == "future":
        for candle in candles:
            candle["time"] += 900
    else:
        candles.pop()
    with pytest.raises(MeasurementError, match=error):
        diagnosis.component_record(row, candles)


def test_unverified_trade_rejected():
    row, _ = saved_trade()
    row["native_row"]["funding_complete"] = False
    with pytest.raises(MeasurementError, match="NOT_VERIFIED"):
        diagnosis.validate_trades([row], "TRAIN", small_plan(1))


def test_duplicate_trade_rejected():
    row, _ = saved_trade()
    with pytest.raises(MeasurementError, match="DUPLICATE"):
        diagnosis.validate_trades([row, row], "TRAIN", small_plan(2))


@pytest.mark.parametrize("value", ["0", "0.000", "0.00", "0.0", "-0", "0E-6"])
def test_native_decimal_zero_representations_preserve_verification(value):
    diagnosis.validate_verified_entry({
        "fully_verified": True, "status": "CLOSED", "funding_known": True,
        "remaining_quantity_decimal": value,
    })


@pytest.mark.parametrize("value", ["0.00001", "1E-99", "-1", "NaN", "Infinity", "invalid", ""])
def test_nonzero_or_invalid_native_remaining_quantity_rejected(value):
    with pytest.raises(MeasurementError, match="ENTRY_"):
        diagnosis.validate_verified_entry({
            "fully_verified": True, "status": "CLOSED", "funding_known": True,
            "remaining_quantity_decimal": value,
        })


@pytest.mark.parametrize("field,value", [
    ("fully_verified", False), ("status", "OPEN_AT_END"), ("funding_known", False),
    ("remaining_quantity_decimal", 0),
])
def test_decimal_zero_does_not_weaken_other_verification_guards(field, value):
    row = {"fully_verified": True, "status": "CLOSED", "funding_known": True,
           "remaining_quantity_decimal": "0"}
    row[field] = value
    with pytest.raises(MeasurementError, match="NOT_VERIFIED"):
        diagnosis.validate_verified_entry(row)


@pytest.mark.parametrize("direction,rsi", [("LONG", 52), ("LONG", 72), ("SHORT", 28), ("SHORT", 48)])
def test_existing_RSI_boundaries_inclusive(direction, rsi):
    _, candles = saved_trade()
    result = analyze(candles)
    result["rsi"] = rsi
    assert diagnosis.directional_flags(result, direction)[4]


def test_existing_volume_and_ADX_boundaries_and_price_equality():
    _, candles = saved_trade()
    result = analyze(candles)
    result["entry"] = result["ema"]["ema20"]
    result["adx"] = 20
    result["volume_ratio"] = 1.05
    flags = diagnosis.directional_flags(result, "LONG")
    assert not flags[0]
    assert flags[6] and flags[7]
    result["adx"] = 19.99
    result["volume_ratio"] = 1.0499
    assert not diagnosis.directional_flags(result, "LONG")[6]
    assert not diagnosis.directional_flags(result, "LONG")[7]


@pytest.mark.parametrize("name", [
    "donchian-test-x", "test-sealed.7z", "independent-block", "donchian-holdout-results",
    "sealed-data", "registry.json", "registries", "research-registry",
])
def test_guard_blocks_all_protected_research_paths(tmp_path, name):
    with pytest.raises(MeasurementError, match="PROTECTED_PATH"):
        diagnosis.check_event("open", (str(tmp_path / name), "r", 0), tmp_path)


def test_guard_allows_python_registry_module_not_research_storage(tmp_path):
    diagnosis.check_event("open", (str(tmp_path / "registry.py"), "r", 0), tmp_path)


@pytest.mark.parametrize("event", ["socket.connect", "socket.bind", "socket.getaddrinfo", "subprocess.Popen"])
def test_guard_denies_network_and_subprocess(event):
    with pytest.raises(MeasurementError, match="OFFLINE_OPERATION"):
        diagnosis.check_event(event, (), None)


def test_guard_restricts_writes_to_new_output(tmp_path):
    output = tmp_path / "new-output"
    diagnosis.check_event("open", (str(output / "report.json"), "x", 0), output)
    with pytest.raises(MeasurementError, match="WRITE_SCOPE"):
        diagnosis.check_event("open", (str(tmp_path / "old.json"), "w", 0), output)
    with pytest.raises(MeasurementError, match="READ_ONLY"):
        diagnosis.check_event("os.mkdir", (str(output), 511, -1), None)


def test_real_frozen_plan_and_new_ledger_are_consistent():
    plan = diagnosis.locked_plan()
    assert plan["context"]["recorded_raw_totals_available"] is False
    assert [p["fully_verified_N"] for p in plan["phases"].values()] == [364, 156, 555]
    assert plan["counter"]["historical_complete"] is False
    assert plan["counter"]["cumulative_known_scalar_cells_lower_bound"] == 1122
    assert plan["counter"]["cumulative_known_feature_exposures_lower_bound"] == 16


def test_existing_output_refused_before_any_input_load(tmp_path):
    with pytest.raises(MeasurementError, match="OUTPUT_EXISTS_OR_SCOPE"):
        diagnosis.execute(small_plan(), tmp_path)
