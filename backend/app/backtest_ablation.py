"""Preregistered offline exits on a frozen native entry cohort."""

from __future__ import annotations

import copy
import math
from collections import Counter
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from . import analysis, execution_core as core, v25_execution as live
from .backtest_baseline import Config, Engine, LocalSpecClient, Position, bootstrap, iso, metrics
from .backtest_data import Dataset
from .backtest_diagnostics import clustered_bootstrap, exit_group, trade_set_changes, utc
from .binance_demo import floor_step, round_tick
from .trade_r_metrics import initial_entry_risk_usdt


@dataclass(frozen=True)
class Variant:
    name: str = "A"
    be: bool = False
    atr_multiplier: float | None = None
    tp1_fraction: float = 0.60
    short_enabled: bool = True
    time_bars: int | None = None

    def __post_init__(self) -> None:
        if self.atr_multiplier not in (None, 2.0, 3.0) or self.tp1_fraction not in (0.40, 0.50, 0.60):
            raise ValueError("Unregistered ATR/TP1 parameter")
        if self.time_bars not in (None, 8, 16, 32):
            raise ValueError("Unregistered time-stop parameter")
        if self.time_bars is not None and (self.be or self.atr_multiplier is not None or self.tp1_fraction != 0.60):
            raise ValueError("Time-stop experiment must be isolated")


EXIT_VARIANTS = (
    Variant(), Variant("B", be=True), Variant("C_ATR2", atr_multiplier=2.0),
    Variant("C_ATR3", atr_multiplier=3.0), Variant("D_ATR2", be=True, atr_multiplier=2.0),
    Variant("D_ATR3", be=True, atr_multiplier=3.0), Variant("E40", tp1_fraction=0.40),
    Variant("E50", tp1_fraction=0.50), Variant("E60"), Variant("G_NO_SHORT", short_enabled=False),
)
TIME_VARIANTS = tuple(Variant(f"F{bars}", time_bars=bars) for bars in (8, 16, 32))
PRESETS = {variant.name: variant for variant in EXIT_VARIANTS + TIME_VARIANTS}


@dataclass(frozen=True)
class Entry:
    reference: dict
    spec: dict
    signal: dict
    planned_notional: float
    tick: Decimal
    at: int


async def prepare_entries(data: Dataset, config: Config, reference: dict) -> list[Entry]:
    engine = Engine(data, config)
    entries = []
    previous = config.start
    seen = set()
    for row in reference["trades"]:
        at = int(utc(row["opened_at"]).timestamp())
        if not previous <= at < config.end or row["signal_id"] in seen:
            raise ValueError("Baseline entries must be ordered, unique and within the period")
        previous = at
        seen.add(row["signal_id"])
        decision = data.canonical(row["symbol"], at, engine.policy)
        if not decision.get("entry_eligible"):
            raise ValueError(f"Frozen entry is not native eligible: {row['signal_id']}")
        signal = decision["analysis"]
        if signal["direction"] != row["direction"]:
            raise ValueError("Frozen direction differs from native decision")
        risk = core.risk_sized_order(signal["entry"], signal["stop_loss"], engine.policy, atr=signal.get("atr"))
        contract = data.frames[row["symbol"]]["15m"].at(at)
        if contract is None:
            raise ValueError("Frozen entry contract candle is missing")
        client = LocalSpecClient(data.metadata["exchange_info"], contract["open"])
        order = live.LiveOrderRequest(
            symbol=row["symbol"], direction=row["direction"], order_type="MARKET",
            margin_usdt=risk["margin_usdt"], leverage=risk["leverage"], stop_loss=signal["stop_loss"],
            tp1=signal["tp1"], tp2=signal["tp2"], tp3=signal["tp3"], atr=signal.get("atr"))
        spec = await live.build_live_spec(client, order, engine.policy, allowed_symbols=engine.policy["allowed_symbols"])
        live.validate_protection_readiness(spec, engine.policy)
        rules = await live.live_symbol_rules(client, row["symbol"], "MARKET")
        for key, value in (
            ("quantity", float(spec["quantity"])), ("expected_entry", float(spec["entry_price"])),
            ("stop", float(spec["stop_loss"])), ("tp1", float(spec["targets"][0])),
            ("tp3", float(spec["targets"][2])), ("initial_risk_usdt", initial_entry_risk_usdt(spec)),
        ):
            if row[key] != value:
                raise ValueError(f"Frozen native spec mismatch: {row['signal_id']} / {key}")
        entries.append(Entry(row, spec, signal, risk["margin_usdt"] * risk["leverage"], rules["tick"], at))
    return entries


