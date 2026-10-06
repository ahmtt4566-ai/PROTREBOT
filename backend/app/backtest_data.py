"""Validated local historical inputs. This module has no network client."""

from __future__ import annotations

import bisect
import csv
import hashlib
import io
import json
import math
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

INTERVALS = {"15m": 900, "1h": 3600, "4h": 14400}


def finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"Invalid {name}")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"Non-finite {name}")
    return result


def timestamp(value: Any) -> float:
    raw = finite(value, "timestamp")
    if raw >= 1e15:
        raw /= 1e6
    elif raw >= 1e12:
        raw /= 1000
    return raw


def epoch(value: Any) -> int:
    raw = timestamp(value)
    if raw != int(raw):
        raise ValueError("Opening/settlement timestamps must be integral seconds")
    return int(raw)


def read_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".parquet":
        import pyarrow.parquet as pq
        return pq.read_table(path).to_pylist()
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != 1 or not names[0].endswith(".csv"):
                raise ValueError(f"Expected one CSV inside {path}")
            content = archive.read(names[0]).decode("utf-8-sig")
    elif path.suffix == ".csv":
        content = path.read_text(encoding="utf-8-sig")
    else:
        raise ValueError(f"Unsupported historical format: {path}")
    stream = io.StringIO(content)
    first = next(csv.reader(stream), [])
    stream.seek(0)
    if first and first[0].replace(".", "", 1).isdigit():
        names = ("open_time", "open", "high", "low", "close", "volume",
                 "close_time", "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore")
        return [dict(zip(names, row)) for row in csv.reader(stream)]
    return list(csv.DictReader(stream))


@dataclass
class Series:
    interval: str
    rows: list[dict[str, Any]]
    times: list[int] = field(init=False)
    quote_prefix: list[float] = field(init=False)
    bad_quote_prefix: list[int] = field(init=False)

    def __post_init__(self) -> None:
        self.times = [row["time"] for row in self.rows]
        self.quote_prefix, self.bad_quote_prefix = [0.0], [0]
        for row in self.rows:
            quote = row.get("quote_volume")
            self.quote_prefix.append(self.quote_prefix[-1] + (quote if quote is not None else 0))
            self.bad_quote_prefix.append(self.bad_quote_prefix[-1] + (quote is None))

    def closed(self, at: int, limit: int = 259) -> list[dict[str, Any]]:
        index = bisect.bisect_right(self.times, at - INTERVALS[self.interval])
        return self.rows[max(0, index - limit):index]

    def at(self, opening: int) -> dict[str, Any] | None:
        index = bisect.bisect_left(self.times, opening)
        return self.rows[index] if index < len(self.times) and self.times[index] == opening else None

    def history_complete(self, at: int) -> bool:
        rows = self.closed(at)
        duration = INTERVALS[self.interval]
        expected_last = at // duration * duration - duration
        return bool(rows) and rows[-1]["time"] == expected_last and all(
            right["time"] - left["time"] == duration for left, right in zip(rows, rows[1:]))

    def ticker(self, at: int, symbol: str) -> dict[str, Any] | None:
        low, high = bisect.bisect_left(self.times, at - 86400), bisect.bisect_left(self.times, at)
        if high - low != 96 or self.bad_quote_prefix[high] != self.bad_quote_prefix[low]:
            return None
        first, last = self.rows[low], self.rows[high - 1]
        return {"symbol": symbol, "quoteVolume": self.quote_prefix[high] - self.quote_prefix[low],
                "priceChangePercent": (last["close"] / first["open"] - 1) * 100,
                "lastPrice": last["close"]}


