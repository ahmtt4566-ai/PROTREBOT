"""Synthetic mechanical compatibility, not admission or performance research.

Branches: immutable v1/v2 selection and rejected presets; cap equality and
immediately adjacent tolerance floats; ATR% floor/plateau/ceiling for v2;
tick-rounding acceptance/rejection; min quantity/notional exact/below limits;
LONG/SHORT liquidation buffer equal/above/below. No replay or real client.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import socket
import sqlite3
import sys
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from app import execution_core as core
from app import main
from app import v25_execution as live
from app.binance_demo import round_tick
from app.liquidation_risk import isolated_liquidation_risk
from app.strategies.contracts import StrategyInput
from app.strategies.donchian_breakout import evaluate
from app.strategies.donchian_params import (
    DONCHIAN_PRESETS,
    DonchianParams,
    DonchianParamsV2,
    donchian_preset,
)

V1_HASH = "a16e9846711f0087e17e2b81cb4c2d8c5c12109a8120ebd4f292b1a205be6ad5"
V1_SOURCE_HASH = "6db6b472bcb2252be9c44f0209790e8bc7e25e250cc2c96be98ef952d78881a1"
AT = 1585699200


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    # Windows initializes the event loop using an internal local socket pair.
    loop = asyncio.new_event_loop()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Mechanical tests forbid network, admission and real orders")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(core, "evaluate_entry_gates", forbidden)
    monkeypatch.setattr(main, "evaluate_entry_gates", forbidden)
    monkeypatch.setattr(main, "risk_sized_order", forbidden)
    monkeypatch.setattr(main, "canonical_historical_decision", forbidden)
    monkeypatch.setattr(main, "fetch_candles", forbidden)
    monkeypatch.setattr(live, "execute_live_order", forbidden)
    monkeypatch.setattr(live.BinanceLiveClient, "signed", forbidden)
    monkeypatch.setattr("app.binance_demo.execute_demo_order", forbidden)
    yield loop
    loop.close()


def signal_request(side: str) -> StrategyInput:
    rows = [
        {"time": AT - (22 - index) * 900, "open": 100.0, "high": 101.0,
         "low": 99.0, "close": 100.0, "volume": 100.0}
        for index in range(22)
    ]
    close = 110.0 if side == "LONG" else 90.0
    rows[-1].update(close=close, high=max(close, 101.0), low=min(close, 99.0))
    return StrategyInput("BTCUSDT", AT, {"15m": rows}, required_intervals=("15m",))


def test_v1_source_prefix_and_provenance_are_not_rewritten():
    source = Path(__file__).parents[1] / "app" / "strategies" / "donchian_params.py"
    assert hashlib.sha256(source.read_bytes()[:2714]).hexdigest() == V1_SOURCE_HASH
    old = DonchianParams()
    assert old.provenance.strategy_version == "signal-v1"
    assert old.provenance.parameter_hash == V1_HASH
    assert old.atr_multiplier == 2.0


def test_v2_is_distinct_frozen_and_explicitly_selected():
    assert {preset.provenance.strategy_version for preset in DONCHIAN_PRESETS} == {"signal-v1", "signal-v2"}
    new = donchian_preset("signal-v2")
    assert new.atr_multiplier == 1.4
    assert new.provenance.strategy_id == "donchian_breakout"
    assert new.provenance.parameter_hash != V1_HASH
    assert new.provenance == DonchianParamsV2().provenance
    old_fields, new_fields = DonchianParams().parameters(), new.parameters()
    assert {key for key in old_fields if old_fields[key] != new_fields[key]} == {"atr_multiplier"}
    with pytest.raises(FrozenInstanceError):
        new.__setattr__("atr_multiplier", 1.5)
    with pytest.raises(ValueError, match="Unsupported Donchian"):
        donchian_preset("signal-v3")


@pytest.mark.parametrize("multiplier", [2, 1.5, 1.3, True, "1.4", math.nan, math.inf])
def test_v2_rejects_all_unapproved_multipliers(multiplier):
    with pytest.raises(ValueError, match="only supports"):
        DonchianParamsV2(atr_multiplier=multiplier)


@pytest.mark.parametrize(
    "changes",
    [{"channel_period": 21}, {"atr_period": 15}, {"strict": False},
     {"first_cross_only": False}, {"breakout_mode": "high_low"}, {"tp_multiples": (1, 2, 4)},
     {"exit_policy_id": "B"}, {"timeframe": "1h"}, {"zero_width_policy": "ALLOW"}],
)
def test_v2_rejects_changes_to_other_fixed_parameters(changes):
    with pytest.raises(ValueError, match="retain the other"):
        replace(DonchianParamsV2(), **changes)


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_shared_formula_changes_only_explicit_v2_stop_and_targets(side):
    request = signal_request(side)
    old = evaluate(request)
    assert old == evaluate(request, donchian_preset("signal-v1"))
    new = evaluate(request, donchian_preset("signal-v2"))
    assert new == evaluate(request, DonchianParamsV2())
    assert new.signal.decision == old.signal.decision == ("BUY" if side == "LONG" else "SELL")
    assert new.signal.strategy_eligible is True
    assert new.signal.entry is not None and new.signal.atr is not None and new.signal.stop is not None
    assert new.signal.atr == old.signal.atr
    assert new.signal.entry == old.signal.entry
    assert new.signal.exit_policy_id == old.signal.exit_policy_id
    assert new.signal.invalidation is None and new.legacy == {}
    sign = 1 if side == "LONG" else -1
    assert new.signal.stop == new.signal.entry - sign * 1.4 * new.signal.atr
    expected = main.v20_target_plan(new.signal.entry, new.signal.stop, side)
    assert (new.signal.tp1, new.signal.tp2, new.signal.tp3) == tuple(expected[key] for key in ("tp1", "tp2", "tp3"))
    check = next(check for check in new.signal.quality_checks if check.key == "stop")
    assert check.value == new.signal.stop
    assert new.signal.diagnostics["admission"] == "NOT_EVALUATED"


def test_cap_exact_equality_is_accepted_without_policy_changes():
    policy = core.sanitize_execution_policy({})
    assert policy["atr_stop_multiplier"] == 1.5 and policy["max_stop_distance_pct"] == 5
    assert core.dynamic_stop_distance_pct(100, 0.1, policy) == 1.0
    assert core.risk_sized_order(100, 99, policy, atr=0.1)["stop_distance_pct"] == 1.0


@pytest.mark.parametrize("accepted", [True, False])
def test_nearest_representable_prices_below_and_above_tolerance(accepted):
    entry, limit = 100.0, 1.0 + 1e-9
    anchor = entry - limit
    stop = math.nextafter(anchor, math.inf) if accepted else math.nextafter(anchor, -math.inf)
    actual_pct = abs(entry - stop) / entry * 100
    assert (actual_pct <= limit) is accepted
    if accepted:
        core.risk_sized_order(entry, stop, {}, atr=0.1)
    else:
        with pytest.raises(ValueError, match="Stop mesafesi"):
            core.risk_sized_order(entry, stop, {}, atr=0.1)


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize(
    ("atr_pct", "accepted"),
    [(0.666, True), (2 / 3, True), (0.667, True),
     (25 / 7 - 1e-8, True), (25 / 7, True), (25 / 7 + 1e-8, False)],
)
def test_v2_atr_price_floor_and_ceiling_boundaries(side, atr_pct, accepted):
    entry = 100.0
    multiplier = donchian_preset("signal-v2").atr_multiplier
    stop = entry - multiplier * atr_pct if side == "LONG" else entry + multiplier * atr_pct
    cap = core.dynamic_stop_distance_pct(entry, atr_pct, {})
    assert cap == min(5.0, max(1.0, 1.5 * atr_pct))
    if accepted:
        core.risk_sized_order(entry, stop, {}, atr=atr_pct)
    else:
        with pytest.raises(ValueError, match="Stop mesafesi"):
            core.risk_sized_order(entry, stop, {}, atr=atr_pct)


def test_v2_nominal_cap_equality_can_fail_after_tick_rounding():
    atr = 25 / 7
    raw_entry = Decimal(100)
    raw_stop = Decimal(str(100 - DonchianParamsV2().atr_multiplier * atr))
    core.risk_sized_order(float(raw_entry), float(raw_stop), {}, atr=atr)
    entry = round_tick(raw_entry, Decimal("0.07"))
    stop = round_tick(raw_stop, Decimal("0.07"))
    assert entry == Decimal("100.03") and stop == Decimal("94.99")
    with pytest.raises(ValueError, match="Stop mesafesi"):
        core.risk_sized_order(float(entry), float(stop), {}, atr=atr)


def test_tick_rounding_stage_can_land_on_cap_but_does_not_skip_raw_sizing():
    with pytest.raises(ValueError, match="Stop mesafesi"):
        core.risk_sized_order(100, 94.9999, {}, atr=25 / 7)
    rounded = round_tick(Decimal("94.9999"), Decimal("0.01"))
    assert rounded == Decimal("95.00")
    core.risk_sized_order(100, float(rounded), {}, atr=25 / 7)


class SyntheticSpecClient(live.BinanceLiveClient):
    """Only in-memory public price/rules; every other endpoint fails."""

    def __init__(self, min_qty: str, min_notional: str):
        self.min_qty, self.min_notional = min_qty, min_notional

    async def public_get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if path == "/fapi/v1/ticker/price":
            assert params == {"symbol": "BTCUSDT"}
            return {"price": "100"}
        if path == "/fapi/v1/exchangeInfo":
            assert params is None
            return {"symbols": [{
                "symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT",
                "filters": [
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.7", "minQty": self.min_qty, "maxQty": "1000"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {"filterType": "MIN_NOTIONAL", "notional": self.min_notional},
                ],
            }]}
        raise AssertionError(f"Unexpected offline endpoint: {path}")


@pytest.mark.parametrize(
    ("min_qty", "min_notional", "accepted"),
    [("2.8", "280", True), ("2.800000001", "280", False), ("2.8", "280.000000001", False)],
)
def test_native_minimum_quantity_and_notional_equality_and_below(
    min_qty, min_notional, accepted, offline_only,
):
    entry, atr = 100.0, 0.7
    stop = entry - DonchianParamsV2().atr_multiplier * atr
    targets = main.v20_target_plan(entry, stop, "LONG")
    body = live.LiveOrderRequest(
        symbol="BTCUSDT", direction="LONG", margin_usdt=25, leverage=30,
        stop_loss=stop, tp1=targets["tp1"], tp2=targets["tp2"], tp3=targets["tp3"], atr=atr,
    )
    client = SyntheticSpecClient(min_qty, min_notional)
    if accepted:
        spec = offline_only.run_until_complete(live.build_live_spec(client, body, {}))
        assert Decimal(spec["quantity"]) == Decimal("2.8")
        assert spec["notional_usdt"] == 280
    else:
        with pytest.raises(live.LiveExchangeError, match="minimum emir"):
            offline_only.run_until_complete(live.build_live_spec(client, body, {}))


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("offset", [Decimal("-0.00000001"), Decimal(0), Decimal("0.00000001")])
def test_liquidation_buffer_below_equal_above(side, offset):
    policy = core.sanitize_execution_policy({})
    spec = {"symbol": "BTCUSDT", "direction": side, "entry_price": "100", "quantity": "1",
            "leverage": 10, "stop_loss": "99" if side == "LONG" else "101"}
    brackets = {"symbol": "BTCUSDT", "brackets": [{
        "notionalFloor": 0, "notionalCap": 1000000, "maintMarginRatio": 0,
        "cum": 0, "initialLeverage": 125,
    }]}
    initial = isolated_liquidation_risk(spec, brackets, policy)
    liquidation = Decimal(initial["liquidation_price_estimate"])
    required = Decimal(initial["minimum_buffer"])
    available = required + offset
    spec["stop_loss"] = str(liquidation + available if side == "LONG" else liquidation - available)
    if offset < 0:
        with pytest.raises(ValueError, match="required buffer"):
            isolated_liquidation_risk(spec, brackets, policy)
    else:
        result = isolated_liquidation_risk(spec, brackets, policy)
        assert Decimal(result["available_buffer"]) == available
        assert result["verified"] is True
