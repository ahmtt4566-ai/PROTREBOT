"""Synthetic gap masks only: fixed H=72h/W=259, no historical input or real I/O.

Boundary and lifecycle assertions are literal; native gates are not replaced.
Original is compared to the real native driver. Leading/trailing gaps use the
declared replay bounds; no missing bar is invented outside known coverage.
"""

from __future__ import annotations

import copy
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import test_offline_facade_lifecycle as life
from app import execution_core as core
from app.backtest_baseline import Engine, iso
from app.backtest_data import Series
from app.strategies.donchian_indicator import closed_contract_candles
from app.strategies.donchian_params import DonchianParams, DonchianParamsV2
from app.strategies.offline_facade import DonchianOfflineEngine, OfflineFacade
from app.strategies.provenance import parameter_hash

AT = life.AT
BTC, ETH = "BTCUSDT", "ETHUSDT"
STEP = 900
H = 288
W = 259
offline_only = life.offline_only


def data_for(*, bars=350, symbols=(BTC,), contract_gaps=None, mark_gaps=None, breakouts=()):
    data = life.dataset(symbols=symbols, bars=bars, tick="0.01")
    for symbol in symbols:
        series = data.frames[symbol]["15m"]
        history = copy.deepcopy(series.closed(AT, limit=len(series.rows)))
        future = [life.candle(AT + index * STEP, 300.0, 301.0, 299.0, 300.0) for index in range(bars)]
        for index in breakouts:
            future[index].update(close=310.0, high=310.0)
            if index + 1 < bars:
                future[index + 1].update(open=310.0, close=310.0, high=311.0, low=309.0)
        contract_missing = set() if contract_gaps is None else set(contract_gaps.get(symbol, ()))
        mark_missing = set() if mark_gaps is None else set(mark_gaps.get(symbol, ()))
        data.frames[symbol]["15m"] = Series(
            "15m", history + [copy.deepcopy(row) for index, row in enumerate(future) if index not in contract_missing],
        )
        data.marks[symbol] = Series(
            "15m", [copy.deepcopy(row) for index, row in enumerate(future) if index not in mark_missing],
        )
    return data


def donor(data, *, bars=1, params=None):
    facade = OfflineFacade(
        data, life.config(bars=bars), strategy_id="donchian_breakout",
        params=DonchianParamsV2() if params is None else params,
    )
    assert isinstance(facade.engine, DonchianOfflineEngine)
    return facade.engine


@pytest.mark.parametrize("stream", ["contract", "mark"])
@pytest.mark.parametrize(("distance", "blocked"), [(H, True), (H + 1, False)])
def test_exact_72_hour_boundary_and_one_bar_before(stream, distance, blocked, offline_only):
    kwargs = {f"{stream}_gaps": {BTC: (distance,)}}
    engine = donor(data_for(**kwargs))
    before = (engine.state.cash, copy.deepcopy(engine.state.events))
    assert engine.canonical(BTC, AT).signal.strategy_eligible is True
    offline_only.run_until_complete(engine.enter(BTC, AT))
    if blocked:
        assert engine.state.trades == []
        assert engine.state.rejections == {"gap_blackout": 1}
        assert engine.admissions == []
        assert before == (engine.state.cash, engine.state.events)
        assert engine.gap_blackouts[0]["gaps"] == [
            {"stream": stream, "start": AT + distance * STEP, "end_exclusive": AT + (distance + 1) * STEP},
        ]
    else:
        assert len(engine.state.trades) == 1
        assert engine.state.rejections == {}
        assert engine.gap_blackouts == []
        assert len(engine.admissions) == 3


def test_replay_counts_blackout_once_and_does_not_call_strategy_or_native_gates(offline_only):
    engine = donor(data_for(mark_gaps={BTC: (H,)}))
    with patch.object(engine, "canonical", side_effect=AssertionError("No signal inside blackout")), \
            patch.object(engine, "_admission", side_effect=AssertionError("No gate call inside blackout")):
        output = offline_only.run_until_complete(engine.replay())
    assert output["all_rejections"] == output["first_rejections"] == {"gap_blackout": 1}
    assert output["gap_blackout_count"] == 1
    assert output["gap_blackouts"][0]["phase"] == "before_market_ranking"
    assert output["trades"] == output["admissions"] == output["decisions"] == []


