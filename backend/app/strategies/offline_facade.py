"""Opt-in offline dispatch, never imported by LIVE/Demo or existing research.

Original uses the literal native Engine/Dataset path, including its old cache.
Donchian composes the native lifecycle, funding, sizing, spec and protections.
It has no Original analysis, confidence tie-breaker, network or persisted cache.
The native market opportunity ranking remains primary, with its stable ordering.

Data-window behavior change:
- Donchian receives only the last W closed 15m rows. W is read from the native
  Series.closed limit default, also used by Series.history_complete (259 now).
  There is no tuning argument. W below max(N + 2, ATR period + 1) is an error.
- An interior contract gap stops poisoning the signal once it leaves W; a gap
  inside W still fails. Missing current/entry data and all native gates remain
  mandatory; this does not reconcile unknown positions or funding.
- ATR uses only these supplied contiguous rows: its first period true ranges
  seed Wilder smoothing, without the preceding out-of-window close or ATR.
  Versus full history, reseeding can change ATR, Stop/TP, sizing and decisions
  in either direction even when the channel/first-cross inputs are unchanged.
- Signal-v1/v2 parameters and provenance hashes remain unchanged: their
  all-contiguous-history rule still applies to all rows supplied to the signal.
  Separate data_window parameters/provenance are recorded on offline outputs.
  Their hash enters the cache data hash; the cache namespace and resulting
  offline signal/intent IDs change. Original outputs/cache are untouched.

Offline-only gap exclusion:
- H=72 hours (288 bars) is fixed. Contract/mark timestamp gaps are derived once
  from the loaded series and declared replay bounds, never from a date list.
  Adjacent missing slots form one gap; the blackout is [gap_start - H, gap_start).
  Coverage includes loaded future rows, leading/trailing replay gaps, but not
  an invented extension beyond both the loaded series and replay end.
- Gap inventories are immutable snapshots for an engine; timestamp edits need
  a new engine. OHLC changes do not change the timestamp-derived mask.
- Contract reentry requires W contiguous closed bars after the latest known
  gap, including short leading-history cases; no waiting beyond W is added.
  Mark-only gaps never alter strategy input or add post-gap warmup. Native
  current-bar availability checks and UNKNOWN_DATA_GAP behavior stay intact.
- gap_blackout is a separate data exclusion, not an added/relaxed native gate.
  Policy parameters and per-symbol inventory enter cache provenance. Unknown
  native gap trades are reported separately using unverified native events.
- Funding gaps use the literal native elapsed > right interval * 3600 + 60
  condition. Interval changes alone do not flag a gap; a transition with excess
  elapsed time still does. The left event is the reported gap start, not an
  inferred missing payment timestamp. No rounding, synthetic events or
  head/tail/month gaps are added. Funding gaps use the same 72h pre-blackout,
  labelled stream=funding, with no post-gap warmup or extra in-gap entry gate.
- Funding affected trades use native right > opened and left < native end.
  This conservative overlap need not prove an actual lost payment in a trade.
  Native settlement, cash, funding_known, closure events and net R stay intact.
  Funding-affected trades and incomplete funding are separate from verified N:
  only CLOSED rows with complete funding/fees and finite net PnL/R count.
Bu dışlama gelecekteki veri boşluklarının bilgisine dayanır; canlıda geçerli
değildir; veri kalitesi dışlamasıdır.

Original'dan bilinçli farklar:
- Closed-history validation checks only 15m, not all Original timeframes.
- Confidence-free tie-break: opportunity -> volume -> symbol order, using the
  native stable market ranking over alphabetically ordered symbols.
- NaN available balance is rejected by the required >= margin comparison;
  Original's < margin rejection does not reject NaN.
"""

from __future__ import annotations

import asyncio
import logging
import math
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from inspect import signature
from itertools import pairwise
from typing import Any

from .. import execution_core as core
from .. import v25_execution as live
from ..backtest_baseline import (
    Config,
    Engine,
    LocalSpecClient,
    Position,
    bootstrap,
    iso,
    metrics,
)
from ..backtest_data import Dataset, Series
from ..liquidation_risk import isolated_liquidation_risk
from .contracts import Number, StrategyInput, StrategyResult
from .donchian_breakout import evaluate
from .donchian_indicator import (
    INTERVAL_SECONDS,
    CandleDataError,
    closed_contract_candles,
)
from .donchian_params import STRATEGY_ID, DonchianParams
from .offline_admission import AdmissionReport, admit, approved_params
from .provenance import KAIS_ORIGINAL_ID, parameter_hash

