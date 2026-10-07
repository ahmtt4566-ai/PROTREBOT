"""Synthetic final-trial blackout boundaries, native parity and strict count schema."""

from __future__ import annotations

import copy
import json
import os
import sys
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import measure_original_gap_counts as measurement
import measure_original_v2_counts as previous
import test_measure_original_counts as fixtures
import test_offline_strategy_facade as native_tests
from app.backtest_data import Series
from app.strategies.offline_facade import _funding_gaps, _series_gaps
from app.strategies.original_gap_engine import STREAMS, OriginalGapRiskEngine
from app.strategies.original_gap_risk import PROFILE, exception_record
from app.strategies.original_offline_engine import OriginalOfflineRiskEngine
from app.strategies.original_offline_risk import PROFILE as OLD_PROFILE
from app.strategies.original_offline_risk import OfflineRiskError
from measure_donchian_counts import locked_plan, stamp

offline_only = native_tests.offline_only
STEP = 900
GAP_START = fixtures.AT + PROFILE.gap_blackout_seconds + STEP


def future_gap_data(streams=()):
    data = fixtures.dataset()
    cfg = fixtures.config()
    cfg = replace(cfg, end=GAP_START + 2 * STEP)
    for group in (data.frames[fixtures.BTC], {"mark": data.marks[fixtures.BTC]}):
        for key, series in list(group.items()):
            if series.interval != "15m":
                continue
            rows = [{**row} for row in series.rows if row["time"] < fixtures.AT]
            rows.extend({"time": at, "open": 100, "high": 100.1, "low": 99.9, "close": 100,
                         "volume": 100000, "quote_volume": 10000000}
                        for at in range(fixtures.AT, cfg.end, STEP))
            kind = "mark" if key == "mark" else "contract"
            if kind in streams:
                rows = [row for row in rows if row["time"] != GAP_START]
            group[key] = Series("15m", rows)
        if "mark" in group:
            data.marks[fixtures.BTC] = group["mark"]
    if "funding" in streams:
        data.funding[fixtures.BTC] = [
            {"time": GAP_START - 28800, "rate": 0.0001, "interval_hours": 8},
            {"time": GAP_START, "rate": 0.0001, "interval_hours": 8},
            {"time": GAP_START + 57600, "rate": 0.0001, "interval_hours": 8},
        ]
    return data, cfg


def report(loop, *, gap=False):
    data = fixtures.dataset(bars=3)
    if gap:
        data.marks[fixtures.BTC] = Series(
            "15m", [row for row in data.marks[fixtures.BTC].rows if row["time"] != fixtures.AT + STEP])
    engine = OriginalGapRiskEngine(data, fixtures.config(bars=3))
    phase = loop.run_until_complete(measurement.replay_counts(engine, fixtures.phase(bars=3)))
    value = {"exception_record": exception_record(), "schema": measurement.SCHEMA,
             "profile": asdict(PROFILE), "provenance": measurement.provenance(locked_plan()),
             "phases": {"TRAIN": phase}, "elapsed_seconds": 0}
    return engine, value


def test_new_registered_immutable_profile_keeps_every_risk_setting_and_old_hash():
    original = asdict(OLD_PROFILE)
    new = asdict(PROFILE)
    for key, value in original.items():
        if key != "profile_id":
            assert new[key] == value
    assert PROFILE.profile_id == "original-fixed-cap6-lev3-gap18d-v1"
    assert PROFILE.gap_blackout_seconds == 432 * 3600 == 1555200
    assert PROFILE.profile_hash != OLD_PROFILE.profile_hash
    from app.strategies.provenance import parameter_hash

    assert OLD_PROFILE.profile_hash == parameter_hash(original)
    with pytest.raises(OfflineRiskError, match="UNREGISTERED_OFFLINE_PROFILE"):
        OriginalOfflineRiskEngine(fixtures.dataset(), fixtures.config(), profile=PROFILE)
    with pytest.raises(FrozenInstanceError):
        PROFILE.gap_blackout_seconds = 1
    with pytest.raises(OfflineRiskError, match="UNREGISTERED_OFFLINE_PROFILE"):
        replace(PROFILE, gap_blackout_seconds=431 * 3600)


@pytest.mark.parametrize("stream", STREAMS)
def test_exact_432_hour_boundary_one_bar_before_and_gap_start(stream, offline_only):
    data, cfg = future_gap_data((stream,))
    engine = OriginalGapRiskEngine(data, cfg)
    assert not engine.entry_data_exclusion(fixtures.BTC, GAP_START - 1555200 - STEP, "before_market_ranking")
    assert engine.entry_data_exclusion(fixtures.BTC, GAP_START - 1555200, "before_market_ranking")
    assert engine.entry_data_exclusion(fixtures.BTC, GAP_START - STEP, "before_market_ranking")
    assert not engine.entry_data_exclusion(fixtures.BTC, GAP_START, "before_market_ranking")
    assert engine.rejections["gap_blackout"] == 2
    assert engine.blackout_by_type[stream] == 2
    assert sum(engine.blackout_by_type.values()) == 2


