"""Count-only tests using synthetic local candles, no real measurement inputs."""

from __future__ import annotations

import copy
import io
import json
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import measure_donchian_counts as counts
import test_offline_facade_funding_gaps as funding
import test_offline_facade_gap_blackout as gaps
import test_offline_facade_lifecycle as life
from app.backtest_baseline import Config
from app.strategies.donchian_params import DonchianParamsV2
from app.strategies.offline_facade import DonchianOfflineEngine
from build_measurement_view import MeasurementScopeError, load_measurement_dataset

offline_only = life.offline_only
AT, BTC, ETH, STEP = gaps.AT, gaps.BTC, gaps.ETH, gaps.STEP
DAY = 86400


def plan(symbols=(BTC,)):
    return {
        "symbols": list(symbols), "initial_equity": 1000, "max_total_exposure_usdt": 350,
        "primary": {"slippage_bps": 3, "spread_bps": 2, "intrabar": "STOP_FIRST"},
    }


def config(bars=96, symbols=(BTC,)):
    return counts.primary_config(counts.Phase("TRAIN", AT, AT + bars * STEP), plan(symbols))


def measure(data, offline_only, *, days=1):
    phase = counts.Phase("TRAIN", AT, AT + days * DAY)
    with counts.quiet_native():
        engine = counts.make_engine(data, config(days * 96))
        report = offline_only.run_until_complete(engine.replay_counts(phase))
    return engine, report


def envelope(phase):
    phases = {"TRAIN": copy.deepcopy(phase), "VALIDATION": copy.deepcopy(phase)}
    return {
        "schema": counts.SCHEMA,
        "provenance": {
            "strategy_version": "signal-v2", "channel_period": 20, "atr_multiplier": 1.4,
            "window_bars": 259, "blackout_hours": 72, "metadata_kind": "SYNTHETIC",
            **{key: "0" * 64 for key in (
                "metadata_sha256", "view_manifest_sha256", "source_manifest_sha256",
                "stage6b_plan_sha256", "stage6_plan_sha256",
            )},
        },
        "phases": phases, "test_prequalification": counts.prequalification(phases), "elapsed_seconds": 1.0,
    }


def test_locked_config_matches_literal_stage6b_primary_runner():
    base = counts.locked_plan()
    phase = counts.PHASES[0]
    expected = Config(
        phase.start, phase.end, initial_equity=base["initial_equity"], conditional_current_metadata=True,
        policy={"allowed_symbols": base["symbols"], "max_total_exposure_usdt": base["max_total_exposure_usdt"]},
    )
    actual = counts.primary_config(phase, base)
    assert asdict(actual) == asdict(expected)
    assert actual.slippage_bps == 3 and actual.spread_bps == 2 and actual.intrabar == "STOP_FIRST"
    assert base["fee_bps_per_side"] == 5 and len(base["symbols"]) == 8
    assert [phase.days for phase in counts.PHASES] == [457, 243]


def test_real_synthetic_counts_and_native_admission_parity(offline_only):
    data = funding.schedule(gaps.data_for(bars=96))
    data.marks[BTC].rows[33]["high"] = 320.0
    with counts.quiet_native():
        native = DonchianOfflineEngine(copy.deepcopy(data), config(), DonchianParamsV2())
        native_output = offline_only.run_until_complete(native.replay())
    engine, report = measure(data, offline_only)
    native_measurement = native_output["measurement_counts"]
    assert report["counts"]["accepted_entries"] == 1
    assert report["counts"]["completed"] == native_measurement["closed"] == 1
    assert report["counts"]["fully_verified_completed"] == native_measurement["fully_verified_completed"] == 1
    assert report["all_rejections"] == counts.reason_counts(native_output["all_rejections"])
    assert report["gate_rejections"] == counts.reason_counts(native_output["stage1_gate_rejections"])
    assert report["counts"]["attempted_candidates"] == 1
    assert report["first_cross"][BTC][counts.month(AT)] == {"LONG": 1, "SHORT": 0}
    assert report["completed_duration"]["median_hours"] == (33 * STEP + STEP - 1) / 3600
    assert report["by_symbol"][BTC]["fully_verified_completed"] == 1
    assert engine.cache_keys == () and engine.decisions == [] and engine.admissions == []
    assert report["unknown_trades"] == report["funding_gap_trades"] == []
    counts.validate_report(envelope(report))
    summary = counts.summarize(envelope(report))
    assert summary["phases"]["TRAIN"]["first_cross_by_direction"] == {"LONG": 1, "SHORT": 0}
    assert summary["phases"]["TRAIN"]["counts"]["fully_verified_completed"] == 1


