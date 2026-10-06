"""Immutable registration and one-shot holdout scope; no trading client."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .backtest_baseline import Config
from .backtest_diagnostics import utc
from .regime_statistics import complete, summary
from .regime_study import closed_features

BACKEND = Path(__file__).resolve().parents[1]
PLAN_PATH = BACKEND / "studies" / "stage6-plan.json"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def registered_plan() -> dict:
    return json.loads(PLAN_PATH.read_text(encoding="utf-8"))


def protocol_digest(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def anchor_directory() -> Path:
    result = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=BACKEND.parent,
                            check=True, capture_output=True, text=True)
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = BACKEND.parent / common
    anchor = common.resolve() / "protrebot-research" / "holdout"
    anchor.mkdir(parents=True, exist_ok=True)
    return anchor


def verify_inputs(stage5: Path, plan: dict) -> None:
    inputs = {"stage5_lock": stage5 / "study-lock.json",
              "sealed_manifest": stage5 / "sealed" / "manifest.json",
              "metadata": stage5 / "development" / "current-metadata.json"}
    for name, path in inputs.items():
        if digest(path) != plan["input_sha256"][name]:
            raise ValueError(f"Registered input changed: {name}")
    for name, expected in plan["protected_source_sha256"].items():
        if digest(BACKEND.joinpath(*name.split("/"))) != expected:
            raise ValueError(f"Protected native/prior-stage source changed: {name}")
    previous = json.loads(inputs["stage5_lock"].read_text(encoding="utf-8"))
    if previous["sha256"] != plan["stage5_plan_sha256"]:
        raise ValueError("Stage 5 protocol differs")
    test = previous["plan"]["splits"]["TEST"]
    if (test["start"], test["end"]) != (plan["test"]["start"], plan["test"]["end"]):
        raise ValueError("Registered TEST differs from sealed Stage 5 TEST")
    manifest = json.loads(inputs["sealed_manifest"].read_text(encoding="utf-8"))
    if manifest["sealed"] is not True:
        raise ValueError("Expected sealed source manifest")
    source = (stage5 / "sealed").resolve()
    for archive in manifest["archives"]:
        if (not plan["warmup_first_month"] <= archive["month"] <= plan["test_last_month"]
                or archive["symbol"] not in plan["symbols"] or archive["status"] != "OK"):
            raise ValueError("Unregistered/missing holdout archive")
        if not (source / archive["path"]).resolve().is_relative_to(source):
            raise ValueError("Holdout archive escapes sealed storage")


def register(stage5: Path, output: Path) -> dict:
    stage5, output = stage5.resolve(), output.resolve()
    if output != stage5.parent / "stage6" or output.is_relative_to(BACKEND.parent):
        raise ValueError("One-shot output must be the fixed external sibling stage6 directory")
    plan = registered_plan()
    verify_inputs(stage5, plan)
    locked = {"plan": plan, "sha256": protocol_digest(plan),
              "stage5_root": str(stage5), "output": str(output)}
    anchor = anchor_directory() / f"registration-{locked['sha256']}.json"
    try:
        with anchor.open("x", encoding="utf-8") as stream:
            json.dump(locked, stream, indent=2)
    except FileExistsError:
        if json.loads(anchor.read_text(encoding="utf-8")) != locked:
            raise ValueError("Protocol already bound to another input/output; no relocation rerun") from None
    output.mkdir(parents=True, exist_ok=True)
    path = output / "holdout-lock.json"
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(locked, stream, indent=2)
    except FileExistsError:
        if json.loads(path.read_text(encoding="utf-8")) != locked:
            raise ValueError("Holdout registration differs; refusing overwrite") from None
    return locked


def claim_once(stage5: Path, output: Path, sources: dict[str, str]) -> dict:
    output = output.resolve()
    if not (output / "holdout-lock.json").exists():
        raise ValueError("Preregister before opening TEST")
    locked = register(stage5, output)
    if (output / "holdout-results.json").exists():
        raise ValueError("TEST results already exist; no rerun")
    record = {"state": "RUNNING", "protocol_sha256": locked["sha256"],
              "started_at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(),
              "source_sha256": sources, "rerun_allowed": False}
    global_claim = anchor_directory() / f"run-{locked['sha256']}.json"
    record["repository_claim_path"] = str(global_claim)
    try:
        with global_claim.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, indent=2)
    except FileExistsError:
        raise ValueError("Repository TEST already claimed; no rerun from another output or worktree") from None
    try:
        with (output / "test-once.json").open("x", encoding="utf-8") as stream:
            json.dump(record, stream, indent=2)
    except FileExistsError:
        raise ValueError("TEST already claimed, including failed/interrupted attempts; no rerun") from None
    return locked


def merged(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if start >= end:
            raise ValueError("Invalid scope interval")
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def period_scopes(plan: dict) -> dict:
    start, end = plan["test"]["start"], plan["test"]["end"]
    seen = []
    for period in plan["seen_periods"]:
        left = max(start, int(utc(period["start"]).timestamp()))
        right = min(end, int(utc(period["end"]).timestamp()))
        if left < right:
            seen.append((left, right))
    seen = merged(seen)
    clean, cursor = [], start
    for left, right in seen:
        if cursor < left:
            clean.append((cursor, left))
        cursor = right
    if cursor < end:
        clean.append((cursor, end))
    return {"full": [(start, end)], "clean": clean, "partly_seen": seen,
            "overlap_seconds": sum(right - left for left, right in seen),
            "label": "k\u0131smen g\u00f6r\u00fclm\u00fc\u015f" if seen else "NO_OVERLAP_ALL_TEST_CLEAN"}


def scoped_rows(rows: list[dict], scopes: dict) -> dict[str, list[dict]]:
    left, right = scopes["full"][0]
    for row in rows:
        opened = int(utc(row["opened_at"]).timestamp())
        if not left <= opened < right:
            raise ValueError("Holdout entry outside TEST")
        if row["closed_at"] and not opened <= int(utc(row["closed_at"]).timestamp()) < right:
            raise ValueError("Holdout close outside TEST/lifetime")
    if not scopes["partly_seen"]:
        return {"full": rows, "clean": rows, "partly_seen": []}
    clean, seen = [], []
    for row in rows:
        start = int(utc(row["opened_at"]).timestamp())
        end = int(utc(row["closed_at"]).timestamp()) + 1 if row["closed_at"] else scopes["full"][0][1]
        if any(left <= start and end <= right for left, right in scopes["clean"]):
            clean.append(row)
        else:
            seen.append(row)
    return {"full": rows, "clean": clean, "partly_seen": seen}


def criteria(value: dict, plan: dict) -> dict:
    rule, reasons = plan["success"], []
    expectancy = value["net_expectancy_r"]
    pf = value["profit_factor"]
    if expectancy is None or not math.isfinite(expectancy) or expectancy <= rule["net_expectancy_r_strict_gt"]:
        reasons.append("EXPECTANCY_NOT_STRICTLY_POSITIVE_OR_UNAVAILABLE")
    unbounded = (pf is None and value["profit_factor_status"] == "NO_LOSSES"
                 and value["closed_curve_net_pnl"] is not None and value["closed_curve_net_pnl"] > 0)
    if not unbounded and (pf is None or not math.isfinite(pf) or pf <= rule["usdt_profit_factor_strict_gt"]):
        reasons.append("USDT_PF_NOT_STRICTLY_ABOVE_1_2_OR_UNAVAILABLE")
    if value["r_eligible_count"] < rule["complete_trades_min"]:
        reasons.append("FEWER_THAN_60_COMPLETE_TRADES")
    return {"passed": not reasons, "label": "BA\u015eARISIZ" if reasons else "BA\u015eARILI",
            "failed_criteria": reasons, "point_criteria_only": True}


def candidate_masks(rows: list[dict], data, plan: dict) -> tuple[dict[str, list[bool]], dict]:
    adx = {}
    for row in rows:
        if row["direction"] not in ("LONG", "SHORT"):
            raise ValueError("Unknown native holdout direction")
        at = int(utc(row["opened_at"]).timestamp())
        adx[row["signal_id"]] = closed_features(data.frames[row["symbol"]]["4h"], at)[0]
    masks = {
        "B": [True] * len(rows),
        "B_ADX4H20": [adx[row["signal_id"]] is not None and adx[row["signal_id"]] >= 20 for row in rows],
        "B_NO_SHORT": [row["direction"] == "LONG" for row in rows],
    }
    if list(masks) != [candidate["name"] for candidate in plan["candidates"]]:
        raise ValueError("Unregistered candidate set")
    return masks, {"adx_4h_by_signal": adx,
                   "unknown_adx_rejected_signal_ids": [key for key, value in adx.items() if value is None],
                   "added_entries": 0, "capacity_refill": False}


def describe(rows: list[dict], config: Config, plan: dict) -> dict:
    for row in rows:
        complete(row)
    value = summary(rows, config, plan["success"]["complete_trades_min"])
    value["criteria"] = criteria(value, plan)
    value["excluded_from_r_count"] = len(rows) - value["r_eligible_count"]
    return value
