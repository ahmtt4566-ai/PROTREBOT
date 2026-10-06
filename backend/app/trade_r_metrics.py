"""R metrics from recorded entry risk and verified closure evidence."""

from __future__ import annotations

import logging
import math
from decimal import Decimal
from typing import Any


logger = logging.getLogger(__name__)


def _decimal(value: Any, label: str, *, positive: bool = False) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (ValueError, ArithmeticError):
        logger.warning("R_METRIC_UNAVAILABLE invalid %s", label)
        return None
    if not number.is_finite() or not math.isfinite(float(number)) or (positive and float(number) <= 0):
        logger.warning("R_METRIC_UNAVAILABLE invalid %s", label)
        return None
    return number


def _finite_float(value: Decimal, label: str) -> float | None:
    result = float(value)
    if not math.isfinite(result):
        logger.warning("R_METRIC_UNAVAILABLE non-finite %s", label)
        return None
    return result


def initial_entry_risk_usdt(spec: dict[str, Any]) -> float | None:
    entry = _decimal(spec.get("entry_price"), "entry price", positive=True)
    stop = _decimal(spec.get("stop_loss"), "initial stop", positive=True)
    quantity = _decimal(spec.get("quantity"), "entry quantity", positive=True)
    if entry is None or stop is None or quantity is None:
        logger.warning("R_METRIC_UNAVAILABLE missing or invalid initial entry inputs")
        return None
    risk = _decimal(abs(entry - stop) * quantity, "initial risk", positive=True)
    if risk is None:
        return None
    result = _finite_float(risk, "initial risk")
    if result == 0:
        logger.warning("R_METRIC_UNAVAILABLE initial risk underflow")
        return None
    return result


def calculate_r_multiple(net_pnl: Any, initial_risk_usdt: Any) -> float | None:
    if net_pnl is None or initial_risk_usdt is None:
        return None
    risk = _decimal(initial_risk_usdt, "initial risk", positive=True)
    pnl = _decimal(net_pnl, "verified net PnL")
    if risk is None or pnl is None:
        return None
    return _finite_float(pnl / risk, "R multiple")


def r_performance_metrics(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    values: list[Decimal] = []
    for row in rows:
        if row.get("verified_realized") is not True or row.get("kind") not in {"POSITION_CLOSED", "LIVE_POSITION_CLOSED"}:
            continue
        risk = _decimal(row.get("initial_risk_usdt"), "initial risk", positive=True)
        value = _decimal(row.get("r_multiple"), "verified R multiple")
        if risk is not None and value is not None:
            values.append(value)
    if not values:
        return {"avg_r": None, "expectancy_r": None}
    count = Decimal(len(values))
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    avg = _finite_float(sum(values, Decimal(0)) / count, "average R")
    # Win/loss weights use only R-eligible closes; breakevens remain in count.
    expectancy = _finite_float(
        sum(wins, Decimal(0)) / count + sum(losses, Decimal(0)) / count,
        "expectancy R",
    )
    return {
        "avg_r": round(avg, 6) if avg is not None else None,
        "expectancy_r": round(expectancy, 6) if expectancy is not None else None,
    }
