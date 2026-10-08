"""Synthetic descriptive arithmetic; no archive, network, orders or rule changes."""

from __future__ import annotations

import copy
import json
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import diagnose_original_v2_trades as diagnosis
import test_measure_original_counts as fixtures
import test_offline_strategy_facade as native_tests
import test_prescreen_original_v2 as performance_fixtures
from app.strategies.original_gap_engine import OriginalGapRiskEngine
from build_measurement_view import (
    TEST_START,
    MeasurementScopeError,
    load_measurement_dataset,
)
from measure_donchian_counts import locked_plan, primary_config

offline_only = native_tests.offline_only


def bar(at, *, high=110, low=95, opening=100):
    candle = {"open": opening, "high": high, "low": low, "close": opening}
    return {"time": at, "contract": candle, "mark": dict(candle)}


@pytest.mark.parametrize(("side", "first", "last"), [
    ("LONG", (110, 95), (130, 80)),
    ("SHORT", (105, 90), (120, 70)),
])
def test_directional_mfe_mae_initial_risk_units_and_first_maximum_time(side, first, last):
    at = fixtures.AT
    observed = diagnosis.excursions(
        [bar(at, high=first[0], low=first[1]), bar(at + 900, high=last[0], low=last[1])],
        100, 10, side, at, at + 1799)
    assert observed["mfe_r"] == 3
    assert observed["mae_r"] == 2
    assert observed["time_to_mfe_seconds"] == 900
    assert observed["confirmed_before_stop_mfe_r"] == 1
    assert observed["terminal_intrabar_unresolved"] is True


def test_opening_exit_never_reads_future_terminal_bar_extrema():
    at = fixtures.AT
    observed = diagnosis.excursions(
        [bar(at, high=105, low=99), bar(at + 900, high=1000, low=1, opening=102)],
        100, 10, "LONG", at, at + 900)
    assert observed["mfe_r"] == .5 and observed["mae_r"] == .1
    assert observed["time_to_mfe_seconds"] == 0
    assert observed["confirmed_before_stop_mfe_r"] == .5
    assert observed["terminal_intrabar_unresolved"] is False


def test_gap_and_unsupported_intrabar_timestamp_are_explicit_errors():
    at = fixtures.AT
    with pytest.raises(diagnosis.MeasurementError, match="NONCONTIGUOUS_TRADE_BARS"):
        diagnosis.excursions([bar(at), bar(at + 1800)], 100, 10, "LONG", at, at + 2699)
    with pytest.raises(diagnosis.MeasurementError, match="UNSUPPORTED_EXIT_TIMESTAMP"):
        diagnosis.excursions([bar(at)], 100, 10, "LONG", at, at + 450)


@pytest.mark.parametrize(("reason", "tp1", "path"), [
    ("STOP", False, "STOP_BEFORE_TP1"), ("STOP", True, "STOP_AFTER_TP1"),
    ("TP3", True, "TP3"), ("OTHER", False, "OTHER"),
])
def test_native_terminal_exit_path_classification(reason, tp1, path):
    assert diagnosis.classify([{"reason": reason}], tp1) == path
    with pytest.raises(diagnosis.MeasurementError, match="MISSING_EXITS"):
        diagnosis.classify([], tp1)


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_same_recorded_path_gross_arithmetic_not_replay_or_cost_model(side):
    position = performance_fixtures.native_position(side)
    tp1, stop = Decimal(position.spec["targets"][0]), Decimal(position.spec["stop_loss"])
    position.fill(tp1, position.tp1_quantity, fixtures.AT + 899, "TP1")
    position.tp1_hit = True
    position.fill(stop, position.remaining, fixtures.AT + 1799, "STOP")
    before = copy.deepcopy(position.row())
    observed = diagnosis.arithmetic_scenarios(position)
    assert observed == pytest.approx({
        "recorded_gross_r": .2, "recorded_post_tp1_stop_at_entry_gross_r": .6,
        "recorded_tp1_close_remaining_gross_r": 1,
    })
    assert position.row() == before


def test_without_a_native_tp1_fill_arithmetic_is_unchanged():
    position = performance_fixtures.native_position()
    position.fill(Decimal(position.spec["stop_loss"]), position.remaining, fixtures.AT + 899, "STOP")
    observed = diagnosis.arithmetic_scenarios(position)
    assert list(observed.values()) == [-1, -1, -1]


