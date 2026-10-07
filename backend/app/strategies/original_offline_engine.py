"""Native Original lifecycle with per-instance, offline-only risk admission."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import replace
from decimal import Decimal
from typing import Any

from app import v25_execution as live
from app.backtest_baseline import Config, Engine, LocalSpecClient, Position, iso
from app.backtest_data import Dataset
from app.binance_demo import decimal_text, floor_step, round_tick, validate_levels
from app.liquidation_risk import isolated_liquidation_risk
from app.signal_journal import signal_id
from app.strategies.original_offline_risk import (
    PROFILE,
    OfflineRiskError,
    OriginalOfflineRiskProfile,
)


async def local_spec(
    client: LocalSpecClient, order: live.LiveOrderRequest, policy: dict[str, Any],
    profile: OriginalOfflineRiskProfile,
) -> dict[str, Any]:
    if type(client) is not LocalSpecClient:
        raise OfflineRiskError("LOCAL_SPEC_CLIENT_REQUIRED")
    if order.order_type != "MARKET" or order.leverage != profile.leverage:
        raise OfflineRiskError("OFFLINE_ORDER_SCOPE")
    if not profile.min_margin_usdt <= order.margin_usdt <= profile.max_margin_usdt:
        raise OfflineRiskError("minimum_margin")
    symbol = live.normalize_symbol(order.symbol)
    if symbol not in policy["allowed_symbols"]:
        raise OfflineRiskError("allowed_symbols")
    if not policy["allow_long" if order.direction == "LONG" else "allow_short"]:
        raise OfflineRiskError("long" if order.direction == "LONG" else "short")
    current = await live.ticker_price(client, symbol)
    rules = await live.live_symbol_rules(client, symbol, "MARKET")
    entry = round_tick(current, rules["tick"])
    stop = round_tick(Decimal(str(order.stop_loss)), rules["tick"])
    targets = [round_tick(Decimal(str(value)), rules["tick"]) for value in (order.tp1, order.tp2, order.tp3)]
    validate_levels(order.direction, current, stop, targets)
    risk = profile.size(float(entry), float(stop), margin_limit=order.margin_usdt)
    if risk["estimated_stop_loss_usdt"] > profile.risk_budget_usdt + 1e-6:
        raise OfflineRiskError("stop_risk")
    # Keep the same pre-spec minimum-margin gate; native spec does not reapply it.
    notional = Decimal(str(risk["notional_usdt"]))
    quantity = floor_step(notional / entry, rules["step"])
    if quantity < rules["min_qty"] or quantity * entry < rules["min_notional"]:
        raise OfflineRiskError("min_notional")
    if quantity > rules["max_qty"]:
        raise OfflineRiskError("native_spec_rejected")
    movement = abs(float(targets[0] - entry) / float(entry))
    expected = float(quantity * entry) * movement
    costs = float(quantity * entry) * (policy["fee_bps_per_side"] + policy["slippage_bps_per_side"]) * 2 / 10_000
    if expected - costs < policy["minimum_net_reward_usdt"]:
        raise OfflineRiskError("cost_filter")
    spec = {
        "symbol": symbol, "direction": order.direction,
        "side": "BUY" if order.direction == "LONG" else "SELL",
        "close_side": "SELL" if order.direction == "LONG" else "BUY",
        "order_type": order.order_type, "margin_usdt": float(risk["margin_usdt"]),
        "leverage": order.leverage, "notional_usdt": float(quantity * entry),
        "quantity": decimal_text(quantity), "entry_price": decimal_text(entry),
        "stop_loss": decimal_text(stop), "targets": [decimal_text(value) for value in targets],
        "step": rules["step"], "min_qty": rules["min_qty"], "min_notional": rules["min_notional"],
        "estimated_stop_loss_usdt": risk["estimated_stop_loss_usdt"],
    }
    live.validate_protection_readiness(spec, policy)
    return spec


def fully_verified(position: Position) -> bool:
    # Validate native accounting completeness without Position.row or R division.
    return (
        position.status == "CLOSED" and position.closed_at is not None and position.remaining == 0
        and position.funding_known and position.initial_risk is not None
        and math.isfinite(position.initial_risk) and position.initial_risk > 0
        and all(value.is_finite() for value in (position.gross, position.commission, position.funding_amount))
        and math.isfinite(float(position.gross - position.commission))
        and math.isfinite(float(position.gross - position.commission) + float(position.funding_amount))
    )


def trade_counts(positions: list[Position]) -> dict[str, int]:
    return {
        "accepted_entries": len(positions),
        "completed": sum(position.status == "CLOSED" for position in positions),
        "fully_verified_completed": sum(fully_verified(position) for position in positions),
        "open_at_end": sum(position.status == "OPEN_AT_END" for position in positions),
        "unknown_data_gap": sum(position.status == "UNKNOWN_DATA_GAP" for position in positions),
    }


class OriginalOfflineRiskEngine(Engine):
    def __init__(self, data: Dataset, config: Config, *, profile: OriginalOfflineRiskProfile = PROFILE):
        if type(profile) is not OriginalOfflineRiskProfile or profile != PROFILE:
            raise OfflineRiskError("UNREGISTERED_OFFLINE_PROFILE")
        fresh = Dataset(data.frames, data.marks, data.funding, data.metadata, data.report, data.funding_months)
        super().__init__(fresh, replace(config, policy=profile.policy(config.policy)))
        self.profile = profile
        self.attempts = 0
        self.risk_stage_arrivals = 0
        self.stage = "PREFILTER"
        self.stage_rejections: dict[str, Counter[str]] = {"PREFILTER": Counter(), "ENTER": Counter()}
        self.minimum_by_symbol: Counter[str] = Counter()
        self.accepted_distances: list[float] = []

    def reject(self, reasons: list[str]) -> None:
        self.stage_rejections[self.stage].update(dict.fromkeys(reasons, 1))
        super().reject(reasons)

    def prefilter(self, signal: dict[str, Any]) -> bool:
        self.risk_stage_arrivals += 1
        if not self.profile.accepts(signal["entry"], signal["stop_loss"]):
            self.reject(["profile_cap"])
            return False
        return True

    async def replay(self) -> dict[str, Any]:
        raise OfflineRiskError("COUNTS_RUNNER_REQUIRED")

    async def enter(self, symbol: str, signal: dict, at: int) -> None:
        self.attempts += 1
        self.stage = "ENTER"
        try:
            await self._enter(symbol, signal, at)
        finally:
            self.stage = "PREFILTER"

    async def _enter(self, symbol: str, signal: dict, at: int) -> None:
        if not self.policy["allowed_symbols"] or symbol not in self.policy["allowed_symbols"]:
            self.reject(["allowed_symbols"])
            return
        failures = self.gate_failures(symbol, signal, at)
        if failures:
            self.reject(failures)
            return
        try:
            risk = self.profile.size(signal["entry"], signal["stop_loss"])
        except OfflineRiskError as exc:
            self.reject([exc.reason])
            return
        if risk["margin_usdt"] < self.profile.min_margin_usdt:
            self.reject(["minimum_margin"])
            return
        failures = self.gate_failures(symbol, signal, at, risk["margin_usdt"] * risk["leverage"])
        if failures:
            self.reject(failures)
            return
        contract, mark = self.data.frames[symbol]["15m"].at(at), self.data.marks[symbol].at(at)
        if contract is None or mark is None:
            self.reject(["data_missing_next_open"])
            return
        request = live.LiveOrderRequest(
            symbol=symbol, direction=signal["direction"], order_type="MARKET",
            margin_usdt=risk["margin_usdt"], leverage=risk["leverage"],
            stop_loss=signal["stop_loss"], tp1=signal["tp1"], tp2=signal["tp2"], tp3=signal["tp3"],
            atr=signal.get("atr"),
        )
        try:
            spec = await local_spec(LocalSpecClient(self.data.metadata["exchange_info"], contract["open"]),
                                    request, self.policy, self.profile)
        except OfflineRiskError as exc:
            self.reject([exc.reason])
            if exc.reason == "min_notional":
                self.minimum_by_symbol[symbol] += 1
            return
        except live.LiveExchangeError as exc:
            reason = "cost_filter" if "TP1" in str(exc) else "native_spec_rejected"
            self.reject([reason])
            return
        failures = self.gate_failures(symbol, signal, at, spec["notional_usdt"])
        for key in ("isolated", "liquidation_buffer"):
            self.gate_evaluations[key] += 1
        try:
            live.validate_live_isolated_snapshot(self.snapshot(at), self.policy)
        except live.LiveExchangeError:
            failures.append("isolated")
        try:
            isolated_liquidation_risk(spec, self.data.metadata["brackets"][symbol], self.policy)
        except (KeyError, ValueError):
            failures.append("liquidation_buffer")
        if self.snapshot(at)["available_balance"] < spec["margin_usdt"]:
            failures.append("available_balance")
        if failures:
            self.reject(failures)
            return
        position = Position(symbol, signal["direction"], at, spec, signal_id(symbol, signal["direction"], at),
                            Decimal(str(self.config.slippage_bps)) / 10000, Decimal(str(mark["open"])),
                            Decimal(str(contract["open"])))
        self.positions[symbol] = position
        self.trades.append(position)
        self.cash -= float(position.commission)
        self.events.insert(0, {"kind": "LIVE_ENTRY", "created_at": iso(at), "plan_id": position.signal_identifier})
        self.accepted_distances.append(abs(signal["entry"] - signal["stop_loss"]) / signal["entry"] * 100)
