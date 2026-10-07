"""One-candidate point criteria and informational reproducible bootstrap only."""

from __future__ import annotations

import math
import random
from collections import Counter
from datetime import datetime, timezone
from typing import Any


def verified(row: dict[str, Any]) -> bool:
    return (
        row["status"] == "CLOSED" and row["commission_complete"] is True and row["funding_complete"] is True
        and all(type(row[key]) in (int, float) and math.isfinite(row[key]) for key in ("net_r", "net_pnl"))
    )


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    complete = [row for row in rows if verified(row)]
    gains = math.fsum(max(0, row["net_pnl"]) for row in complete)
    losses = -math.fsum(min(0, row["net_pnl"]) for row in complete)
    status = "DEFINED" if losses > 0 else "NO_LOSSES" if gains > 0 else "UNDEFINED"
    return {
        "trade_count": len(rows), "fully_verified_completed": len(complete),
        "completed": sum(row["status"] == "CLOSED" for row in rows),
        "open_at_end": sum(row["status"] == "OPEN_AT_END" for row in rows),
        "unknown_data_gap": sum(row["status"] == "UNKNOWN_DATA_GAP" for row in rows),
        "funding_incomplete": sum(row["funding_usdt"] is None for row in rows),
        "mean_net_r": math.fsum(row["net_r"] for row in complete) / len(complete) if complete else None,
        "profit_factor": gains / losses if losses > 0 else None, "profit_factor_status": status,
        "win_rate": sum(row["net_pnl"] > 0 for row in complete) / len(complete) if complete else None,
        "net_pnl": math.fsum(row["net_pnl"] for row in complete) if complete else None,
    }


def criteria(value: dict[str, Any]) -> dict[str, Any]:
    reasons = []
    mean, pf = value["mean_net_r"], value["profit_factor"]
    if mean is None or not math.isfinite(mean) or mean <= 0:
        reasons.append("NET_R_NOT_STRICTLY_POSITIVE")
    unbounded = value["profit_factor_status"] == "NO_LOSSES" and value["net_pnl"] is not None and value["net_pnl"] > 0
    if not unbounded and (pf is None or not math.isfinite(pf) or pf <= 1.2):
        reasons.append("USDT_PF_NOT_STRICTLY_ABOVE_1_2")
    if value["fully_verified_completed"] < 60:
        reasons.append("FEWER_THAN_60_FULLY_VERIFIED_COMPLETED")
    return {"passed": not reasons, "decision": "PASS" if not reasons else "FAIL",
            "failed_criteria": reasons, "primary_point_criteria_only": True}


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    at = (len(ordered) - 1) * fraction
    low, high = math.floor(at), math.ceil(at)
    return ordered[low] + (ordered[high] - ordered[low]) * (at - low)


def bootstrap(rows: list[dict[str, Any]], start: int, end: int, *, samples: int = 20000, seed: int = 2026) -> dict:
    if type(samples) is not int or samples < 1 or type(seed) is not int or start >= end:
        raise ValueError("Invalid bootstrap settings")
    days = {day: [] for day in range(start // 86400, (end - 1) // 86400 + 1)}
    trades = []
    for row in rows:
        if not verified(row):
            continue
        day = int(datetime.fromisoformat(row["opened_at"]).timestamp()) // 86400
        if day not in days:
            raise ValueError("Bootstrap entry outside TEST")
        observation = (1, row["net_r"], max(row["net_pnl"], 0), -min(row["net_pnl"], 0))
        trades.append(observation)
        days[day].append(observation)
    calendar = [tuple(math.fsum(item[field] for item in block) for field in range(4)) for block in days.values()]
    results = {}
    for unit, blocks in (("TRADE", trades), ("UTC_DAY", calendar)):
        active = sum(block[0] > 0 for block in blocks)
        enough = active >= (2 if unit == "UTC_DAY" else 1)
        means, factors = [], []
        empty = undefined = 0
        if enough:
            rng = random.Random(seed)
            for _ in range(samples):
                weights = Counter(rng.randrange(len(blocks)) for _ in blocks)
                totals = [math.fsum(blocks[index][field] * weight for index, weight in weights.items()) for field in range(4)]
                if not totals[0]:
                    empty += 1
                    continue
                means.append(totals[1] / totals[0])
                if totals[3] > 0:
                    factors.append(totals[2] / totals[3])
                elif totals[2] > 0:
                    factors.append(math.inf)
                else:
                    undefined += 1
        ordered = sorted(factors)
        bounds = None
        lower_unbounded = upper_unbounded = False
        if ordered:
            lower = ordered[math.floor((len(ordered) - 1) * 0.025)]
            upper = ordered[math.ceil((len(ordered) - 1) * 0.975)]
            lower_unbounded, upper_unbounded = math.isinf(lower), math.isinf(upper)
            bounds = [None if lower_unbounded else lower, None if upper_unbounded else upper]
        results[unit] = {
            "status": "OK" if enough else "INSUFFICIENT_CLUSTERS", "samples": samples, "seed": seed,
            "active_clusters": active, "total_clusters": len(blocks), "valid_r_resamples": len(means),
            "valid_pf_resamples": len(factors), "empty_resamples": empty, "undefined_pf_resamples": undefined,
            "mean_net_r_95": [percentile(means, 0.025), percentile(means, 0.975)] if means else None,
            "profit_factor_95": {"bounds": bounds, "lower_unbounded": lower_unbounded, "upper_unbounded": upper_unbounded},
            "family_size": 1, "informational_only": True,
        }
    return results


def scenario_report(
    rows: list[dict], uncertainty: dict, start: int, end: int, *,
    samples=20000, seed=2026, symbols: list[str] | None = None,
) -> dict:
    by_symbol = {symbol: [] for symbol in (symbols if symbols is not None else sorted({row["symbol"] for row in rows}))}
    by_month = {datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m"): []
                for at in range(start, end, 86400)}
    for row in rows:
        opened = datetime.fromisoformat(row["opened_at"])
        if opened.tzinfo is None or not start <= opened.timestamp() < end:
            raise ValueError("Report entry outside TEST")
        if symbols is not None and row["symbol"] not in by_symbol:
            raise ValueError("Report symbol outside registered universe")
        by_symbol.setdefault(row["symbol"], []).append(row)
        by_month.setdefault(row["opened_at"][:7], []).append(row)
    value = summary(rows)
    return {
        "summary": value, "criteria": criteria(value), "uncertainty": uncertainty,
        "by_symbol": {key: summary(group) for key, group in sorted(by_symbol.items())},
        "by_entry_month": {key: summary(group) for key, group in sorted(by_month.items())},
        "bootstrap": bootstrap(rows, start, end, samples=samples, seed=seed),
    }
