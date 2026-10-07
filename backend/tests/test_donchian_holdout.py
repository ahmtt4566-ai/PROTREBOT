"""Only synthetic claims, ZIPs and trades; never real TEST or sealed archives."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import sys
import zipfile
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import donchian_holdout_cli as cli
import test_offline_facade_funding_gaps as funding
import test_offline_facade_gap_blackout as gaps
import test_offline_facade_lifecycle as life
from app import donchian_holdout as protocol
from app import donchian_holdout_statistics as stats
from build_measurement_view import MeasurementScopeError, load_measurement_dataset
from download_independent_block import month_start, planned_archives
from measure_donchian_counts import Phase, make_engine, primary_config, quiet_native

offline_only = life.offline_only
AT, BTC, STEP, DAY = gaps.AT, gaps.BTC, 900, 86400


def sha_bytes(value):
    return hashlib.sha256(value).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


@pytest.fixture
def context(tmp_path, monkeypatch):
    backend = tmp_path / "fake-repository" / "backend"
    for name in protocol.source_names():
        path = backend.joinpath(*name.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("SYNTHETIC CODE " + name, encoding="utf-8")
    inputs = tmp_path / "evidence"
    inputs.mkdir()
    payloads, records = {}, []
    for job in planned_archives("2022-08", "2023-05"):
        at = int(month_start(job.month).timestamp())
        if job.kind == "fundingRate":
            content = f"calc_time,funding_interval_hours,last_funding_rate\n{at * 1000},8,0.0001\n"
        else:
            content = f"open_time,open,high,low,close,volume,quote_volume\n{at * 1000},300,301,299,300,100,30000\n"
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr(job.filename.replace(".zip", ".csv"), content)
        payloads[job.path] = stream.getvalue()
        records.append({**asdict(job), "status": "OK", "sha256": sha_bytes(payloads[job.path]),
                        "checksum_file_sha256": "0" * 64})
    values = {
        "measurement_manifest": {}, "source_manifest": {"archives": records},
        "metadata": life.metadata(protocol.SYMBOLS, "0.01"), "counts": {}, "summary": {},
    }
    paths = {}
    for name, value in values.items():
        paths[name] = inputs / f"{name}.json"
        write(paths[name], value)
    hashes = {name: protocol.digest(path) for name, path in paths.items()}
    monkeypatch.setattr(protocol, "PINNED_INPUTS", hashes)
    plan = {
        "schema": protocol.SCHEMA, "registered_at": "SYNTHETIC",
        "protocol": copy.deepcopy(protocol.PROTOCOL), "input_sha256": hashes,
        "code_sha256": protocol.code_hashes(backend), "sealed_test_archive_sha256": "a" * 64,
    }
    plan_path = backend / "studies" / "plan.json"
    write(plan_path, plan)
    ctx = protocol.Inputs(plan_path, **paths, test_root=tmp_path / "manual-test",
                          output=tmp_path / "new-output")
    return ctx, backend, tmp_path / "registry", payloads


def manual_extract(ctx, payloads):
    for relative, payload in payloads.items():
        path = ctx.test_root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)


def acquire(context):
    ctx, backend, anchor, payloads = context
    protocol.lock(ctx, backend=backend, anchor=anchor)
    manual_extract(ctx, payloads)
    return protocol.claim_once(ctx, backend=backend, anchor=anchor)


def row(*, net=1.0, r=None, at=AT, symbol=BTC, status="CLOSED", complete=True):
    return {
        "symbol": symbol, "opened_at": datetime.fromtimestamp(at, timezone.utc).isoformat(),
        "status": status, "commission_complete": status == "CLOSED",
        "funding_complete": complete and status == "CLOSED", "funding_usdt": 0.0 if complete else None,
        "net_pnl": net, "net_r": net if r is None else r,
    }


def test_dry_and_lock_read_no_test_or_archive_or_signals(context):
    ctx, backend, anchor, _ = context
    with patch.object(cli, "bundle", side_effect=AssertionError("No strategy")), \
            patch.object(cli, "load_claimed_dataset", side_effect=AssertionError("No TEST")):
        dry = protocol.dry_verify(ctx, backend=backend, anchor=anchor)
        locked = protocol.lock(ctx, backend=backend, anchor=anchor)
    assert dry["signals"] == dry["replays"] == dry["test_files_read"] == 0
    assert dry["archive_read"] is False
    assert dry["archive_hash_user_attested"] is True
    assert not ctx.test_root.exists() and not (anchor / "test-once.json").exists()
    assert locked["evidence"]["plan_sha256"] == protocol.digest(ctx.plan)
    assert protocol.lock(ctx, backend=backend, anchor=anchor) == locked


@pytest.mark.parametrize("change", ["code", "metadata", "source_manifest", "measurement_manifest", "counts", "summary"])
def test_dry_rejects_fingerprint_change(context, change):
    ctx, backend, anchor, _ = context
    path = backend / "app" / "execution_core.py" if change == "code" else getattr(ctx, change)
    path.write_text("CHANGED_SYNTHETIC", encoding="utf-8")
    with pytest.raises(protocol.HoldoutError, match="hash|fingerprint"):
        protocol.dry_verify(ctx, backend=backend, anchor=anchor)
    assert not (anchor / "test-once.json").exists()


def test_empty_user_archive_hash_fails_before_test_read(context):
    ctx, backend, anchor, _ = context
    plan = protocol.read_json(ctx.plan)
    plan["sealed_test_archive_sha256"] = ""
    write(ctx.plan, plan)
    with pytest.raises(protocol.HoldoutError, match="empty/invalid"):
        protocol.dry_verify(ctx, backend=backend, anchor=anchor)


@pytest.mark.parametrize("mutation", ["k", "confidence", "family", "n", "stress", "tp_entries", "declarations"])
def test_unregistered_parameter_or_declaration_changes_fail_closed(context, mutation):
    ctx, backend, anchor, _ = context
    plan = protocol.read_json(ctx.plan)
    fixed = plan["protocol"]
    if mutation == "k":
        fixed["candidate"]["k"] = 2.0
    elif mutation == "confidence":
        fixed["confidence_tiebreak"] = True
    elif mutation == "family":
        fixed["bootstrap"]["family_size"] = 3
    elif mutation == "n":
        fixed["success"]["fully_verified_completed_min"] = 59
    elif mutation == "stress":
        fixed["sensitivities"][1]["spread_bps"] = 2
    elif mutation == "tp_entries":
        fixed["sensitivities"][0]["entry_schedule"] = "RESCAN"
    else:
        fixed["declarations"]["kais_original_pre_2023_06_partly_seen"] = "UNSEEN"
    write(ctx.plan, plan)
    with pytest.raises(protocol.HoldoutError, match="Locked protocol"):
        protocol.dry_verify(ctx, backend=backend, anchor=anchor)
    assert not (anchor / "test-once.json").exists()


@pytest.mark.parametrize("nonempty", [False, True])
def test_existing_test_directory_is_rejected_even_empty(context, nonempty):
    ctx, backend, anchor, _ = context
    ctx.test_root.mkdir()
    if nonempty:
        (ctx.test_root / "synthetic.txt").write_text("FAKE", encoding="utf-8")
    with pytest.raises(protocol.HoldoutError, match="already exists"):
        protocol.dry_verify(ctx, backend=backend, anchor=anchor)


def test_claim_consumed_once_across_outputs_and_changed_plans(context):
    ctx, backend, anchor, _ = context
    claim = acquire(context)
    claim.validate(backend)
    with pytest.raises(protocol.HoldoutError, match="already claimed"):
        protocol.claim_once(ctx, backend=backend, anchor=anchor)
    another = replace(ctx, output=ctx.output.parent / "second-output")
    with pytest.raises(protocol.HoldoutError, match="already claimed"):
        protocol.claim_once(another, backend=backend, anchor=anchor)
    protocol.finish(claim, "FAILED", {"error": "SYNTHETIC_FAILURE"})
    with pytest.raises(protocol.HoldoutError, match="running claim"):
        claim.validate(backend)
    with pytest.raises(protocol.HoldoutError, match="consumed"):
        protocol.dry_verify(ctx, backend=backend, anchor=anchor)
    assert protocol.read_json(anchor / "test-once.json")["state"] == "RUNNING"
    assert protocol.read_json(anchor / "receipt.json")["state"] == "FAILED"


def test_changed_plan_after_lock_denied_before_claim(context):
    ctx, backend, anchor, _ = context
    protocol.lock(ctx, backend=backend, anchor=anchor)
    plan = protocol.read_json(ctx.plan)
    plan["registered_at"] = "CHANGED"
    write(ctx.plan, plan)
    with pytest.raises(protocol.HoldoutError, match="Unchanged"):
        protocol.claim_once(ctx, backend=backend, anchor=anchor)
    assert not (anchor / "test-once.json").exists()


def test_registration_cannot_move_to_another_output(context):
    ctx, backend, anchor, _ = context
    protocol.lock(ctx, backend=backend, anchor=anchor)
    second = replace(ctx, output=ctx.output.parent / "other-results")
    with pytest.raises(protocol.HoldoutError, match="already exists"):
        protocol.lock(second, backend=backend, anchor=anchor)


def test_claim_mirror_failure_consumes_global_attempt(context):
    ctx, backend, anchor, payloads = context
    protocol.lock(ctx, backend=backend, anchor=anchor)
    manual_extract(ctx, payloads)
    original = protocol.exclusive_json

    def partial_write_failure(path, record, **kwargs):
        if path == ctx.output / "test-once.json":
            path.write_text("PARTIAL", encoding="utf-8")
            raise OSError("SYNTHETIC_IO_FAILURE")
        return original(path, record, **kwargs)

    with patch.object(protocol, "exclusive_json", side_effect=partial_write_failure), pytest.raises(OSError):
        protocol.claim_once(ctx, backend=backend, anchor=anchor)
    assert protocol.read_json(anchor / "receipt.json")["state"] == "FAILED"
    with pytest.raises(protocol.HoldoutError, match="already claimed"):
        protocol.claim_once(ctx, backend=backend, anchor=anchor)


def test_test_guard_denies_no_claim_without_opening_any_file(tmp_path):
    with patch.object(Path, "open", side_effect=AssertionError("No files")) as opened:
        with pytest.raises(protocol.HoldoutError, match="without a running"):
            protocol.load_claimed_dataset(None)
        start = int(datetime(2022, 9, 1, tzinfo=timezone.utc).timestamp())
        with pytest.raises(MeasurementScopeError, match="TEST access denied"):
            load_measurement_dataset(tmp_path, tmp_path / "metadata.json", start, start + STEP)
    assert opened.call_count == 0


def test_claim_gated_native_loader_parses_only_400_synthetic_test_warmup_zips(context, offline_only):
    claim = acquire(context)
    data = protocol.load_claimed_dataset(claim, backend=context[1])
    assert set(data.frames) == set(protocol.SYMBOLS)
    assert len(data.report["sources"]) == 400
    assert len(protocol.read_json(claim.inputs.output / "verified-input" / "manifest.json")["archives"]) == 400
    protocol.verify_zip_inputs(claim)
    start = int(datetime(2022, 9, 1, tzinfo=timezone.utc).timestamp())
    with pytest.raises(MeasurementScopeError, match="TEST access denied"):
        load_measurement_dataset(claim.inputs.test_root, claim.inputs.metadata, start, start + STEP)


def test_changed_zip_or_ledger_is_not_silently_accepted(context, offline_only):
    claim = acquire(context)
    protocol.load_claimed_dataset(claim, backend=context[1])
    write(claim.inputs.output / "verified-input" / "manifest.json", {"archives": []})
    with pytest.raises(protocol.HoldoutError, match="ledger/grid"):
        protocol.verify_zip_inputs(claim)


def test_bad_zip_fails_after_claim_and_cannot_retry(context, offline_only):
    claim = acquire(context)
    first = next(claim.inputs.test_root.rglob("*.zip"))
    first.write_bytes(b"BAD_SYNTHETIC")
    with pytest.raises(protocol.HoldoutError, match="path/hash"):
        protocol.load_claimed_dataset(claim, backend=context[1])
    with pytest.raises(protocol.HoldoutError, match="already claimed"):
        protocol.claim_once(claim.inputs, backend=context[1], anchor=claim.anchor)


@pytest.mark.parametrize("failure", [ValueError("SYNTHETIC"), KeyboardInterrupt()])
def test_post_claim_exception_or_interruption_records_failure(context, monkeypatch, offline_only, failure):
    ctx, backend, anchor, payloads = context
    protocol.lock(ctx, backend=backend, anchor=anchor)
    manual_extract(ctx, payloads)
    for name in ("DATA_DIR", "PROTREBOT_DATA_DIR", "DATABASE_URL", "PROTREBOT_DURABLE_AUTH_REQUIRED", "ASSISTANT_LIVE_TESTS"):
        monkeypatch.setenv(name, os.environ.get(name, ""))
    monkeypatch.setattr(cli.asyncio, "new_event_loop", lambda: offline_only)
    with patch.object(cli, "load_claimed_dataset", side_effect=failure), pytest.raises(type(failure)):
        cli.execute(ctx, backend=backend, anchor=anchor)
    assert protocol.read_json(anchor / "receipt.json")["state"] == "FAILED"
    with pytest.raises(protocol.HoldoutError, match="already claimed"):
        protocol.claim_once(ctx, backend=backend, anchor=anchor)


def test_completed_bundle_receipt_result_hash_and_no_second_run(context, monkeypatch, offline_only):
    ctx, backend, anchor, payloads = context
    protocol.lock(ctx, backend=backend, anchor=anchor)
    manual_extract(ctx, payloads)
    for name in ("DATA_DIR", "PROTREBOT_DATA_DIR", "DATABASE_URL", "PROTREBOT_DURABLE_AUTH_REQUIRED", "ASSISTANT_LIVE_TESTS"):
        monkeypatch.setenv(name, os.environ.get(name, ""))
    monkeypatch.setattr(cli.asyncio, "new_event_loop", lambda: offline_only)

    async def synthetic_bundle(_data, claim):
        start, end = (int(datetime.fromisoformat(value).timestamp()) for value in protocol.PROTOCOL["splits"]["TEST"])
        scenarios = {}
        for name in ("primary", "tp_first", "stress"):
            scenario = stats.scenario_report([], {}, start, end, symbols=protocol.SYMBOLS)
            scenario.update(counts=None, confirmatory=name == "primary",
                            entry_mode="FROZEN_PRIMARY" if name == "tp_first" else "FRESH_NATIVE_GATES")
            scenarios[name] = scenario
        benchmark = copy.deepcopy(scenarios["primary"])
        benchmark.pop("criteria")
        benchmark["descriptive_only"] = True
        return {
            "schema": protocol.SCHEMA, "plan_sha256": claim.evidence["plan_sha256"],
            "claim_id": claim.record["claim_id"], "primary_scenario": "primary", "candidate_count": 1,
            "multiple_comparisons": False, "scenarios": scenarios, "original_benchmark": benchmark,
            "decision": scenarios["primary"]["criteria"], "declarations": protocol.PROTOCOL["declarations"],
            "archive_sha256_user_attested_not_read": claim.evidence["sealed_archive_sha256_attestation"],
        }

    result = cli.execute(ctx, backend=backend, anchor=anchor, runner=synthetic_bundle)
    receipt = protocol.read_json(anchor / "receipt.json")
    assert receipt["state"] == "COMPLETED" and result["decision"]["passed"] is False
    assert receipt["results_sha256"] == protocol.digest(ctx.output / "holdout-results.json")
    assert receipt == protocol.read_json(ctx.output / "receipt.json")
    with pytest.raises(protocol.HoldoutError, match="already claimed"):
        cli.execute(ctx, backend=backend, anchor=anchor, runner=synthetic_bundle)


@pytest.mark.parametrize(("mean", "pf", "n", "status", "passed"), [
    (0.1, 1.21, 60, "DEFINED", True), (0.1, 1.2, 60, "DEFINED", False),
    (0.0, 1.21, 60, "DEFINED", False), (-0.1, 2, 60, "DEFINED", False),
    (0.1, 2, 59, "DEFINED", False), (None, None, 0, "UNDEFINED", False),
    (0.1, None, 60, "UNDEFINED", False), (0.1, None, 60, "NO_LOSSES", True),
])
def test_strict_point_criteria_boundaries(mean, pf, n, status, passed):
    value = {"mean_net_r": mean, "profit_factor": pf, "fully_verified_completed": n,
             "profit_factor_status": status, "net_pnl": 1 if mean else None}
    assert stats.criteria(value)["passed"] is passed


def test_verified_n_excludes_open_unknown_funding_missing_and_nonfinite():
    rows = [row(), row(status="OPEN_AT_END"), row(status="UNKNOWN_DATA_GAP"),
            row(complete=False), row(r=float("nan"))]
    result = stats.summary(rows)
    assert result["fully_verified_completed"] == 1
    assert result["open_at_end"] == result["unknown_data_gap"] == result["funding_incomplete"] == 1
    assert stats.summary([])["profit_factor_status"] == "UNDEFINED"
    assert stats.criteria(stats.summary([]))["passed"] is False
    zero = stats.summary([row(net=0)] * 60)
    assert zero["fully_verified_completed"] == 60 and zero["profit_factor_status"] == "UNDEFINED"
    assert stats.criteria(zero)["passed"] is False


def test_bootstrap_fixed_seed_trade_and_empty_utc_day_blocks_are_reproducible():
    rows = [row(net=2), row(net=-1, at=AT + DAY)]
    first = stats.bootstrap(rows, AT, AT + 3 * DAY, samples=200, seed=2026)
    assert first == stats.bootstrap(rows, AT, AT + 3 * DAY, samples=200, seed=2026)
    assert set(first) == {"TRADE", "UTC_DAY"}
    assert first["UTC_DAY"]["total_clusters"] == 3 and first["UTC_DAY"]["active_clusters"] == 2
    assert first["UTC_DAY"]["empty_resamples"] > 0
    assert first["TRADE"]["family_size"] == 1 and first["TRADE"]["informational_only"] is True
    json.dumps(first, allow_nan=False)


def test_synthetic_bundle_schema_primary_only_decision_and_frozen_entries(monkeypatch, offline_only):
    local_protocol = copy.deepcopy(cli.PROTOCOL)
    local_protocol["splits"]["TEST"] = [
        datetime.fromtimestamp(AT, timezone.utc).isoformat(),
        datetime.fromtimestamp(AT + 2 * DAY, timezone.utc).isoformat(),
    ]
    monkeypatch.setattr(cli, "PROTOCOL", local_protocol)
    data = funding.schedule(gaps.data_for(bars=192, symbols=protocol.SYMBOLS))
    for series in data.marks.values():
        series.rows[33]["high"] = 320.0
    claim = type("SyntheticClaim", (), {
        "evidence": {"plan_sha256": "0" * 64, "sealed_archive_sha256_attestation": "a" * 64},
        "record": {"claim_id": "SYNTHETIC"},
    })()
    with quiet_native():
        report = offline_only.run_until_complete(cli.bundle(data, claim))
    cli.validate_report(report)
    assert report["candidate_count"] == 1 and report["multiple_comparisons"] is False
    assert report["decision"] == report["scenarios"]["primary"]["criteria"]
    assert report["scenarios"]["tp_first"]["entry_mode"] == "FROZEN_PRIMARY"
    assert report["original_benchmark"]["descriptive_only"] is True
    assert "criteria" not in report["original_benchmark"]
    assert set(report["scenarios"]["primary"]["by_symbol"]) == set(protocol.SYMBOLS)
    for scenario in report["scenarios"].values():
        assert scenario["bootstrap"]["TRADE"]["samples"] == 20000
    changed = copy.deepcopy(report)
    changed["scenarios"]["stress"]["confirmatory"] = True
    with pytest.raises(protocol.HoldoutError, match="Scenario"):
        cli.validate_report(changed)
    changed = copy.deepcopy(report)
    changed["decision"]["passed"] = not changed["decision"]["passed"]
    with pytest.raises(protocol.HoldoutError):
        cli.validate_report(changed)


def test_frozen_priority_retains_spec_quantity_time_and_no_entry_gates(offline_only):
    data = funding.schedule(gaps.data_for(bars=96))
    data.marks[BTC].rows[33].update(high=320.0, low=290.0)
    phase = Phase("SYNTHETIC", AT, AT + DAY)
    config = primary_config(phase, {"symbols": [BTC], "initial_equity": 1000, "max_total_exposure_usdt": 350,
                                  "primary": {"slippage_bps": 3, "spread_bps": 2, "intrabar": "STOP_FIRST"}})
    with quiet_native():
        primary = make_engine(data, config)
        offline_only.run_until_complete(primary.replay_counts(phase))
        with patch.object(life.core, "evaluate_entry_gates", side_effect=AssertionError("No frozen admission")):
            frozen = cli.frozen_priority(data, primary, replace(config, intrabar="TP_FIRST"))
    assert len(primary.state.trades) == len(frozen.state.trades) == 1
    old, new = primary.state.trades[0], frozen.state.trades[0]
    assert old.spec == new.spec and old.quantity == new.quantity and old.opened_at == new.opened_at
    assert old.status == new.status == "CLOSED"
    assert [exit["reason"] for exit in old.exits] == ["STOP"]
    assert [exit["reason"] for exit in new.exits] == ["TP1", "TP3"]
