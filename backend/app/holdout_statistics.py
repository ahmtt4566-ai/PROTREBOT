"""Absolute net-R and USDT-PF bootstrap; no strategy calculation."""

from __future__ import annotations

import math
import random
from collections import Counter

from .backtest_baseline import percentile
from .backtest_diagnostics import utc
from .regime_statistics import complete


def pf_interval(values: list[float], alpha: float) -> dict:
    if not values:
        return {"bounds": None, "lower_unbounded": False, "upper_unbounded": False}
    values = sorted(values)
    lower = values[math.floor((len(values) - 1) * alpha / 2)]
    upper = values[math.ceil((len(values) - 1) * (1 - alpha / 2))]
    return {"bounds": [lower if math.isfinite(lower) else None, upper if math.isfinite(upper) else None],
            "lower_unbounded": math.isinf(lower), "upper_unbounded": math.isinf(upper)}


def bootstrap(rows: list[dict], intervals: list[tuple[int, int]], settings: dict) -> dict:
    samples, family, alpha = settings["samples"], settings["family_size"], settings["alpha"]
    if samples < 1 or family != 3 or not 0 < alpha < 1:
        raise ValueError("Invalid registered holdout bootstrap")
    eligible = [row for row in rows if complete(row)]
    days = {}
    for start, end in intervals:
        if start >= end:
            raise ValueError("Invalid bootstrap scope")
        for day in range(start // 86400, (end - 1) // 86400 + 1):
            days[day] = []
    trades = []
    for row in eligible:
        day = int(utc(row["opened_at"]).timestamp()) // 86400
        if day not in days:
            raise ValueError("Bootstrap entry outside declared scope")
        observation = (1, row["net_r"], max(row["net_pnl"], 0), -min(row["net_pnl"], 0))
        trades.append(observation)
        days[day].append(observation)
    calendar = [tuple(sum(item[index] for item in block) for index in range(4))
                for block in days.values()]
    result = {}
    for method, blocks in (("TRADE", trades), ("DAY", calendar)):
        active = sum(block[0] > 0 for block in blocks)
        means, factors = [], []
        empty, undefined_pf = 0, 0
        enough = active >= (2 if method == "DAY" else 1)
        if enough:
            rng = random.Random(settings["seed"])
            for _ in range(samples):
                totals = [0.0] * 4
                weights = Counter(rng.randrange(len(blocks)) for _ in blocks)
                for index, weight in weights.items():
                    block = blocks[index]
                    if not block[0]:
                        continue
                    for field in range(4):
                        totals[field] += block[field] * weight
                if not totals[0]:
                    empty += 1
                    continue
                means.append(totals[1] / totals[0])
                if totals[3] > 0:
                    factors.append(totals[2] / totals[3])
                elif totals[2] > 0:
                    factors.append(math.inf)
                else:
                    undefined_pf += 1
        result[method] = {
            "status": "OK" if enough else "INSUFFICIENT_COMPLETE_TRADES_OR_DAY_CLUSTERS",
            "samples": samples, "seed": settings["seed"], "active_clusters": active,
            "calendar_or_trade_clusters": len(blocks), "valid_r_resamples": len(means),
            "valid_pf_resamples": len(factors), "empty_resamples": empty,
            "undefined_pf_resamples": undefined_pf,
            "nominal_95": {
                "expectancy_r": [percentile(means, alpha / 2), percentile(means, 1 - alpha / 2)] if means else None,
                "profit_factor": pf_interval(factors, alpha),
            },
            "bonferroni_family_95": {
                "expectancy_r": [percentile(means, alpha / (2 * family)), percentile(means, 1 - alpha / (2 * family))] if means else None,
                "profit_factor": pf_interval(factors, alpha / family),
            },
            "family_size": family, "mean_unit": "NET_R", "pf_unit": "USDT",
            "sampling": "CANDIDATE_COMPLETE_TRADES_OR_ALL_SCOPE_UTC_ENTRY_DAYS_WITH_EMPTY_DAYS",
        }
    return result