def compact_row(value, *, confidence=None, path="STOP_BEFORE_TP1", mfe=.75, confirmed=.25):
    return {
        "ema_distance_atr": value, "confidence": value if confidence is None else confidence,
        "net_r": value / 100, "path": path, "exit_reason": "STOP" if path.startswith("STOP") else "TP3",
        "direction": "LONG", "mfe_r": mfe, "mae_r": 1, "time_to_mfe_seconds": 900,
        "confirmed_before_stop_mfe_r": confirmed, "duration_seconds": 1799,
        "arithmetic_scenarios": {
            "recorded_gross_r": .2, "recorded_post_tp1_stop_at_entry_gross_r": .6,
            "recorded_tp1_close_remaining_gross_r": 1,
        },
    }


def test_tertile_cuts_use_train_only_and_ties_do_not_force_equal_bucket_counts():
    train = [compact_row(value) for value in (0, 3, 6, 9)]
    cuts = diagnosis.train_cuts(train)
    assert cuts == {"source": "TRAIN", "N": 4, "ema_distance_atr": [3, 6], "confidence": [3, 6]}
    validation = [compact_row(100), compact_row(200)]
    observed = diagnosis.tables(validation, cuts)["tertiles"]["ema_distance_atr"]
    assert observed["HIGH"]["N"] == 2 and observed["LOW"]["N"] == observed["MIDDLE"]["N"] == 0
    assert cuts["ema_distance_atr"] == [3, 6]
    assert diagnosis.tertile(3, [3, 6]) == "LOW"
    assert diagnosis.tertile(6, [3, 6]) == "MIDDLE"
    equal = diagnosis.train_cuts([compact_row(95) for _ in range(3)])
    assert equal["confidence"] == [95, 95] and diagnosis.tertile(95, equal["confidence"]) == "LOW"


def test_small_sample_labels_have_exact_thirty_boundary_and_empty_is_not_zero_mean():
    assert diagnosis.cell(1, 29)["reliability"] == "guvenilmez"
    assert diagnosis.cell(1, 30)["reliability"] == "descriptive_only"
    assert diagnosis.quantiles([])["p50"] == diagnosis.cell(None, 0)
    with pytest.raises(diagnosis.MeasurementError, match="TRAIN_CUTS_REQUIRE_VERIFIED"):
        diagnosis.train_cuts([])


def test_before_stop_confirmed_and_unknown_terminal_thresholds_are_separate():
    observed = diagnosis.before_stop_table([
        compact_row(1, mfe=1.5, confirmed=.6), compact_row(2, mfe=.2, confirmed=.1),
    ])
    assert observed["N"] == 2
    assert observed["thresholds"]["0.5"]["confirmed_pct"]["value"] == 50
    assert observed["thresholds"]["1.0"]["confirmed_pct"]["value"] == 0
    assert observed["thresholds"]["1.0"]["envelope_upper_bound_pct"]["value"] == 50
    assert observed["thresholds"]["1.0"]["terminal_intrabar_unresolved_pct"]["value"] == 50
    assert observed["duration_seconds"]["median"]["value"] == 1799


def test_each_table_cell_retains_its_denominator_and_groups_never_pool_phases():
    train = [compact_row(1), compact_row(2, path="TP3")]
    validation = [compact_row(9)]
    cuts = diagnosis.train_cuts(train)
    first, second = diagnosis.tables(train, cuts), diagnosis.tables(validation, cuts)
    assert first["N"] == 2 and second["N"] == 1
    assert first["paths"]["STOP_BEFORE_TP1"]["share_pct"] == diagnosis.cell(50, 2)
    assert first["paths"]["STOP_BEFORE_TP1"]["mean_net_r"] == diagnosis.cell(.01, 1)
    assert second["paths"]["STOP_BEFORE_TP1"]["share_pct"] == diagnosis.cell(100, 1)


def phase_and_rows(loop, name="TRAIN"):
    data, plan, phase = fixtures.dataset(), locked_plan(), fixtures.phase(name=name)
    native = OriginalGapRiskEngine(data, primary_config(phase, plan))
    reference = loop.run_until_complete(diagnosis.parent.gap.replay_counts(native, phase))
    counts, rows = loop.run_until_complete(diagnosis.phase_result(data, plan, phase, reference))
    return counts, rows, reference, data, plan, phase


