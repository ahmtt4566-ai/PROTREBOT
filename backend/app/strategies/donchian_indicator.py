"""Pure closed-contract-candle validation and prior-window channel calculation."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

INTERVAL_SECONDS = 900


class CandleDataError(ValueError):
    """Explicit, displayable data rejection; no silent replacement or repair."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class ClosedCandle:
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Channel:
    upper: float
    lower: float
    start_open_time: int
    end_open_time: int
    count: int

    @property
    def width(self) -> float:
        return self.upper - self.lower


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise CandleDataError("INVALID_CANDLE_DATA", f"{field}: expected a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CandleDataError("INVALID_CANDLE_DATA", f"{field}: invalid number") from exc
    if not math.isfinite(result):
        raise CandleDataError("INVALID_CANDLE_DATA", f"{field}: nonfinite number")
    return result


def closed_contract_candles(rows: object, decision_time: int) -> tuple[ClosedCandle, ...]:
    """Select closed 15m rows before reading OHLCV; future prices cannot leak in.

    The caller supplies contract, not mark-price, OHLCV. Opening timestamps are
    UTC Unix seconds. All supplied closed history must be contiguous for ATR.
    """
    if type(decision_time) is not int or decision_time < 0:
        raise CandleDataError("INVALID_DECISION_TIME", "decision_time must be nonnegative Unix seconds")
    if not isinstance(rows, list):
        raise CandleDataError("INVALID_CANDLE_DATA", "15m candles must be a list")
    closed: list[ClosedCandle] = []
    for row in rows:
        if not isinstance(row, Mapping) or "time" not in row:
            raise CandleDataError("INVALID_CANDLE_DATA", "candle opening timestamp is missing")
        timestamp = _number(row["time"], "time")
        if timestamp < 0 or not timestamp.is_integer():
            raise CandleDataError("INVALID_CANDLE_DATA", "time must be nonnegative integer Unix seconds")
        opening = int(timestamp)
        if opening + INTERVAL_SECONDS > decision_time:
            continue
        if opening % INTERVAL_SECONDS:
            raise CandleDataError("INVALID_CANDLE_DATA", "closed candle is not aligned to 15m UTC")
        if closed:
            if opening <= closed[-1].time:
                raise CandleDataError("NON_CHRONOLOGICAL_CANDLES", "closed timestamps must strictly increase")
            if opening != closed[-1].time + INTERVAL_SECONDS:
                raise CandleDataError("DATA_GAP", "closed 15m contract history has a missing slot")
        try:
            values = {key: _number(row[key], key) for key in ("open", "high", "low", "close", "volume")}
        except KeyError as exc:
            raise CandleDataError("INVALID_CANDLE_DATA", f"missing OHLCV field: {exc.args[0]}") from exc
        if any(values[key] <= 0 for key in ("open", "high", "low", "close")) or values["volume"] < 0:
            raise CandleDataError("INVALID_CANDLE_DATA", "prices must be positive and volume nonnegative")
        if values["high"] < max(values["open"], values["close"]) or values["low"] > min(
            values["open"], values["close"],
        ) or values["high"] < values["low"]:
            raise CandleDataError("INVALID_CANDLE_DATA", "inconsistent OHLC bounds")
        closed.append(ClosedCandle(
            opening, values["open"], values["high"], values["low"], values["close"], values["volume"],
        ))
    return tuple(closed)


def reference_channel(
    candles: Sequence[ClosedCandle], decision_index: int, period: int = 20,
) -> Channel | None:
    """Use exactly period preceding validated candles, excluding decision_index.

    None means insufficient history, not a zero-filled channel.
    """
    if type(period) is not int or period <= 0:
        raise ValueError("Channel period must be a positive integer")
    if type(decision_index) is not int or not 0 <= decision_index < len(candles):
        raise ValueError("Decision index must identify an existing candle")
    if decision_index < period:
        return None
    window = candles[decision_index - period:decision_index]
    return Channel(
        max(row.high for row in window), min(row.low for row in window),
        window[0].time, window[-1].time, period,
    )
