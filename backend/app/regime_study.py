"""Stage 5 preregistration and causal research features; no trading client."""

from __future__ import annotations

import bisect
import hashlib
import json
import math
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any, Iterator

from . import analysis
from .backtest_data import Dataset, INTERVALS, Series

PLAN_PATH = Path(__file__).resolve().parents[1] / "studies" / "stage5-plan.json"


def registered_plan() -> dict[str, Any]:
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    splits = plan["splits"]
    if (splits["TEST"]["end"] != splits["TRAIN"]["start"]
            or splits["TRAIN"]["end"] != splits["VALIDATION"]["start"]
            or splits["TEST"]["sealed"] is not True
            or plan["test_access_before_stage6"] is not False):
        raise ValueError("Invalid sealed study splits")
    return plan


def lock_plan(root: Path) -> dict[str, Any]:
    plan = registered_plan()
    payload = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    locked = {"plan": plan, "sha256": hashlib.sha256(payload).hexdigest()}
    root.mkdir(parents=True, exist_ok=True)
    path = root / "study-lock.json"
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(locked, stream, indent=2)
    except FileExistsError:
        if json.loads(path.read_text(encoding="utf-8")) != locked:
            raise ValueError("Preregistered study lock differs; refusing to overwrite") from None
    return locked


def development_manifest(root: Path, plan: dict[str, Any]) -> None:
    """Check archive scope BEFORE the native loader can read any candle."""
    first = datetime.fromtimestamp(plan["splits"]["TRAIN"]["start"], timezone.utc).strftime("%Y-%m")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for row in manifest["archives"]:
        if not first <= row["month"][:7] <= plan["archive_end_month"]:
            raise ValueError("Sealed TEST/pre-TEST archive in development manifest")
        path = (root / row["path"]).resolve()
        if not path.is_relative_to(root.resolve()) or "sealed" in path.parts:
            raise ValueError("Archive escapes development storage")


@contextmanager
def short_gate_disabled() -> Iterator[None]:
    """Pure native-helper adapter, only inside standalone offline workers."""
    from . import main

    original = main.shared_mtf_decision

    def adapter(symbol: str, entry_direction: str, confidence_15m: float,
                timeframe_results: dict[str, dict], short_filter: bool = True,
                short_alignment_max: float = main.SHORT_MTF_ALIGNMENT_MAX,
                allow_either_timeframe: bool = False) -> dict:
        return original(symbol, entry_direction, confidence_15m, timeframe_results,
                        short_filter=False, short_alignment_max=short_alignment_max,
                        allow_either_timeframe=allow_either_timeframe)

    main.shared_mtf_decision = adapter
    try:
        yield
    finally:
        main.shared_mtf_decision = original


class ResearchDataset(Dataset):
    short_gate_on: bool = True

    def canonical(self, symbol: str, at: int, policy: dict[str, Any]) -> dict[str, Any]:
        key = (symbol, at, policy["min_confidence"], policy["mtf_allow_either_timeframe"], self.short_gate_on)
        if key not in self.decisions:
            raise ValueError(f"Missing verified native decision cache: {symbol}/{at}/{self.short_gate_on}")
        return self.decisions[key]