def test_blackout_is_symbol_local_and_type_counts_overlap(offline_only):
    data, cfg = future_gap_data(("contract", "mark"))
    other = "ETHUSDT"
    data.frames[other] = copy.deepcopy(data.frames[fixtures.BTC])
    rows = data.frames[other]["15m"].rows
    rows = sorted([*rows, {**rows[-1], "time": GAP_START}], key=lambda row: row["time"])
    data.frames[other]["15m"] = Series("15m", rows)
    data.marks[other] = Series("15m", copy.deepcopy(rows))
    data.funding[other] = copy.deepcopy(data.funding[fixtures.BTC])
    cfg = replace(cfg, policy={**cfg.policy, "allowed_symbols": [fixtures.BTC, other]})
    engine = OriginalGapRiskEngine(data, cfg)
    at = GAP_START - 1555200
    assert engine.entry_data_exclusion(fixtures.BTC, at, "before_market_ranking")
    assert not engine.entry_data_exclusion(other, at, "before_market_ranking")
    result = engine.blackout_counts([fixtures.BTC, other])
    assert result["count"] == 1
    assert result["by_symbol"] == {fixtures.BTC: 1, other: 0}
    assert result["by_type"] == {"contract": 1, "mark": 1, "funding": 0}
    assert result["type_counts_overlap"] is True


@pytest.mark.parametrize("stream", STREAMS)
def test_inventory_is_exactly_the_existing_donchian_helper_output(stream, offline_only):
    data, cfg = future_gap_data((stream,))
    engine = OriginalGapRiskEngine(data, cfg)
    contract_coverage, contract = _series_gaps(data.frames[fixtures.BTC]["15m"], "contract", cfg)
    mark_coverage, mark = _series_gaps(data.marks[fixtures.BTC], "mark", cfg)
    funding_report, funding = _funding_gaps(data.funding[fixtures.BTC])
    assert engine.gaps[fixtures.BTC] == (*contract, *mark, *funding)
    assert engine.gap_inventory[fixtures.BTC] == {
        "coverage": {"contract": contract_coverage, "mark": mark_coverage}, "funding": funding_report,
        "gaps": [asdict(gap) for gap in (*contract, *mark, *funding)],
    }


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_no_gap_native_decision_stop_targets_state_and_report_are_identical(side, offline_only):
    data, cfg = fixtures.dataset(side), fixtures.config()
    old, new = OriginalOfflineRiskEngine(data, cfg), OriginalGapRiskEngine(data, cfg)
    assert new.profile.policy(cfg.policy) == old.profile.policy(cfg.policy)
    assert new.data.canonical(fixtures.BTC, fixtures.AT, new.policy) == old.data.canonical(
        fixtures.BTC, fixtures.AT, old.policy)
    old.data.decisions.clear()
    new.data.decisions.clear()
    expected = offline_only.run_until_complete(previous.replay_counts(old, fixtures.phase()))
    actual = offline_only.run_until_complete(measurement.replay_counts(new, fixtures.phase()))
    assert actual.pop("gap_blackout")["count"] == 0
    assert actual["rejections"].pop("gap_blackout") == 0
    assert actual == expected
    assert new.events == old.events
    assert new.cash == old.cash
    assert [position.spec for position in new.trades] == [position.spec for position in old.trades]
    assert new.positions == old.positions


def test_position_opened_before_blackout_can_hit_gap_and_native_lock_is_preserved(offline_only):
    data, cfg = future_gap_data(("mark",))
    engine = OriginalGapRiskEngine(data, cfg)
    signal = {"direction": "LONG", "entry": 100, "stop_loss": 99, "tp1": 101, "tp2": 102,
              "tp3": 103, "confidence": 95, "radar": {"trap_score": 0}, "atr": None}
    offline_only.run_until_complete(engine.enter(fixtures.BTC, signal, fixtures.AT))
    assert len(engine.trades) == 1
    position = engine.trades[0]
    assert position.status != "UNKNOWN_DATA_GAP"
    engine.advance(position, GAP_START, opening_only=True)
    assert position.status == "UNKNOWN_DATA_GAP"
    assert not engine.positions
    offline_only.run_until_complete(engine.enter(fixtures.BTC, signal, GAP_START + STEP))
    assert engine.rejections["pnl_verified"] == 1
    trace = previous.closure_trace(engine, stamp(GAP_START + STEP))
    assert trace["unknown_data_gap"] == {
        "count": 1, "items": [{"symbol": fixtures.BTC, "opened_at": stamp(fixtures.AT),
                             "detected_at": stamp(GAP_START)}],
    }
    assert trace["accounting_lock"]["first_lock_at"] == stamp(GAP_START)
    assert trace["accounting_lock"]["rejected_candidates_since_lock"] == 1


@pytest.mark.parametrize("gap", [False, True])
def test_count_only_schema_validates_blackout_breakdowns_and_exception(gap, offline_only):
    engine, value = report(offline_only, gap=gap)
    measurement.validate_report(value, expected_phases={"TRAIN"})
    assert value["exception_record"] == value["provenance"]["exception_record"] == exception_record()
    assert value["profile"] == asdict(PROFILE)
    assert value["provenance"]["profile_hash"] == PROFILE.profile_hash
    assert value["phases"]["TRAIN"]["gap_blackout"]["count"] == engine.rejections["gap_blackout"]
    if gap:
        assert value["phases"]["TRAIN"]["gap_blackout"]["count"] == 1
        assert all(position.opened_at >= fixtures.AT + STEP for position in engine.trades)


