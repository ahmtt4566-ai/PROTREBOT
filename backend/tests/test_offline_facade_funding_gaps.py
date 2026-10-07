"""Synthetic funding schedules; native settlement/gates/protection stay real.

Intervals alone do not define gaps: use elapsed time > right interval + 60s.
The left event is the native reported gap start, not an invented missing-event
timestamp. Funding gap intersections use the native strict lifetime overlap.
"""

from __future__ import annotations

import copy
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import test_offline_facade_gap_blackout as gaps
import test_offline_facade_lifecycle as life
from app import execution_core as core
from app.backtest_baseline import Engine, iso
from app.strategies.donchian_params import DonchianParams, DonchianParamsV2
from app.strategies.provenance import parameter_hash

AT, BTC, ETH, STEP = gaps.AT, gaps.BTC, gaps.ETH, gaps.STEP
HOUR = 3600
offline_only = life.offline_only


def schedule(data, *, missing=None):
    for symbol in data.frames:
        absent = set() if missing is None else set(missing.get(symbol, ()))
        data.funding[symbol] = [
            {"time": AT + hour * HOUR, "rate": 0.0001, "interval_hours": 8}
            for hour in range(-8, 129, 8) if hour not in absent
        ]
    return data


def funding_gaps(engine, symbol=BTC):
    return [row for row in engine.gap_inventory[symbol]["gaps"] if row["stream"] == "funding"]


def test_8_to_4_to_2_interval_change_alone_is_not_a_native_gap():
    data = gaps.data_for(bars=100)
    data.funding[BTC] = [
        {"time": AT - 8 * HOUR, "rate": 0.0001, "interval_hours": 8},
        {"time": AT - 4 * HOUR, "rate": 0.0001, "interval_hours": 4},
        {"time": AT - 2 * HOUR, "rate": 0.0001, "interval_hours": 2},
        {"time": AT, "rate": 0.0001, "interval_hours": 2},
        {"time": AT + 2 * HOUR, "rate": 0.0001, "interval_hours": 2},
    ]
    assert data.funding_complete(BTC, AT - 4 * HOUR, AT + HOUR) is True
    engine = gaps.donor(data)
    assert funding_gaps(engine) == []


def test_old_cadence_with_smaller_right_interval_is_flagged_by_native_definition():
    data = gaps.data_for(bars=100)
    data.funding[BTC] = [
        {"time": AT - 8 * HOUR, "rate": 0.0001, "interval_hours": 8},
        {"time": AT, "rate": 0.0001, "interval_hours": 4},
        {"time": AT + 2 * HOUR, "rate": 0.0001, "interval_hours": 2},
    ]
    assert data.funding_complete(BTC, AT - HOUR, AT + HOUR) is False
    assert funding_gaps(gaps.donor(data)) == [{
        "stream": "funding", "start": AT - 8 * HOUR, "end_exclusive": AT,
    }]


@pytest.mark.parametrize("right_hours", [8, 4, 2])
@pytest.mark.parametrize(("extra", "complete"), [(60, True), (60.001, False)])
def test_native_tolerance_boundary_and_fractional_time_are_exact(right_hours, extra, complete):
    data = gaps.data_for(bars=150)
    right = AT + right_hours * HOUR + extra
    data.funding[BTC] = [
        {"time": AT, "rate": 0.0001, "interval_hours": 8},
        {"time": right, "rate": 0.0001, "interval_hours": right_hours},
    ]
    assert data.funding_complete(BTC, AT + 1, int(right) - 1) is complete
    detected = funding_gaps(gaps.donor(data))
    assert len(detected) == (0 if complete else 1)
    if not complete:
        assert detected[0]["start"] == AT
        assert detected[0]["end_exclusive"] == right


