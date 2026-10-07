"""Locked TRAIN/VALIDATION counts, never profitability or TEST evaluation.

Only load_measurement_dataset reads candles. The original facade methods still
make entry/gate/exit/funding decisions; this separate orchestration mirrors its
replay order without constructing its profitability summary/bootstrap. The only
extra admission restriction is a full native W at initial warmup.

First-cross counts cover every symbol at every decision time, including occupied
or excluded symbols, independent of ranking/admission. They count a passed pure
first_cross quality check, not just READY; quality failures are counted separately.
Months are UTC decision-availability months, not the preceding bar's open month.
Admission attempts are top-three post-Stop-prefilter candidates, not raw crosses.
All rejection counters have their own denominators and must not be added together.

Caches and price-bearing facade telemetry are discarded each bar. Native monetary
state remains in memory only for unchanged mandatory gates and verified-N checks.
No position carries between phases or is forced closed at a phase end.

CLI requires the hash-locked Stage 6 metadata snapshot, no other price source.
The source manifest is read only as opaque bytes for authorized hash comparison.
Native stdout/stderr/logging are muted because rejection details can contain
prices. Failures surface as safe codes/classes without exception messages.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import logging
import math
import os
import re
import socket
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO
from unittest.mock import patch

from build_measurement_view import load_measurement_dataset, sha256_file

if TYPE_CHECKING:
    from app.backtest_baseline import Config
    from app.backtest_data import Dataset

BACKEND = Path(__file__).resolve().parent
DATA_HOME = Path.home() / "kaistrade-data"
VIEW = DATA_HOME / "measurement-view-2020-10_2022-08"
OUTPUT = DATA_HOME / "measurement-results"
SOURCE_MANIFEST = DATA_HOME / "independent-block-2020-10_2023-05" / "manifest.json"
STEP, WINDOW, HORIZON = 900, 259, 72
SCHEMA = "donchian-counts-only-v1"
FORBIDDEN = ("net_r", "pnl", "profit", "win_rate", "winrate", "equity", "price", "return", "gross")


class MeasurementError(ValueError):
    """Safe fixed code, never raw native exception text."""


@dataclass(frozen=True)
class Phase:
    name: str
    start: int
    end: int

    @property
    def days(self) -> int:
        if self.end <= self.start or (self.end - self.start) % 86400:
            raise MeasurementError("PHASE_NOT_CALENDAR_DAYS")
        return (self.end - self.start) // 86400


def epoch(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp())


PHASES = (
    Phase("TRAIN", epoch("2020-10-01"), epoch("2022-01-01")),
    Phase("VALIDATION", epoch("2022-01-01"), epoch("2022-09-01")),
)


def stamp(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def month(at: int) -> str:
    return stamp(at)[:7]


def digest_object(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def locked_plan() -> dict[str, Any]:
    extension = json.loads((BACKEND / "studies" / "stage6b-plan.json").read_bytes())
    base = json.loads((BACKEND / "studies" / "stage6-plan.json").read_bytes())
    if digest_object(base) != extension["base_plan_sha256"]:
        raise MeasurementError("INHERITED_PLAN_CHANGED")
    if (
        base["primary"] != {"slippage_bps": 3, "spread_bps": 2, "intrabar": "STOP_FIRST"}
        or base["initial_equity"] != 1000 or base["max_total_exposure_usdt"] != 350
        or base["fee_bps_per_side"] != 5 or base["indicator_window"] != WINDOW
        or len(base["symbols"]) != 8 or len(set(base["symbols"])) != 8
    ):
        raise MeasurementError("PRIMARY_CONFIG_CHANGED")
    return base


def primary_config(phase: Phase, plan: dict[str, Any]) -> Config:
    from app.backtest_baseline import Config

    return Config(
        phase.start, phase.end, initial_equity=plan["initial_equity"],
        slippage_bps=plan["primary"]["slippage_bps"], spread_bps=plan["primary"]["spread_bps"],
        intrabar=plan["primary"]["intrabar"], conditional_current_metadata=True,
        policy={"allowed_symbols": list(plan["symbols"]),
                "max_total_exposure_usdt": plan["max_total_exposure_usdt"]},
    )


def safe_reason(reason: str) -> str:
    alias = "accounting_verified" if reason == "pnl_verified" else reason
    if not re.fullmatch(r"[A-Za-z0-9_]+", alias) or any(word in alias.lower() for word in FORBIDDEN):
        raise MeasurementError("UNSAFE_REASON")
    return alias


def reason_counts(counts: Counter | dict[str, int]) -> dict[str, int]:
    return dict(sorted((safe_reason(key), value) for key, value in counts.items()))


def duration_distribution(hours: list[float]) -> dict[str, float | int | None]:
    if any(not math.isfinite(value) or value < 0 for value in hours):
        raise MeasurementError("INVALID_DURATION")
    ordered = sorted(hours)

    def percentile(fraction: float) -> float | None:
        if not ordered:
            return None
        offset = (len(ordered) - 1) * fraction
        low, high = math.floor(offset), math.ceil(offset)
        return ordered[low] + (ordered[high] - ordered[low]) * (offset - low)

    return {
        "count": len(hours), "median_hours": percentile(0.5),
        "p90_hours": percentile(0.9), "p99_hours": percentile(0.99),
        "maximum_hours": max(hours) if hours else None,
        "over_72_hours": sum(value > HORIZON for value in hours),
    }


def prequalification(reports: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rates = {
        phase: Fraction(reports[phase]["counts"]["fully_verified_completed"], reports[phase]["calendar_days"])
        for phase in ("TRAIN", "VALIDATION")
    }
    conservative = min(rates.values())
    estimate = conservative * 273 * Fraction(4, 5)
    count = estimate.numerator // estimate.denominator
    return {
        "verified_per_calendar_day": {phase: float(rate) for phase, rate in rates.items()},
        "lower_rate": float(conservative), "test_calendar_days": 273,
        "discount": 0.8, "n_lower": count, "threshold": 60,
        "decision": "TEST A\u00c7ILMAZ" if count < 60 else "\u00f6n yeterlilik ge\u00e7ti (garanti de\u011fil)",
    }


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    validate_report(report)
    return {
        "status": "completed",
        "phases": {
            name: {
                "counts": phase["counts"], "completed_duration": phase["completed_duration"],
                "daily_limit_days": phase["daily_limit_days"]["count"],
                "first_cross_by_direction": {
                    direction: sum(cell[direction] for months in phase["first_cross"].values()
                                   for cell in months.values())
                    for direction in ("LONG", "SHORT")
                },
                "first_rejections_top10": phase["first_rejections"][:10],
                "gap_blackout_by_kind": phase["gap_blackout_by_kind"],
            } for name, phase in report["phases"].items()
        },
        "test_prequalification": report["test_prequalification"],
        "elapsed_seconds": report["elapsed_seconds"],
    }


def validate_loaded_scope(data: Dataset, symbols: list[str]) -> None:
    if set(data.frames) != set(symbols) or data.report["missing_archives"]:
        raise MeasurementError("INCOMPLETE_EIGHT_SYMBOL_VIEW")
    for symbol in symbols:
        if set(data.frames[symbol]) != {"15m", "1h", "4h"} or symbol not in data.marks:
            raise MeasurementError("MISSING_STREAM")
        for series in (*data.frames[symbol].values(), data.marks[symbol]):
            if not series.times or series.times[0] < PHASES[0].start or series.times[-1] >= PHASES[-1].end:
                raise MeasurementError("CANDLE_OUTSIDE_MEASUREMENT_SCOPE")
        if any(not PHASES[0].start <= event["time"] < PHASES[-1].end for event in data.funding.get(symbol, [])):
            raise MeasurementError("FUNDING_OUTSIDE_MEASUREMENT_SCOPE")


def validate_report(report: dict[str, Any]) -> None:
    """Positive output schema, not merely removing known monetary field names."""
    expected = {"schema", "provenance", "phases", "test_prequalification", "elapsed_seconds"}
    if set(report) != expected or report["schema"] != SCHEMA or set(report["phases"]) != {"TRAIN", "VALIDATION"}:
        raise MeasurementError("REPORT_SCHEMA")
    provenance_keys = {
        "strategy_version", "channel_period", "atr_multiplier", "window_bars", "blackout_hours",
        "metadata_kind", "metadata_sha256", "view_manifest_sha256", "source_manifest_sha256",
        "stage6b_plan_sha256", "stage6_plan_sha256",
    }
    if set(report["provenance"]) != provenance_keys:
        raise MeasurementError("PROVENANCE_SCHEMA")
    qualification_keys = {
        "verified_per_calendar_day", "lower_rate", "test_calendar_days", "discount",
        "n_lower", "threshold", "decision",
    }
    if (
        set(report["test_prequalification"]) != qualification_keys
        or set(report["test_prequalification"]["verified_per_calendar_day"]) != {"TRAIN", "VALIDATION"}
        or report["test_prequalification"] != prequalification(report["phases"])
    ):
        raise MeasurementError("QUALIFICATION_SCHEMA")
    phase_keys = {
        "period", "calendar_days", "state_at_start", "first_cross", "signal_quality_rejections",
        "all_rejections", "first_rejections", "gate_rejections", "gate_evaluations",
        "gap_blackout_by_kind", "gap_blackout_by_kind_symbol_month", "counts",
        "daily_limit_days", "completed_duration", "by_symbol", "unknown_trades",
        "funding_gap_trades", "funding_incomplete_trades", "gap_exposed_trades",
        "h_insufficient_cases", "config_sha256", "policy_sha256", "elapsed_seconds",
    }
    count_keys = {
        "attempted_candidates", "ranked_signal_candidates", "rank_limit_discarded", "accepted_entries",
        "completed", "fully_verified_completed", "open_at_end", "unknown_data_gap",
        "funding_incomplete", "funding_gap_affected", "gap_exposed", "h_insufficient",
    }
    trade_keys = {"symbol", "opened_at", "end_at", "status"}

    def nonnegative_count(value: Any) -> bool:
        return type(value) is int and value >= 0

    def counter(value: dict[str, int]) -> bool:
        return all(safe_reason(key) == key and nonnegative_count(count) for key, count in value.items())

    for phase in report["phases"].values():
        if set(phase) != phase_keys or set(phase["counts"]) != count_keys:
            raise MeasurementError("PHASE_SCHEMA")
        if (
            set(phase["period"]) != {"start_inclusive", "end_exclusive"}
            or set(phase["state_at_start"]) != {"positions", "events", "entries"}
            or set(phase["daily_limit_days"]) != {"count", "utc_days"}
            or any(set(row) != {"reason", "count"} for row in phase["first_rejections"])
            or set(phase["gap_blackout_by_kind"]) != {"contract", "mark", "funding"}
        ):
            raise MeasurementError("NESTED_SCHEMA")
        if (
            not all(nonnegative_count(value) for value in phase["counts"].values())
            or phase["state_at_start"] != {"positions": 0, "events": 0, "entries": 0}
            or type(phase["calendar_days"]) is not int or phase["calendar_days"] <= 0
            or not all(counter(phase[key]) for key in (
                "all_rejections", "signal_quality_rejections", "gate_rejections", "gate_evaluations",
            ))
            or any(safe_reason(row["reason"]) != row["reason"] or not nonnegative_count(row["count"])
                   for row in phase["first_rejections"])
        ):
            raise MeasurementError("COUNTER_SCHEMA")
        for cells in phase["gap_blackout_by_kind_symbol_month"].values():
            if any(set(value) != {"contract", "mark", "funding"} for value in cells.values()):
                raise MeasurementError("BLACKOUT_SCHEMA")
        for key in ("unknown_trades", "funding_gap_trades", "funding_incomplete_trades", "gap_exposed_trades"):
            if any(set(row) != trade_keys for row in phase[key]):
                raise MeasurementError("TRADE_SCHEMA")
            for row in phase[key]:
                if (
                    not isinstance(row["symbol"], str) or not re.fullmatch(r"[A-Z0-9]+USDT", row["symbol"])
                    or row["status"] not in ("CLOSED", "OPEN_AT_END", "UNKNOWN_DATA_GAP")
                    or any(not isinstance(row[key], str) for key in ("opened_at", "end_at"))
                ):
                    raise MeasurementError("TRADE_VALUE_SCHEMA")
                if epoch(row["end_at"]) < epoch(row["opened_at"]):
                    raise MeasurementError("TRADE_TIME_SCHEMA")
        if any(set(row) != trade_keys | {"kind", "gap_start", "reason"} for row in phase["h_insufficient_cases"]):
            raise MeasurementError("EXPOSURE_SCHEMA")
        if set(phase["completed_duration"]) != {
            "count", "median_hours", "p90_hours", "p99_hours", "maximum_hours", "over_72_hours",
        }:
            raise MeasurementError("DURATION_SCHEMA")
        for cell in phase["first_cross"].values():
            if any(set(value) != {"LONG", "SHORT"} or not all(nonnegative_count(count) for count in value.values())
                   for value in cell.values()):
                raise MeasurementError("SIGNAL_SCHEMA")
        for value in phase["by_symbol"].values():
            if set(value) != {"accepted_entries", "completed", "fully_verified_completed", "open_at_end", "unknown_data_gap"}:
                raise MeasurementError("SYMBOL_SCHEMA")
            if not all(nonnegative_count(count) for count in value.values()):
                raise MeasurementError("SYMBOL_VALUE_SCHEMA")
        total = phase["counts"]
        if (
            total["completed"] + total["open_at_end"] + total["unknown_data_gap"] != total["accepted_entries"]
            or total["fully_verified_completed"] > total["completed"]
            or total["accepted_entries"] > total["attempted_candidates"]
            or total["attempted_candidates"] + total["rank_limit_discarded"] != total["ranked_signal_candidates"]
            or phase["completed_duration"]["count"] != total["completed"]
            or len(phase["unknown_trades"]) != total["unknown_data_gap"]
            or len(phase["funding_gap_trades"]) != total["funding_gap_affected"]
            or len(phase["funding_incomplete_trades"]) != total["funding_incomplete"]
            or len(phase["gap_exposed_trades"]) != total["gap_exposed"]
            or phase["daily_limit_days"]["count"] != len(set(phase["daily_limit_days"]["utc_days"]))
        ):
            raise MeasurementError("COUNT_RECONCILIATION")
        for key in ("accepted_entries", "completed", "fully_verified_completed", "open_at_end", "unknown_data_gap"):
            if sum(value[key] for value in phase["by_symbol"].values()) != total[key]:
                raise MeasurementError("SYMBOL_RECONCILIATION")

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str) or key.lower() == "pf" or any(word in key.lower() for word in FORBIDDEN):
                    raise MeasurementError("UNSAFE_OUTPUT_KEY")
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, str):
            if re.search(r"\bpf\b", value.lower()) or any(word in value.lower() for word in FORBIDDEN):
                raise MeasurementError("UNSAFE_OUTPUT_TEXT")
        elif value is not None and (type(value) not in (int, float, bool) or not math.isfinite(value)):
            raise MeasurementError("UNSAFE_OUTPUT_VALUE")

    walk(report)
    json.dumps(report, allow_nan=False)


def make_engine(data: Dataset, config: Config, progress: Callable[[dict[str, Any]], None] | None = None):
    from app import execution_core as core
    from app import v25_execution as live
    from app.strategies.donchian_params import DonchianParamsV2
    from app.strategies.offline_facade import DonchianOfflineEngine

    class CountsEngine(DonchianOfflineEngine):
        def __init__(self):
            super().__init__(data, config, DonchianParamsV2())
            if (
                self.data_window.closed_bars != WINDOW or self.gap_policy["parameters"]["blackout_hours"] != HORIZON
                or self.state.policy["daily_trade_limit"] != 3 or self.state.policy["fee_bps_per_side"] != 5
            ):
                raise MeasurementError("NATIVE_POLICY_CHANGED")
            self.crosses: dict[str, dict[str, dict[str, int]]] = {symbol: {} for symbol in data.frames}
            self.quality: Counter = Counter()
            self.blackout_kinds: Counter = Counter()
            self.blackout_cells: dict[str, dict[str, Counter]] = {symbol: {} for symbol in data.frames}
            self.limit_days: set[str] = set()
            self.at = config.start
            self.attempts = self.ranked_candidates = self.rank_discarded = 0
            self.start_counts = {"positions": len(self.state.positions), "events": len(self.state.events), "entries": len(self.state.trades)}

        def reject(self, failures):
            if "daily_trades" in failures:
                self.limit_days.add(stamp(self.at)[:10])
            super().reject(failures)

        def _entry_data_exclusion(self, symbol, at, phase):
            if super()._entry_data_exclusion(symbol, at, phase):
                return True
            if len(self.state.data.frames[symbol]["15m"].closed(at, limit=WINDOW)) < WINDOW:
                self.reject(["warmup_window"])
                return True
            return False

        async def replay_counts(self, phase: Phase) -> dict[str, Any]:
            started = time.monotonic()
            state = self.state
            symbols = sorted(set(state.policy["allowed_symbols"]) & set(state.data.frames))
            last_month = month(phase.start)
            for at in range(config.start, config.end, STEP):
                self.at = at
                for symbol in symbols:
                    signal = self.canonical(symbol, at).signal
                    if any(check.key == "first_cross" and check.passed is True for check in signal.quality_checks):
                        if signal.direction not in ("LONG", "SHORT"):
                            raise MeasurementError("FIRST_CROSS_DIRECTION")
                        cell = self.crosses[symbol].setdefault(month(at), {"LONG": 0, "SHORT": 0})
                        cell[signal.direction] += 1
                    if not signal.strategy_eligible:
                        self.quality[safe_reason(signal.reason)] += 1
                for position in list(state.positions.values()):
                    state.advance(position, at, opening_only=True)
                entry_symbols = [
                    symbol for symbol in symbols if symbol not in state.positions
                    and not self._entry_data_exclusion(symbol, at, "before_market_ranking")
                ]
                tickers = [
                    ticker for symbol in entry_symbols
                    if (ticker := state.data.frames[symbol]["15m"].ticker(at, symbol)) is not None
                ]
                ranked = live.rank_market_tickers(
                    state.data.metadata["exchange_info"], tickers,
                    excluded_symbols=set(state.positions), allowed_symbols=set(symbols),
                )
                signals = []
                for candidate in ranked:
                    symbol = candidate["symbol"]
                    if not state.data.frames[symbol]["15m"].history_complete(at):
                        self.reject(["data_gap_history"])
                        continue
                    signal = self.canonical(symbol, at).signal
                    state.decisions_evaluated += 1
                    if not signal.strategy_eligible:
                        self.reject([signal.reason])
                        continue
                    if signal.entry is None or signal.stop is None:
                        raise MeasurementError("ELIGIBLE_LEVEL_INVARIANT")
                    distance = abs(signal.entry - signal.stop) / signal.entry * 100
                    if distance > core.dynamic_stop_distance_pct(signal.entry, None, state.policy):
                        self.reject(["stop_distance"])
                        continue
                    signals.append(candidate)
                signals.sort(key=lambda item: item["opportunity_score"], reverse=True)
                self.ranked_candidates += len(signals)
                self.rank_discarded += max(0, len(signals) - 3)
                for candidate in signals[:3]:
                    self.attempts += 1
                    await self.enter(candidate["symbol"], at)
                for position in list(state.positions.values()):
                    state.advance(position, at, opening_only=False)
                for trace in self.gap_blackouts:
                    kinds = {gap["stream"] for gap in trace["gaps"]}
                    self.blackout_kinds.update(kinds)
                    self.blackout_cells[trace["symbol"]].setdefault(month(at), Counter()).update(kinds)
                self._decisions.clear()
                self.decisions.clear()
                self.admissions.clear()
                self.gap_blackouts.clear()
                self.data_quality_rejections.clear()
                if progress is not None and month(at + STEP) != last_month:
                    progress({"phase": phase.name, "month_completed": last_month,
                              "bars_processed": (at + STEP - phase.start) // STEP,
                              "accepted_entries": len(state.trades)})
                    last_month = month(at + STEP)
            for position in state.positions.values():
                position.status = "OPEN_AT_END"
                position.funding_known = position.funding_known and state.data.funding_complete(
                    position.symbol, position.opened_at, config.end - 1,
                )
            rows = [position.row() for position in state.trades]
            native_counts = self._measurement_counts(rows)
            unknown = self._unknown_data_gap_report()
            funding = self._funding_data_gap_report(rows, unknown)
            unknown_by_id = {row["signal_id"]: row for row in unknown["trades"]}
            funding_ids = {row["signal_id"] for row in funding["trades"]}
            lists: dict[str, list[dict[str, Any]]] = {
                "unknown_trades": [], "funding_gap_trades": [], "funding_incomplete_trades": [],
                "gap_exposed_trades": [], "h_insufficient_cases": [],
            }
            by_symbol = {
                symbol: {"accepted_entries": 0, "completed": 0, "fully_verified_completed": 0,
                         "open_at_end": 0, "unknown_data_gap": 0}
                for symbol in symbols
            }
            hours, exposed_ids, insufficient_ids = [], set(), set()
            for position, row in zip(state.trades, rows, strict=True):
                end = position.closed_at if position.status == "CLOSED" else (
                    epoch(unknown_by_id[position.signal_identifier]["detected_at"])
                    if position.status == "UNKNOWN_DATA_GAP" else config.end - 1
                )
                if end is None:
                    raise MeasurementError("TRADE_END_MISSING")
                record = {"symbol": position.symbol, "opened_at": stamp(position.opened_at),
                          "end_at": stamp(end), "status": position.status}
                cell = by_symbol[position.symbol]
                cell["accepted_entries"] += 1
                cell["completed"] += int(position.status == "CLOSED")
                cell["fully_verified_completed"] += self._measurement_counts([row])["fully_verified_completed"]
                cell["open_at_end"] += int(position.status == "OPEN_AT_END")
                cell["unknown_data_gap"] += int(position.status == "UNKNOWN_DATA_GAP")
                if position.status == "CLOSED":
                    hours.append((end - position.opened_at) / 3600)
                if position.status == "UNKNOWN_DATA_GAP":
                    lists["unknown_trades"].append(record)
                if position.signal_identifier in funding_ids:
                    lists["funding_gap_trades"].append(record)
                if row["funding_usdt"] is None:
                    lists["funding_incomplete_trades"].append(record)
                overlaps = [
                    gap for gap in self._gaps[position.symbol] if gap.end_exclusive > position.opened_at
                    and (gap.start < end if gap.stream == "funding" else gap.start <= end)
                ]
                if overlaps:
                    exposed_ids.add(position.signal_identifier)
                    lists["gap_exposed_trades"].append(record)
                for gap in overlaps:
                    if gap.start - HORIZON * 3600 <= position.opened_at < gap.start:
                        raise MeasurementError("ENTRY_INSIDE_BLACKOUT")
                    reason = (
                        "holding_exceeded_pre_gap_horizon" if position.opened_at < gap.start
                        else "entry_at_or_after_gap_start"
                    )
                    insufficient_ids.add(position.signal_identifier)
                    lists["h_insufficient_cases"].append({
                        **record, "kind": gap.stream, "gap_start": stamp(gap.start), "reason": reason,
                    })
            first = reason_counts(state.first_rejections)
            all_months = sorted({month(at) for at in range(config.start, config.end, 86400)})
            crosses = {
                symbol: {key: self.crosses[symbol].get(key, {"LONG": 0, "SHORT": 0}) for key in all_months}
                for symbol in symbols
            }
            return {
                "period": {"start_inclusive": stamp(config.start), "end_exclusive": stamp(config.end)},
                "calendar_days": phase.days, "state_at_start": self.start_counts,
                "first_cross": crosses, "signal_quality_rejections": reason_counts(self.quality),
                "all_rejections": reason_counts(state.rejections),
                "first_rejections": [{"reason": key, "count": value}
                                     for key, value in sorted(first.items(), key=lambda pair: (-pair[1], pair[0]))],
                "gate_rejections": reason_counts(state.gate_counts),
                "gate_evaluations": reason_counts(state.gate_evaluations),
                "gap_blackout_by_kind": {kind: self.blackout_kinds[kind] for kind in ("contract", "mark", "funding")},
                "gap_blackout_by_kind_symbol_month": {
                    symbol: {key: {kind: self.blackout_cells[symbol].get(key, Counter())[kind]
                                   for kind in ("contract", "mark", "funding")} for key in all_months}
                    for symbol in symbols
                },
                "counts": {
                    "attempted_candidates": self.attempts, "ranked_signal_candidates": self.ranked_candidates,
                    "rank_limit_discarded": self.rank_discarded, "accepted_entries": len(rows),
                    "completed": native_counts["closed"],
                    "fully_verified_completed": native_counts["fully_verified_completed"],
                    "open_at_end": native_counts["open_at_end"], "unknown_data_gap": native_counts["unknown_data_gap"],
                    "funding_incomplete": native_counts["funding_incomplete"],
                    "funding_gap_affected": funding["trade_count"], "gap_exposed": len(exposed_ids),
                    "h_insufficient": len(insufficient_ids),
                },
                "daily_limit_days": {"count": len(self.limit_days), "utc_days": sorted(self.limit_days)},
                "completed_duration": duration_distribution(hours), "by_symbol": by_symbol, **lists,
                "config_sha256": digest_object(asdict(config)), "policy_sha256": digest_object(state.policy),
                "elapsed_seconds": time.monotonic() - started,
            }

    return CountsEngine()


async def measure_phases(
    data: Dataset, plan: dict[str, Any], *, phases: tuple[Phase, ...] = PHASES,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, dict[str, Any]]:
    reports = {}
    for phase in phases:
        engine = make_engine(data, primary_config(phase, plan), progress)
        reports[phase.name] = await engine.replay_counts(phase)
    return reports


class _Discard(io.TextIOBase):
    def write(self, text: str) -> int:
        return len(text)


@contextmanager
def quiet_native() -> Iterator[None]:
    prior = logging.root.manager.disable
    logging.disable(sys.maxsize)
    try:
        with redirect_stdout(_Discard()), redirect_stderr(_Discard()):
            yield
    finally:
        logging.disable(prior)


@contextmanager
def deny_network() -> Iterator[None]:
    def denied(*_args, **_kwargs):
        raise MeasurementError("NETWORK_FORBIDDEN")

    with (
        patch.object(socket.socket, "connect", denied), patch.object(socket.socket, "connect_ex", denied),
        patch.object(socket.socket, "sendto", denied), patch.object(socket, "getaddrinfo", denied),
        patch.object(socket, "create_connection", denied),
    ):
        yield


def emit(stream: TextIO, value: dict[str, Any]) -> None:
    stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
    stream.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Locked Donchian TRAIN/VALIDATION counts only; no TEST.")
    parser.add_argument("--metadata", required=True, type=Path, help="Hash-locked Stage 6 metadata snapshot")
    args = parser.parse_args(argv)
    stream = sys.stdout
    if OUTPUT.exists():
        emit(stream, {"status": "failed", "code": "RESULTS_ALREADY_EXIST"})
        return 2
    started = time.monotonic()
    try:
        plan = locked_plan()
        metadata_hash = sha256_file(args.metadata)
        if metadata_hash != plan["input_sha256"]["metadata"]:
            raise MeasurementError("LOCKED_METADATA_MISMATCH")
        view_hash, source_hash = sha256_file(VIEW / "manifest.json"), sha256_file(SOURCE_MANIFEST)
        view_manifest = json.loads((VIEW / "manifest.json").read_bytes())
        if view_manifest["source_manifest_sha256"] != source_hash:
            raise MeasurementError("SOURCE_MANIFEST_MISMATCH")
        OUTPUT.mkdir(parents=True, exist_ok=False)
        os.environ.update({
            "DATA_DIR": str(OUTPUT / "runtime"), "PROTREBOT_DATA_DIR": str(OUTPUT / "runtime"),
            "DATABASE_URL": "", "PROTREBOT_DURABLE_AUTH_REQUIRED": "0", "ASSISTANT_LIVE_TESTS": "0",
        })
        loop = asyncio.new_event_loop()
        try:
            with quiet_native(), deny_network():
                data = load_measurement_dataset(VIEW, args.metadata, PHASES[0].start, PHASES[-1].end)
                validate_loaded_scope(data, plan["symbols"])
                phases = loop.run_until_complete(measure_phases(data, plan, progress=lambda item: emit(stream, item)))
        finally:
            loop.close()
        if (
            sha256_file(VIEW / "manifest.json") != view_hash or sha256_file(SOURCE_MANIFEST) != source_hash
            or sha256_file(args.metadata) != metadata_hash
        ):
            raise MeasurementError("INPUT_HASH_CHANGED")
        report = {
            "schema": SCHEMA,
            "provenance": {
                "strategy_version": "signal-v2", "channel_period": 20, "atr_multiplier": 1.4,
                "window_bars": WINDOW, "blackout_hours": HORIZON, "metadata_kind": "CONDITIONAL_CURRENT_SNAPSHOT",
                "metadata_sha256": metadata_hash, "view_manifest_sha256": view_hash,
                "source_manifest_sha256": source_hash,
                "stage6b_plan_sha256": sha256_file(BACKEND / "studies" / "stage6b-plan.json"),
                "stage6_plan_sha256": sha256_file(BACKEND / "studies" / "stage6-plan.json"),
            },
            "phases": phases, "test_prequalification": prequalification(phases),
            "elapsed_seconds": time.monotonic() - started,
        }
        validate_report(report)
        path = OUTPUT / "counts.json"
        with path.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
        summary = summarize(report)
        with (OUTPUT / "summary.json").open("x", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
        emit(stream, summary)
        emit(stream, {"counts_sha256": sha256_file(path),
                      "summary_sha256": sha256_file(OUTPUT / "summary.json")})
        return 0
    except MeasurementError as exc:
        emit(stream, {"status": "failed", "code": str(exc)})
        return 2
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, AssertionError, ArithmeticError) as exc:
        emit(stream, {"status": "failed", "code": "MEASUREMENT_ABORTED", "error_type": type(exc).__name__})
        return 2


if __name__ == "__main__":
    sys.excepthook = lambda kind, _value, _trace: emit(
        sys.stderr, {"status": "failed", "code": "UNEXPECTED_FAILURE", "error_type": kind.__name__},
    )
    raise SystemExit(main())
