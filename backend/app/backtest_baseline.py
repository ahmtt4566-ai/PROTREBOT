"""Conditional baseline replay, using the unmodified LIVE decision/risk helpers."""

from __future__ import annotations

import asyncio
import bisect
import math
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from . import execution_core as core, v25_execution as live
from .backtest_data import Dataset
from .binance_demo import floor_step
from .liquidation_risk import isolated_liquidation_risk
from .signal_journal import signal_id
from .trade_r_metrics import calculate_r_multiple, initial_entry_risk_usdt

STAGE1_GATES = ("exposure", "liquidation_buffer", "same_direction_positions", "direction_exposure",
                "daily_trades", "daily_loss", "consecutive_losses", "isolated", "positions",
                "duplicate", "active_plan", "open_loss", "pnl_verified")


def iso(at: int) -> str:
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


@dataclass(frozen=True)
class Config:
    start: int
    end: int
    initial_equity: float = 1000
    spread_bps: float = 2
    slippage_bps: float = 3
    intrabar: str = "STOP_FIRST"
    bootstrap_samples: int = 2000
    seed: int = 2026
    conditional_current_metadata: bool = False
    policy: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.start >= self.end or self.start % 900 or self.end % 900:
            raise ValueError("Invalid replay period")
        if self.intrabar not in {"STOP_FIRST", "TP_FIRST"}:
            raise ValueError("Unknown intrabar ordering")
        if any(not math.isfinite(value) for value in (self.initial_equity, self.spread_bps, self.slippage_bps)):
            raise ValueError("Non-finite simulation input")
        if self.initial_equity <= 0 or self.spread_bps < 0 or not 0 <= self.slippage_bps <= 30 or self.bootstrap_samples < 1:
            raise ValueError("Invalid simulation costs/equity/bootstrap count")


class LocalSpecClient:
    """The native sizing helper can only read these two local responses."""

    def __init__(self, exchange_info: dict, opening: float):
        self.exchange_info, self.opening = exchange_info, opening

    async def public_get(self, path: str, params: dict | None = None) -> dict:
        if path == "/fapi/v1/exchangeInfo":
            return self.exchange_info
        if path == "/fapi/v1/ticker/price":
            return {"price": str(self.opening)}
        raise AssertionError(f"Offline client forbids endpoint: {path}")

    async def signed(self, *args, **kwargs) -> None:
        raise AssertionError("Offline backtest must never use a signed endpoint")