@pytest.mark.parametrize("field", ["net_r", "pf", "pnl", "win_rate", "return", "equity_curve"])
@pytest.mark.parametrize("location", ["root", "phase", "rejections"])
def test_schema_rejects_performance_fields_at_every_report_surface(field, location, offline_only):
    _, value = report(offline_only)
    target = value if location == "root" else value["phases"]["TRAIN"]
    if location == "rejections":
        target = target["rejections"]
    target[field] = 0
    with pytest.raises(measurement.MeasurementError, match="SCHEMA_REJECTED"):
        measurement.validate_report(value)


def test_blackout_counter_inconsistency_is_rejected(offline_only):
    _, value = report(offline_only, gap=True)
    value["phases"]["TRAIN"]["gap_blackout"]["by_type"]["mark"] += 1
    with pytest.raises(measurement.MeasurementError, match="by_type.mark.sum"):
        measurement.validate_report(value)


def test_existing_output_and_missing_validation_stop_without_reading_inputs(tmp_path):
    with patch.object(measurement, "OUTPUT", tmp_path), \
         patch.object(measurement, "reference_evidence", side_effect=AssertionError("must not read")):
        assert measurement.main(["--metadata", str(measurement.METADATA), "--phase", "TRAIN"]) == 2
        (tmp_path / "VALIDATION").mkdir()
        assert measurement.main(["--metadata", str(measurement.METADATA), "--phase", "VALIDATION"]) == 2


def test_threshold_unchanged_for_final_trial_and_test_scope_denied(offline_only):
    for count, allowed in ((149, False), (150, True)):
        assert previous.validation_allows_train(
            {"phases": {"VALIDATION": {"counts": {"fully_verified_completed": count}}}}) is allowed
    with pytest.raises(measurement.MeasurementError, match="MEASUREMENT_SCOPE_DENIED"):
        offline_only.run_until_complete(measurement.measure_phases(
            fixtures.dataset(), locked_plan(),
            phases=(measurement.Phase("TEST", measurement.PHASES[-1].end,
                                      measurement.PHASES[-1].end + STEP),)))


def test_validation_cli_preserves_loader_warmup_and_writes_only_requested_phase(tmp_path, offline_only):
    _, value = report(offline_only)
    value["phases"] = {"VALIDATION": value["phases"]["TRAIN"]}
    data = fixtures.dataset()
    plan = locked_plan()
    measured = AsyncMock(return_value=value["phases"])
    with patch.dict(os.environ), \
         patch.object(measurement.asyncio, "new_event_loop", return_value=offline_only), \
         patch.object(measurement, "OUTPUT", tmp_path), \
         patch.object(measurement, "reference_evidence", return_value={}), \
         patch.object(measurement, "sha256_file", return_value=measurement.VIEW_SHA), \
         patch.object(measurement, "locked_plan", return_value={
             **plan, "input_sha256": {**plan["input_sha256"], "metadata": measurement.VIEW_SHA}}), \
         patch.object(measurement, "provenance", return_value=value["provenance"]), \
         patch.object(measurement.previous, "load_measurement_dataset", return_value=data) as loader, \
         patch.object(measurement, "validate_loaded_scope") as scope, \
         patch.object(measurement, "measure_phases", measured):
        assert measurement.main(["--metadata", str(measurement.METADATA), "--phase", "VALIDATION"]) == 0
    loader.assert_called_once_with(measurement.VIEW, measurement.METADATA,
                                   measurement.PHASES[0].start, measurement.PHASES[-1].end)
    scope.assert_called_once_with(data, plan["symbols"])
    assert measured.call_args.kwargs["phases"] == (measurement.PHASES[1],)
    stored = json.loads((tmp_path / "VALIDATION" / "counts.json").read_text(encoding="utf-8"))
    assert next(iter(stored)) == "exception_record"
    assert set(stored["phases"]) == {"VALIDATION"}
    measurement.validate_report(stored, expected_phases={"VALIDATION"})
    assert not (tmp_path / "TRAIN").exists()


def test_run3_below_threshold_blocks_train_before_any_measurement_or_reference_read(tmp_path, offline_only):
    _, value = report(offline_only)
    value["phases"] = {"VALIDATION": value["phases"]["TRAIN"]}
    directory = tmp_path / "VALIDATION"
    directory.mkdir()
    (directory / "counts.json").write_text(json.dumps(value), encoding="utf-8")
    with patch.object(measurement, "OUTPUT", tmp_path), \
         patch.object(measurement, "reference_evidence", side_effect=AssertionError("reference read forbidden")), \
         patch.object(measurement, "measure_phases", side_effect=AssertionError("TRAIN forbidden")):
        assert measurement.main(["--metadata", str(measurement.METADATA), "--phase", "TRAIN"]) == 2
    assert not (tmp_path / "TRAIN").exists()