def test_two_fresh_phases_do_not_carry_open_positions_or_events(offline_only):
    data = funding.schedule(gaps.data_for(bars=192, symbols=(BTC, ETH)))
    for series in (data.frames[ETH]["15m"], data.marks[ETH]):
        by_time = {row["time"]: row for row in series.rows}
        by_time[AT + 95 * STEP].update(close=310.0, high=310.0)
        for at, row in by_time.items():
            if at >= AT + 96 * STEP:
                row.update(open=310.0, close=310.0, high=311.0, low=309.0)
    phases = (counts.Phase("TRAIN", AT, AT + DAY), counts.Phase("VALIDATION", AT + DAY, AT + 2 * DAY))
    with counts.quiet_native():
        result = offline_only.run_until_complete(counts.measure_phases(data, plan((BTC, ETH)), phases=phases))
    assert result["TRAIN"]["counts"]["open_at_end"] == 1
    assert result["VALIDATION"]["counts"]["accepted_entries"] == 1
    for phase in result.values():
        assert phase["state_at_start"] == {"positions": 0, "events": 0, "entries": 0}
        assert phase["counts"]["open_at_end"] == 1
        assert phase["counts"]["completed"] == 0
    assert result["VALIDATION"]["counts"]["attempted_candidates"] == 1
    assert result["VALIDATION"]["period"]["start_inclusive"] == counts.stamp(AT + DAY)


def test_initial_full_259_bar_warmup_is_mandatory(offline_only):
    data = funding.schedule(gaps.data_for(bars=96, breakouts=(21,)))
    rows = data.frames[BTC]["15m"].rows
    data.frames[BTC]["15m"] = life.Series("15m", [row for row in rows if row["time"] >= AT])
    _, report = measure(data, offline_only)
    assert report["counts"]["accepted_entries"] == 0
    assert report["counts"]["attempted_candidates"] == 0
    assert report["all_rejections"] == {"warmup_window": 96}
    assert report["first_cross"][BTC][counts.month(AT)] == {"LONG": 1, "SHORT": 0}


def test_initial_warmup_releases_at_exact_259_closed_bars(offline_only):
    data = funding.schedule(gaps.data_for(bars=384, breakouts=(258,)))
    data.frames[BTC]["15m"] = life.Series(
        "15m", [row for row in data.frames[BTC]["15m"].rows if row["time"] >= AT],
    )
    engine, report = measure(data, offline_only, days=3)
    assert report["all_rejections"]["warmup_window"] == 259
    assert report["counts"]["accepted_entries"] == 1
    assert counts.stamp(engine.state.trades[0].opened_at) == counts.stamp(AT + 259 * STEP)


def test_unknown_mark_gap_remains_unverified_and_is_reported(offline_only):
    data = funding.schedule(gaps.data_for(bars=384, mark_gaps={BTC: (300,)}, breakouts=(350,)))
    _, report = measure(data, offline_only, days=4)
    assert report["counts"]["accepted_entries"] == report["counts"]["unknown_data_gap"] == 1
    assert report["counts"]["fully_verified_completed"] == report["counts"]["completed"] == 0
    assert report["counts"]["gap_exposed"] == report["counts"]["h_insufficient"] == 1
    assert report["unknown_trades"][0]["end_at"] == counts.stamp(AT + 300 * STEP)
    assert report["h_insufficient_cases"][0]["kind"] == "mark"
    assert report["all_rejections"]["accounting_verified"] == 1
    counts.validate_report(envelope(report))


