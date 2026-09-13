"""Deterministic historical execution adapter for Phase 2 validation.

This module owns execution simulation only. Strategy decisions come from
``canonical_historical_decision`` and no exchange or network call is made.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any


@dataclass(frozen=True)
class ExecutionConfig:
    starting_equity: float = 10_000.0
    maker_fee_bps: float = 2.0
    taker_fee_bps: float = 5.0
    fee_side: str = "TAKER"
    spread_bps: float = 4.0
    slippage_bps: float = 2.0
    funding_bps_per_8h: float = 1.0
    funding_model: str = "CONFIGURED_FALLBACK"
    tick_size: float = 0.01
    quantity_step: float = 0.001
    min_quantity: float = 0.001
    min_notional: float = 5.0
    same_candle_policy: str = "STOP_FIRST"
    execution_timing: str = "NEXT_CANDLE_OPEN"
    max_positions: int = 1

    def fee_bps(self) -> float:
        return self.maker_fee_bps if self.fee_side.upper() == "MAKER" else self.taker_fee_bps

    def assumptions(self) -> dict[str, Any]:
        return {
            "fee_model": self.fee_side.upper(),
            "maker_fee_bps": self.maker_fee_bps,
            "taker_fee_bps": self.taker_fee_bps,
            "spread_bps": self.spread_bps,
            "slippage_bps": self.slippage_bps,
            "funding_model": self.funding_model,
            "funding_bps_per_8h": self.funding_bps_per_8h,
            "same_candle_policy": self.same_candle_policy,
            "execution_timing": self.execution_timing,
            "precision_source": "CONFIGURED_FALLBACK",
            "tick_size": self.tick_size,
            "quantity_step": self.quantity_step,
            "minimum_order_source": "CONFIGURED_FALLBACK",
            "min_quantity": self.min_quantity,
            "min_notional": self.min_notional,
            "liquidation_model": "UNSUPPORTED_FAIL_SAFE",
            "partial_fill_model": "FULL_FILL_WHEN_MINIMUMS_PASS",
        }


@dataclass
class SimulatedPosition:
    order_id: str
    symbol: str
    side: str
    requested_quantity: Decimal
    filled_quantity: Decimal
    entry_requested_price: Decimal
    entry_fill_price: Decimal
    entry_timestamp: int
    signal_timestamp: int
    stop_loss: Decimal
    take_profit: Decimal
    notional: Decimal
    entry_fee: Decimal
    entry_spread: Decimal
    entry_slippage: Decimal
    funding: Decimal = Decimal("0")


@dataclass
class SimulatedTrade:
    trade_id: str
    order_id: str
    symbol: str
    side: str
    entry_timestamp: int
    signal_timestamp: int
    entry_requested_price: Decimal
    entry_fill_price: Decimal
    exit_timestamp: int
    exit_reason: str
    exit_requested_price: Decimal
    exit_fill_price: Decimal
    quantity: Decimal
    gross_pnl: Decimal
    fees: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    funding_cost: Decimal
    net_pnl: Decimal
    return_pct: Decimal
    duration_seconds: int


class HistoricalExecutionSimulator:
    def __init__(self, config: ExecutionConfig | None = None) -> None:
        self.config = config or ExecutionConfig()
        self.equity = self._decimal(self.config.starting_equity)
        self.position: SimulatedPosition | None = None
        self.trades: list[SimulatedTrade] = []
        self.equity_curve: list[dict[str, Any]] = []
        self.rejections: list[dict[str, Any]] = []
        self._sequence = 0

    @staticmethod
    def _decimal(value: Any) -> Decimal:
        return value if isinstance(value, Decimal) else Decimal(str(value))

    @staticmethod
    def _round_down(value: Decimal, step: Decimal) -> Decimal:
        if step <= 0:
            return value
        return max(Decimal("0"), (value / step).to_integral_value(rounding=ROUND_DOWN) * step)

    def _price(self, value: Decimal, *, buy: bool) -> Decimal:
        tick = self._decimal(self.config.tick_size)
        if tick <= 0:
            return value
        rounding = ROUND_UP if buy else ROUND_DOWN
        return (value / tick).to_integral_value(rounding=rounding) * tick

    def _quantity(self, value: Decimal) -> Decimal:
        return self._round_down(value, self._decimal(self.config.quantity_step))

    def _fill(self, requested: Decimal, side: str, *, is_entry: bool) -> tuple[Decimal, Decimal, Decimal]:
        spread = requested * self._decimal(self.config.spread_bps) / Decimal("2") / Decimal("10000")
        slippage = requested * self._decimal(self.config.slippage_bps) / Decimal("10000")
        buy = (side == "LONG") == is_entry
        spread_price = requested + spread if buy else requested - spread
        final_price = spread_price + slippage if buy else spread_price - slippage
        return self._price(final_price, buy=buy), spread, slippage

    def _funding(self, position: SimulatedPosition, exit_timestamp: int) -> Decimal:
        elapsed = max(0, exit_timestamp - position.entry_timestamp)
        intervals = elapsed // (8 * 60 * 60)
        return position.notional * self._decimal(self.config.funding_bps_per_8h) / Decimal("10000") * intervals

    def _record_equity(self, timestamp: int, realized: Decimal = Decimal("0")) -> None:
        self.equity += realized
        self.equity_curve.append({"timestamp": timestamp, "equity": float(self.equity), "realized_pnl": float(realized)})

    def _reject(self, timestamp: int, reason: str) -> None:
        self.rejections.append({"timestamp": timestamp, "reason": reason})

    def open_from_decision(self, decision: dict[str, Any], candle: dict[str, Any], next_candle: dict[str, Any]) -> bool:
        if self.position is not None or decision.get("decision") not in {"BUY", "SELL"}:
            return False
        analysis = decision.get("analysis") or {}
        risk = decision.get("risk")
        if not risk:
            self._reject(int(candle["time"]), "MISSING_RISK_SIZE")
            return False
        requested_open = self._decimal(next_candle["open"])
        quantity = self._quantity(self._decimal(risk["notional_usdt"]) / requested_open)
        if quantity < self._decimal(self.config.min_quantity):
            self._reject(int(candle["time"]), "MINIMUM_ORDER_SIZE")
            return False
        side = "LONG" if decision["decision"] == "BUY" else "SHORT"
        requested = self._price(requested_open, buy=side == "LONG")
        fill, spread, slippage = self._fill(requested, side, is_entry=True)
        notional = fill * quantity
        if notional < self._decimal(self.config.min_notional):
            self._reject(int(candle["time"]), "MINIMUM_ORDER_SIZE")
            return False
        fee = notional * self._decimal(self.config.fee_bps()) / Decimal("10000")
        self._sequence += 1
        self.position = SimulatedPosition(
            order_id=f"BT-{self._sequence:08d}", symbol=str(decision["symbol"]), side=side,
            requested_quantity=quantity, filled_quantity=quantity, entry_requested_price=requested,
            entry_fill_price=fill, entry_timestamp=int(next_candle["time"]), signal_timestamp=int(decision["signal_timestamp"]),
            stop_loss=self._decimal(analysis["stop_loss"]), take_profit=self._decimal(analysis["tp1"]), notional=notional,
            entry_fee=fee, entry_spread=spread * quantity, entry_slippage=slippage * quantity,
        )
        return True

    def close_on_candle(self, candle: dict[str, Any]) -> SimulatedTrade | None:
        position = self.position
        if position is None:
            return None
        high, low = self._decimal(candle["high"]), self._decimal(candle["low"])
        stop_hit = low <= position.stop_loss if position.side == "LONG" else high >= position.stop_loss
        target_hit = high >= position.take_profit if position.side == "LONG" else low <= position.take_profit
        if stop_hit and target_hit:
            reason, requested = self.config.same_candle_policy, position.stop_loss
        elif stop_hit:
            reason, requested = "STOP", position.stop_loss
        elif target_hit:
            reason, requested = "TAKE_PROFIT", position.take_profit
        else:
            return None
        buy = position.side == "SHORT"
        requested = self._price(self._decimal(requested), buy=buy)
        fill, spread, slippage = self._fill(requested, position.side, is_entry=False)
        gross = (fill - position.entry_fill_price) * position.filled_quantity if position.side == "LONG" else (position.entry_fill_price - fill) * position.filled_quantity
        exit_fee = abs(fill * position.filled_quantity) * self._decimal(self.config.fee_bps()) / Decimal("10000")
        funding = self._funding(position, int(candle["time"]))
        spread_cost = position.entry_spread + spread * position.filled_quantity
        slippage_cost = position.entry_slippage + slippage * position.filled_quantity
        fees = position.entry_fee + exit_fee
        net = gross - fees - funding
        trade = SimulatedTrade(
            trade_id=position.order_id, order_id=position.order_id, symbol=position.symbol, side=position.side,
            entry_timestamp=position.entry_timestamp, signal_timestamp=position.signal_timestamp,
            entry_requested_price=position.entry_requested_price, entry_fill_price=position.entry_fill_price,
            exit_timestamp=int(candle["time"]), exit_reason=reason, exit_requested_price=requested,
            exit_fill_price=fill, quantity=position.filled_quantity, gross_pnl=gross, fees=fees,
            spread_cost=spread_cost, slippage_cost=slippage_cost, funding_cost=funding, net_pnl=net,
            return_pct=net / max(position.notional, Decimal("0.000000000001")) * 100,
            duration_seconds=max(0, int(candle["time"]) - position.entry_timestamp),
        )
        self.trades.append(trade)
        self._record_equity(int(candle["time"]), net)
        self.position = None
        return trade

    def close_at_market(self, candle: dict[str, Any], requested: float | None = None) -> SimulatedTrade | None:
        position = self.position
        if position is None:
            return None
        requested = self._price(self._decimal(requested if requested is not None else candle["close"]), buy=position.side == "SHORT")
        fill, spread, slippage = self._fill(requested, position.side, is_entry=False)
        gross = (fill - position.entry_fill_price) * position.filled_quantity if position.side == "LONG" else (position.entry_fill_price - fill) * position.filled_quantity
        exit_fee = abs(fill * position.filled_quantity) * self._decimal(self.config.fee_bps()) / Decimal("10000")
        funding = self._funding(position, int(candle["time"]))
        spread_cost = position.entry_spread + spread * position.filled_quantity
        slippage_cost = position.entry_slippage + slippage * position.filled_quantity
        fees = position.entry_fee + exit_fee
        net = gross - fees - funding
        trade = SimulatedTrade(
            trade_id=position.order_id, order_id=position.order_id, symbol=position.symbol, side=position.side,
            entry_timestamp=position.entry_timestamp, signal_timestamp=position.signal_timestamp,
            entry_requested_price=position.entry_requested_price, entry_fill_price=position.entry_fill_price,
            exit_timestamp=int(candle["time"]), exit_reason="TIMEOUT", exit_requested_price=requested,
            exit_fill_price=fill, quantity=position.filled_quantity, gross_pnl=gross, fees=fees,
            spread_cost=spread_cost, slippage_cost=slippage_cost, funding_cost=funding, net_pnl=net,
            return_pct=net / max(position.notional, Decimal("0.000000000001")) * 100,
            duration_seconds=max(0, int(candle["time"]) - position.entry_timestamp),
        )
        self.trades.append(trade)
        self._record_equity(int(candle["time"]), net)
        self.position = None
        return trade

    def result(self) -> dict[str, Any]:
        profits = [trade.net_pnl for trade in self.trades if trade.net_pnl > 0]
        losses = [trade.net_pnl for trade in self.trades if trade.net_pnl <= 0]
        peak = self.config.starting_equity
        max_drawdown = 0.0
        for point in self.equity_curve:
            peak = max(peak, point["equity"])
            max_drawdown = max(max_drawdown, peak - point["equity"])
        gross_profit = sum(profits)
        gross_loss = abs(sum(losses))
        return {
            "configuration": asdict(self.config), "assumptions": self.config.assumptions(),
            "trades": [{key: float(value) if isinstance(value, Decimal) else value for key, value in asdict(trade).items()} for trade in self.trades], "equity_curve": self.equity_curve,
            "rejections": self.rejections, "starting_equity": self.config.starting_equity,
            "ending_equity": float(self.equity), "net_pnl": float(self.equity - self._decimal(self.config.starting_equity)),
            "net_return_pct": float((self.equity / self._decimal(self.config.starting_equity) - 1) * 100),
            "gross_profit": float(gross_profit), "gross_loss": float(gross_loss),
            "win_rate": len(profits) / len(self.trades) * 100 if self.trades else 0.0,
            "number_of_trades": len(self.trades), "average_win": float(gross_profit / len(profits)) if profits else 0.0,
            "average_loss": float(sum(losses) / len(losses)) if losses else 0.0,
            "expectancy": float(sum(trade.net_pnl for trade in self.trades) / len(self.trades)) if self.trades else 0.0,
            "profit_factor": float(gross_profit / gross_loss) if gross_loss else (99.0 if gross_profit else 0.0),
            "maximum_drawdown": float(max_drawdown), "total_fees": float(sum(trade.fees for trade in self.trades)),
            "total_spread_cost": float(sum(trade.spread_cost for trade in self.trades)),
            "total_slippage_cost": float(sum(trade.slippage_cost for trade in self.trades)),
            "total_funding_cost": float(sum(trade.funding_cost for trade in self.trades)),
            "long_trades": sum(trade.side == "LONG" for trade in self.trades),
            "short_trades": sum(trade.side == "SHORT" for trade in self.trades),
        }


def run_realistic_backtest(
    candles_by_timeframe: dict[str, list[dict[str, Any]]],
    symbol: str,
    *,
    policy: dict[str, Any] | None = None,
    config: ExecutionConfig | None = None,
) -> dict[str, Any]:
    """Run the canonical historical decision stage through the execution adapter."""
    candles = candles_by_timeframe.get("15m", [])
    simulator = HistoricalExecutionSimulator(config)
    if len(candles) < 222:
        result = simulator.result()
        result["data_quality"] = {"valid": False, "warning": "INSUFFICIENT_15M_CANDLES"}
        return result
    from .main import canonical_historical_decision

    required = tuple(interval for interval in ("15m", "1h", "4h", "1d") if interval in candles_by_timeframe)
    if "15m" not in required:
        required = ("15m",)
    for index in range(220, len(candles) - 1):
        current = candles[index]
        simulator.close_on_candle(current)
        if simulator.position is not None:
            continue
        decision = canonical_historical_decision(
            symbol,
            candles_by_timeframe,
            int(current["time"]),
            required_intervals=required,
            policy=policy,
            account_context={
                "snapshot": {"positions": [], "open_orders": [], "hedge_mode": False},
                "daily": {}, "spread_bps": (config or ExecutionConfig()).spread_bps,
                "armed": True, "allowed_symbols": [symbol],
            } if policy is not None else None,
        )
        simulator.open_from_decision(decision, current, candles[index + 1])
    if simulator.position is not None:
        simulator.close_at_market(candles[-1])
    result = simulator.result()
    result["data_period"] = {"start": candles[0].get("time"), "end": candles[-1].get("time")}
    result["symbol"] = symbol
    result["timeframes"] = list(required)
    result["data_quality"] = {"valid": True, "warning": None}
    return result
