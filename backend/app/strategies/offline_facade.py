"""Opt-in offline dispatch, never imported by LIVE/Demo or existing research.

Original uses the literal native Engine/Dataset path, including its old cache.
Donchian composes the native lifecycle, funding, sizing, spec and protections.
It has no Original analysis, confidence tie-breaker, network or persisted cache.
The native market opportunity ranking remains primary, with its stable ordering.

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
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
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
from ..backtest_data import Dataset
from ..liquidation_risk import isolated_liquidation_risk
from .contracts import StrategyInput, StrategyResult
from .donchian_breakout import evaluate
from .donchian_indicator import CandleDataError, closed_contract_candles
from .donchian_params import STRATEGY_ID, DonchianParams
from .offline_admission import AdmissionReport, admit, approved_params
from .provenance import KAIS_ORIGINAL_ID, parameter_hash

logger = logging.getLogger(__name__)
CACHE_NAMESPACE = "offline_donchian_closed_contract_v1"
CacheKey = tuple[str, str, str, str, str, str, int]


class DonchianOfflineEngine:
    """Separate offline driver; shared native state does not decide strategy quality."""

    def __init__(self, data: Dataset, config: Config, params: DonchianParams):
        self.params = approved_params(params)
        self.state = Engine(data, config)
        self._decisions: dict[CacheKey, StrategyResult] = {}
        self.admissions: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []

    @property
    def cache_keys(self) -> tuple[CacheKey, ...]:
        return tuple(self._decisions)

    def request_at(self, symbol: str, at: int) -> StrategyInput:
        series = self.state.data.frames[symbol]["15m"]
        return StrategyInput(
            symbol, at, {"15m": deepcopy(series.closed(at, limit=len(series.rows)))},
            required_intervals=("15m",), market_type="USD_M_CONTRACT",
        )

    def cache_key(self, request: StrategyInput) -> CacheKey:
        closed = closed_contract_candles(request.candles_by_timeframe.get("15m", []), request.decision_time)
        data_hash = parameter_hash({
            "closed_contract_15m": [asdict(candle) for candle in closed],
            "market_type": request.market_type, "required_intervals": request.required_intervals,
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
        self.admissions.append({"symbol": symbol, "at": at, "phase": phase, **report.as_dict()})
        if "gate_schema" not in report.failures:
            for gate in raw["gates"]:
                if gate["key"] in state.gate_evaluations:
                    state.gate_evaluations[gate["key"]] += 1
        return report

    def reject(self, failures: list[str] | tuple[str, ...]) -> None:
        logger.info("OFFLINE_DONCHIAN_ENTRY_REJECTED failures=%s", failures)
        self.state.reject(list(failures))

    async def enter(self, symbol: str, at: int) -> None:
        state = self.state
        if not state.policy["allowed_symbols"] or symbol not in state.policy["allowed_symbols"]:
            self.reject(["allowed_symbols"])
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
            tickers = [
                ticker for symbol in symbols
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
                self.decisions.append({"symbol": symbol, "at": at, "signal": asdict(result.signal)})
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
             "strategy": asdict(self.params.provenance), "exit_policy_id": self.params.exit_policy_id}
            for position in state.trades
        ]
        summary = metrics(rows, state.config.initial_equity)
        summary["long_short"] = {
            direction: metrics([row for row in rows if row["direction"] == direction], state.config.initial_equity)
            for direction in ("LONG", "SHORT")
        }
        return {
            "label": "OFFLINE DONCHIAN ONLY / NOT LIVE OR DEMO",
            "strategy": asdict(self.params.provenance), "exit_policy_id": self.params.exit_policy_id,
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