def test_capture_preserves_all_native_counts_and_context_has_no_lookahead(offline_only):
    counts, rows, reference, data, plan, phase = phase_and_rows(offline_only)
    assert counts == reference and len(rows) == counts["counts"]["fully_verified_completed"] == 1
    row = rows[0]
    policy = diagnosis.parent.PROFILE.policy(primary_config(phase, plan).policy)
    analysis = data.canonical(fixtures.BTC, fixtures.AT, policy)["analysis"]
    assert row["confidence"] == analysis["confidence"]
    assert row["ema20_15m"] == analysis["ema"]["ema20"]
    assert row["atr14_15m"] == analysis["atr"]
    assert row["ema_distance_atr"] == abs(analysis["entry"] - analysis["ema"]["ema20"]) / analysis["atr"]
    assert row["net_r"] == row["native_row"]["net_r"]
    assert row["bars_15m"][0]["time"] == fixtures.AT
    diagnosis.validate_rows(rows, "TRAIN", counts)
    changed = copy.deepcopy(reference)
    changed["counts"]["accepted_entries"] += 1
    with pytest.raises(diagnosis.MeasurementError, match="RUN3_COUNT_PARITY_MISMATCH"):
        offline_only.run_until_complete(diagnosis.phase_result(data, plan, phase, changed))


def test_jsonl_roundtrip_hash_exclusive_creation_and_no_repo_output(tmp_path, offline_only):
    counts, rows, *_ = phase_and_rows(offline_only)
    diagnosis.validate_rows(rows, "TRAIN", counts)
    manifest = diagnosis.write_raw(tmp_path, "TRAIN", rows)
    path = tmp_path / "trades-TRAIN.jsonl"
    assert manifest == {"file": path.name, "sha256": diagnosis.sha256_file(path), "N": len(rows)}
    assert [json.loads(line) for line in path.read_text().splitlines()] == rows
    with pytest.raises(FileExistsError):
        diagnosis.write_raw(tmp_path, "TRAIN", rows)


def test_report_registration_native_schema_raw_manifests_and_train_only_cuts(tmp_path, offline_only):
    data, plan = fixtures.dataset(), locked_plan()
    phases, raw = {}, {}
    original = diagnosis.parent.gap.provenance(plan)
    provenance = diagnosis.parent.provenance(plan, {name: {"provenance": original} for name in ("TRAIN", "VALIDATION")})
    for name in ("TRAIN", "VALIDATION"):
        counts, rows, *_ = phase_and_rows(offline_only, name)
        raw[name] = rows
        phases[name] = {"native_counts": counts, "raw_trades": diagnosis.write_raw(tmp_path, name, rows)}
    cuts = diagnosis.train_cuts(raw["TRAIN"])
    for name, value in phases.items():
        value["tables"] = diagnosis.tables(raw[name], cuts)
    report = {
        "schema": diagnosis.SCHEMA, "definitions": copy.deepcopy(diagnosis.DEFINITIONS),
        "profile": diagnosis.parent.asdict(diagnosis.parent.PROFILE),
        "provenance": {"parent_prescreen_provenance": provenance},
        "train_cutpoints": cuts, "phases": phases, "elapsed_seconds": 0,
    }
    diagnosis.validate_report(report, raw)
    report["train_cutpoints"]["confidence"][0] += 1
    with pytest.raises(diagnosis.MeasurementError, match="TRAIN_ONLY_CUTS"):
        diagnosis.validate_report(report, raw)
    assert data.decisions == {}


def test_existing_output_stops_before_input_read(tmp_path):
    with patch.object(diagnosis, "OUTPUT", tmp_path), \
         patch.object(diagnosis, "locked_plan", side_effect=AssertionError("input read forbidden")):
        assert diagnosis.main(["--metadata", str(diagnosis.METADATA)]) == 2


def test_test_dates_are_denied_before_any_io_or_engine(offline_only, tmp_path):
    with patch.object(Path, "open", side_effect=AssertionError("TEST read forbidden")), \
         pytest.raises(MeasurementScopeError, match="TEST access denied"):
        load_measurement_dataset(tmp_path, tmp_path / "metadata.json", TEST_START, TEST_START + 900)
    phase = diagnosis.parent.Phase("VALIDATION", TEST_START, TEST_START + 900)
    with pytest.raises(diagnosis.MeasurementError, match="MEASUREMENT_SCOPE_DENIED"):
        offline_only.run_until_complete(diagnosis.phase_result(None, None, phase, None))
