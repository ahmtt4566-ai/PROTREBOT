"""Conservative pre-entry isolated USD-M liquidation estimate, without I/O."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"Missing or invalid {label}")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid {label}") from exc
    if not number.is_finite():
        raise ValueError(f"Non-finite {label}")
    return number


def isolated_liquidation_risk(
    spec: dict[str, Any],
    payload: Any,
    policy: dict[str, Any],
) -> dict[str, Any]:
    symbol = str(spec.get("symbol") or "").upper()
    direction = str(spec.get("direction") or "").upper()
    entry = _decimal(spec.get("entry_price"), "entry price")
    quantity = _decimal(spec.get("quantity"), "quantity")
    leverage = _decimal(spec.get("leverage"), "leverage")
    stop = _decimal(spec.get("stop_loss"), "stop price")
    buffer_pct = _decimal(policy.get("liquidation_buffer_pct"), "liquidation buffer")
    fee = _decimal(policy.get("fee_bps_per_side"), "fee estimate") / Decimal(10_000)
    if direction not in {"LONG", "SHORT"} or min(entry, quantity, stop, buffer_pct) <= 0 or leverage < 1 or leverage != leverage.to_integral_value() or fee < 0:
        raise ValueError("Invalid isolated liquidation inputs")
    if (direction == "LONG" and stop >= entry) or (direction == "SHORT" and stop <= entry):
        raise ValueError("Stop is on the wrong side of entry")
    rows = payload if isinstance(payload, list) else [payload]
    matches = [row for row in rows if isinstance(row, dict) and str(row.get("symbol") or "").upper() == symbol]
    if len(matches) != 1:
        raise ValueError("Exact symbol maintenance brackets are unavailable")
    coefficient = _decimal(matches[0].get("notionalCoef", 1), "notional coefficient")
    if coefficient != 1:
        raise ValueError("Customized maintenance coefficients require separately verified tier semantics")
    brackets = matches[0].get("brackets")
    if not isinstance(brackets, list) or not brackets:
        raise ValueError("Maintenance brackets are unavailable")

    tiers = []
    for row in brackets:
        if not isinstance(row, dict):
            raise ValueError("Invalid maintenance bracket")
        floor = _decimal(row.get("notionalFloor"), "bracket floor")
        cap = _decimal(row.get("notionalCap"), "bracket cap")
        rate = _decimal(row.get("maintMarginRatio"), "maintenance rate")
        cumulative = _decimal(row.get("cum"), "maintenance deduction")
        max_leverage = _decimal(row.get("initialLeverage"), "bracket leverage")
        if floor < 0 or cap <= floor or rate < 0 or not 0 <= rate + fee < 1 or cumulative < 0 or max_leverage < 1:
            raise ValueError("Invalid maintenance bracket bounds")
        tiers.append((floor, cap, rate, cumulative, max_leverage))
    tiers.sort()
    if tiers[0][0] != 0 or any(left[1] != right[0] for left, right in zip(tiers, tiers[1:])):
        raise ValueError("Maintenance brackets overlap or have gaps")
    if tiers[0][3] != 0 or any(
        right[2] < left[2] or right[3] != left[3] + right[0] * (right[2] - left[2])
        for left, right in zip(tiers, tiers[1:])
    ):
        raise ValueError("Maintenance deductions are inconsistent")

    notional = quantity * entry
    entry_tiers = [tier for tier in tiers if tier[0] <= notional < tier[1]]
    if len(entry_tiers) != 1 or leverage > entry_tiers[0][4]:
        raise ValueError("Entry exceeds verified maintenance/leverage bracket")
    # Entry and exit fee reserves make this stricter than a fee-free estimate.
    margin = notional / leverage
    entry_fee = notional * fee
    solutions = []
    for floor, cap, rate, cumulative, _ in tiers:
        if direction == "LONG":
            liquidation = (notional - margin + entry_fee - cumulative) / (quantity * (1 - rate - fee))
        else:
            liquidation = (notional + margin - entry_fee + cumulative) / (quantity * (1 + rate + fee))
        liquidation_notional = quantity * liquidation
        if liquidation >= 0 and floor <= liquidation_notional < cap:
            if liquidation_notional * rate - cumulative < 0:
                raise ValueError("Maintenance requirement is negative")
            solutions.append((liquidation, rate, cumulative))
    if len(solutions) != 1:
        raise ValueError("Isolated liquidation estimate cannot be verified")
    liquidation, rate, cumulative = solutions[0]
    minimum_buffer = entry * buffer_pct / 100
    available_buffer = stop - liquidation if direction == "LONG" else liquidation - stop
    if available_buffer < minimum_buffer:
        raise ValueError("Stop is not before liquidation with the required buffer")
    return {
        "verified": True,
        "model": "ISOLATED_MAINTENANCE_BRACKETS_WITH_FEE_RESERVE",
        "liquidation_price_estimate": str(liquidation),
        "buffer_pct": float(buffer_pct),
        "minimum_buffer": str(minimum_buffer),
        "available_buffer": str(available_buffer),
        "maintenance_rate": str(rate),
        "maintenance_deduction": str(cumulative),
        "funding_included": False,
    }