def test_daily_limit_days_count_unique_utc_days(offline_only):
    with counts.quiet_native():
        engine = counts.make_engine(funding.schedule(gaps.data_for(bars=192)), config(bars=192))
        engine.at = AT
        engine.reject(["daily_trades", "exposure"])
        engine.at = AT + STEP
        engine.reject(["daily_trades"])
        engine.at = AT + DAY
        engine.reject(["daily_trades"])
    assert engine.limit_days == {counts.stamp(AT)[:10], counts.stamp(AT + DAY)[:10]}
    assert counts.reason_counts(engine.state.rejections)["daily_trades"] == 3


@pytest.mark.parametrize(("start", "end"), [
    (counts.PHASES[1].end, counts.PHASES[1].end + STEP),
    (counts.PHASES[1].end - STEP, counts.PHASES[1].end + STEP),
])
def test_test_request_denied_before_any_file_read(tmp_path, start, end):
    with (
        patch.object(Path, "open", side_effect=AssertionError("No file read")) as opened,
        pytest.raises(MeasurementScopeError, match="TEST access denied"),
    ):
        load_measurement_dataset(tmp_path / "fake", tmp_path / "metadata.json", start, end)
    assert opened.call_count == 0


def test_first_cross_counts_include_blacked_out_symbols_and_kinds(offline_only):
    data = funding.schedule(gaps.data_for(bars=400, mark_gaps={BTC: (288,)}), missing={BTC: (80,)})
    _, report = measure(data, offline_only)
    assert report["first_cross"][BTC][counts.month(AT)]["LONG"] == 1
    assert report["counts"]["attempted_candidates"] == report["counts"]["accepted_entries"] == 0
    assert report["gap_blackout_by_kind"] == {"contract": 0, "mark": 96, "funding": 96}
    assert report["all_rejections"] == {"gap_blackout": 96}
    assert report["gap_blackout_by_kind_symbol_month"][BTC][counts.month(AT)]["funding"] == 96


def test_native_funding_gap_report_verified_n_and_h_insufficiency(offline_only):
    data = funding.schedule(gaps.data_for(bars=480), missing={BTC: (88,)})
    data.marks[BTC].rows[336]["high"] = 320.0
    _, report = measure(data, offline_only, days=5)
    assert report["counts"]["accepted_entries"] == report["counts"]["completed"] == 1
    assert report["counts"]["fully_verified_completed"] == 0
    assert report["counts"]["funding_gap_affected"] == report["counts"]["funding_incomplete"] == 1
    assert report["counts"]["gap_exposed"] == report["counts"]["h_insufficient"] == 1
    assert report["h_insufficient_cases"][0]["reason"] == "holding_exceeded_pre_gap_horizon"
    assert report["h_insufficient_cases"][0]["kind"] == "funding"
    assert report["funding_gap_trades"][0]["end_at"] == counts.stamp(AT + 336 * STEP + STEP - 1)
    assert report["completed_duration"]["over_72_hours"] == 1
    assert report["unknown_trades"] == []
    counts.validate_report(envelope(report))


@pytest.mark.parametrize("field", ["net_r", "PF", "win_rate", "net_pnl", "equity_curve", "entry_price", "return"])
def test_schema_forbids_outcome_fields_at_trade_level(field, offline_only):
    _, phase = measure(funding.schedule(gaps.data_for(bars=96)), offline_only)
    report = envelope(phase)
    report["phases"]["TRAIN"]["funding_gap_trades"] = [{
        "symbol": BTC, "opened_at": counts.stamp(AT), "end_at": counts.stamp(AT + STEP),
        "status": "CLOSED", field: 999,
    }]
    with pytest.raises(counts.MeasurementError, match="TRADE_SCHEMA"):
        counts.validate_report(report)