logger = logging.getLogger(__name__)
CACHE_NAMESPACE = "offline_donchian_closed_contract_window_gap_funding_v1"
GAP_BLACKOUT_HOURS = 72
GAP_BLACKOUT_BARS = GAP_BLACKOUT_HOURS * 3600 // INTERVAL_SECONDS
FUNDING_GAP_TOLERANCE_SECONDS = 60
CacheKey = tuple[str, str, str, str, str, str, int]


@dataclass(frozen=True)
class _ClosedDataWindow:
    closed_bars: int

    def parameters(self) -> dict[str, Any]:
        return {
            "closed_bars": self.closed_bars, "interval": "15m",
            "selection": "LAST_CLOSED_ROWS",
            "atr_history": "WINDOW_ONLY_SEED_FIRST_PERIOD_TRUE_RANGES",
        }

    def as_dict(self) -> dict[str, Any]:
        parameters = self.parameters()
        return {
            "policy_id": "native_closed_15m_window_v1",
            "parameters": parameters, "parameter_hash": parameter_hash(parameters),
        }


@dataclass(frozen=True)
class _DataGap:
    stream: str
    start: Number
    end_exclusive: Number


def _series_gaps(series: Series, stream: str, config: Config) -> tuple[dict[str, int], tuple[_DataGap, ...]]:
    times = tuple(series.times)
    if series.interval != "15m" or any(type(at) is not int or at % INTERVAL_SECONDS for at in times):
        raise ValueError("Offline gap inventory requires aligned integer 15m timestamps")
    if any(left >= right for left, right in pairwise(times)):
        raise ValueError("Offline gap inventory requires strictly increasing timestamps")
    start = min(config.start, times[0]) if times else config.start
    end = max(config.end, times[-1] + INTERVAL_SECONDS) if times else config.end
    expected, gaps = start, []
    for at in times:
        if at > expected:
            gaps.append(_DataGap(stream, expected, at))
        expected = at + INTERVAL_SECONDS
    if expected < end:
        gaps.append(_DataGap(stream, expected, end))
    return {"start_inclusive": start, "end_exclusive": end}, tuple(gaps)


def _funding_gaps(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], tuple[_DataGap, ...]]:
    schedule = []
    for row in rows:
        if "time" not in row or "interval_hours" not in row:
            raise ValueError("Offline funding gap inventory lacks event time/interval")
        at, hours = row["time"], row["interval_hours"]
        if (
            type(at) not in (int, float) or not math.isfinite(at)
            or type(hours) not in (int, float) or not math.isfinite(hours) or hours <= 0
            or not math.isfinite(hours * 3600 + FUNDING_GAP_TOLERANCE_SECONDS)
        ):
            raise ValueError("Offline funding gap inventory requires finite event time and positive interval")
        schedule.append({"time": at, "interval_hours": hours})
    gaps, details = [], []
    for left, right in pairwise(schedule):
        elapsed = right["time"] - left["time"]
        if elapsed <= 0 or not math.isfinite(elapsed):
            raise ValueError("Offline funding gap inventory requires strictly increasing finite timestamps")
        limit = right["interval_hours"] * 3600 + FUNDING_GAP_TOLERANCE_SECONDS
        if elapsed > limit:
            gap = _DataGap("funding", left["time"], right["time"])
            gaps.append(gap)
            details.append({
                **asdict(gap), "elapsed_seconds": elapsed,
                "right_interval_hours": right["interval_hours"], "limit_seconds": limit,
            })
    return {
        "event_count": len(schedule),
        "first_event": schedule[0]["time"] if schedule else None,
        "last_event": schedule[-1]["time"] if schedule else None,
        "schedule_hash": parameter_hash(schedule), "gaps": details,
    }, tuple(gaps)