@dataclass
class Position:
    symbol: str
    direction: str
    opened_at: int
    spec: dict[str, Any]
    signal_identifier: str
    slip: Decimal
    mark_entry: Decimal
    market_open: Decimal | None = None
    quantity: Decimal = field(init=False)
    remaining: Decimal = field(init=False)
    tp1_quantity: Decimal = field(init=False)
    actual_entry: Decimal = field(init=False)
    initial_risk: float | None = field(init=False)
    commission: Decimal = field(init=False)
    gross: Decimal = Decimal(0)
    funding_amount: Decimal = Decimal(0)
    funding_known: bool = True
    tp1_hit: bool = False
    ambiguous_bars: int = 0
    exits: list[dict[str, Any]] = field(default_factory=list)
    closed_at: int | None = None
    status: str = "OPEN"

    def __post_init__(self) -> None:
        self.quantity = self.remaining = Decimal(str(self.spec["quantity"]))
        self.actual_entry = (self.market_open if self.market_open is not None else Decimal(str(self.spec["entry_price"]))) * (1 + self.sign * self.slip)
        self.initial_risk = initial_entry_risk_usdt(self.spec)
        self.commission = self.quantity * self.actual_entry * Decimal("0.0005")
        partial = floor_step(self.quantity * Decimal("0.60"), self.spec["step"])
        self.tp1_quantity = partial if partial >= self.spec["min_qty"] and partial * self.mark_entry >= self.spec["min_notional"] else Decimal(0)

    @property
    def sign(self) -> int:
        return 1 if self.direction == "LONG" else -1

    def fill(self, expected: Decimal, quantity: Decimal, at: int, reason: str) -> None:
        actual = expected * (1 - self.sign * self.slip)
        self.gross += quantity * self.sign * (actual - self.actual_entry)
        fee = quantity * actual * Decimal("0.0005")
        self.commission += fee
        self.remaining -= quantity
        self.exits.append({"time": at, "reason": reason, "quantity": float(quantity),
                           "expected_price": float(expected), "actual_price": float(actual),
                           "commission_usdt": float(fee), "slippage_price": float(actual - expected)})
        if self.remaining == 0:
            self.closed_at, self.status = at, "CLOSED"

    def advance(self, contract: dict, mark: dict, ordering: str, *, opening_only: bool) -> None:
        at = int(contract["time"]) if opening_only else int(contract["time"]) + 899
        stop, tp1, tp3 = (Decimal(str(value)) for value in (self.spec["stop_loss"], self.spec["targets"][0], self.spec["targets"][2]))
        adverse = Decimal(str(mark["open"] if opening_only else mark["low"] if self.sign == 1 else mark["high"]))
        favorable = Decimal(str(mark["open"] if opening_only else mark["high"] if self.sign == 1 else mark["low"]))
        stop_hit = self.sign * (adverse - stop) <= 0
        tp1_hit = self.tp1_quantity > 0 and not self.tp1_hit and self.sign * (favorable - tp1) >= 0
        tp3_hit = self.sign * (favorable - tp3) >= 0
        if stop_hit and (tp1_hit or tp3_hit):
            self.ambiguous_bars += 1
        price = Decimal(str(contract["open"])) if opening_only else None
        if stop_hit and (ordering == "STOP_FIRST" or not (tp1_hit or tp3_hit)):
            self.fill(price if price is not None else stop, self.remaining, at, "STOP")
            return
        if tp1_hit:
            self.fill(price if price is not None else tp1, min(self.tp1_quantity, self.remaining), at, "TP1")
            self.tp1_hit = True
        if tp3_hit and self.remaining:
            self.fill(price if price is not None else tp3, self.remaining, at, "TP3")
        elif stop_hit and self.remaining:
            self.fill(price if price is not None else stop, self.remaining, at, "STOP")

    def row(self) -> dict[str, Any]:
        closed = self.status == "CLOSED"
        excluding_funding = float(self.gross - self.commission) if closed else None
        funding = float(self.funding_amount) if self.funding_known else None
        net = excluding_funding + funding if excluding_funding is not None and funding is not None else None
        return {
            "signal_id": self.signal_identifier, "intent_id": f"auto-{self.symbol}-15m-{self.opened_at * 1000}",
            "symbol": self.symbol, "direction": self.direction, "decision": "ACCEPTED",
            "status": self.status, "opened_at": iso(self.opened_at), "closed_at": iso(self.closed_at) if self.closed_at is not None else None,
            "expected_entry": float(self.spec["entry_price"]), "actual_fill_price": float(self.actual_entry),
            "actual_slippage_price": float(self.actual_entry - Decimal(str(self.spec["entry_price"]))),
            "quantity": float(self.quantity), "remaining_quantity": float(self.remaining),
            "initial_risk_usdt": self.initial_risk, "gross_pnl": float(self.gross) if closed else None,
            "commission_usdt": float(self.commission), "commission_complete": closed,
            "funding_usdt": funding, "funding_complete": self.funding_known and closed,
            "funding_status": "HISTORICAL_MARK_OPEN_MODEL" if self.funding_known else "FUNDING YOK / INCOMPLETE",
            "net_pnl": net, "net_r": calculate_r_multiple(net, self.initial_risk),
            "net_pnl_ex_funding": excluding_funding,
            "net_r_ex_funding": calculate_r_multiple(excluding_funding, self.initial_risk),
            "stop": float(self.spec["stop_loss"]), "tp1": float(self.spec["targets"][0]), "tp3": float(self.spec["targets"][2]),
            "tp1_quantity": float(self.tp1_quantity), "tp1_effective_fraction": float(self.tp1_quantity / self.quantity),
            "tp_protection_state": "EXCHANGE_BACKED_MODEL" if self.tp1_quantity else "TP1_UNPROTECTED_MINIMUM",
            "ambiguous_bars": self.ambiguous_bars, "exits": self.exits,
        }


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    offset = (len(ordered) - 1) * fraction
    low = math.floor(offset)
    return ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (offset - low)