@pytest.mark.parametrize(("distance", "blocked"), [(72 * HOUR, True), (72 * HOUR + STEP, False)])
def test_funding_gap_72_hour_entry_boundary(distance, blocked, offline_only):
    data = gaps.data_for(bars=450)
    data.funding[BTC] = [
        {"time": AT + distance + offset * 8 * HOUR, "rate": 0.0001, "interval_hours": 8}
        for offset in range(-10, 3) if offset != 1
    ]
    engine = gaps.donor(data)
    offline_only.run_until_complete(engine.enter(BTC, AT))
    assert len(engine.state.trades) == (0 if blocked else 1)
    if blocked:
        assert engine.state.rejections == {"gap_blackout": 1}
        assert engine.admissions == []
        assert engine.gap_blackouts[0]["gaps"] == [{
            "stream": "funding", "start": AT + distance, "end_exclusive": AT + distance + 16 * HOUR,
        }]
    else:
        assert engine.state.rejections == {} and engine.gap_blackouts == []


def test_funding_blackout_is_symbol_local_and_replay_trace_is_counted(offline_only):
    data = schedule(gaps.data_for(symbols=(BTC, ETH)), missing={BTC: (80,)})
    engine = gaps.donor(data)
    output = offline_only.run_until_complete(engine.replay())
    assert [row["symbol"] for row in output["trades"]] == [ETH]
    assert output["all_rejections"] == {"gap_blackout": 1}
    assert output["gap_blackout_count"] == 1
    assert output["gap_blackouts"][0]["symbol"] == BTC
    assert output["gap_blackouts"][0]["gaps"][0]["stream"] == "funding"


def test_funding_does_not_change_signal_or_add_post_gap_warmup(offline_only):
    data = gaps.data_for(bars=100, breakouts=(63,))
    intact = schedule(copy.deepcopy(data))
    data.funding[BTC] = [
        {"time": AT, "rate": 0.0001, "interval_hours": 8},
        {"time": AT + 16 * HOUR, "rate": 0.0001, "interval_hours": 8},
        {"time": AT + 24 * HOUR, "rate": 0.0001, "interval_hours": 8},
    ]
    first, second = gaps.donor(intact), gaps.donor(data)
    at = AT + 16 * HOUR
    life.exact(first.request_at(BTC, at), second.request_at(BTC, at))
    life.exact(first.canonical(BTC, at), second.canonical(BTC, at))
    assert second.canonical(BTC, at).signal.strategy_eligible is True
    offline_only.run_until_complete(second.enter(BTC, at))
    assert len(second.state.trades) == 1 and second.state.rejections == {}


@pytest.mark.parametrize(("has_gap", "verified_n"), [(True, 0), (False, 1)])
def test_native_closed_funding_unknown_preserved_and_excluded_from_verified_n(has_gap, verified_n, offline_only):
    close_index = 84 * HOUR // STEP
    data = schedule(gaps.data_for(bars=450), missing={BTC: (88,)} if has_gap else None)
    for series in (data.frames[BTC]["15m"], data.marks[BTC]):
        next(row for row in series.rows if row["time"] == AT + close_index * STEP)["high"] = 320.0
    engine = gaps.donor(data, bars=close_index + 1)
    offline_only.run_until_complete(engine.enter(BTC, AT))
    assert len(engine.state.trades) == 1
    native = Engine(copy.deepcopy(data), engine.state.config)
    native.positions = copy.deepcopy(engine.state.positions)
    native.trades = list(native.positions.values())
    native.cash = engine.state.cash
    native.events = copy.deepcopy(engine.state.events)
    expected = offline_only.run_until_complete(native.replay())
    actual = offline_only.run_until_complete(engine.replay())
    life.exact(life.economic_row(native.trades[0]), life.economic_row(engine.state.trades[0]))
    assert actual["trades"][0]["status"] == "CLOSED"
    assert actual["trades"][0]["net_r"] == expected["trades"][0]["net_r"]
    assert actual["measurement_counts"]["fully_verified_completed"] == verified_n
    assert actual["measurement_counts"]["closed"] == 1
    assert actual["measurement_counts"]["funding_incomplete"] == (1 if has_gap else 0)
    report = actual["funding_data_gaps"]
    assert report["trade_count"] == (1 if has_gap else 0)
    if has_gap:
        assert actual["trades"][0]["net_r"] is None
        assert actual["trades"][0]["funding_usdt"] is None
        assert expected["trades"][0]["closed_at"] == iso(AT + close_index * STEP + STEP - 1)
        assert report["trades"] == [{
            "signal_id": engine.state.trades[0].signal_identifier, "symbol": BTC,
            "opened_at": iso(AT), "end_at": iso(AT + close_index * STEP + STEP - 1),
            "native_status": "CLOSED", "funding_complete": False, "net_r_known": False,
            "gaps": [{"stream": "funding", "start": AT + 80 * HOUR, "end_exclusive": AT + 96 * HOUR}],
        }]
    else:
        assert actual["trades"][0]["net_r"] is not None
    daily = core.daily_execution_metrics(
        engine.state.events, datetime.fromtimestamp(AT + close_index * STEP, timezone.utc),
    )
    assert daily["unverified_closures"] == 0
    assert actual["unknown_data_gaps"] == {"trade_count": 0, "trades": []}


