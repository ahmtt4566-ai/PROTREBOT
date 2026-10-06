"""Stage 6b registration and pre-claim data validation; no signal calculation."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .backtest_data import Dataset
from .holdout_study import (
    BACKEND, anchor_directory, digest, protocol_digest, registered_plan, verify_inputs,
)

PLAN_PATH = BACKEND / "studies" / "stage6b-plan.json"
SOURCE_NAMES = ("holdout_gap_cli.py", "app/holdout_gap_study.py",
                "app/holdout_gap_model.py", "studies/stage6b-plan.json")


def amendment() -> dict:
    return json.loads(PLAN_PATH.read_text(encoding="utf-8"))


def inherited_plan(extension: dict) -> dict:
    base = registered_plan()
    if protocol_digest(base) != extension["base_plan_sha256"]:
        raise ValueError("Stage 6 base protocol changed")
    return base


def source_hashes() -> dict[str, str]:
    return {name: digest(BACKEND.joinpath(*name.split("/"))) for name in SOURCE_NAMES}


def verify_context(stage5: Path, extension: dict) -> dict:
    base = inherited_plan(extension)
    verify_inputs(stage5, base)
    for name, expected in extension["protected_stage6_source_sha256"].items():
        if digest(BACKEND.joinpath(*name.split("/"))) != expected:
            raise ValueError(f"Protected Stage 6 source changed: {name}")
    if digest(BACKEND.parent / "README.md") != extension["protected_readme_sha256"]:
        raise ValueError("Protected prior-stage README changed")
    previous = stage5.parent / "stage6"
    for name, expected in extension["prior_failure"].items():
        if name.endswith(".json") and digest(previous / name) != expected:
            raise ValueError(f"Previous failure evidence changed: {name}")
    receipt = json.loads((previous / "test-once.json").read_text(encoding="utf-8"))
    repository = json.loads(Path(receipt["repository_claim_path"]).read_text(encoding="utf-8"))
    if receipt != repository or receipt["state"] != "FAILED" or receipt["rerun_allowed"] is not False:
        raise ValueError("Previous FAILED receipts must remain identical and irreversible")
    if receipt["protocol_sha256"] != extension["base_plan_sha256"]:
        raise ValueError("Previous failed protocol differs")
    if receipt["error"] != "Holdout candle gaps/duplicates: ADAUSDT / markPriceKlines_15m":
        raise ValueError("Previous failure was not the registered pre-signal data gate")
    if (previous / "native-cache").exists() or (previous / "holdout-results.json").exists():
        raise ValueError("Prior strategy evaluation evidence exists; amendment not authorized")
    return base


def exclusive_json(path: Path, value: dict, *, idempotent: bool = False) -> None:
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
    except FileExistsError:
        if not idempotent or json.loads(path.read_text(encoding="utf-8")) != value:
            raise ValueError(f"Immutable Stage 6b record already exists: {path.name}") from None


def register(stage5: Path, output: Path) -> dict:
    stage5, output = stage5.resolve(), output.resolve()
    if output != stage5.parent / "stage6b" or output.is_relative_to(BACKEND.parent):
        raise ValueError("Stage 6b requires fixed external sibling stage6b output")
    extension = amendment()
    verify_context(stage5, extension)
    locked = {"plan": extension, "sha256": protocol_digest(extension),
              "stage5_root": str(stage5), "output": str(output)}
    exclusive_json(anchor_directory() / f"registration-6b-{locked['sha256']}.json",
                   locked, idempotent=True)
    output.mkdir(parents=True, exist_ok=True)
    exclusive_json(output / "holdout-lock.json", locked, idempotent=True)
    return locked


def validate_quality(data: Dataset, base: dict, extension: dict) -> None:
    rule, symbols = extension["gap_rule"], set(base["symbols"])
    if set(data.frames) != symbols or set(data.marks) != symbols or set(data.report["symbols"]) != symbols:
        raise ValueError("Stage 6b data symbols differ")
    if set(rule["symbols"]) != symbols or data.report["missing_archives"]:
        raise ValueError("Stage 6b archival coverage differs")
    gap, duration = rule["mark_bar_open"], rule["duration_seconds"]
    if duration != 900 or rule["allowed_missing_mark_bars_per_symbol"] != 1:
        raise ValueError("Only the single preregistered 15m mark gap is allowed")
    expected_gap = [{"start": gap, "end_exclusive": gap + 900, "missing_candles": 1}]
    start, end = base["test"]["start"], base["test"]["end"]
    for symbol, parts in data.report["symbols"].items():
        for interval in ("15m", "1h", "4h"):
            part, series = parts[f"klines_{interval}"], data.frames[symbol][interval]
            if part["missing_candles"] or part["duplicates"] or part["gaps"]:
                raise ValueError(f"Contract data gap/duplicate: {symbol} / {interval}")
            spacing = {"15m": 900, "1h": 3600, "4h": 14400}[interval]
            if (len(series.closed(start)) < base["indicator_window"]
                    or not series.history_complete(start)
                    or any(right - left != spacing for left, right in zip(series.times, series.times[1:]))
                    or series.at(end - spacing) is None):
                raise ValueError(f"Incomplete warmup/contract history: {symbol} / {interval}")
        mark = parts["markPriceKlines_15m"]
        if mark["missing_candles"] != 1 or mark["duplicates"] or mark["gaps"] != expected_gap:
            raise ValueError(f"Unregistered mark gap/duplicate: {symbol}")
        series = data.marks[symbol]
        if (series.at(gap) is not None or series.at(gap - 900) is None
                or series.at(gap + 900) is None or data.frames[symbol]["15m"].at(gap) is None
                or len([at for at in series.times if start <= at < end]) != (end - start) // 900 - 1
                or any(right - left != (1800 if left == gap - 900 else 900)
                       for left, right in zip(series.times, series.times[1:]))):
            raise ValueError(f"Raw mark history differs from exact registered gap: {symbol}")


def verify_dry_receipt(stage5: Path, output: Path, locked: dict) -> dict:
    verify_context(stage5, locked["plan"])
    record = json.loads((output / "dry-verify.json").read_text(encoding="utf-8"))
    if (record["state"] != "PASSED" or record["protocol_sha256"] != locked["sha256"]
            or record["source_sha256"] != source_hashes()
            or record["quality_sha256"] != digest(output / "test-data-quality.json")
            or record["signals_calculated"] != 0 or record["replays"] != 0):
        raise ValueError("Successful unchanged signal-free dry verification required before claim")
    return record


def claim_once(stage5: Path, output: Path) -> dict:
    if not (output / "holdout-lock.json").exists():
        raise ValueError("Preregister Stage 6b before TEST")
    locked = register(stage5, output)
    dry = verify_dry_receipt(stage5, output, locked)
    if (output / "holdout-results.json").exists():
        raise ValueError("Stage 6b results already exist; no rerun")
    global_path = anchor_directory() / f"run-6b-{locked['sha256']}.json"
    record = {"state": "RUNNING", "protocol_sha256": locked["sha256"],
              "base_plan_sha256": locked["plan"]["base_plan_sha256"],
              "started_at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(),
              "source_sha256": dry["source_sha256"], "rerun_allowed": False,
              "repository_claim_path": str(global_path),
              "prior_failed_receipt_sha256": locked["plan"]["prior_failure"]["test-once.json"]}
    exclusive_json(global_path, record)
    exclusive_json(output / "test-once.json", record)
    return locked