def bootstrap(rows: list[dict], samples: int, seed: int) -> dict:
    valid = [row for row in rows if row["net_r"] is not None and row["net_pnl"] is not None]
    if not valid:
        return {"method": "IID_PAIRED_TRADES", "seed": seed, "samples": samples,
                "expectancy_r_95": None, "profit_factor_95": None, "status": "NO_COMPLETE_TRADES"}
    rng = random.Random(seed)
    expectancies, factors = [], []
    unbounded = 0
    for _ in range(samples):
        picked = [valid[rng.randrange(len(valid))] for _ in valid]
        expectancies.append(sum(row["net_r"] for row in picked) / len(picked))
        wins = sum(max(0, row["net_pnl"]) for row in picked)
        losses = -sum(min(0, row["net_pnl"]) for row in picked)
        if losses:
            factors.append(wins / losses)
        else:
            factors.append(math.inf)
            unbounded += 1
    factors.sort()
    lower = factors[int((samples - 1) * 0.025)]
    upper = factors[math.ceil((samples - 1) * 0.975)]
    return {"method": "IID_PAIRED_TRADES", "seed": seed, "samples": samples,
            "expectancy_r_95": [percentile(expectancies, 0.025), percentile(expectancies, 0.975)],
            "profit_factor_95": [lower if math.isfinite(lower) else None, upper if math.isfinite(upper) else None],
            "pf_upper_unbounded": not math.isfinite(upper), "no_loss_resamples": unbounded}


def metrics(rows: list[dict], equity: float) -> dict:
    closed = [row for row in rows if row["status"] == "CLOSED"]
    known = [row for row in closed if row["net_pnl"] is not None and row["net_r"] is not None]
    gains = [row["net_pnl"] for row in known if row["net_pnl"] > 0]
    losses = [row["net_pnl"] for row in known if row["net_pnl"] < 0]
    curve: dict[str, list[float]] = {}
    for row in known:
        values = curve.setdefault(row["closed_at"], [0.0, 0.0])
        values[0] += row["net_pnl"]
        values[1] += row["net_r"]
    current, peak, dd = equity, equity, 0.0
    current_r, peak_r, dd_r = 0.0, 0.0, 0.0
    for at in sorted(curve):
        current += curve[at][0]
        current_r += curve[at][1]
        peak, peak_r = max(peak, current), max(peak_r, current_r)
        dd, dd_r = max(dd, peak - current), max(dd_r, peak_r - current_r)
    return {"trade_count": len(rows), "closed_trade_count": len(closed), "r_eligible_count": len(known),
            "open_or_unverified_count": len(rows) - len(closed),
            "funding_null_count": sum(row["funding_usdt"] is None for row in rows),
            "net_expectancy_r": sum(row["net_r"] for row in known) / len(known) if known else None,
            "profit_factor": sum(gains) / -sum(losses) if losses else None,
            "profit_factor_status": "FINITE" if losses else "NO_LOSSES" if known else "NO_COMPLETE_TRADES",
            "win_rate": len(gains) / len(known) if known else None,
            "average_win_usdt": sum(gains) / len(gains) if gains else None,
            "average_loss_usdt": sum(losses) / len(losses) if losses else None,
            "average_win_r": sum(row["net_r"] for row in known if row["net_r"] > 0) / len(gains) if gains else None,
            "average_loss_r": sum(row["net_r"] for row in known if row["net_r"] < 0) / len(losses) if losses else None,
            "max_drawdown_usdt": dd if known else None, "max_drawdown_r": dd_r if known else None,
            "closed_curve_net_pnl": current - equity if known else None,
            "ambiguous_bars": sum(row["ambiguous_bars"] for row in rows)}