def test_open_at_end_funding_gap_is_reported_but_not_completed_n(offline_only):
    data = schedule(gaps.data_for(bars=450), missing={BTC: (88,)})
    engine = gaps.donor(data, bars=81 * HOUR // STEP)
    offline_only.run_until_complete(engine.enter(BTC, AT))
    output = offline_only.run_until_complete(engine.replay())
    assert output["trades"][0]["status"] == "OPEN_AT_END"
    assert output["trades"][0]["net_r"] is None
    assert output["funding_data_gaps"]["trade_count"] == 1
    assert output["funding_data_gaps"]["trades"][0]["end_at"] == iso(engine.state.config.end - 1)
    assert output["measurement_counts"]["fully_verified_completed"] == 0


def test_no_pair_gaps_does_not_fabricate_missing_month_or_tail_gaps(offline_only):
    data = schedule(gaps.data_for(bars=34))
    data.funding_months[BTC].clear()
    data.marks[BTC].rows[-1]["high"] = 320.0
    engine = gaps.donor(data, bars=34)
    offline_only.run_until_complete(engine.enter(BTC, AT))
    output = offline_only.run_until_complete(engine.replay())
    assert funding_gaps(engine) == []
    assert output["funding_data_gaps"]["trade_count"] == 0
    assert output["trades"][0]["status"] == "CLOSED"
    assert output["trades"][0]["net_r"] is None
    assert output["measurement_counts"]["funding_incomplete"] == 1
    assert output["measurement_counts"]["fully_verified_completed"] == 0


def test_funding_inventory_isolates_same_candle_cache_without_preset_hash_changes():
    data = schedule(gaps.data_for())
    missing = schedule(copy.deepcopy(data), missing={BTC: (80,)})
    first, second = gaps.donor(data), gaps.donor(missing)
    life.exact(first.request_at(BTC, AT), second.request_at(BTC, AT))
    life.exact(first.canonical(BTC, AT), second.canonical(BTC, AT))
    assert first.cache_keys[0][:4] == second.cache_keys[0][:4]
    assert first.cache_keys[0][5:] == second.cache_keys[0][5:]
    assert first.cache_keys[0][4] != second.cache_keys[0][4]
    assert second.gap_policy["parameters"]["funding_gap_tolerance_seconds"] == 60
    assert second.gap_policy["parameter_hash"] == parameter_hash(second.gap_policy["parameters"])
    assert second.gap_inventory[BTC]["funding"]["gaps"][0]["right_interval_hours"] == 8
    for params in (DonchianParams(), DonchianParamsV2()):
        engine = gaps.donor(copy.deepcopy(data), params=params)
        assert engine.params.provenance == params.provenance
        assert engine.canonical(BTC, AT).signal.provenance == params.provenance


@pytest.mark.parametrize("mutation", ["nan_time", "bool_time", "duplicate", "zero_interval", "nan_interval"])
def test_invalid_funding_evidence_fails_closed(mutation):
    data = schedule(gaps.data_for(bars=4))
    if mutation == "nan_time":
        data.funding[BTC][0]["time"] = float("nan")
    elif mutation == "bool_time":
        data.funding[BTC][0]["time"] = True
    elif mutation == "duplicate":
        data.funding[BTC][1]["time"] = data.funding[BTC][0]["time"]
    elif mutation == "zero_interval":
        data.funding[BTC][0]["interval_hours"] = 0
    else:
        data.funding[BTC][0]["interval_hours"] = float("nan")
    with pytest.raises(ValueError, match="Offline funding gap"):
        gaps.donor(data)