@dataclass
class ExitPosition(Position):
    variant: Variant = field(default_factory=Variant)
    tick: Decimal = Decimal("0.01")
    initial_stop: Decimal = field(init=False)
    tp1_at: int | None = None
    peak: Decimal | None = None
    best_progress_r: Decimal = Decimal(0)
    time_due: bool = False
    stop_updates: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.initial_stop = Decimal(str(self.spec["stop_loss"]))
        if self.variant.tp1_fraction != 0.60:
            partial = floor_step(self.quantity * Decimal(str(self.variant.tp1_fraction)), self.spec["step"])
            self.tp1_quantity = partial if partial >= self.spec["min_qty"] and partial * self.mark_entry >= self.spec["min_notional"] else Decimal(0)

    def fill(self, expected: Decimal, quantity: Decimal, at: int, reason: str) -> None:
        super().fill(expected, quantity, at, reason)
        if reason == "TP1":
            self.tp1_at = at

    def fee_break_even(self) -> Decimal:
        fee = Decimal("0.0005")
        if self.sign == 1:
            raw = self.actual_entry * (1 + fee) / ((1 - self.slip) * (1 - fee))
        else:
            raw = self.actual_entry * (1 - fee) / ((1 + self.slip) * (1 + fee))
        rounded = round_tick(raw, self.tick)
        if self.sign * (rounded - raw) < 0:
            rounded += self.sign * self.tick
        return rounded

    def closed_update(self, data: Dataset, at: int) -> None:
        if at <= self.opened_at or self.remaining == 0:
            return
        prior = at - 900
        mark = data.marks[self.symbol].at(prior)
        if mark is None:
            raise ValueError("Closed-candle exit update mark is missing")
        if self.variant.time_bars is not None:
            favorable = Decimal(str(mark["high"] if self.sign == 1 else mark["low"]))
            if self.initial_risk is None or self.initial_risk <= 0:
                raise ValueError("Time stop requires immutable initial risk")
            self.best_progress_r = max(self.best_progress_r,
                                       self.sign * (favorable - self.actual_entry) * self.quantity / Decimal(str(self.initial_risk)))
            if (at - self.opened_at) // 900 >= self.variant.time_bars and self.best_progress_r < Decimal("0.3"):
                self.time_due = True
        if not self.tp1_hit or not (self.variant.be or self.variant.atr_multiplier is not None):
            return
        candidates, causes = [], []
        atr_value = None
        if self.variant.be:
            candidates.append(self.fee_break_even())
            causes.append("BE_FEE_AND_EXIT_SLIP")
        if self.variant.atr_multiplier is not None:
            favorable = Decimal(str(mark["high"] if self.sign == 1 else mark["low"]))
            self.peak = favorable if self.peak is None else max(self.peak, favorable) if self.sign == 1 else min(self.peak, favorable)
            rows = data.frames[self.symbol]["15m"].closed(at)
            atr_value = analysis.atr([row["high"] for row in rows], [row["low"] for row in rows],
                                     [row["close"] for row in rows])
            if not math.isfinite(atr_value) or atr_value < 0:
                raise ValueError("Invalid native closed-candle ATR")
            if atr_value > 0:
                raw = self.peak - self.sign * Decimal(str(self.variant.atr_multiplier)) * Decimal(str(atr_value))
                if raw > 0:
                    candidates.append(round_tick(raw, self.tick))
                    causes.append("CLOSED_CHANDELIER")
                else:
                    causes.append("NONPOSITIVE_TRAILING_CANDIDATE")
            else:
                causes.append("ATR_UNAVAILABLE_ZERO")
        old = Decimal(str(self.spec["stop_loss"]))
        new = max([old, *candidates]) if self.sign == 1 else min([old, *candidates])
        if new != old or not candidates:
            self.stop_updates.append({"applied_at": iso(at), "source_bar_open": iso(prior),
                                      "old_stop": float(old), "new_stop": float(new), "causes": causes,
                                      "atr": atr_value, "peak": float(self.peak) if self.peak is not None else None})
        self.spec["stop_loss"] = str(new)

    def advance(self, contract: dict, mark: dict, ordering: str, *, opening_only: bool) -> None:
        super().advance(contract, mark, ordering, opening_only=opening_only)
        if opening_only and self.time_due and self.remaining:
            self.fill(Decimal(str(contract["open"])), self.remaining, int(contract["time"]), "TIME_STOP")

    def row(self) -> dict[str, Any]:
        return {**super().row(), "variant": self.variant.name, "initial_stop": float(self.initial_stop),
                "stop_updates": self.stop_updates, "time_stop_progress_r": float(self.best_progress_r),
                "configured_tp1_fraction": self.variant.tp1_fraction}