def normalize_candles(rows: list[dict[str, Any]], interval: str) -> tuple[Series, int]:
    duration = INTERVALS[interval]
    by_time: dict[int, dict[str, Any]] = {}
    duplicates = 0
    for raw in rows:
        opening = epoch(raw.get("time", raw.get("open_time")))
        if opening % duration:
            raise ValueError(f"Misaligned {interval} opening: {opening}")
        candle = {"time": opening, **{key: finite(raw[key], key) for key in ("open", "high", "low", "close", "volume")}}
        if not 0 < candle["low"] <= min(candle["open"], candle["close"]) <= max(candle["open"], candle["close"]) <= candle["high"] or candle["volume"] < 0:
            raise ValueError(f"Invalid OHLCV at {opening}")
        if raw.get("close_time") not in (None, ""):
            declared = finite(raw["close_time"], "close_time")
            scale = 1e6 if declared >= 1e15 else 1000 if declared >= 1e12 else 1
            if abs(declared / scale - (opening + duration - 1 / scale)) > 0.001:
                raise ValueError(f"Invalid provider closing time at {opening}")
        quote = raw.get("quote_volume")
        candle["quote_volume"] = finite(quote, "quote volume") if quote not in (None, "") else None
        if candle["quote_volume"] is not None and candle["quote_volume"] < 0:
            raise ValueError("Negative quote volume")
        if opening in by_time:
            if by_time[opening] != candle:
                raise ValueError(f"Conflicting duplicate candle at {opening}")
            duplicates += 1
        by_time[opening] = candle
    return Series(interval, [by_time[key] for key in sorted(by_time)]), duplicates


def gap_report(series: Series, start: int, end: int) -> dict[str, Any]:
    duration = INTERVALS[series.interval]
    available = {opening for opening in series.times if start <= opening < end}
    first = (start + duration - 1) // duration * duration
    missing = [opening for opening in range(first, end, duration) if opening not in available]
    ranges: list[dict[str, int]] = []
    for opening in missing:
        if ranges and ranges[-1]["end_exclusive"] == opening:
            ranges[-1]["end_exclusive"] += duration
            ranges[-1]["missing_candles"] += 1
        else:
            ranges.append({"start": opening, "end_exclusive": opening + duration, "missing_candles": 1})
    return {"rows_in_period": len(available), "missing_candles": len(missing), "gaps": ranges}


@dataclass
class Dataset:
    frames: dict[str, dict[str, Series]]
    marks: dict[str, Series]
    funding: dict[str, list[dict[str, Any]]]
    metadata: dict[str, Any]
    report: dict[str, Any]
    funding_months: dict[str, set[str]]
    decisions: dict[tuple, dict[str, Any]] = field(default_factory=dict)

    def canonical(self, symbol: str, at: int, policy: dict[str, Any]) -> dict[str, Any]:
        from .main import canonical_historical_decision

        key = (symbol, at, policy["min_confidence"], policy["mtf_allow_either_timeframe"])
        if key not in self.decisions:
            frames = {interval: series.closed(at) for interval, series in self.frames[symbol].items()}
            result = canonical_historical_decision(
                symbol, frames, at, required_intervals=("15m", "1h", "4h"),
                historical_policy_override={"confidence_threshold": policy["min_confidence"],
                                            "mtf_allow_either_timeframe": policy["mtf_allow_either_timeframe"]})
            if result.get("analysis"):
                result = {**result, "analysis": {key: value for key, value in result["analysis"].items() if key != "series"}}
            if not result.get("entry_eligible"):
                result = {key: result.get(key) for key in ("decision", "signal_timestamp", "entry_eligible", "reason", "reasons")}
            self.decisions[key] = result
        return self.decisions[key]

    def funding_complete(self, symbol: str, start: int, end: int) -> bool:
        rows = self.funding.get(symbol, [])
        if not rows:
            return False
        if rows[0]["time"] > start + rows[0]["interval_hours"] * 3600 + 60:
            return False
        if rows[-1]["time"] + rows[-1]["interval_hours"] * 3600 + 60 < end:
            return False
        month = datetime.fromtimestamp(start, timezone.utc).replace(day=1, hour=0, minute=0, second=0)
        while month.timestamp() <= end:
            if month.strftime("%Y-%m") not in self.funding_months.get(symbol, set()):
                return False
            month = month.replace(year=month.year + 1, month=1) if month.month == 12 else month.replace(month=month.month + 1)
        for left, right in zip(rows, rows[1:]):
            if right["time"] > start and left["time"] < end and right["time"] - left["time"] > right["interval_hours"] * 3600 + 60:
                return False
        return True


