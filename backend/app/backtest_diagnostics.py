"""Opt-in offline diagnosis; no entry, exit or LIVE source changes."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator

from . import execution_core as core
from .backtest_baseline import Config, bootstrap, metrics, percentile, run
from .backtest_data import Dataset

CAPS = (350, 700, 1050, None)
EXIT_GROUPS = ("STOP_BEFORE_TP1", "TP1_THEN_STOP", "TP3", "OTHER_OR_UNCLOSED")


@contextmanager
def diagnostic_exposure_cap(cap: int | None) -> Generator[None, None, None]:
    """Standalone, serial offline replays only; restore even on failure."""
    if cap not in CAPS:
        raise ValueError("Diagnostic exposure cap must be 350/700/1050/unlimited")
    original = core.sanitize_execution_policy

    def adapter(payload: Any, *, preserve_empty_allowed_symbols: bool = False) -> dict[str, Any]:
        settings = original(payload, preserve_empty_allowed_symbols=preserve_empty_allowed_symbols)
        settings["max_total_exposure_usdt"] = float(cap) if cap is not None else math.inf
        return settings

    core.sanitize_execution_policy = adapter
    try:
        yield
    finally:
        core.sanitize_execution_policy = original


def utc(raw: str) -> datetime:
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        raise ValueError("Diagnostic trade timestamps must have a timezone")
    return value.astimezone(timezone.utc)


def distribution(values: list[float | None]) -> dict[str, Any]:
    known = [value for value in values if value is not None]
    if any(not math.isfinite(value) for value in known):
        raise ValueError("Non-finite excursion evidence")
    return {"count": len(known), "missing_count": len(values) - len(known),
            "p25": percentile(known, 0.25) if known else None,
            "median": percentile(known, 0.5) if known else None,
            "p75": percentile(known, 0.75) if known else None}


def excursions(row: dict, data: Dataset) -> dict[str, Any]:
    empty = {"mae_r": None, "mfe_r": None, "mae_r_upper": None, "mfe_r_upper": None,
             "pre_stop_mfe_r": None, "pre_stop_mfe_r_upper": None}
    if row["status"] != "CLOSED" or row["closed_at"] is None:
        return {**empty, "excursion_status": "UNCLOSED_OR_UNVERIFIED"}
    risk = row["initial_risk_usdt"]
    if risk is None or risk <= 0:
        return {**empty, "excursion_status": "INITIAL_RISK_MISSING"}
    entry, quantity = row["actual_fill_price"], row["quantity"]
    if not all(math.isfinite(value) for value in (risk, entry, quantity)) or quantity <= 0:
        raise ValueError("Invalid excursion entry/quantity/risk")
    if row["direction"] not in {"LONG", "SHORT"}:
        raise ValueError("Unknown diagnostic direction")
    sign = 1 if row["direction"] == "LONG" else -1
    opened, closed = int(utc(row["opened_at"]).timestamp()), int(utc(row["closed_at"]).timestamp())
    if opened % 900 or closed < opened or closed % 900 not in (0, 899):
        raise ValueError("Excursions require native 15m replay exit timestamps")
    final = closed // 900 * 900
    reason = row["exits"][-1]["reason"] if row["exits"] else None
    if reason not in {"STOP", "TP3"}:
        return {**empty, "excursion_status": "UNKNOWN_TERMINAL_EXIT"}
    scale = quantity / risk
    mae = mfe = 0.0
    mae_upper = mfe_upper = 0.0
    for at in range(opened, final + 1, 900):
        mark = data.marks[row["symbol"]].at(at)
        if mark is None:
            return {**empty, "excursion_status": "MARK_DATA_MISSING"}
        opening = sign * (mark["open"] - entry) * scale
        mae, mfe = max(mae, -opening), max(mfe, opening)
        mae_upper, mfe_upper = max(mae_upper, -opening), max(mfe_upper, opening)
        if at == final and closed % 900 == 0:
            break
        adverse = sign * ((mark["low"] if sign == 1 else mark["high"]) - entry) * scale
        favorable = sign * ((mark["high"] if sign == 1 else mark["low"]) - entry) * scale
        if at != final:
            mae, mfe = max(mae, -adverse), max(mfe, favorable)
            mae_upper, mfe_upper = mae, mfe
        elif reason == "STOP":
            # STOP_FIRST cannot establish that the exit bar's high preceded its Stop.
            mae = max(mae, -sign * (row["stop"] - entry) * scale)
            mae_upper = mae
            mfe_upper = max(mfe_upper, favorable)
        else:
            mfe = max(mfe, sign * (row["tp3"] - entry) * scale)
            mfe_upper = mfe
            mae_upper = max(mae_upper, -adverse)
    return {"mae_r": mae, "mfe_r": mfe, "mae_r_upper": mae_upper, "mfe_r_upper": mfe_upper,
            "pre_stop_mfe_r": mfe if reason == "STOP" else None,
            "pre_stop_mfe_r_upper": mfe_upper if reason == "STOP" else None,
            "excursion_status": "MARK_OHLC_EXIT_BAR_BOUNDS"}


def exit_group(row: dict) -> str:
    if row["status"] != "CLOSED" or not row["exits"]:
        return "OTHER_OR_UNCLOSED"
    reasons = [event["reason"] for event in row["exits"]]
    if reasons[-1] == "TP3":
        return "TP3"
    if reasons[-1] == "STOP":
        return "TP1_THEN_STOP" if "TP1" in reasons else "STOP_BEFORE_TP1"
    return "OTHER_OR_UNCLOSED"


def exit_distribution(rows: list[dict]) -> dict[str, Any]:
    def summarize(selected: list[dict]) -> dict:
        known = [row["net_r"] for row in selected if row["net_r"] is not None]
        return {"trade_count": len(selected), "r_eligible_count": len(known),
                "mean_net_r": sum(known) / len(known) if known else None,
                **{field: distribution([row[field] for row in selected]) for field in (
                    "mae_r", "mfe_r", "mae_r_upper", "mfe_r_upper",
                    "pre_stop_mfe_r", "pre_stop_mfe_r_upper")}}
    return {"all": summarize(rows),
            "groups": {group: summarize([row for row in rows if exit_group(row) == group])
                       for group in EXIT_GROUPS}}


def cluster_key(value: datetime, unit: str) -> str:
    day = value.date()
    if unit == "WEEK":
        day -= timedelta(days=day.weekday())
    return day.isoformat()


def clustered_bootstrap(rows: list[dict], config: Config, unit: str) -> dict[str, Any]:
    if unit not in {"DAY", "WEEK"}:
        raise ValueError("Bootstrap unit must be DAY or WEEK")
    first = datetime.fromtimestamp(config.start, timezone.utc).date()
    last = datetime.fromtimestamp(config.end - 1, timezone.utc).date()
    keys = []
    day = first
    while day <= last:
        key = cluster_key(datetime.combine(day, datetime.min.time(), timezone.utc), unit)
        if not keys or keys[-1] != key:
            keys.append(key)
        day += timedelta(days=1)
    clusters = {key: [0, 0.0, 0.0, 0.0] for key in keys}
    valid = [row for row in rows if row["status"] == "CLOSED"
             and row["net_r"] is not None and row["net_pnl"] is not None]
    for row in valid:
        opened = utc(row["opened_at"])
        if not config.start <= opened.timestamp() < config.end:
            raise ValueError("Trade entry lies outside bootstrap period")
        if not all(math.isfinite(row[field]) for field in ("net_r", "net_pnl")):
            raise ValueError("Non-finite bootstrap evidence")
        group = clusters[cluster_key(opened, unit)]
        group[0] += 1
        group[1] += row["net_r"]
        group[2] += max(0, row["net_pnl"])
        group[3] -= min(0, row["net_pnl"])
    nonempty = sum(group[0] > 0 for group in clusters.values())
    result: dict[str, Any] = {
        "method": f"CALENDAR_{unit}_CLUSTER", "cluster_basis": "UTC_ENTRY_TIME",
        "seed": config.seed, "samples": config.bootstrap_samples, "valid_trade_count": len(valid),
        "cluster_count": len(clusters), "nonempty_cluster_count": nonempty,
        "empty_calendar_cluster_count": len(clusters) - nonempty,
        "expectancy_r_95": None, "profit_factor_95": None,
        "pf_lower_unbounded": False, "pf_upper_unbounded": False,
        "empty_resamples": 0, "no_loss_resamples": 0, "zero_pnl_resamples": 0,
    }
    if nonempty < 2:
        return {**result, "status": "INSUFFICIENT_NONEMPTY_CLUSTERS"}
    rng = random.Random(config.seed)
    groups = list(clusters.values())
    expectancies, factors = [], []
    for _ in range(config.bootstrap_samples):
        count = total_r = wins = losses = 0.0
        for _ in groups:
            group = groups[rng.randrange(len(groups))]
            count += group[0]
            total_r += group[1]
            wins += group[2]
            losses += group[3]
        if not count:
            result["empty_resamples"] += 1
            continue
        expectancies.append(total_r / count)
        if losses:
            factors.append(wins / losses)
        elif wins:
            factors.append(math.inf)
            result["no_loss_resamples"] += 1
        else:
            result["zero_pnl_resamples"] += 1
    if expectancies:
        result["expectancy_r_95"] = [percentile(expectancies, 0.025), percentile(expectancies, 0.975)]
    if factors:
        factors.sort()
        low = factors[int((len(factors) - 1) * 0.025)]
        high = factors[math.ceil((len(factors) - 1) * 0.975)]
        result.update(profit_factor_95=[low if math.isfinite(low) else None,
                                       high if math.isfinite(high) else None],
                      pf_lower_unbounded=not math.isfinite(low), pf_upper_unbounded=not math.isfinite(high))
    result["status"] = "CONDITIONAL_NONEMPTY_RESAMPLES" if result["empty_resamples"] else "OK"
    return result


def bootstrap_comparison(rows: list[dict], config: Config) -> dict[str, Any]:
    return {"TRADE": bootstrap(rows, config.bootstrap_samples, config.seed),
            **{unit: clustered_bootstrap(rows, config, unit) for unit in ("DAY", "WEEK")}}


def monthly_distribution(rows: list[dict], config: Config) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["status"] == "CLOSED" and row["closed_at"] is not None:
            at = utc(row["closed_at"])
            if not config.start <= at.timestamp() < config.end:
                raise ValueError("Trade close lies outside diagnostic period")
            grouped[at.strftime("%Y-%m")].append(row)
    first = datetime.fromtimestamp(config.start, timezone.utc).replace(day=1, hour=0, minute=0, second=0)
    output = []
    while first.timestamp() < config.end:
        month = first.strftime("%Y-%m")
        for direction in ("ALL", "LONG", "SHORT"):
            selected = [row for row in grouped[month] if direction == "ALL" or row["direction"] == direction]
            output.append({"month": month, "direction": direction, "basis": "UTC_CLOSED_AT",
                           **metrics(selected, config.initial_equity)})
        first = first.replace(year=first.year + 1, month=1) if first.month == 12 else first.replace(month=first.month + 1)
    return output


def trade_set_changes(rows: list[dict], reference: list[dict], config: Config) -> dict[str, Any]:
    current = {row["signal_id"]: row for row in rows}
    previous = {row["signal_id"]: row for row in reference}
    if len(current) != len(rows) or len(previous) != len(reference):
        raise ValueError("Duplicate diagnostic signal IDs")
    added = [row for row in rows if row["signal_id"] not in previous]
    removed = [row for row in reference if row["signal_id"] not in current]
    common = current.keys() & previous.keys()
    changed = sum(any(current[key][field] != previous[key][field] for field in (
        "quantity", "actual_fill_price", "closed_at", "net_pnl", "net_r")) for key in common)
    return {"added": metrics(added, config.initial_equity), "removed": metrics(removed, config.initial_equity),
            "common_trade_count": len(common), "changed_common_trade_count": changed,
            "added_signal_ids": [row["signal_id"] for row in added],
            "removed_signal_ids": [row["signal_id"] for row in removed]}


def replay_cap(data: Dataset, config: Config, cap: int | None) -> dict[str, Any]:
    with diagnostic_exposure_cap(cap):
        result = run(data, config)
    result["policy"] = {**result["policy"], "max_total_exposure_usdt": cap}
    result["diagnostic_exposure_cap_usdt"] = cap
    result["exposure_cap_label"] = str(cap) if cap is not None else "UNLIMITED_OFFLINE_ONLY"
    result["bootstrap_comparison"] = bootstrap_comparison(result["trades"], config)
    return result