@pytest.mark.parametrize("complete_bars", [W - 1, W])
def test_contract_reentry_requires_exact_full_window_after_gap(complete_bars, offline_only):
    gap = 1
    at_index = gap + 1 + complete_bars
    data = data_for(bars=600, contract_gaps={BTC: (gap,)}, breakouts=(at_index - 1,))
    engine = donor(data)
    at = AT + at_index * STEP
    offline_only.run_until_complete(engine.enter(BTC, at))
    if complete_bars < W:
        assert engine.state.trades == []
        assert engine.state.rejections == {"data_gap_history": 1}
    else:
        assert data.frames[BTC]["15m"].history_complete(at) is True
        assert len(engine.request_at(BTC, at).candles_by_timeframe["15m"]) == W
        assert len(engine.state.trades) == 1
        assert engine.state.rejections == {}


def test_leading_contract_gap_does_not_resume_at_strategy_minimum(offline_only):
    data = data_for(bars=400, contract_gaps={BTC: (0,)}, breakouts=(22,))
    data.frames[BTC]["15m"] = Series("15m", [row for row in data.frames[BTC]["15m"].rows if row["time"] >= AT])
    engine = donor(data)
    at = AT + 23 * STEP
    assert engine.canonical(BTC, at).signal.strategy_eligible is True
    offline_only.run_until_complete(engine.enter(BTC, at))
    assert engine.state.trades == []
    assert engine.state.rejections == {"data_gap_history": 1}


def test_mark_only_preserves_signal_and_has_no_post_gap_warmup(offline_only):
    intact = donor(data_for(bars=10, breakouts=(1,)))
    missing = donor(data_for(bars=10, mark_gaps={BTC: (0,)}, breakouts=(1,)))
    for at in (AT, AT + 2 * STEP):
        life.exact(intact.request_at(BTC, at), missing.request_at(BTC, at))
        life.exact(intact.canonical(BTC, at), missing.canonical(BTC, at))
    offline_only.run_until_complete(missing.enter(BTC, AT))
    assert missing.state.rejections == {"data_missing_next_open": 1}
    assert missing.gap_blackouts == []
    assert missing.canonical(BTC, AT + 2 * STEP).signal.strategy_eligible is True
    offline_only.run_until_complete(missing.enter(BTC, AT + 2 * STEP))
    assert len(missing.state.trades) == 1
    assert missing.state.trades[0].opened_at == AT + 2 * STEP
    assert missing.state.rejections == {"data_missing_next_open": 1}


def test_symbol_blackout_does_not_block_other_symbol(offline_only):
    engine = donor(data_for(symbols=(BTC, ETH), mark_gaps={BTC: (H,)}))
    output = offline_only.run_until_complete(engine.replay())
    assert [trade["symbol"] for trade in output["trades"]] == [ETH]
    assert output["all_rejections"] == {"gap_blackout": 1}
    assert [row["symbol"] for row in output["gap_blackouts"]] == [BTC]
    assert output["unknown_data_gaps"] == {"trade_count": 0, "trades": []}