def compact_decision(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("entry_eligible"):
        return {key: result.get(key) for key in ("decision", "signal_timestamp", "entry_eligible", "reason", "reasons")}
    return {**result, "analysis": {key: value for key, value in result["analysis"].items() if key != "series"}}


def common_period_dataset(data: ResearchDataset, manifest: dict, metadata: dict) -> ResearchDataset:
    """Match original warmup/prefix sums using the expanded, verified rows."""
    first_month = min(row["month"][:7] for row in manifest["archives"] if row["status"] == "OK")
    first = int(datetime.strptime(first_month, "%Y-%m").replace(tzinfo=timezone.utc).timestamp())
    source_hashes = {row["path"]: row["sha256"] for row in data.report["sources"]}
    for row in manifest["archives"]:
        if row["status"] == "OK" and source_hashes.get(row["path"]) != row["sha256"]:
            raise ValueError("Original common-period archive differs from expanded data")
    symbols = sorted(metadata["brackets"])
    result = ResearchDataset(
        {symbol: {interval: Series(interval, [row for row in series.rows if row["time"] >= first])
                  for interval, series in data.frames[symbol].items()} for symbol in symbols},
        {symbol: Series("15m", [row for row in data.marks[symbol].rows if row["time"] >= first]) for symbol in symbols},
        {symbol: [row for row in data.funding[symbol] if row["time"] >= first] for symbol in symbols},
        metadata, data.report, {symbol: {month for month in data.funding_months[symbol] if month >= first_month}
                               for symbol in symbols}, data.decisions)
    return result


def native_pair(symbol: str, frames: dict[str, Series], at: int, policy: dict) -> tuple[dict, dict]:
    from .main import canonical_historical_decision

    inputs = {interval: series.closed(at) for interval, series in frames.items()}
    kwargs = {"required_intervals": ("15m", "1h", "4h"), "historical_policy_override": {
        "confidence_threshold": policy["min_confidence"],
        "mtf_allow_either_timeframe": policy["mtf_allow_either_timeframe"]}}
    result = canonical_historical_decision(symbol, inputs, at, **kwargs)
    off = result
    if result.get("mtf", {}).get("blocked_by_short_filter"):
        with short_gate_disabled():
            off = canonical_historical_decision(symbol, inputs, at, **kwargs)
    return compact_decision(result), compact_decision(off)


def closed_features(series: Series, at: int, *, include_adx: bool = True) -> tuple[float | None, float | None, float | None]:
    rows = series.closed(at)
    if len(rows) < 259 or not series.history_complete(at):
        return None, None, None
    closes = [row["close"] for row in rows]
    atr = analysis.atr([row["high"] for row in rows], [row["low"] for row in rows], closes)
    adx = analysis.adx([row["high"] for row in rows], [row["low"] for row in rows], closes) if include_adx else None
    middle = fmean(closes[-20:])
    return adx, atr / closes[-1], 4 * pstdev(closes[-20:]) / middle


def causal_percentiles(series: Series, start: int, end: int) -> dict[int, dict[str, float | None]]:
    """Sliding sorted samples include only closed observations, never TEST."""
    window = 90 * 86400
    sorted_atr: list[float] = []
    sorted_bb: list[float] = []
    samples: list[tuple[int, float | None, float | None]] = []
    left = 0
    output = {}
    for opening in series.times:
        at = opening + INTERVALS[series.interval]
        if at >= end:
            break
        _, atr, bb = closed_features(series, at, include_adx=False)
        samples.append((at, atr, bb))
        for value, ordered in ((atr, sorted_atr), (bb, sorted_bb)):
            if value is not None:
                if not math.isfinite(value):
                    raise ValueError("Non-finite native feature")
                bisect.insort(ordered, value)
        while left < len(samples) and samples[left][0] <= at - window:
            _, old_atr, old_bb = samples[left]
            for value, ordered in ((old_atr, sorted_atr), (old_bb, sorted_bb)):
                if value is not None:
                    ordered.pop(bisect.bisect_left(ordered, value))
            left += 1
        if at >= start:
            complete = (len(samples) - left == window // 900
                        and len(sorted_atr) == window // 900 and len(sorted_bb) == window // 900)
            output[at] = {
                "atr_percentile": 100 * bisect.bisect_right(sorted_atr, atr) / len(sorted_atr)
                if complete and atr is not None else None,
                "bb_percentile": 100 * bisect.bisect_right(sorted_bb, bb) / len(sorted_bb)
                if complete and bb is not None else None,
            }
    return output


def filter_masks(rows: list[dict], features: dict[str, dict], plan: dict) -> dict[str, list[bool]]:
    masks = {}
    for rule in plan["filters"]:
        masks[rule["name"]] = [
            features[row["signal_id"]].get(rule["feature"]) is not None
            and features[row["signal_id"]][rule["feature"]] >= rule["threshold"] for row in rows]
    first, second = plan["combination"]
    masks["F1_F3_COMBINED"] = [a and b for a, b in zip(masks[first], masks[second])]
    masks["B_NO_SHORT"] = [row["direction"] == "LONG" for row in rows]
    return masks
