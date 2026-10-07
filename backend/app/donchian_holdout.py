"""Independent one-shot Donchian registry; no Stage 6/6b receipts are touched.

Dry verification hashes opaque local evidence only, never the sealed archive,
TEST CSVs, signals or data health. TEST root must not yet exist. The archive hash
is USER-ATTESTED, not verified by reading/decrypting an archive.

Registration and consumption are fixed repository-wide filenames, not keyed by
plan/output hashes: another output, worktree or edited plan cannot reset a claim.
Claim files are immutable. Any post-claim failure consumes the attempt; a killed
process may lack a terminal receipt but its claim still forbids a rerun.

The measurement loader stays unchanged and always denies TEST. A separate loader
accepts only an in-process, registry-validated running claim. This is an accidental
access/rerun guard, not OS isolation against arbitrary Python or filesystem edits.
Manual extraction is external; this module contains no 7z/password handling.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
REPOSITORY = BACKEND.parent
PLAN_PATH = BACKEND / "studies" / "donchian-holdout-plan.json"
SCHEMA = "donchian-independent-holdout-v1"
PINNED_INPUTS = {
    "measurement_manifest": "23e8d8b55a3a12ed76d562eb072a440115d3a2aeaefe6571e2c155c15f316a6e",
    "source_manifest": "09e0f77b488ae9e272c7960e827b584bd4eb1e64116c84f146189a26c352c024",
    "metadata": "4153972de075f9372640632621a3153d50350d721f4efe979a869fe821a5ab57",
    "counts": "ece60be820789257716f8ec444c6f77011432f81e8a2a1509c843f79d1e7bc7f",
    "summary": "e813db2f89555440bae3965a023af352a9ca90ac8556eadc5add7afc172acf25",
}
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT"]
PROTOCOL = {
    "candidate": {
        "strategy_id": "donchian_breakout", "strategy_version": "signal-v2", "k": 1.4,
        "N": 20, "ATR": 14, "tp_r": [1, 2, 3], "W": 259, "H_hours": 72,
        "gap_kinds": ["contract", "mark", "funding"], "exit_policy": "existing_stop_tp1_60_tp3",
        "gap_policy": "offline_future_gap_blackout_funding_v1",
    },
    "symbols": SYMBOLS,
    "splits": {
        "TRAIN": ["2020-10-01T00:00:00+00:00", "2022-01-01T00:00:00+00:00"],
        "VALIDATION": ["2022-01-01T00:00:00+00:00", "2022-09-01T00:00:00+00:00"],
        "TEST": ["2022-09-01T00:00:00+00:00", "2023-06-01T00:00:00+00:00"],
        "warmup_start": "2022-08-01T00:00:00+00:00", "end_exclusive": True,
        "fresh_engine": True, "carried_positions": False, "force_close_at_end": False,
    },
    "primary": {
        "name": "primary", "intrabar": "STOP_FIRST", "fee_bps": 5, "slippage_bps": 3,
        "spread_bps": 2, "initial_equity": 1000, "max_total_exposure_usdt": 350,
        "policy": "NATIVE_SANITIZER_WITH_ONLY_SYMBOLS_AND_EXPOSURE_OVERRIDDEN",
        "policy_sha256": "dd3769d5cdd1bb4ee2b02858091e2a754430bcef43166d0fc54cd3eba4a66232",
    },
    "sensitivities": [
        {"name": "tp_first", "intrabar": "TP_FIRST", "slippage_bps": 3, "spread_bps": 2,
         "entry_schedule": "FROZEN_PRIMARY_NO_GATES_RESCAN_NO_REFILL", "confirmatory": False},
        {"name": "stress", "intrabar": "STOP_FIRST", "slippage_bps": 6, "spread_bps": 5,
         "entry_schedule": "FRESH_ENGINE_RECOMPUTE_NATIVE_GATES", "confirmatory": False},
    ],
    "success": {"mean_net_r_strict_gt": 0, "usdt_pf_strict_gt": 1.2, "fully_verified_completed_min": 60,
                "scope": "PRIMARY_POINT_CRITERIA_ONLY"},
    "N": "CLOSED_AND_COMPLETE_FUNDING_AND_FEES_AND_FINITE_NET_PNL_AND_NET_R",
    "undefined": {
        "r": "UNKNOWN_NONFINITE_OR_INCOMPLETE_EXCLUDED_FROM_N_NO_IMPUTATION",
        "pf_no_loss_positive_gain": "UNBOUNDED_PASSES_PF_POINT_THRESHOLD_NULL_WITH_NO_LOSSES_STATUS",
        "pf_zero_gain_zero_loss": "UNDEFINED_FAIL",
        "zero_completed": "UNDEFINED_R_PF_AND_FAILED_N",
    },
    "bootstrap": {"samples": 20000, "seed": 2026, "alpha": 0.05, "units": ["TRADE", "UTC_DAY"],
                  "include_empty_calendar_days": True, "family_size": 1, "informational_only": True},
    "ranking": ["opportunity_score_DESC", "volume_DESC", "symbol_ASC"],
    "confidence_tiebreak": False, "multiple_comparisons": False,
    "original_benchmark": "DESCRIPTIVE_PRIMARY_ONLY_NOT_A_CANDIDATE_OR_SUCCESS_CRITERION",
    "uncertainty": "REPORT_OPEN_UNKNOWN_FUNDING_INCOMPLETE_AND_FUNDING_GAP_EXPOSURES_SEPARATELY",
    "data_rule": "EXACT_400_SOURCE_HASHED_ZIPS_2022_08_THROUGH_2023_05_NO_REPAIR_OR_INTERPOLATION",
    "duplicate_candles": "FAIL_BEFORE_SIGNALS_AFTER_CLAIM",
    "dry_test_root": "MUST_NOT_EXIST_EVEN_IF_EMPTY",
    "claim": "FIXED_REPOSITORY_O_EXCL_ANY_POST_CLAIM_ERROR_OR_INTERRUPTION_FORBIDS_RERUN",
    "declarations": {
        "metadata": "CURRENT_SNAPSHOT_NOT_HISTORICAL",
        "kais_original_pre_2023_06_partly_seen": "UNKNOWN",
        "test_health_seen_before_planning": True,
        "test_prices_or_strategy_outcomes_analyzed_before_planning": False,
        "future_gap_information": "DATA_QUALITY_EXCLUSION_NOT_APPLICABLE_LIVE",
        "parameter_decisions": "DECLARED_PRE_RESULT_MECHANICAL_STOP_CAP_COMPATIBILITY_NOT_OUTCOME_TUNING",
        "declaration_verification": "HISTORICAL_UNKNOWN_AND_INTENT_DECLARATIONS_NOT_INDEPENDENTLY_PROVEN",
        "holdout": "REVERSE_TIME_NOT_FORWARD_WALK_FORWARD_NOT_ASSERTED_WHOLLY_UNSEEN",
    },
}


class HoldoutError(ValueError):
    """Explicit protocol failure; no success-shaped fallback."""


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise HoldoutError("Duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise HoldoutError("Non-finite JSON")

    value = json.loads(path.read_bytes(), object_pairs_hook=unique, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise HoldoutError("JSON object required")
    return value


def source_names(backend: Path = BACKEND) -> list[str]:
    fixed = [
        "app/backtest_baseline.py", "app/execution_core.py", "app/backtest_data.py",
        "app/analysis.py", "app/main.py", "app/v25_execution.py", "app/liquidation_risk.py",
        "app/donchian_holdout.py", "app/donchian_holdout_statistics.py", "donchian_holdout_cli.py",
        "measure_donchian_counts.py", "build_measurement_view.py", "download_independent_block.py",
        "studies/stage6-plan.json", "studies/stage6b-plan.json",
        "tests/test_strategy_original_adapter.py", "tests/test_donchian_strategy.py",
        "tests/test_donchian_v2_boundaries.py", "tests/test_offline_strategy_facade.py",
        "tests/test_offline_facade_lifecycle.py", "tests/test_offline_facade_window.py",
        "tests/test_offline_facade_gap_blackout.py", "tests/test_offline_facade_funding_gaps.py",
        "tests/test_measure_donchian_counts.py", "tests/test_build_measurement_view.py",
        "tests/test_download_independent_block.py", "tests/test_donchian_holdout.py",
    ]
    native = [path.relative_to(backend).as_posix() for path in (backend / "app").glob("*.py")]
    strategies = [path.relative_to(backend).as_posix() for path in (backend / "app" / "strategies").glob("*.py")]
    return sorted(set(fixed + native + strategies))


def code_hashes(backend: Path = BACKEND) -> dict[str, str]:
    return {name: digest(backend.joinpath(*PurePosixPath(name).parts)) for name in source_names(backend)}


def validate_plan(plan: dict[str, Any], backend: Path = BACKEND) -> None:
    if (
        set(plan) != {"schema", "registered_at", "protocol", "input_sha256", "code_sha256", "sealed_test_archive_sha256"}
        or plan["schema"] != SCHEMA or plan["protocol"] != PROTOCOL or plan["input_sha256"] != PINNED_INPUTS
        or set(plan["code_sha256"]) != set(source_names(backend))
    ):
        raise HoldoutError("Locked protocol/inputs/source inventory differs")
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
           for value in plan["code_sha256"].values()):
        raise HoldoutError("Incomplete code fingerprints")
    sealed = plan["sealed_test_archive_sha256"]
    if not isinstance(sealed, str) or not re.fullmatch(r"[0-9a-f]{64}", sealed):
        raise HoldoutError("User-supplied TEST archive SHA-256 is empty/invalid; archive is never read")


@dataclass(frozen=True)
class Inputs:
    plan: Path
    measurement_manifest: Path
    source_manifest: Path
    metadata: Path
    counts: Path
    summary: Path
    test_root: Path
    output: Path

    def paths(self) -> dict[str, str]:
        return {name: str(value.resolve()) for name, value in self.__dict__.items()}


def check_locations(inputs: Inputs, repository: Path = REPOSITORY) -> None:
    output, test_root = inputs.output.resolve(), inputs.test_root.resolve()
    if output.is_relative_to(repository.resolve()) or test_root.is_relative_to(repository.resolve()):
        raise HoldoutError("TEST inputs/results must be outside Git")
    if output == test_root or output.is_relative_to(test_root) or test_root.is_relative_to(output):
        raise HoldoutError("TEST and result directories must be separate")
    forbidden = Path.home() / "kaistrade-data" / "independent-block-2020-10_2023-05"
    if any(Path(value).is_relative_to(forbidden.resolve()) for value in inputs.paths().values()):
        raise HoldoutError("Use an external identical source-manifest COPY, not independent-block")
    if inputs.plan.resolve().is_relative_to(output) or inputs.metadata.resolve().is_relative_to(test_root):
        raise HoldoutError("Plan/metadata cannot be inside mutable result/TEST roots")
    own_names = {"dry-verify.json", "holdout-lock.json", "test-once.json", "receipt.json",
                 "verified-input", "runtime", "holdout-results.json"}
    if output.exists() and (not output.is_dir() or any(path.name not in own_names for path in output.iterdir())):
        raise HoldoutError("Result directory contains foreign study/evidence files")


def verify_inputs(inputs: Inputs, backend: Path = BACKEND) -> dict[str, Any]:
    check_locations(inputs, backend.parent)
    plan = read_json(inputs.plan)
    validate_plan(plan, backend)
    hashes = code_hashes(backend)
    if hashes != plan["code_sha256"]:
        raise HoldoutError("Code hash changed")
    evidence = {name: digest(getattr(inputs, name)) for name in PINNED_INPUTS}
    if evidence != plan["input_sha256"]:
        raise HoldoutError("Manifest/metadata/measurement fingerprint changed")
    return {"plan_sha256": digest(inputs.plan), "code_sha256": hashes, "input_sha256": evidence,
            "sealed_archive_sha256_attestation": plan["sealed_test_archive_sha256"], "paths": inputs.paths()}


def exclusive_json(path: Path, record: dict[str, Any], *, identical_ok: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if identical_ok and read_json(path) == record:
            return
        raise HoldoutError("Immutable registration/claim/receipt already exists") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def repository_anchor() -> Path:
    common = subprocess.check_output(["git", "rev-parse", "--git-common-dir"], cwd=REPOSITORY, text=True).strip()
    base = Path(common)
    if not base.is_absolute():
        base = REPOSITORY / base
    return base.resolve() / "protrebot-research" / "donchian-independent-holdout-v1"


def dry_verify(inputs: Inputs, *, backend: Path = BACKEND, anchor: Path | None = None) -> dict[str, Any]:
    registry = repository_anchor() if anchor is None else anchor
    if (registry / "test-once.json").exists() or (inputs.output / "test-once.json").exists():
        raise HoldoutError("Already consumed; no dry verification/reset")
    if inputs.test_root.exists():
        raise HoldoutError("TEST directory already exists, even if empty")
    proof = verify_inputs(inputs, backend)
    record = {"state": "PASSED", "evidence": proof, "signals": 0, "replays": 0, "test_files_read": 0,
              "archive_read": False, "archive_hash_user_attested": True}
    exclusive_json(inputs.output / "dry-verify.json", record, identical_ok=True)
    return record


def lock(inputs: Inputs, *, backend: Path = BACKEND, anchor: Path | None = None) -> dict[str, Any]:
    registry = repository_anchor() if anchor is None else anchor
    dry = dry_verify(inputs, backend=backend, anchor=registry)
    record = {"schema": SCHEMA, "evidence": dry["evidence"], "dry_sha256": digest(inputs.output / "dry-verify.json")}
    exclusive_json(registry / "lock.json", record, identical_ok=True)
    exclusive_json(inputs.output / "holdout-lock.json", record, identical_ok=True)
    return record


@dataclass(frozen=True)
class Claim:
    inputs: Inputs
    anchor: Path
    record: dict[str, Any]
    evidence: dict[str, Any]

    def validate(self, backend: Path = BACKEND) -> None:
        if (
            read_json(self.anchor / "test-once.json") != self.record
            or read_json(self.inputs.output / "test-once.json") != self.record
            or self.record["state"] != "RUNNING"
            or (self.anchor / "receipt.json").exists() or (self.inputs.output / "receipt.json").exists()
            or verify_inputs(self.inputs, backend) != self.evidence
            or read_json(self.anchor / "lock.json") != read_json(self.inputs.output / "holdout-lock.json")
            or self.record["lock_sha256"] != digest(self.anchor / "lock.json")
        ):
            raise HoldoutError("No valid unconsumed running claim")


def claim_once(inputs: Inputs, *, backend: Path = BACKEND, anchor: Path | None = None) -> Claim:
    registry = repository_anchor() if anchor is None else anchor
    if (registry / "test-once.json").exists():
        raise HoldoutError("TEST already claimed, including failed/interrupted attempts")
    locked = read_json(registry / "lock.json")
    if locked != read_json(inputs.output / "holdout-lock.json"):
        raise HoldoutError("Repository and local lock differ")
    proof = verify_inputs(inputs, backend)
    dry = read_json(inputs.output / "dry-verify.json")
    if (
        locked["evidence"] != proof or dry["state"] != "PASSED" or dry["evidence"] != proof
        or locked["dry_sha256"] != digest(inputs.output / "dry-verify.json")
        or dry["signals"] != 0 or dry["replays"] != 0 or dry["test_files_read"] != 0 or dry["archive_read"] is not False
    ):
        raise HoldoutError("Unchanged locked plan and signal-free dry receipt required")
    record = {
        "state": "RUNNING", "claim_id": uuid.uuid4().hex, "plan_sha256": proof["plan_sha256"],
        "lock_sha256": digest(registry / "lock.json"), "started_at": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(), "rerun_allowed": False,
    }
    exclusive_json(registry / "test-once.json", record)
    mirrored = False
    try:
        exclusive_json(inputs.output / "test-once.json", record)
        mirrored = True
    finally:
        if not mirrored:
            exclusive_json(registry / "receipt.json", {**record, "state": "FAILED", "error": "CLAIM_MIRROR_FAILED"})
    return Claim(inputs, registry, record, proof)


def finish(claim: Claim, state: str, details: dict[str, Any]) -> None:
    if state not in ("FAILED", "COMPLETED"):
        raise HoldoutError("Invalid terminal state")
    receipt = {**claim.record, "state": state, "finished_at": datetime.now(timezone.utc).isoformat(), **details}
    exclusive_json(claim.anchor / "receipt.json", receipt)
    exclusive_json(claim.inputs.output / "receipt.json", receipt)


def selected_records(claim: Claim) -> dict[str, dict[str, Any]]:
    from download_independent_block import planned_archives

    manifest = read_json(claim.inputs.source_manifest)
    jobs = {job.path: job for job in planned_archives("2022-08", "2023-05")}
    records = {}
    for record in manifest["archives"]:
        relative = record["path"]
        if relative not in jobs:
            continue
        job = jobs[relative]
        if (
            relative in records or record["status"] != "OK" or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
            or any(record[key] != getattr(job, key) for key in ("symbol", "kind", "interval", "month"))
        ):
            raise HoldoutError("Source archive evidence/grid differs")
        records[relative] = record
    if set(records) != set(jobs):
        raise HoldoutError("Incomplete 400 ZIP source inventory")
    return records


def load_claimed_dataset(claim: Claim | None, *, backend: Path = BACKEND):
    """Native loader behind a claim gate; existing measurement guard is untouched."""
    if not isinstance(claim, Claim):
        raise HoldoutError("TEST access denied without a running exclusive claim")
    claim.validate(backend)
    root = claim.inputs.test_root.resolve()
    if not root.is_dir():
        raise HoldoutError("Manual TEST extraction missing; attempt consumed")
    records = selected_records(claim)
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*.zip")}
    if actual != set(records):
        raise HoldoutError("TEST/warmup ZIP grid must match exact source paths")
    allowed_files = set(records) | {name + ".CHECKSUM" for name in records}
    if any(path.relative_to(root).as_posix() not in allowed_files for path in root.rglob("*") if path.is_file()):
        raise HoldoutError("Foreign files in manually extracted TEST root")
    projection = claim.inputs.output / "verified-input"
    projection.mkdir(exist_ok=False)
    for relative, record in records.items():
        source = root.joinpath(*PurePosixPath(relative).parts)
        if source.is_symlink() or not source.resolve().is_relative_to(root) or digest(source) != record["sha256"]:
            raise HoldoutError("TEST/warmup ZIP path/hash differs")
        checksum = Path(str(source) + ".CHECKSUM")
        if checksum.exists() and (checksum.is_symlink() or digest(checksum) != record["checksum_file_sha256"]):
            raise HoldoutError("CHECKSUM file differs from source manifest")
        target = projection.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if digest(target) != record["sha256"]:
            raise HoldoutError("Verified ZIP copy differs")
    exclusive_json(projection / "manifest.json", {"archives": list(records.values())})
    claim.validate(backend)
    from app.backtest_data import load_dataset

    start, end = (int(datetime.fromisoformat(value).timestamp()) for value in PROTOCOL["splits"]["TEST"])
    data = load_dataset(projection, claim.inputs.metadata, start, end)
    if set(data.frames) != set(SYMBOLS) or set(data.marks) != set(SYMBOLS) or data.report["missing_archives"]:
        raise HoldoutError("Loaded TEST universe incomplete")
    warmup = int(datetime.fromisoformat(PROTOCOL["splits"]["warmup_start"]).timestamp())
    for symbol in SYMBOLS:
        for part in data.report["symbols"][symbol].values():
            if part.get("duplicates", 0):
                raise HoldoutError("Duplicate candle evidence")
        for series in (*data.frames[symbol].values(), data.marks[symbol]):
            if any(not warmup <= at < end for at in series.times):
                raise HoldoutError("Loaded candles escape TEST/warmup scope")
        if any(not warmup <= event["time"] < end for event in data.funding[symbol]):
            raise HoldoutError("Loaded funding escapes TEST/warmup scope")
    return data


def verify_zip_inputs(claim: Claim) -> None:
    """Rehash all staged inputs after replay; never silently accept a changed source."""
    projection = claim.inputs.output / "verified-input"
    records = selected_records(claim)
    manifest = read_json(projection / "manifest.json")
    if (
        set(manifest) != {"archives"} or len(manifest["archives"]) != len(records)
        or {record["path"]: record for record in manifest["archives"]} != records
        or {path.relative_to(claim.inputs.test_root).as_posix() for path in claim.inputs.test_root.rglob("*.zip")} != set(records)
    ):
        raise HoldoutError("Verified input ledger/grid changed")
    for record in manifest["archives"]:
        parts = PurePosixPath(record["path"]).parts
        for root in (projection, claim.inputs.test_root):
            if digest(root.joinpath(*parts)) != record["sha256"]:
                raise HoldoutError("ZIP input changed during the bundle")
        checksum = claim.inputs.test_root.joinpath(*parts)
        checksum = Path(str(checksum) + ".CHECKSUM")
        if checksum.exists() and digest(checksum) != record["checksum_file_sha256"]:
            raise HoldoutError("CHECKSUM input changed during the bundle")
