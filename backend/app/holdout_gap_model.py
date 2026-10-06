"""Exact gap adapter; native sizing, protective fills and exit rules are reused."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .backtest_ablation import (
    Entry, ExitPosition, Variant, advance_exit, finish_open, new_position, summarize,
)
from .backtest_baseline import Config, Engine, iso
from .backtest_data import Dataset, Series
from .backtest_diagnostics import utc
from holdout_cli import HoldoutDataset


@dataclass
class GapMarkSeries(Series):
    contract: Series
    gap_open: int

    def at(self, opening: int) -> dict | None:
        original = super().at(opening)
        if original is None and opening == self.gap_open:
            return self.contract.at(opening)
        return original


class GapDataset(HoldoutDataset):
    def __init__(self, native: Dataset, rule: dict):
        gap = rule["mark_bar_open"]
        marks = {symbol: GapMarkSeries(series.interval, series.rows,
                                      native.frames[symbol]["15m"], gap)
                 for symbol, series in native.marks.items()}
        super().__init__(native.frames, marks, native.funding, native.metadata,
                         native.report, native.funding_months)
        self.gap_rule = rule

    def canonical(self, symbol: str, at: int, policy: dict) -> dict:
        if at in self.gap_rule["blocked_native_decision_times"]:
            return blocked_decision()
        return super().canonical(symbol, at, policy)


def blocked_decision() -> dict:
    return {"entry_eligible": False, "decision": "WAIT", "reason": "REGISTERED_MARK_DATA_GAP",
            "reasons": ["REGISTERED_MARK_DATA_GAP"], "native_signal_calculated": False}


def annotate(row: dict, gap: int) -> dict:
    opening = int(utc(row["opened_at"]).timestamp())
    closing = int(utc(row["closed_at"]).timestamp()) if row["closed_at"] else None
    affected = opening <= gap and (closing is None or closing >= gap)
    return {**row, "gap_affected": affected,
            "data_quality": "GAP_AFFECTED" if affected else "COMPLETE",
            "gap_bar_opens": [iso(gap)] if affected else [],
            "gap_protective_exits": [exit for exit in row["exits"] if gap <= exit["time"] < gap + 900],
            "gap_trigger_source": "CONTRACT_OHLC" if affected else None}


@dataclass
class GapExitPosition(ExitPosition):
    gap_open: int = 0
    gap_stop_updates_deferred: list[int] = field(default_factory=list)

    def closed_update(self, data: Dataset, at: int) -> None:
        if at == self.gap_open or at - 900 == self.gap_open:
            if self.variant.be and at > self.opened_at and self.remaining:
                self.gap_stop_updates_deferred.append(at)
            return
        super().closed_update(data, at)

    def row(self) -> dict:
        result = annotate(super().row(), self.gap_open)
        result["gap_stop_updates_deferred"] = self.gap_stop_updates_deferred
        return result


def gap_position(entry: Entry, data: GapDataset, config: Config, variant: Variant) -> GapExitPosition:
    seed = new_position(entry, data, config, variant)
    return GapExitPosition(
        symbol=seed.symbol, direction=seed.direction, opened_at=seed.opened_at,
        spec=seed.spec, signal_identifier=seed.signal_identifier, slip=seed.slip,
        mark_entry=seed.mark_entry, market_open=seed.market_open,
        variant=seed.variant, tick=seed.tick, gap_open=data.gap_rule["mark_bar_open"])


def frozen_gap_replay(data: GapDataset, config: Config, entries: list[Entry], variant: Variant) -> dict:
    rows, excluded = [], []
    for entry in entries:
        if entry.at in data.gap_rule["blocked_native_decision_times"]:
            raise ValueError("Frozen entry violates preregistered gap entry embargo")
        if not variant.short_enabled and entry.reference["direction"] == "SHORT":
            excluded.append(entry.reference["signal_id"])
            continue
        engine = Engine(data, config)
        position = gap_position(entry, data, config, variant)
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