def test_no_native_details_in_json_stdout_or_logs(offline_only, caplog, capsys):
    _, phase = measure(funding.schedule(gaps.data_for(bars=96)), offline_only)
    report = envelope(phase)
    counts.validate_report(report)
    serialized = json.dumps(report).lower()
    for fragment in counts.FORBIDDEN:
        assert fragment not in serialized
    with counts.quiet_native():
        print("PRIVATE_NATIVE_DETAILS")
        sys.stderr.write("PRIVATE_NATIVE_DETAILS")
        counts.logging.error("PRIVATE_NATIVE_DETAILS")
    captured = capsys.readouterr()
    assert "PRIVATE_NATIVE_DETAILS" not in captured.out + captured.err + caplog.text
    assert counts.safe_reason("pnl_verified") == "accounting_verified"
    with pytest.raises(counts.MeasurementError, match="UNSAFE_REASON"):
        counts.safe_reason("PRIVATE_price")


@pytest.mark.parametrize(("hours", "expected"), [
    ([], {"count": 0, "median_hours": None, "p90_hours": None, "p99_hours": None,
          "maximum_hours": None, "over_72_hours": 0}),
    ([1], {"count": 1, "median_hours": 1, "p90_hours": 1, "p99_hours": 1,
           "maximum_hours": 1, "over_72_hours": 0}),
    ([100, 1, 3, 2, 72], {"count": 5, "median_hours": 3, "p90_hours": 88.8, "p99_hours": 98.88,
                         "maximum_hours": 100, "over_72_hours": 1}),
])
def test_duration_linear_percentiles_and_strict_72_hours(hours, expected):
    actual = counts.duration_distribution(hours)
    for key, value in expected.items():
        if isinstance(value, float):
            assert actual[key] == pytest.approx(value, abs=1e-12)
        else:
            assert actual[key] == value


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1])
def test_duration_invalid_values_are_errors(bad):
    with pytest.raises(counts.MeasurementError, match="INVALID_DURATION"):
        counts.duration_distribution([bad])


@pytest.mark.parametrize(("train", "validation", "expected"), [(0, 80, 0), (100, 80, 47), (126, 100, 60)])
def test_exact_conservative_test_count_and_threshold(train, validation, expected):
    report = {
        "TRAIN": {"calendar_days": 457, "counts": {"fully_verified_completed": train}},
        "VALIDATION": {"calendar_days": 243, "counts": {"fully_verified_completed": validation}},
    }
    result = counts.prequalification(report)
    assert result["n_lower"] == expected
    assert result["decision"] == ("TEST A\u00c7ILMAZ" if expected < 60 else "\u00f6n yeterlilik ge\u00e7ti (garanti de\u011fil)")


def test_existing_output_stops_before_other_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(counts, "OUTPUT", tmp_path)
    with (
        patch.object(counts, "locked_plan", side_effect=AssertionError("No reads")),
        counts.redirect_stdout(io.StringIO()) as stream,
    ):
        code = counts.main(["--metadata", str(tmp_path / "unused.json")])
    assert code == 2
    assert json.loads(stream.getvalue()) == {"status": "failed", "code": "RESULTS_ALREADY_EXIST"}


def test_runtime_blocks_network_without_probing():
    with counts.deny_network(), pytest.raises(counts.MeasurementError, match="NETWORK_FORBIDDEN"):
        counts.socket.getaddrinfo("example.invalid", 443)


@pytest.mark.parametrize(("kind", "code"), [
    ("candle", "CANDLE_OUTSIDE_MEASUREMENT_SCOPE"),
    ("funding", "FUNDING_OUTSIDE_MEASUREMENT_SCOPE"),
])
def test_loaded_scope_rejects_test_timestamp_content(kind, code):
    series = SimpleNamespace(times=[counts.PHASES[0].start])
    data = SimpleNamespace(
        frames={BTC: {interval: copy.deepcopy(series) for interval in ("15m", "1h", "4h")}},
        marks={BTC: copy.deepcopy(series)}, funding={BTC: []}, report={"missing_archives": []},
    )
    if kind == "candle":
        data.frames[BTC]["15m"].times.append(counts.PHASES[-1].end)
    else:
        data.funding[BTC].append({"time": counts.PHASES[-1].end})
    with pytest.raises(counts.MeasurementError, match=code):
        counts.validate_loaded_scope(data, [BTC])