class DonchianOfflineEngine:
    """Separate offline driver; shared native state does not decide strategy quality."""

    def __init__(self, data: Dataset, config: Config, params: DonchianParams):
        self.params = approved_params(params)
        native_limit = signature(Series.closed).parameters["limit"].default
        if type(native_limit) is not int or native_limit <= 0:
            raise ValueError("Native closed-history limit must be a positive integer")
        minimum = max(self.params.channel_period + 2, self.params.atr_period + 1)
        if native_limit < minimum:
            raise ValueError(f"Native closed-history window {native_limit} is below strategy warmup {minimum}")
        self._data_window = _ClosedDataWindow(native_limit)
        self.state = Engine(data, config)
        self._decisions: dict[CacheKey, StrategyResult] = {}
        self.admissions: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        self.gap_blackouts: list[dict[str, Any]] = []
        self.data_quality_rejections: list[dict[str, Any]] = []
        self._gaps: dict[str, tuple[_DataGap, ...]] = {}
        self._gap_inventory: dict[str, dict[str, Any]] = {}
        for symbol, frames in sorted(data.frames.items()):
            if "15m" not in frames or symbol not in data.marks:
                raise ValueError("Offline gap inventory requires contract and mark series per symbol")
            coverage, gaps = {}, []
            for stream, series in (("contract", frames["15m"]), ("mark", data.marks[symbol])):
                coverage[stream], missing = _series_gaps(series, stream, config)
                gaps.extend(missing)
            funding, missing = _funding_gaps(data.funding.get(symbol, []))
            gaps.extend(missing)
            self._gaps[symbol] = tuple(gaps)
            self._gap_inventory[symbol] = {
                "coverage": coverage, "funding": funding, "gaps": [asdict(gap) for gap in gaps],
            }

    @property
    def data_window(self) -> _ClosedDataWindow:
        return self._data_window

    @property
    def gap_policy(self) -> dict[str, Any]:
        parameters = {
            "blackout_hours": GAP_BLACKOUT_HOURS, "blackout_bars": GAP_BLACKOUT_BARS,
            "window_closed_bars": self.data_window.closed_bars, "interval_seconds": INTERVAL_SECONDS,
            "blackout_interval": "[GAP_START_MINUS_H,GAP_START)",
            "gap_source": "LOADED_TIMESTAMPS_AND_REPLAY_BOUNDS",
            "contract_reentry": "W_CONTIGUOUS_CLOSED_BARS_AFTER_GAP",
            "mark_reentry": "NATIVE_CURRENT_BAR_NO_EXTRA_WARMUP",
            "funding_gap_rule": "ELAPSED_GT_RIGHT_INTERVAL_HOURS_X_3600_PLUS_TOLERANCE",
            "funding_gap_tolerance_seconds": FUNDING_GAP_TOLERANCE_SECONDS,
            "funding_gap_start": "LEFT_EVENT_TIME",
            "funding_reentry": "NATIVE_NO_EXTRA_WARMUP",
            "verified_n": "CLOSED_COMPLETE_FUNDING_FEES_FINITE_NET_PNL_AND_R",
        }
        return {
            "policy_id": "offline_future_gap_blackout_funding_v1",
            "parameters": parameters, "parameter_hash": parameter_hash(parameters),
        }

    @property
    def gap_inventory(self) -> dict[str, dict[str, Any]]:
        return deepcopy(self._gap_inventory)

    @property
    def cache_keys(self) -> tuple[CacheKey, ...]:
        return tuple(self._decisions)

    def request_at(self, symbol: str, at: int) -> StrategyInput:
        series = self.state.data.frames[symbol]["15m"]
        return StrategyInput(
            symbol, at, {"15m": deepcopy(series.closed(at, limit=self.data_window.closed_bars))},
            required_intervals=("15m",), market_type="USD_M_CONTRACT",
        )

    def cache_key(self, request: StrategyInput) -> CacheKey:
        closed = closed_contract_candles(request.candles_by_timeframe.get("15m", []), request.decision_time)
        data_hash = parameter_hash({
            "closed_contract_15m": [asdict(candle) for candle in closed],
            "market_type": request.market_type, "required_intervals": request.required_intervals,
            "data_window": self.data_window.as_dict(),
            "gap_policy": self.gap_policy, "gap_inventory": self._gap_inventory[request.symbol],
        })
        provenance = self.params.provenance
        return (CACHE_NAMESPACE, provenance.strategy_id, provenance.strategy_version,
                provenance.parameter_hash, data_hash, request.symbol, request.decision_time)

    def canonical(self, symbol: str, at: int) -> StrategyResult:
        request = self.request_at(symbol, at)
        try:
            key = self.cache_key(request)
        except CandleDataError as exc:
            logger.warning("OFFLINE_DONCHIAN_INVALID_CACHE_DATA symbol=%s reason=%s", symbol, exc.detail)
            return evaluate(request, self.params)
        if key not in self._decisions:
            self._decisions[key] = deepcopy(evaluate(request, self.params))
        return deepcopy(self._decisions[key])

    def _admission(
        self, request: StrategyInput, result: StrategyResult, notional: float, phase: str,
    ) -> AdmissionReport:
        state, at, symbol = self.state, request.decision_time, request.symbol
        raw = core.evaluate_entry_gates(
            symbol=symbol, signal={"direction": result.signal.direction},
            snapshot=state.snapshot(at), policy=state.policy,
            daily=core.daily_execution_metrics(state.events, datetime.fromtimestamp(at, timezone.utc)),
            spread_bps=state.config.spread_bps, armed=True, allowed_symbols=state.policy["allowed_symbols"],
            candidate_notional_usdt=notional,
            active_plans=[{**position.spec, "status": "ACTIVE"} for position in state.positions.values()],
        )
        report = admit(request, result, self.params, raw_entry_gates=raw, candidate_notional_usdt=notional)
        self.admissions.append({
            "symbol": symbol, "at": at, "phase": phase, **report.as_dict(),
            "data_window": self.data_window.as_dict(),
            "gap_policy": self.gap_policy,
        })
        if "gate_schema" not in report.failures:
            for gate in raw["gates"]:
                if gate["key"] in state.gate_evaluations:
                    state.gate_evaluations[gate["key"]] += 1
        return report

    def reject(self, failures: list[str] | tuple[str, ...]) -> None:
        logger.info("OFFLINE_DONCHIAN_ENTRY_REJECTED failures=%s", failures)
        self.state.reject(list(failures))

    def _entry_data_exclusion(self, symbol: str, at: int, phase: str) -> bool:
        gaps = self._gaps[symbol]
        blackout = [
            gap for gap in gaps
            if gap.start - GAP_BLACKOUT_BARS * INTERVAL_SECONDS <= at < gap.start
        ]
        if blackout:
            trace = {
                "symbol": symbol, "at": at, "phase": phase, "reason": "gap_blackout",
                "gaps": [asdict(gap) for gap in blackout], "gap_policy": self.gap_policy,
            }
            self.gap_blackouts.append(deepcopy(trace))
            self.data_quality_rejections.append(trace)
            self.reject(["gap_blackout"])
            return True
        past = [gap for gap in gaps if gap.stream == "contract" and gap.start < at]
        if past:
            last = max(past, key=lambda gap: gap.start)
            series = self.state.data.frames[symbol]["15m"]
            closed = series.closed(at, limit=self.data_window.closed_bars)
            if (
                len(closed) < self.data_window.closed_bars
                or closed[0]["time"] < last.end_exclusive
                or not series.history_complete(at)
            ):
                self.data_quality_rejections.append({
                    "symbol": symbol, "at": at, "phase": phase, "reason": "data_gap_history",
                    "gaps": [asdict(last)], "gap_policy": self.gap_policy,
                })
                self.reject(["data_gap_history"])
                return True
        return False

    def _unknown_data_gap_report(self) -> dict[str, Any]:
        rows = []
        events = {
            event["plan_id"]: event
            for event in self.state.events if event.get("kind") == "LIVE_POSITION_CLOSED_UNVERIFIED"
        }
        for position in self.state.trades:
            if position.status != "UNKNOWN_DATA_GAP":
                continue
            event = events.get(position.signal_identifier)
            if event is None:
                raise ValueError("Native UNKNOWN_DATA_GAP has no unverified closure evidence")
            detected = datetime.fromisoformat(event["created_at"])
            if detected.tzinfo is None or not detected.timestamp().is_integer():
                raise ValueError("Native UNKNOWN_DATA_GAP event has an invalid timestamp")
            at = int(detected.timestamp())
            missing = [
                name for name, series in (
                    ("contract", self.state.data.frames[position.symbol]["15m"]),
                    ("mark", self.state.data.marks[position.symbol]),
                ) if series.at(at) is None
            ]
            if not missing:
                raise ValueError("Native UNKNOWN_DATA_GAP no longer matches loaded data evidence")
            rows.append({
                "signal_id": position.signal_identifier, "symbol": position.symbol,
                "opened_at": iso(position.opened_at), "detected_at": event["created_at"],
                "native_status": position.status, "missing_streams": missing,
            })
        return {"trade_count": len(rows), "trades": rows}

    def _funding_data_gap_report(
        self, rows: list[dict[str, Any]], unknown: dict[str, Any],
    ) -> dict[str, Any]:
        unknown_ends = {
            row["signal_id"]: int(datetime.fromisoformat(row["detected_at"]).timestamp())
            for row in unknown["trades"]
        }
        affected = []
        for position, row in zip(self.state.trades, rows, strict=True):
            if position.status == "CLOSED":
                if position.closed_at is None:
                    raise ValueError("Native CLOSED trade lacks a funding coverage end")
                end = position.closed_at
            elif position.status == "OPEN_AT_END":
                end = self.state.config.end - 1
            elif position.status == "UNKNOWN_DATA_GAP":
                if position.signal_identifier not in unknown_ends:
                    raise ValueError("Native unknown trade lacks a funding lifetime end")
                end = unknown_ends[position.signal_identifier]
            else:
                raise ValueError("Native trade status is not final for funding gap reporting")
            matched = [
                gap for gap in self._gaps[position.symbol] if gap.stream == "funding"
                and gap.end_exclusive > position.opened_at and gap.start < end
            ]
            if matched:
                affected.append({
                    "signal_id": position.signal_identifier, "symbol": position.symbol,
                    "opened_at": row["opened_at"], "end_at": iso(end),
                    "native_status": position.status, "funding_complete": row["funding_complete"],
                    "net_r_known": row["net_r"] is not None, "gaps": [asdict(gap) for gap in matched],
                })
        return {"trade_count": len(affected), "trades": affected}

    @staticmethod
    def _measurement_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
        verified = sum(
            row["status"] == "CLOSED" and row["funding_complete"] is True
            and row["commission_complete"] is True
            and row["net_pnl"] is not None and math.isfinite(row["net_pnl"])
            and row["net_r"] is not None and math.isfinite(row["net_r"])
            for row in rows
        )
        return {
            "fully_verified_completed": verified,
            "closed": sum(row["status"] == "CLOSED" for row in rows),
            "open_at_end": sum(row["status"] == "OPEN_AT_END" for row in rows),
            "unknown_data_gap": sum(row["status"] == "UNKNOWN_DATA_GAP" for row in rows),
            "funding_incomplete": sum(row["funding_usdt"] is None for row in rows),
        }

    async def enter(self, symbol: str, at: int) -> None:
        state = self.state
        if not state.policy["allowed_symbols"] or symbol not in state.policy["allowed_symbols"]:
            self.reject(["allowed_symbols"])
            return
        if self._entry_data_exclusion(symbol, at, "before_entry"):
            return
        request, result = self.request_at(symbol, at), self.canonical(symbol, at)
        report = self._admission(request, result, 0.0, "before_sizing")
        if not report.offline_eligible:
            self.reject(report.failures)
            return
        signal = result.signal
        if signal.entry is None or signal.stop is None or signal.direction not in {"LONG", "SHORT"}:
            raise ValueError("Offline admission level/direction invariant violated")
        try:
            risk = core.risk_sized_order(signal.entry, signal.stop, state.policy, atr=signal.atr)
        except ValueError as exc:
            logger.info("OFFLINE_DONCHIAN_STOP_RISK_REJECTED reason=%s", exc)
            self.reject(["stop_risk"])
            return
        if risk["margin_usdt"] < 5:
            self.reject(["minimum_margin"])
            return
        report = self._admission(request, result, risk["margin_usdt"] * risk["leverage"], "before_spec")
        if not report.offline_eligible:
            self.reject(report.failures)
            return
        contract, mark = state.data.frames[symbol]["15m"].at(at), state.data.marks[symbol].at(at)
        if contract is None or mark is None:
            self.reject(["data_missing_next_open"])
            return
        if signal.tp1 is None or signal.tp2 is None or signal.tp3 is None:
            raise ValueError("Offline admission target invariant violated")
        order = live.LiveOrderRequest(
            symbol=symbol, direction=signal.direction, order_type="MARKET",
            margin_usdt=risk["margin_usdt"], leverage=risk["leverage"], stop_loss=signal.stop,
            tp1=signal.tp1, tp2=signal.tp2, tp3=signal.tp3, atr=signal.atr,
        )
        try:
            spec = await live.build_live_spec(
                LocalSpecClient(state.data.metadata["exchange_info"], contract["open"]),
                order, state.policy, allowed_symbols=state.policy["allowed_symbols"],
            )
            live.validate_protection_readiness(spec, state.policy)
        except live.LiveExchangeError as exc:
            logger.info("OFFLINE_DONCHIAN_SPEC_REJECTED reason=%s", exc)
            text = str(exc)
            self.reject(["cost_filter" if "TP1" in text else "min_notional" if "minimum emir" in text else "native_spec_rejected"])
            return
        report = self._admission(request, result, spec["notional_usdt"], "after_spec")
        failures = list(report.failures)
        protection: list[dict[str, Any]] = []
        for key in ("isolated", "liquidation_buffer"):
            state.gate_evaluations[key] += 1
        try:
            live.validate_live_isolated_snapshot(state.snapshot(at), state.policy)
            protection.append({"key": "isolated", "passed": True})
        except live.LiveExchangeError as exc:
            failures.append("isolated")
            protection.append({"key": "isolated", "passed": False, "reason": str(exc)})
        try:
            evidence = isolated_liquidation_risk(spec, state.data.metadata["brackets"][symbol], state.policy)
            protection.append({"key": "liquidation_buffer", "passed": True, "evidence": evidence})
        except (KeyError, ValueError) as exc:
            failures.append("liquidation_buffer")
            protection.append({"key": "liquidation_buffer", "passed": False, "reason": str(exc)})
        balance_ok = state.snapshot(at)["available_balance"] >= spec["margin_usdt"]
        protection.append({"key": "available_balance", "passed": balance_ok})
        if not balance_ok:
            failures.append("available_balance")
        self.admissions[-1]["protections"] = protection
        self.admissions[-1]["final_offline_eligible"] = not failures
        if failures:
            self.reject(failures)
            return
        identifier = "offline-donchian-" + parameter_hash({"cache_key": self.cache_key(request)})
        position = Position(
            symbol, signal.direction, at, spec, identifier,
            Decimal(str(state.config.slippage_bps)) / 10000, Decimal(str(mark["open"])),
            Decimal(str(contract["open"])),
        )
        state.positions[symbol] = position
        state.trades.append(position)
        state.cash -= float(position.commission)
        state.events.insert(0, {"kind": "LIVE_ENTRY", "created_at": iso(at), "plan_id": identifier})

    async def replay(self) -> dict[str, Any]:
        state = self.state
        symbols = sorted(set(state.policy["allowed_symbols"]) & set(state.data.frames))
        for at in range(state.config.start, state.config.end, 900):
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
                result = self.canonical(symbol, at)
                state.decisions_evaluated += 1
                self.decisions.append({
                    "symbol": symbol, "at": at, "signal": asdict(result.signal),
                    "data_window": self.data_window.as_dict(),
                    "gap_policy": self.gap_policy,
                })
                if not result.signal.strategy_eligible:
                    self.reject([result.signal.reason])
                    continue
                signal = result.signal
                if signal.entry is None or signal.stop is None:
                    raise ValueError("Offline eligible signal is missing entry or Stop")
                distance = abs(signal.entry - signal.stop) / signal.entry * 100
                if distance > core.dynamic_stop_distance_pct(signal.entry, None, state.policy):
                    self.reject(["stop_distance"])
                    continue
                signals.append((candidate, result))
            signals.sort(key=lambda pair: pair[0]["opportunity_score"], reverse=True)
            for candidate, _result in signals[:3]:
                await self.enter(candidate["symbol"], at)
            for position in list(state.positions.values()):
                state.advance(position, at, opening_only=False)
        for position in state.positions.values():
            position.status = "OPEN_AT_END"
            position.funding_known = position.funding_known and state.data.funding_complete(
                position.symbol, position.opened_at, state.config.end - 1,
            )
        rows = [
            {**position.row(), "intent_id": f"offline-intent-{position.signal_identifier}",
             "strategy": asdict(self.params.provenance), "exit_policy_id": self.params.exit_policy_id,
             "data_window": self.data_window.as_dict(), "gap_policy": self.gap_policy}
            for position in state.trades
        ]
        summary = metrics(rows, state.config.initial_equity)
        summary["long_short"] = {
            direction: metrics([row for row in rows if row["direction"] == direction], state.config.initial_equity)
            for direction in ("LONG", "SHORT")
        }
        unknown = self._unknown_data_gap_report()
        return {
            "label": "OFFLINE DONCHIAN ONLY / NOT LIVE OR DEMO",
            "strategy": asdict(self.params.provenance), "exit_policy_id": self.params.exit_policy_id,
            "data_window": self.data_window.as_dict(),
            "gap_policy": self.gap_policy, "gap_inventory": self.gap_inventory,
            "gap_blackout_count": len(self.gap_blackouts), "gap_blackouts": deepcopy(self.gap_blackouts),
            "data_quality_rejections": deepcopy(self.data_quality_rejections),
            "unknown_data_gaps": unknown,
            "funding_data_gaps": self._funding_data_gap_report(rows, unknown),
            "measurement_counts": self._measurement_counts(rows),
            "period": {"start_inclusive": iso(state.config.start), "end_exclusive": iso(state.config.end)},
            "intrabar": state.config.intrabar, "spread_bps": state.config.spread_bps,
            "slippage_bps": state.config.slippage_bps, "fee_bps_per_side": 5,
            "policy": deepcopy(state.policy), "summary": summary,
            "bootstrap": bootstrap(rows, state.config.bootstrap_samples, state.config.seed),
            "admissions": deepcopy(self.admissions), "decisions": deepcopy(self.decisions),
            "all_rejections": dict(sorted(state.rejections.items())),
            "first_rejections": dict(sorted(state.first_rejections.items())),
            "stage1_gate_rejections": dict(state.gate_counts),
            "stage1_gate_evaluations": dict(state.gate_evaluations), "trades": rows,
            "assumptions": [
                "Native 24h closed-contract ticker universe; opportunity ranking without confidence tie-break.",
                "Native baseline costs, local exchangeInfo/brackets, position exits and funding model.",
                "No persisted cache, signed requests, real orders, LIVE/Demo admission or Stage research wiring.",
                "Offline authorization is modeled armed, exactly as in the native baseline.",
                "Only confidence/trap are inapplicable; all other native gates and protections are mandatory.",
            ],
        }