def load_dataset(root: Path, metadata_path: Path, start: int, end: int) -> Dataset:
    if start >= end or start % 900 or end % 900:
        raise ValueError("Period must be increasing and aligned to UTC 15m boundaries")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    symbols = sorted(metadata["brackets"])
    collected = {(symbol, kind, interval): [] for symbol in symbols
                 for kind, interval in [("klines", "15m"), ("klines", "1h"), ("klines", "4h"),
                                        ("markPriceKlines", "15m"), ("fundingRate", None)]}
    coverage = {symbol: set() for symbol in symbols}
    missing_archives, sources = [], []
    for archive in manifest["archives"]:
        if archive["symbol"] not in symbols:
            continue
        if archive["status"] != "OK":
            missing_archives.append(archive)
            continue
        path = (root / archive["path"]).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError("Archive path escapes data directory")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != archive["sha256"]:
            raise ValueError(f"Archive SHA-256 mismatch: {path}")
        key = (archive["symbol"], archive["kind"], archive["interval"])
        if key not in collected:
            raise ValueError(f"Unsupported archive kind: {key}")
        rows = read_rows(path)
        collected[key].extend(rows)
        sources.append({"path": archive["path"], "sha256": digest, "rows": len(rows)})
        if archive["kind"] == "fundingRate":
            coverage[archive["symbol"]].add(archive["month"])
    frames, marks, funding, reports = {}, {}, {}, {}
    for symbol in symbols:
        frames[symbol], reports[symbol] = {}, {}
        for kind, interval in [("klines", "15m"), ("klines", "1h"), ("klines", "4h"), ("markPriceKlines", "15m")]:
            series, duplicates = normalize_candles(collected[(symbol, kind, interval)], interval)
            if kind == "klines":
                frames[symbol][interval] = series
            else:
                marks[symbol] = series
            reports[symbol][f"{kind}_{interval}"] = {**gap_report(series, start, end), "duplicates": duplicates,
                                                     "total_rows_with_warmup": len(series.rows)}
        values = {}
        for row in collected[(symbol, "fundingRate", None)]:
            at = timestamp(row.get("time", row.get("calc_time")))
            event = {"time": at, "rate": finite(row.get("rate", row.get("last_funding_rate")), "funding rate"),
                     "interval_hours": finite(row["funding_interval_hours"], "funding interval")}
            if event["interval_hours"] <= 0 or (at in values and values[at] != event):
                raise ValueError("Invalid/contradictory funding evidence")
            values[at] = event
        funding[symbol] = [values[key] for key in sorted(values)]
        reports[symbol]["funding"] = {"events": len(values), "status": "PRESENT" if values else "FUNDING YOK",
                                     "gaps": [{"start": left["time"], "end": right["time"]}
                                              for left, right in zip(funding[symbol], funding[symbol][1:])
                                              if right["time"] - left["time"] > right["interval_hours"] * 3600 + 60]}
        if funding[symbol]:
            last = funding[symbol][-1]
            reports[symbol]["funding"].update(
                first_event=funding[symbol][0]["time"], last_event=last["time"],
                trailing_missing_events=max(0, math.floor((end - 1 - last["time"]) / (last["interval_hours"] * 3600))))
    return Dataset(frames, marks, funding, metadata, {"symbols": reports, "missing_archives": missing_archives,
                                                     "repairs": manifest.get("repairs", []),
                                                     "sources": sources}, coverage)