class Engine:
    def __init__(self, data: Dataset, config: Config):
        if not config.conditional_current_metadata and data.metadata.get("historical") is not True:
            raise ValueError("Current metadata requires explicit conditional-baseline consent")
        self.data, self.config = data, config
        self.policy = core.sanitize_execution_policy({
            **config.policy, "fee_bps_per_side": 5, "slippage_bps_per_side": config.slippage_bps,
        }, preserve_empty_allowed_symbols=True)
        if self.policy["interval"] != "15m":
            raise ValueError("Baseline is LIVE 15m only")
        self.positions: dict[str, Position] = {}
        self.trades: list[Position] = []
        self.events: list[dict] = []
        self.cash = config.initial_equity
        self.rejections: Counter = Counter()
        self.first_rejections: Counter = Counter()
        self.gate_counts = Counter({key: 0 for key in STAGE1_GATES})
        self.gate_evaluations = Counter({key: 0 for key in STAGE1_GATES})
        self.decisions_evaluated = 0
        self.funding_times = {symbol: [row["time"] for row in rows] for symbol, rows in data.funding.items()}

    def reject(self, reasons: list[str]) -> None:
        unique = list(dict.fromkeys(reasons))
        self.first_rejections[unique[0]] += 1
        self.rejections.update(unique)
        for key in unique:
            if key in self.gate_counts:
                self.gate_counts[key] += 1

    def gate_failures(self, symbol: str, signal: dict, at: int, notional: float = 0) -> list[str]:
        snapshot = self.snapshot(at)
        daily = core.daily_execution_metrics(self.events, datetime.fromtimestamp(at, timezone.utc))
        gates = core.evaluate_entry_gates(
            symbol=symbol, signal=signal, snapshot=snapshot, policy=self.policy, daily=daily,
            spread_bps=self.config.spread_bps, armed=True, allowed_symbols=self.policy["allowed_symbols"],
            candidate_notional_usdt=notional, active_plans=[{**position.spec, "status": "ACTIVE"}
                                                          for position in self.positions.values()])
        for gate in gates["gates"]:
            if gate["key"] in self.gate_evaluations:
                self.gate_evaluations[gate["key"]] += 1
        return [gate["key"] for gate in gates["gates"] if not gate["passed"]]

    def snapshot(self, at: int) -> dict:
        positions, margin, unrealized = [], 0.0, 0.0
        for position in self.positions.values():
            mark = self.data.marks[position.symbol].at(at)
            if mark is None:
                raise ValueError("Open position mark data is unavailable")
            positions.append({"symbol": position.symbol, "direction": position.direction,
                              "quantity": float(position.remaining), "mark_price": mark["open"],
                              "entry_price": float(position.actual_entry), "margin_type": "ISOLATED"})
            margin += float(position.remaining * position.actual_entry) / position.spec["leverage"]
            unrealized += float(position.remaining) * position.sign * (mark["open"] - float(position.actual_entry))
        return {"positions": positions, "open_orders": [], "hedge_mode": False, "multi_assets_mode": False,
                "unrealized_pnl": unrealized, "available_balance": self.cash - margin + unrealized}

    def finalize(self, position: Position, at: int) -> None:
        del self.positions[position.symbol]
        if position.status == "CLOSED":
            position.funding_known = position.funding_known and self.data.funding_complete(position.symbol, position.opened_at, at)
            # Native daily loss/streak accounting deliberately excludes funding.
            value = float(position.gross - position.commission)
            self.events.insert(0, {"kind": "LIVE_POSITION_CLOSED", "created_at": iso(at),
                                   "realized_pnl": value, "plan_id": position.signal_identifier})
        else:
            self.events.insert(0, {"kind": "LIVE_POSITION_CLOSED_UNVERIFIED", "created_at": iso(at),
                                   "plan_id": position.signal_identifier})

    def funding_at(self, position: Position, at: int, *, opening_only: bool) -> None:
        times = self.funding_times.get(position.symbol, [])
        low = bisect.bisect_left(times, at) if opening_only else bisect.bisect_right(times, at)
        high = bisect.bisect_right(times, at) if opening_only else bisect.bisect_left(times, at + 900)
        for event in self.data.funding.get(position.symbol, [])[low:high]:
            if event["time"] <= position.opened_at:
                continue
            mark = self.data.marks[position.symbol].at(event["time"] // 900 * 900)
            if mark is None:
                position.funding_known = False
                continue
            payment = -position.sign * position.remaining * Decimal(str(mark["open"])) * Decimal(str(event["rate"]))
            position.funding_amount += payment
            self.cash += float(payment)

    def advance(self, position: Position, at: int, *, opening_only: bool) -> None:
        contract = self.data.frames[position.symbol]["15m"].at(at)
        mark = self.data.marks[position.symbol].at(at)
        if contract is None or mark is None:
            position.status, position.funding_known = "UNKNOWN_DATA_GAP", False
            self.finalize(position, at)
            return
        self.funding_at(position, at, opening_only=opening_only)
        before_gross, before_fees = position.gross, position.commission
        position.advance(contract, mark, self.config.intrabar, opening_only=opening_only)
        self.cash += float(position.gross - before_gross - (position.commission - before_fees))
        if position.status == "CLOSED":
            self.finalize(position, position.closed_at)

    async def enter(self, symbol: str, signal: dict, at: int) -> None:
        if not self.policy["allowed_symbols"] or symbol not in self.policy["allowed_symbols"]:
            self.reject(["allowed_symbols"])
            return
        failures = self.gate_failures(symbol, signal, at)
        if failures:
            self.reject(failures)
            return
        try:
            risk = core.risk_sized_order(signal["entry"], signal["stop_loss"], self.policy, atr=signal.get("atr"))
        except ValueError:
            self.reject(["stop_risk"])
            return
        if risk["margin_usdt"] < 5:
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
        request = live.LiveOrderRequest(symbol=symbol, direction=signal["direction"], order_type="MARKET",
                                        margin_usdt=risk["margin_usdt"], leverage=risk["leverage"],
                                        stop_loss=signal["stop_loss"], tp1=signal["tp1"],
                                        tp2=signal["tp2"], tp3=signal["tp3"], atr=signal.get("atr"))
        try:
            spec = await live.build_live_spec(LocalSpecClient(self.data.metadata["exchange_info"], contract["open"]),
                                              request, self.policy, allowed_symbols=self.policy["allowed_symbols"])
            live.validate_protection_readiness(spec, self.policy)
        except live.LiveExchangeError as exc:
            text = str(exc)
            key = "cost_filter" if "TP1" in text else "min_notional" if "minimum emir" in text else "native_spec_rejected"
            self.reject([key])
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

    async def replay(self) -> dict[str, Any]:
        symbols = sorted(set(self.policy["allowed_symbols"]) & set(self.data.frames))
        for at in range(self.config.start, self.config.end, 900):
            for position in list(self.positions.values()):
                self.advance(position, at, opening_only=True)
            tickers = []
            for symbol in symbols:
                series = self.data.frames[symbol]["15m"]
                ticker = series.ticker(at, symbol)
                if ticker is not None:
                    tickers.append(ticker)
            ranked = live.rank_market_tickers(self.data.metadata["exchange_info"], tickers,
                                             excluded_symbols=set(self.positions), allowed_symbols=set(symbols))
            signals = []
            for candidate in ranked:
                symbol = candidate["symbol"]
                if not all(series.history_complete(at) for series in self.data.frames[symbol].values()):
                    self.reject(["data_gap_history"])
                    continue
                decision = self.data.canonical(symbol, at, self.policy)
                self.decisions_evaluated += 1
                if not decision.get("entry_eligible"):
                    self.reject([decision.get("reason", "canonical_wait")])
                    continue
                signal = decision["analysis"]
                distance = abs(signal["entry"] - signal["stop_loss"]) / signal["entry"] * 100
                if distance > core.dynamic_stop_distance_pct(signal["entry"], None, self.policy):
                    self.reject(["stop_distance"])
                    continue
                signals.append((candidate, signal))
            signals.sort(key=lambda pair: (pair[0]["opportunity_score"], pair[1]["confidence"]), reverse=True)
            for candidate, signal in signals[:3]:
                await self.enter(candidate["symbol"], signal, at)
            for position in list(self.positions.values()):
                self.advance(position, at, opening_only=False)
        for position in self.positions.values():
            position.status = "OPEN_AT_END"
            position.funding_known = position.funding_known and self.data.funding_complete(
                position.symbol, position.opened_at, self.config.end - 1)
        rows = [position.row() for position in self.trades]
        summary = metrics(rows, self.config.initial_equity)
        summary["long_short"] = {direction: metrics([row for row in rows if row["direction"] == direction],
                                                    self.config.initial_equity) for direction in ("LONG", "SHORT")}
        return {
            "label": "CONDITIONAL BASELINE / KOŞULLU BASELINE",
            "period": {"start_inclusive": iso(self.config.start), "end_exclusive": iso(self.config.end)},
            "assumptions": [
                "Current exchangeInfo step/min-notional/tick rules, NOT historical.",
                "Current public maintenance brackets held constant, NOT historical.",
                f"Constant spread {self.config.spread_bps} bp; historical spread gate effectively untested.",
                f"MARKET modeled at following 15m open plus adverse {self.config.slippage_bps} bp slippage; full instantaneous fill.",
                "5 bp commission per side; native planned-spec initial risk remains immutable.",
                "One scan at each 15m boundary; 24h ticker reconstructed from CLOSED contract candles; native scan is 120s.",
                "Authorization/session readiness modeled continuously enabled; real 1h session renewals are outside this baseline.",
                "MARK_PRICE OHLC triggers; intrabar exits at trigger plus adverse slip; gaps at contract open plus slip.",
                "Price-protection divergence delays, API failures and order rejection are not simulated.",
                "Historical funding uses settlement-bar mark open and pre-exit remaining quantity; settlement-before-exit ordering.",
                "Intrabar exits timestamped at last second of bar; simultaneous closed-equity points grouped.",
                "No forced end close, BE, trailing, time stop or strategy change.",
                "Missing funding is NULL; main statistics exclude incomplete net/R trades.",
                "IID paired-trade bootstrap; confidence intervals do not correct serial dependence.",
                f"Initial simulated equity {self.config.initial_equity} USDT; isolated one-way account.",
            ],
            "intrabar": self.config.intrabar, "spread_bps": self.config.spread_bps,
            "slippage_bps": self.config.slippage_bps, "policy": self.policy,
            "summary": summary, "bootstrap": bootstrap(rows, self.config.bootstrap_samples, self.config.seed),
            "stage1_gate_rejections": dict(self.gate_counts), "stage1_gate_evaluations": dict(self.gate_evaluations),
            "all_rejections": dict(sorted(self.rejections.items())),
            "first_rejections": dict(sorted(self.first_rejections.items())),
            "canonical_decisions_evaluated": self.decisions_evaluated,
            "trades": rows,
        }


def run(data: Dataset, config: Config) -> dict[str, Any]:
    return asyncio.run(Engine(data, config).replay())