@pytest.mark.parametrize("stream", ["contract", "mark"])
def test_long_lived_position_keeps_native_unknown_gap_and_pnl_verified(stream, offline_only):
    gap = H + 1
    data = data_for(bars=310, breakouts=(gap,), **{f"{stream}_gaps": {BTC: (gap,)}})
    engine = donor(data, bars=gap + 1)
    offline_only.run_until_complete(engine.enter(BTC, AT))
    assert len(engine.state.trades) == 1
    output = offline_only.run_until_complete(engine.replay())
    unknown = output["unknown_data_gaps"]
    assert unknown["trade_count"] == 1
    assert unknown["trades"] == [{
        "signal_id": engine.state.trades[0].signal_identifier, "symbol": BTC,
        "opened_at": iso(AT), "detected_at": iso(AT + gap * STEP),
        "native_status": "UNKNOWN_DATA_GAP", "missing_streams": [stream],
    }]
    assert output["trades"][0]["status"] == "UNKNOWN_DATA_GAP"
    assert output["trades"][0]["net_r"] is None
    assert output["trades"][0]["funding_usdt"] is None
    assert engine.state.positions == {}
    metrics = core.daily_execution_metrics(
        engine.state.events, datetime.fromtimestamp(AT + (gap + 1) * STEP, timezone.utc),
    )
    assert metrics["unverified_closures"] == 1
    if stream == "mark":
        assert engine.canonical(BTC, AT + (gap + 1) * STEP).signal.strategy_eligible is True
        offline_only.run_until_complete(engine.enter(BTC, AT + (gap + 1) * STEP))
        assert len(engine.state.trades) == 1
        assert engine.admissions[-1]["failures"] == ("pnl_verified",)


def test_multiple_consecutive_gaps_are_grouped_and_overlap_is_one_rejection(offline_only):
    engine = donor(data_for(
        bars=700, contract_gaps={BTC: (H, H + 1, H + 2, 650)},
        mark_gaps={BTC: (H, H + 1, 640)},
    ))
    gaps = engine.gap_inventory[BTC]["gaps"]
    assert gaps == [
        {"stream": "contract", "start": AT + H * STEP, "end_exclusive": AT + (H + 3) * STEP},
        {"stream": "contract", "start": AT + 650 * STEP, "end_exclusive": AT + 651 * STEP},
        {"stream": "mark", "start": AT + H * STEP, "end_exclusive": AT + (H + 2) * STEP},
        {"stream": "mark", "start": AT + 640 * STEP, "end_exclusive": AT + 641 * STEP},
    ]
    offline_only.run_until_complete(engine.enter(BTC, AT))
    assert engine.state.rejections == {"gap_blackout": 1}
    assert len(engine.gap_blackouts) == 1
    assert len(engine.gap_blackouts[0]["gaps"]) == 2


@pytest.mark.parametrize("stream", ["contract", "mark"])
@pytest.mark.parametrize("edge", ["first", "last", "empty"])
def test_dataset_edges_and_empty_stream_use_declared_bounds(stream, edge):
    bars = 4
    indices = (0,) if edge == "first" else (bars - 1,) if edge == "last" else tuple(range(bars))
    data = data_for(bars=bars, **{f"{stream}_gaps": {BTC: indices}})
    if stream == "contract" and edge in {"first", "empty"}:
        data.frames[BTC]["15m"] = Series("15m", [row for row in data.frames[BTC]["15m"].rows if row["time"] >= AT])
    engine = donor(data, bars=bars)
    gaps = [gap for gap in engine.gap_inventory[BTC]["gaps"] if gap["stream"] == stream]
    assert gaps == [{
        "stream": stream, "start": AT if edge != "last" else AT + (bars - 1) * STEP,
        "end_exclusive": AT + STEP if edge == "first" else AT + bars * STEP,
    }]


def test_no_tail_is_invented_beyond_loaded_and_replay_end():
    engine = donor(data_for(bars=4), bars=4)
    assert engine.gap_inventory[BTC]["gaps"] == []
    assert engine.gap_inventory[BTC]["coverage"]["mark"]["end_exclusive"] == AT + 4 * STEP