def new_position(entry: Entry, data: Dataset, config: Config, variant: Variant) -> ExitPosition:
    contract, mark = data.frames[entry.reference["symbol"]]["15m"].at(entry.at), data.marks[entry.reference["symbol"]].at(entry.at)
    if contract is None or mark is None:
        raise ValueError("Frozen opening price/mark missing")
    position = ExitPosition(
        entry.reference["symbol"], entry.reference["direction"], entry.at, copy.deepcopy(entry.spec),
        entry.reference["signal_id"], Decimal(str(config.slippage_bps)) / 10000,
        Decimal(str(mark["open"])), Decimal(str(contract["open"])), variant=variant, tick=entry.tick)
    if position.initial_risk != entry.reference["initial_risk_usdt"] or float(position.actual_entry) != entry.reference["actual_fill_price"]:
        raise ValueError("Frozen actual fill/initial risk differs")
    return position


def advance_exit(engine: Engine, position: ExitPosition, at: int) -> None:
    position.closed_update(engine.data, at)
    if at > position.opened_at:
        engine.advance(position, at, opening_only=True)
    if position.status == "OPEN":
        engine.advance(position, at, opening_only=False)


def finish_open(engine: Engine) -> None:
    for position in engine.positions.values():
        position.status = "OPEN_AT_END"
        position.funding_known = position.funding_known and engine.data.funding_complete(
            position.symbol, position.opened_at, engine.config.end - 1)


def summarize(rows: list[dict], config: Config) -> dict:
    result = metrics(rows, config.initial_equity)
    result["long_short"] = {direction: metrics([row for row in rows if row["direction"] == direction],
                                              config.initial_equity) for direction in ("LONG", "SHORT")}
    groups = Counter()
    for row in rows:
        category = "TIME_STOP" if row["exits"] and row["exits"][-1]["reason"] == "TIME_STOP" else exit_group(row)
        groups[category] += 1
    return {"summary": result, "bootstrap": bootstrap(rows, config.bootstrap_samples, config.seed),
            "exit_groups": {key: groups[key] for key in (
                "STOP_BEFORE_TP1", "TP1_THEN_STOP", "TP3", "TIME_STOP", "OTHER_OR_UNCLOSED")}}


def frozen_replay(data: Dataset, config: Config, entries: list[Entry], variant: Variant) -> dict:
    rows = []
    excluded = []
    for entry in entries:
        if not variant.short_enabled and entry.reference["direction"] == "SHORT":
            excluded.append(entry.reference["signal_id"])
            continue
        engine = Engine(data, config)
        position = new_position(entry, data, config, variant)
        engine.positions[position.symbol] = position
        engine.cash -= float(position.commission)
        for at in range(entry.at, config.end, 900):
            advance_exit(engine, position, at)
            if position.status != "OPEN":
                break
        finish_open(engine)
        rows.append(position.row())
    return {"cohort_mode": "FROZEN_INDEPENDENT_TRADES_NO_PORTFOLIO_GATES",
            "variant": asdict(variant), "intrabar": config.intrabar,
            "intentional_direction_exclusions": excluded, "trades": rows, **summarize(rows, config)}