class OfflineFacade:
    """One selected strategy per instance; Original dispatch is literal delegation."""

    def __init__(
        self, data: Dataset, config: Config, *, strategy_id: str = KAIS_ORIGINAL_ID,
        params: DonchianParams | None = None,
    ):
        if strategy_id == KAIS_ORIGINAL_ID:
            if params is not None:
                raise ValueError("Donchian parameters cannot modify the literal Original path")
            self.engine: Engine | DonchianOfflineEngine = Engine(data, config)
        elif strategy_id == STRATEGY_ID:
            self.engine = DonchianOfflineEngine(data, config, params if params is not None else DonchianParams())
        else:
            raise ValueError(f"Unsupported offline strategy: {strategy_id!r}")

    def canonical(self, symbol: str, at: int) -> dict[str, Any] | StrategyResult:
        if isinstance(self.engine, Engine):
            return self.engine.data.canonical(symbol, at, self.engine.policy)
        return self.engine.canonical(symbol, at)

    async def replay(self) -> dict[str, Any]:
        return await self.engine.replay()


def run(
    data: Dataset, config: Config, *, strategy_id: str = KAIS_ORIGINAL_ID,
    params: DonchianParams | None = None,
) -> dict[str, Any]:
    return asyncio.run(OfflineFacade(data, config, strategy_id=strategy_id, params=params).replay())
