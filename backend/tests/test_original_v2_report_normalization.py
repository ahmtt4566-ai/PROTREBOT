"""Synthetic accounting-gate, closure-trace and selected-phase regression tests."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import measure_original_v2_counts as measurement
import test_measure_original_counts as fixtures
import test_offline_strategy_facade as native_tests
import test_original_offline_risk as profile_tests
from app.backtest_data import Dataset, Series
from app.strategies.original_offline_engine import OriginalOfflineRiskEngine
from measure_donchian_counts import locked_plan, stamp

offline_only = native_tests.offline_only


class SyntheticSignalDataset(Dataset):
    def canonical(self, symbol, at, policy):
        return {
            "decision": "BUY", "entry_eligible": True, "reason": "APPROVED",
            "analysis": {"direction": "LONG", "entry": 100, "stop_loss": 99,
                         "tp1": 101, "tp2": 102, "tp3": 103, "confidence": 95,
                         "radar": {"trap_score": 0}, "atr": None},
        }


def gap_dataset():
    data = fixtures.dataset(bars=3)
    series = data.frames[fixtures.BTC]["15m"]
    for row in series.rows:
        if row["time"] >= fixtures.AT:
            row.update(open=100, close=100, high=100.1, low=99.9)
    mark_rows = [
        {**row, "open": 100, "close": 100, "high": 100.1, "low": 99.9}
        for row in data.marks[fixtures.BTC].rows if row["time"] != fixtures.AT + 900
    ]
    data.marks[fixtures.BTC] = Series("15m", mark_rows)
    return data


def accounting_report(loop):
    data = gap_dataset()
    engine = OriginalOfflineRiskEngine(data, fixtures.config(bars=3))
    original = engine.data
    engine.data = SyntheticSignalDataset(
        original.frames, original.marks, original.funding, original.metadata, original.report, original.funding_months)
    phase = loop.run_until_complete(measurement.replay_counts(engine, fixtures.phase(bars=3)))
    report = {"schema": measurement.SCHEMA, "profile": copy.deepcopy(profile_tests.asdict(profile_tests.PROFILE)),
              "provenance": measurement.provenance(locked_plan()),
              "phases": {"TRAIN": phase}, "elapsed_seconds": 0}
    return engine, report


def test_unknown_closure_blocks_next_candidates_and_normalizes_every_counter(offline_only):
    engine, report = accounting_report(offline_only)
    phase = report["phases"]["TRAIN"]
    assert engine.rejections["pnl_verified"] == 2
    assert engine.stage_rejections["ENTER"]["pnl_verified"] == 2
    assert phase["rejections"]["accounting_verified"] == 2
    assert phase["rejections_by_stage"]["ENTER"]["accounting_verified"] == 2
    assert phase["gate_rejections"]["accounting_verified"] == 2
    assert phase["gate_evaluations"]["accounting_verified"] > 0
    assert "pnl_verified" not in phase["rejections"]
    assert "pnl_verified" not in phase["rejections_by_stage"]["ENTER"]
    assert phase["counts"]["accepted_entries"] == phase["counts"]["unknown_data_gap"] == 1
    assert phase["counts"]["completed"] == phase["counts"]["fully_verified_completed"] == 0
    assert phase["closure_trace"] == {
        "unknown_data_gap": {"count": 1, "items": [
            {"symbol": fixtures.BTC, "opened_at": stamp(fixtures.AT), "detected_at": stamp(fixtures.AT + 900)},
        ]},
        "funding_incomplete_closed": {"count": 0, "items": []},
        "accounting_lock": {
            "first_lock_at": stamp(fixtures.AT + 900), "first_rejection_at": stamp(fixtures.AT + 900),
            "rejected_candidates_since_lock": 2,
        },
    }
    measurement.validate_report(report, expected_phases={"TRAIN"})


def test_normalization_keeps_all_existing_native_reason_counts(offline_only):
    engine, report = accounting_report(offline_only)
    from measure_donchian_counts import reason_counts

    phase = report["phases"]["TRAIN"]
    for key, count in reason_counts(engine.rejections).items():
        assert phase["rejections"][key] == count
    assert phase["rejections_by_stage"] == {
        stage: reason_counts(values) for stage, values in engine.stage_rejections.items()}
    assert phase["gate_rejections"] == reason_counts(engine.gate_counts)
    assert phase["gate_evaluations"] == reason_counts(engine.gate_evaluations)


def test_funding_incomplete_closed_trace_does_not_create_native_accounting_lock(offline_only):
    engine = OriginalOfflineRiskEngine(fixtures.dataset(), fixtures.config())
    phase = offline_only.run_until_complete(measurement.replay_counts(engine, fixtures.phase()))
    assert phase["counts"]["completed"] == 1
    position = engine.trades[0]
    position.funding_known = False
    trace = measurement.closure_trace(engine, None)
    assert trace["funding_incomplete_closed"] == {
        "count": 1, "items": [{"symbol": fixtures.BTC, "opened_at": stamp(position.opened_at),
                             "closed_at": stamp(position.closed_at)}],
    }
    assert trace["unknown_data_gap"] == {"count": 0, "items": []}
    assert trace["accounting_lock"] == {
        "first_lock_at": None, "first_rejection_at": None, "rejected_candidates_since_lock": 0,
    }


@pytest.mark.parametrize(("kind", "control", "path", "key"), [
    ("stage", "normalized_native_reasons", "phases.TRAIN.rejections_by_stage.ENTER", "pnl_verified"),
    ("top", "mapping_keys", "phases.TRAIN.rejections", "pnl_verified"),
    ("count", "nonnegative_integer", "phases.TRAIN.counts", "completed"),
    ("lock", "accounting_rejection_count", "phases.TRAIN.closure_trace.accounting_lock.rejected_candidates_since_lock", ""),
])
def test_schema_errors_name_control_path_and_rejected_key(kind, control, path, key, offline_only):
    _, report = accounting_report(offline_only)
    phase = report["phases"]["TRAIN"]
    if kind == "stage":
        phase["rejections_by_stage"]["ENTER"]["pnl_verified"] = 2
    elif kind == "top":
        phase["rejections"]["pnl_verified"] = 2
    elif kind == "count":
        phase["counts"]["completed"] = -1
    else:
        phase["closure_trace"]["accounting_lock"]["rejected_candidates_since_lock"] = 1
    with pytest.raises(measurement.MeasurementError) as raised:
        measurement.validate_report(report)
    message = str(raised.value)
    assert f"control={control}" in message
    assert f"path={path}" in message
    if key:
        assert key in message


def test_phase_selection_keeps_exact_original_periods():
    assert measurement.selected_phases("VALIDATION") == (measurement.PHASES[1],)
    assert measurement.selected_phases("TRAIN") == (measurement.PHASES[0],)
    with pytest.raises(measurement.MeasurementError, match="PHASE_SELECTION_REQUIRED"):
        measurement.selected_phases("TEST")


@pytest.mark.parametrize(("count", "allowed"), [(0, False), (149, False), (150, True), (151, True)])
def test_train_gate_uses_only_fully_verified_validation_count(count, allowed):
    report = {"phases": {"VALIDATION": {"counts": {"fully_verified_completed": count, "completed": 999}}}}
    assert measurement.validation_allows_train(report) is allowed


def test_requested_phase_schema_rejects_wrong_phase(offline_only):
    _, report = accounting_report(offline_only)
    with pytest.raises(measurement.MeasurementError, match="control=requested_phases path=phases"):
        measurement.validate_report(report, expected_phases={"VALIDATION"})


@pytest.mark.parametrize("phase", ["TRAIN", "VALIDATION"])
def test_existing_phase_directory_stops_before_any_input_read(phase, tmp_path):
    (tmp_path / phase).mkdir()
    with patch.object(measurement, "OUTPUT", tmp_path), \
         patch.object(measurement, "locked_plan", side_effect=AssertionError("input read forbidden")):
        assert measurement.main(["--metadata", str(measurement.METADATA), "--phase", phase]) == 2


def test_train_without_validation_stops_before_input_or_output_creation(tmp_path):
    with patch.object(measurement, "OUTPUT", tmp_path), \
         patch.object(measurement, "locked_plan", side_effect=AssertionError("input read forbidden")):
        assert measurement.main(["--metadata", str(measurement.METADATA), "--phase", "TRAIN"]) == 2
    assert not (tmp_path / "TRAIN").exists()


def test_validation_below_threshold_prevents_train_start(tmp_path, offline_only):
    report = profile_tests.make_report(offline_only)
    report["phases"] = {"VALIDATION": report["phases"]["VALIDATION"]}
    measurement.validate_report(report, expected_phases={"VALIDATION"})
    directory = tmp_path / "VALIDATION"
    directory.mkdir()
    (directory / "counts.json").write_text(json.dumps(report), encoding="utf-8")
    with patch.object(measurement, "OUTPUT", tmp_path), \
         patch.object(measurement, "locked_plan", side_effect=AssertionError("input read forbidden")):
        assert measurement.main(["--metadata", str(measurement.METADATA), "--phase", "TRAIN"]) == 2
    assert not (tmp_path / "TRAIN").exists()


def test_selected_validation_matches_full_measurement_phase_without_changing_native_state(offline_only):
    data, plan = fixtures.dataset(), locked_plan()
    train, validation = fixtures.phase(name="TRAIN"), fixtures.phase(name="VALIDATION")
    all_phases = offline_only.run_until_complete(measurement.measure_phases(data, plan, phases=(train, validation)))
    selected = offline_only.run_until_complete(measurement.measure_phases(data, plan, phases=(validation,)))
    assert set(selected) == {"VALIDATION"}
    assert selected["VALIDATION"] == all_phases["VALIDATION"]