def test_gap_policy_and_inventory_isolate_identical_closed_candle_cache():
    intact = donor(data_for(bars=350))
    missing = donor(data_for(bars=350, mark_gaps={BTC: (H,)}))
    life.exact(intact.request_at(BTC, AT), missing.request_at(BTC, AT))
    life.exact(intact.canonical(BTC, AT), missing.canonical(BTC, AT))
    assert intact.cache_keys[0][:4] == missing.cache_keys[0][:4]
    assert intact.cache_keys[0][5:] == missing.cache_keys[0][5:]
    assert intact.cache_keys[0][4] != missing.cache_keys[0][4]
    assert set(intact.cache_keys).isdisjoint(missing.cache_keys)
    record = missing.gap_policy
    assert record["parameters"]["blackout_hours"] == 72
    assert record["parameters"]["blackout_bars"] == 288
    assert record["parameters"]["window_closed_bars"] == 259
    assert record["parameter_hash"] == parameter_hash(record["parameters"])
    request = missing.request_at(BTC, AT)
    payload = {
        "closed_contract_15m": [
            asdict(row) for row in closed_contract_candles(request.candles_by_timeframe["15m"], AT)
        ],
        "market_type": request.market_type, "required_intervals": request.required_intervals,
        "data_window": missing.data_window.as_dict(), "gap_policy": record,
        "gap_inventory": missing.gap_inventory[BTC],
    }
    assert missing.cache_keys[0][4] == parameter_hash(payload)
    del payload["gap_policy"]
    assert missing.cache_keys[0][4] != parameter_hash(payload)
    fresh = missing.gap_inventory
    fresh[BTC]["gaps"].clear()
    assert len(missing.gap_inventory[BTC]["gaps"]) == 1


@pytest.mark.parametrize("params", [DonchianParams(), DonchianParamsV2()])
def test_preset_hashes_and_gap_free_execution_are_unchanged(params, offline_only):
    data = data_for(bars=1)
    engine = donor(data, params=params)
    original_parameters = copy.deepcopy(params.parameters())
    output = offline_only.run_until_complete(engine.replay())
    assert engine.params.parameters() == original_parameters
    assert output["strategy"]["parameter_hash"] == params.provenance.parameter_hash
    assert output["gap_blackout_count"] == 0
    assert output["gap_blackouts"] == []
    assert output["unknown_data_gaps"] == {"trade_count": 0, "trades": []}
    if params.atr_multiplier == 2.0:
        assert output["trades"] == []
        assert output["all_rejections"] == {"stop_risk": 1}
    else:
        assert len(output["trades"]) == 1
        assert output["all_rejections"] == {}
    assert all(row["gap_policy"] == engine.gap_policy for row in output["admissions"])
    assert all(row["gap_policy"] == engine.gap_policy for row in output["decisions"])
    assert output["gap_policy"] == engine.gap_policy
    assert all(row["gap_policy"] == engine.gap_policy for row in output["trades"])


def test_original_future_gap_dispatch_remains_literal_full_parity(offline_only):
    first = data_for(bars=350, mark_gaps={BTC: (H,)})
    second = copy.deepcopy(first)
    cfg = life.config()
    native, facade = Engine(first, cfg), OfflineFacade(second, cfg)
    assert type(facade.engine) is Engine
    life.exact(first.canonical(BTC, AT, native.policy), facade.canonical(BTC, AT))
    expected = offline_only.run_until_complete(native.replay())
    actual = offline_only.run_until_complete(facade.replay())
    assert len(expected["trades"]) == len(actual["trades"]) == 1
    life.exact(expected, actual)
    assert "gap_policy" not in actual and "gap_blackouts" not in actual


@pytest.mark.parametrize("mutation", ["float", "bool", "unaligned", "duplicate"])
def test_invalid_gap_timestamp_evidence_fails_closed(mutation):
    data = data_for(bars=4)
    times = data.marks[BTC].times
    if mutation == "float":
        times[0] = float(times[0])
    elif mutation == "bool":
        times[0] = True
    elif mutation == "unaligned":
        times[0] += 1
    else:
        times[1] = times[0]
    with pytest.raises(ValueError, match="Offline gap inventory"):
        donor(data)


def test_unknown_trade_without_native_evidence_is_not_silently_reported_as_success(offline_only):
    engine = donor(data_for(bars=1))
    offline_only.run_until_complete(engine.enter(BTC, AT))
    engine.state.trades[0].status = "UNKNOWN_DATA_GAP"
    with pytest.raises(ValueError, match="no unverified closure evidence"):
        engine._unknown_data_gap_report()