def scheduled_replay(data: Dataset, config: Config, entries: list[Entry], variant: Variant) -> dict:
    engine = Engine(data, config)
    by_time: dict[int, list[Entry]] = {}
    for entry in entries:
        by_time.setdefault(entry.at, []).append(entry)
    rejected = []
    intentional = []
    for at in range(config.start, config.end, 900):
        for position in list(engine.positions.values()):
            if not isinstance(position, ExitPosition):
                raise TypeError("Scheduled ablation requires offline exit positions")
            position.closed_update(data, at)
            engine.advance(position, at, opening_only=True)
        for entry in by_time.get(at, []):
            if not variant.short_enabled and entry.reference["direction"] == "SHORT":
                intentional.append(entry.reference["signal_id"])
                continue
            failures = []
            for notional in (0, entry.planned_notional, entry.spec["notional_usdt"]):
                failures = engine.gate_failures(entry.reference["symbol"], entry.signal, at, notional)
                if failures:
                    break
            if not failures and engine.snapshot(at)["available_balance"] < entry.spec["margin_usdt"]:
                failures = ["available_balance"]
            if failures:
                engine.reject(failures)
                rejected.append({"signal_id": entry.reference["signal_id"], "reasons": failures})
                continue
            position = new_position(entry, data, config, variant)
            engine.positions[position.symbol] = position
            engine.trades.append(position)
            engine.cash -= float(position.commission)
            engine.events.insert(0, {"kind": "LIVE_ENTRY", "created_at": iso(at), "plan_id": position.signal_identifier})
        for position in list(engine.positions.values()):
            engine.advance(position, at, opening_only=False)
    finish_open(engine)
    rows = [position.row() for position in engine.trades]
    return {"cohort_mode": "SCHEDULED_BASELINE_ENTRIES_WITH_NATIVE_PORTFOLIO_GATES",
            "trades": rows, "rejected_entries": rejected, "intentional_direction_exclusions": intentional,
            "gate_rejections": dict(engine.rejections), **summarize(rows, config)}


def paired_difference(rows: list[dict], baseline: list[dict], config: Config) -> dict:
    current = {row["signal_id"]: row for row in rows}
    if len(current) != len(rows) or len({row["signal_id"] for row in baseline}) != len(baseline):
        raise ValueError("Duplicate paired signal IDs")
    pairs, incomplete = [], []
    for old in baseline:
        new = current.get(old["signal_id"])
        if new is None:
            continue
        for key in ("symbol", "direction", "opened_at", "actual_fill_price", "quantity", "initial_risk_usdt"):
            if new[key] != old[key]:
                raise ValueError(f"Pair has changed entry/risk: {old['signal_id']} / {key}")
        if any(row["net_r"] is None or row["net_pnl"] is None for row in (old, new)):
            incomplete.append(old["signal_id"])
            continue
        pairs.append({"signal_id": old["signal_id"], "opened_at": old["opened_at"],
                      "baseline_net_r": old["net_r"], "variant_net_r": new["net_r"],
                      "delta_net_r": new["net_r"] - old["net_r"],
                      "delta_net_pnl": new["net_pnl"] - old["net_pnl"]})
    observations = [{"status": "CLOSED", "opened_at": row["opened_at"],
                     "net_r": row["delta_net_r"], "net_pnl": row["delta_net_pnl"]} for row in pairs]
    iid = bootstrap(observations, config.bootstrap_samples, config.seed)
    day = clustered_bootstrap(observations, config, "DAY")
    return {"matched_complete_pairs": len(pairs), "unmatched_current": len(set(current) - {r["signal_id"] for r in baseline}),
            "unmatched_baseline": len(baseline) - sum(row["signal_id"] in current for row in baseline),
            "incomplete_pair_ids": incomplete, "pairs": pairs,
            "mean_delta_net_r": sum(row["delta_net_r"] for row in pairs) / len(pairs) if pairs else None,
            "mean_delta_net_pnl": sum(row["delta_net_pnl"] for row in pairs) / len(pairs) if pairs else None,
            "trade_delta_r_95": iid["expectancy_r_95"], "day_delta_r_95": day["expectancy_r_95"],
            "day_bootstrap_status": day["status"], "day_empty_resamples": day["empty_resamples"],
            "seed": config.seed, "samples": config.bootstrap_samples}


def verify_control(result: dict, reference: dict) -> None:
    rows = result["trades"]
    if len(rows) != len(reference["trades"]):
        raise ValueError("A control trade count differs")
    for new, old in zip(rows, reference["trades"]):
        fields = {key: value for key, value in old.items() if key != "variant"}
        if {key: new[key] for key in fields} != fields:
            raise ValueError(f"A control trade differs: {old['signal_id']}")
    for key in ("summary", "bootstrap"):
        if result[key] != reference[key]:
            raise ValueError(f"A control differs: {key}")


def portfolio_changes(rows: list[dict], baseline: list[dict], config: Config) -> dict:
    return trade_set_changes(rows, baseline, config)
