"""Single-candidate exploratory screening, NOT Engine/facade or a gated account.

Each independent trade has 3 USDT initial risk (1R); 1000 USDT is a reference,
not an equity curve, budget or compounding rule. Daily limits, exposure, margin,
stop caps, exchange filters and liquidation gates are NOT applied. Only one
position per symbol is allowed. Protective prices use CONTRACT 15m OHLC, not
native MARK protection. No BE, spread charge, bootstrap or forced final exit.

ATR includes the closed decision candle, per the user's explicit choice. The
current and previous channels exclude their respective decision candles.
ATR calls analysis.atr on the entire contiguous CLOSED 4h prefix, resetting
after gaps; there is no 259-bar window. Warmup is max(N+2, ATR+1)=22 bars.
The next 15m open anchors entry/Stop/targets, then adverse slippage is applied.

Future timestamp gaps are offline data-quality exclusions, NOT live signals:
72 hours before contract 4h/15m, mark 15m and native funding gaps. Funding gaps
use elapsed > right interval +60 seconds, starting at the left settlement.
Missing funding months/leading/trailing coverage also fail closed. Unknown and
open trades never contribute to N or performance. Symbol/month outputs contain
COUNTS ONLY. Existing Original/LIVE/Demo and holdout protocols are untouched.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from statistics import fmean, median
from typing import Any

from app.analysis import atr
from app.backtest_data import Dataset, Series

MIN_N, MIN_NET_R, MIN_PF = 150, 0.0, 1.15
N, ATR_PERIOD, K, WARMUP = 20, 14, 1.4, 22
STEP, SIGNAL_STEP, BLACKOUT = 900, 14400, 72 * 3600
RISK, FEE, SLIP, PARTIAL = Decimal(3), Decimal("0.0005"), Decimal("0.0003"), Decimal("0.60")
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT")
VIEW = Path(r"C:\Users\ahmtt\kaistrade-data\measurement-view-2020-10_2022-08")
OUTPUT = Path(r"C:\Users\ahmtt\kaistrade-data\screening-results")
METADATA_SHA = "4153972de075f9372640632621a3153d50350d721f4efe979a869fe821a5ab57"
SCHEMA = "donchian-4h-independent-screen-v1"
DISCLAIMER = (
    "EXPLORATORY_NOT_ENGINE_OR_FACADE; INDEPENDENT_1R_TRADES; "
    "NO_ACCOUNT_DAILY_EXPOSURE_MARGIN_CAP_EXCHANGE_OR_LIQUIDATION_GATES; "
    "1000_USDT_REFERENCE_ONLY_NO_COMPOUNDING; CONTRACT_15M_PROTECTION; "
    "FUTURE_GAP_INFORMATION_NOT_LIVE; NO_BOOTSTRAP"
)


class ScreeningError(ValueError):
    """Explicit failure; never repair inputs or retry a run."""


def require(ok: bool, message: str) -> None:
    if not ok:
        raise ScreeningError(message)


def stamp(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def month(at: float) -> str:
    return stamp(at)[:7]


@dataclass(frozen=True)
class Phase:
    name: str
    start: int
    end: int


PHASES = (
    Phase("TRAIN", 1601510400, 1640995200),
    Phase("VALIDATION", 1640995200, 1661990400),
)


@dataclass(frozen=True)
class Observation:
    direction: str | None
    atr_value: float | None
    reason: str


def observe(series: Series, at: int) -> Observation:
    require(series.interval == "4h" and at % SIGNAL_STEP == 0, "Invalid 4h decision clock")
    rows = series.closed(at, limit=len(series.rows))
    if not rows or rows[-1]["time"] != at - SIGNAL_STEP:
        return Observation(None, None, "missing_closed_4h")
    low = len(rows) - 1
    while low and rows[low]["time"] - rows[low - 1]["time"] == SIGNAL_STEP:
        low -= 1
    rows = rows[low:]
    if len(rows) < WARMUP:
        return Observation(None, None, "warmup_4h")
    current, previous = rows[-N - 1:-1], rows[-N - 2:-2]
    upper, lower = max(row["high"] for row in current), min(row["low"] for row in current)
    old_upper, old_lower = max(row["high"] for row in previous), min(row["low"] for row in previous)
    if upper <= lower or old_upper <= old_lower:
        return Observation(None, None, "zero_width_channel")
    close, old_close = rows[-1]["close"], rows[-2]["close"]
    direction = "LONG" if close > upper else "SHORT" if close < lower else None
    if direction is None:
        return Observation(None, None, "no_breakout")
    if (direction == "LONG" and old_close > old_upper) or (direction == "SHORT" and old_close < old_lower):
        return Observation(None, None, "already_outside")
    value = atr([row["high"] for row in rows], [row["low"] for row in rows],
                [row["close"] for row in rows], period=ATR_PERIOD)
    require(math.isfinite(value) and value > 0, "Invalid closed 4h ATR")
    return Observation(direction, value, "first_cross")


@dataclass(frozen=True)
class Gap:
    kind: str
    start: float
    end: float


def series_gaps(series: Series, duration: int, phase: Phase, kind: str) -> list[Gap]:
    times = series.times
    require(all(type(at) is int and at % duration == 0 for at in times), "Unaligned series")
    require(all(left < right for left, right in pairwise(times)), "Unordered series")
    start = min(phase.start, times[0]) if times else phase.start
    end = max(phase.end, times[-1] + duration) if times else phase.end
    expected, gaps = start, []
    for at in times:
        if at > expected:
            gaps.append(Gap(kind, expected, at))
        expected = at + duration
    if expected < end:
        gaps.append(Gap(kind, expected, end))
    return gaps


def funding_gaps(data: Dataset, symbol: str, phase: Phase) -> list[Gap]:
    rows = data.funding[symbol]
    if not rows:
        return [Gap("funding", phase.start, phase.end)]
    require(all(math.isfinite(row["time"]) and math.isfinite(row["rate"])
                and math.isfinite(row["interval_hours"]) and row["interval_hours"] > 0 for row in rows),
            "Invalid funding schedule")
    gaps = []
    for left, right in pairwise(rows):
        elapsed = right["time"] - left["time"]
        require(elapsed > 0, "Unordered funding schedule")
        if elapsed > right["interval_hours"] * 3600 + 60:
            gaps.append(Gap("funding", left["time"], right["time"]))
    leading = rows[0]["time"] - rows[0]["interval_hours"] * 3600 - 60
    trailing = rows[-1]["time"] + rows[-1]["interval_hours"] * 3600 + 60
    if leading > phase.start:
        gaps.append(Gap("funding", phase.start, min(leading, phase.end)))
    if trailing < phase.end:
        gaps.append(Gap("funding", max(trailing, phase.start), phase.end))
    cursor = datetime.fromtimestamp(phase.start, timezone.utc).replace(day=1, hour=0, minute=0, second=0)
    while cursor.timestamp() < phase.end:
        following = cursor.replace(year=cursor.year + 1, month=1) if cursor.month == 12 else cursor.replace(month=cursor.month + 1)
        if cursor.strftime("%Y-%m") not in data.funding_months[symbol]:
            gaps.append(Gap("funding", cursor.timestamp(), following.timestamp()))
        cursor = following
    return gaps


def inventory(data: Dataset, symbol: str, phase: Phase) -> list[Gap]:
    return [
        *series_gaps(data.frames[symbol]["4h"], SIGNAL_STEP, phase, "contract_4h"),
        *series_gaps(data.frames[symbol]["15m"], STEP, phase, "contract_15m"),
        *series_gaps(data.marks[symbol], STEP, phase, "mark"),
        *funding_gaps(data, symbol, phase),
    ]


def blackout(gaps: list[Gap], at: int) -> bool:
    return any(gap.start - BLACKOUT <= at < gap.start for gap in gaps)


def missing(gaps: list[Gap], at: int, *, position: bool) -> list[str]:
    return sorted({gap.kind for gap in gaps if (not position or gap.kind != "contract_4h")
                   and gap.start < at + STEP and at < gap.end})


@dataclass
class ScreenPosition:
    symbol: str
    direction: str
    opened_at: int
    entry: Decimal
    stop: Decimal
    targets: tuple[Decimal, Decimal, Decimal]
    quantity: Decimal = field(init=False)
    remaining: Decimal = field(init=False)
    actual_entry: Decimal = field(init=False)
    commission: Decimal = field(init=False)
    gross: Decimal = Decimal(0)
    frictionless: Decimal = Decimal(0)
    funding: Decimal = Decimal(0)
    tp1_hit: bool = False
    status: str = "OPEN"
    closed_at: int | None = None
    uncertainty: list[str] = field(default_factory=list)
    exits: list[tuple[str, Decimal, int]] = field(default_factory=list)

    def __post_init__(self) -> None:
        require(self.direction in ("LONG", "SHORT"), "Invalid position direction")
        require(self.entry > 0 and self.stop > 0 and self.sign * (self.entry - self.stop) > 0,
                "Invalid independent Stop")
        require(all(value > 0 for value in self.targets)
                and all(self.sign * (right - left) > 0 for left, right in zip((self.entry, *self.targets), self.targets)),
                "Invalid independent targets")
        self.quantity = self.remaining = RISK / abs(self.entry - self.stop)
        self.actual_entry = self.entry * (1 + self.sign * SLIP)
        self.commission = self.quantity * self.actual_entry * FEE

    @property
    def sign(self) -> int:
        return 1 if self.direction == "LONG" else -1

    @property
    def net(self) -> Decimal:
        return self.gross - self.commission + self.funding

    def fill(self, expected: Decimal, quantity: Decimal, at: int, reason: str) -> None:
        actual = expected * (1 - self.sign * SLIP)
        self.gross += quantity * self.sign * (actual - self.actual_entry)
        self.frictionless += quantity * self.sign * (expected - self.entry)
        self.commission += quantity * actual * FEE
        self.remaining -= quantity
        self.exits.append((reason, quantity, at))
        if self.remaining == 0:
            self.status, self.closed_at = "CLOSED", at

    def advance(self, contract: dict[str, Any], *, opening_only: bool) -> None:
        at = int(contract["time"]) + (0 if opening_only else STEP - 1)
        adverse = Decimal(str(contract["open"] if opening_only else contract["low"] if self.sign == 1 else contract["high"]))
        favorable = Decimal(str(contract["open"] if opening_only else contract["high"] if self.sign == 1 else contract["low"]))
        opening = Decimal(str(contract["open"])) if opening_only else None
        stop_hit = self.sign * (adverse - self.stop) <= 0
        if stop_hit:
            self.fill(opening if opening is not None else self.stop, self.remaining, at, "STOP")
            return
        if not self.tp1_hit and self.sign * (favorable - self.targets[0]) >= 0:
            self.fill(opening if opening is not None else self.targets[0], self.quantity * PARTIAL, at, "TP1")
            self.tp1_hit = True
        if self.remaining and self.sign * (favorable - self.targets[2]) >= 0:
            self.fill(opening if opening is not None else self.targets[2], self.remaining, at, "TP3")


def create_position(symbol: str, at: int, opening: float, observation: Observation) -> ScreenPosition | None:
    from app.main import v20_target_plan

    require(observation.direction is not None and observation.atr_value is not None, "Missing signal levels")
    sign = 1 if observation.direction == "LONG" else -1
    stop = opening - sign * K * observation.atr_value
    if stop <= 0:
        return None
    levels = v20_target_plan(opening, stop, observation.direction)
    if any(levels[key] <= 0 for key in ("tp1", "tp2", "tp3")):
        return None
    return ScreenPosition(symbol, observation.direction, at, Decimal(str(opening)), Decimal(str(stop)),
                          tuple(Decimal(str(levels[key])) for key in ("tp1", "tp2", "tp3")))


def settle(data: Dataset, position: ScreenPosition, at: int, times: list[float], *, opening_only: bool) -> None:
    low = bisect.bisect_left(times, at) if opening_only else bisect.bisect_right(times, at)
    high = bisect.bisect_right(times, at) if opening_only else bisect.bisect_left(times, at + STEP)
    for event in data.funding[position.symbol][low:high]:
        if event["time"] <= position.opened_at:
            continue
        mark = data.marks[position.symbol].at(int(event["time"] // STEP * STEP))
        require(mark is not None, "Funding mark unexpectedly unavailable after gap check")
        position.funding += -position.sign * position.remaining * Decimal(str(mark["open"])) * Decimal(str(event["rate"]))


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    offset = (len(ordered) - 1) * fraction
    low = math.floor(offset)
    return ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (offset - low)


def metrics(trades: list[ScreenPosition]) -> dict[str, Any]:
    verified = [trade for trade in trades if trade.status == "CLOSED"]
    net = [float(trade.net) for trade in verified]
    gains, losses = sum(max(0.0, value) for value in net), -sum(min(0.0, value) for value in net)
    pf = gains / losses if losses else None
    status = "DEFINED" if losses else "NO_LOSSES" if gains else "UNDEFINED"
    hours = [(trade.closed_at - trade.opened_at) / 3600 for trade in verified]
    return {
        "N": len(verified), "uncertain": sum(trade.status == "UNKNOWN" for trade in trades),
        "open_at_end": sum(trade.status == "OPEN_AT_END" for trade in trades), "accepted": len(trades),
        "mean_gross_r": fmean(float(trade.frictionless / RISK) for trade in verified) if verified else None,
        "mean_net_r": fmean(float(trade.net / RISK) for trade in verified) if verified else None,
        "usdt_pf": pf, "pf_status": status,
        "win_rate": sum(value > 0 for value in net) / len(net) if net else None,
        "duration_hours": {"median": median(hours) if hours else None, "p90": percentile(hours, 0.9),
                           "maximum": max(hours) if hours else None},
        "mean_cost_r": {
            "commission": fmean(float(trade.commission / RISK) for trade in verified) if verified else None,
            "slippage": fmean(float((trade.frictionless - trade.gross) / RISK) for trade in verified) if verified else None,
            "funding": fmean(float(-trade.funding / RISK) for trade in verified) if verified else None,
        },
    }


def phase_passes(value: dict[str, Any]) -> bool:
    pf_ok = value["usdt_pf"] is not None and value["usdt_pf"] > MIN_PF
    pf_ok = pf_ok or value["pf_status"] == "NO_LOSSES"
    return value["N"] >= MIN_N and value["mean_net_r"] is not None and value["mean_net_r"] > MIN_NET_R and pf_ok


def decision(phases: dict[str, dict[str, Any]]) -> str:
    require(set(phases) == {"TRAIN", "VALIDATION"}, "Both independent phases required")
    return "DEVAM" if all(phase_passes(value["metrics"]) for value in phases.values()) else "BIRAK"


def run_phase(data: Dataset, phase: Phase) -> dict[str, Any]:
    require(phase.start < phase.end and phase.start % STEP == 0 and phase.end % STEP == 0, "Invalid phase")
    symbols = sorted(data.frames)
    gaps = {symbol: inventory(data, symbol, phase) for symbol in symbols}
    funding_times = {symbol: [event["time"] for event in data.funding[symbol]] for symbol in symbols}
    positions: dict[str, ScreenPosition] = {}
    trades: list[ScreenPosition] = []
    rejections: Counter[str] = Counter()
    first_crosses = skipped = 0

    def finish(position: ScreenPosition) -> None:
        if position.status == "CLOSED" and not data.funding_complete(position.symbol, position.opened_at, position.closed_at):
            position.status = "UNKNOWN"
            position.uncertainty = ["funding_incomplete"]
        if position.status != "OPEN":
            del positions[position.symbol]

    for at in range(phase.start, phase.end, STEP):
        for symbol in symbols:
            contract, mark = data.frames[symbol]["15m"].at(at), data.marks[symbol].at(at)
            unavailable = missing(gaps[symbol], at, position=True)
            position = positions.get(symbol)
            if position is not None:
                if contract is None or mark is None or unavailable:
                    position.status, position.closed_at = "UNKNOWN", at
                    position.uncertainty = unavailable or ["missing_15m"]
                    finish(position)
                else:
                    settle(data, position, at, funding_times[symbol], opening_only=True)
                    position.advance(contract, opening_only=True)
                    finish(position)
            if at % SIGNAL_STEP == 0:
                observation = observe(data.frames[symbol]["4h"], at)
                if observation.direction is not None:
                    first_crosses += 1
                    if symbol in positions:
                        skipped += 1
                    elif blackout(gaps[symbol], at):
                        rejections["gap_blackout"] += 1
                    elif contract is None or mark is None or missing(gaps[symbol], at, position=False):
                        rejections["missing_entry_data"] += 1
                    else:
                        position = create_position(symbol, at, contract["open"], observation)
                        if position is None:
                            rejections["invalid_positive_levels"] += 1
                        else:
                            positions[symbol] = position
                            trades.append(position)
                else:
                    rejections[observation.reason] += 1
            position = positions.get(symbol)
            if position is not None:
                settle(data, position, at, funding_times[symbol], opening_only=False)
                position.advance(contract, opening_only=False)
                finish(position)
    for position in positions.values():
        position.status = "OPEN_AT_END"
    counts = lambda rows: {
        "accepted": len(rows), "N": sum(trade.status == "CLOSED" for trade in rows),
        "uncertain": sum(trade.status == "UNKNOWN" for trade in rows),
        "open_at_end": sum(trade.status == "OPEN_AT_END" for trade in rows),
    }
    months = sorted({month(at) for at in range(phase.start, phase.end, 86400)})
    return {
        "period": {"start": stamp(phase.start), "end_exclusive": stamp(phase.end)},
        "metrics": metrics(trades), "first_crosses": first_crosses, "overlap_skipped": skipped,
        "entry_rejections": dict(sorted(rejections.items())),
        "by_symbol_counts": {symbol: counts([trade for trade in trades if trade.symbol == symbol]) for symbol in symbols},
        "by_entry_month_counts": {key: counts([trade for trade in trades if month(trade.opened_at) == key]) for key in months},
        "uncertain_trades": [
            {"symbol": trade.symbol, "opened_at": stamp(trade.opened_at), "detected_at": stamp(trade.closed_at),
             "reasons": trade.uncertainty} for trade in trades if trade.status == "UNKNOWN"
        ],
        "open_trades": [{"symbol": trade.symbol, "opened_at": stamp(trade.opened_at)}
                        for trade in trades if trade.status == "OPEN_AT_END"],
    }


def validate_report(report: dict[str, Any]) -> None:
    require(set(report) == {"schema", "disclaimer", "candidate", "assumptions", "phases", "decision", "provenance"},
            "Unexpected top-level screening schema")
    require(report["schema"] == SCHEMA and report["disclaimer"] == DISCLAIMER, "Missing independent-screen declaration")
    require(report["decision"] == decision(report["phases"]), "Decision differs from fixed rule")
    for phase in report["phases"].values():
        require(set(phase) == {"period", "metrics", "first_crosses", "overlap_skipped", "entry_rejections",
                              "by_symbol_counts", "by_entry_month_counts", "uncertain_trades", "open_trades"},
                "Unexpected phase schema")
        for key in ("by_symbol_counts", "by_entry_month_counts"):
            for value in phase[key].values():
                require(set(value) == {"accepted", "N", "uncertain", "open_at_end"},
                        "Symbol/month performance is forbidden")
        for row in phase["uncertain_trades"]:
            require(set(row) == {"symbol", "opened_at", "detected_at", "reasons"}, "Uncertain trade schema differs")
        for row in phase["open_trades"]:
            require(set(row) == {"symbol", "opened_at"}, "Open trade schema differs")
        value = phase["metrics"]
        require(value["accepted"] == value["N"] + value["uncertain"] + value["open_at_end"], "Count partition differs")


def screen(data: Dataset, provenance: dict[str, Any]) -> dict[str, Any]:
    report = {
        "schema": SCHEMA, "disclaimer": DISCLAIMER,
        "candidate": {"timeframe": "4h", "N": N, "ATR": ATR_PERIOD, "k": K, "warmup_bars": WARMUP,
                      "atr_history": "ALL_CONTIGUOUS_CLOSED_PREFIX_INCLUDING_DECISION",
                      "tp_r": [1, 2, 3], "tp1_fraction": 0.6, "protection": "CONTRACT_15M_STOP_FIRST",
                      "blackout_seconds": BLACKOUT, "fee_bps_per_side": 5, "slip_bps_per_side": 3,
                      "spread_applied": False, "BE": False},
        "assumptions": {"reference_capital_usdt": 1000, "risk_usdt_per_trade": 3,
                        "pf": "SUM_POSITIVE_NET_USDT_DIV_ABS_SUM_NEGATIVE_NET_USDT_SAME_FIXED_3_USDT_RISK",
                        "no_loss_pf": "NULL_WITH_NO_LOSSES_STATUS_UNBOUNDED_PASSES_THRESHOLD",
                        "zero_result_pf": "UNDEFINED_FAIL",
                        "decision_rule": {"both_phases": True, "N_ge": MIN_N, "net_r_gt": MIN_NET_R, "pf_gt": MIN_PF}},
        "phases": {phase.name: run_phase(data, phase) for phase in PHASES},
        "decision": "", "provenance": provenance,
    }
    report["decision"] = decision(report["phases"])
    validate_report(report)
    return report


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, required=True, help="Existing hash-locked metadata COPY outside data blocks")
    args = parser.parse_args(argv)
    try:
        require(not OUTPUT.exists(), "screening-results already exists; no overwrite/retry")
        metadata = args.metadata.resolve()
        require(metadata == Path.home().joinpath("kaistrade-data", "donchian-holdout-evidence", "current-metadata.json").resolve(),
                "Only the previously verified external metadata COPY is allowed")
        require(digest(metadata) == METADATA_SHA, "Metadata fingerprint changed")
        OUTPUT.mkdir(parents=True, exist_ok=False)
        os.environ.update({"DATA_DIR": str(OUTPUT / "runtime"), "PROTREBOT_DATA_DIR": str(OUTPUT / "runtime"),
                           "DATABASE_URL": "", "PROTREBOT_DURABLE_AUTH_REQUIRED": "0", "ASSISTANT_LIVE_TESTS": "0"})
        from build_measurement_view import load_measurement_dataset
        from measure_donchian_counts import deny_network, quiet_native

        started = time.monotonic()
        with quiet_native(), deny_network():
            data = load_measurement_dataset(VIEW, metadata, PHASES[0].start, PHASES[-1].end)
            require(set(data.frames) == set(SYMBOLS) and set(data.marks) == set(SYMBOLS), "Incomplete eight-symbol universe")
            require(not data.report["missing_archives"], "Missing measurement archives")
            for parts in data.report["symbols"].values():
                require(not any(part.get("duplicates", 0) for part in parts.values()), "Duplicate measurement candles")
            require(all(set(frames) == {"15m", "1h", "4h"} for frames in data.frames.values()), "Incomplete contract frames")
            result = screen(data, {"metadata_sha256": METADATA_SHA,
                                   "source_archives": data.report["sources"],
                                   "script_sha256": digest(Path(__file__))})
        require(digest(metadata) == METADATA_SHA, "Metadata changed during screening")
        result["provenance"]["elapsed_seconds"] = time.monotonic() - started
        summary = {"schema": SCHEMA, "disclaimer": DISCLAIMER, "decision": result["decision"],
                   "phases": {name: value["metrics"] for name, value in result["phases"].items()}}
        for name, value in (("screen.json", result), ("summary.json", summary)):
            path = OUTPUT / name
            with path.open("x", encoding="utf-8") as stream:
                json.dump(value, stream, indent=2, allow_nan=False)
                stream.write("\n")
            print(str(path), digest(path))
        print(json.dumps(summary, allow_nan=False))
        return 0
    except (ScreeningError, OSError, ValueError, KeyError, TypeError, RuntimeError, ArithmeticError) as error:
        print(f"SCREENING_FAILED {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
