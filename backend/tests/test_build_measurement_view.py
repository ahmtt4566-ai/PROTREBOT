"""Synthetic ZIP/manifest fixtures only; never inspect real historical inputs.

Branches: exact 920-pair scope, skipped TEST/audit JSON, preserved records,
both file hashes, no ZIP parsing during build, source immutability, existing
output, unsafe locations, missing/duplicate/bad evidence, copy failures,
early TEST/range denial, allowed native loading and raw native guard limitation.
"""

from __future__ import annotations

import copy
import json
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import build_measurement_view as view
import download_independent_block as block
import test_download_independent_block as synthetic
from app import backtest_data

no_network = synthetic.no_network
POISON = "DO_NOT_PARSE_TEST_HEALTH_PAYLOAD"


def fingerprints(root):
    return {
        str(path.relative_to(root)): view.sha256_file(path)
        for path in sorted(root.rglob("*")) if path.is_file()
    }


@pytest.fixture(scope="module")
def synthetic_source(tmp_path_factory):
    root = tmp_path_factory.mktemp("measurement_source")
    records = []
    for job in block.planned_archives("2020-10", "2023-05"):
        at = int(block.month_start(job.month).timestamp())
        if job.interval:
            step = block.SECONDS[job.interval]
            rows = [[str(at * 1000), "100", "101", "99", "100", "20",
                     str((at + step) * 1000 - 1), "2000", "1", "0", "0", "0"]]
        else:
            rows = [[str(at * 1000), "8", "0.0001"]]
        record = synthetic.write_job(root, job, rows=rows)
        if job.month >= "2022-09":
            record[POISON] = {"records": 987654321, "funding": "FORBIDDEN_SYNTHETIC_SENTINEL"}
        records.append(record)
    manifest = {
        "schema": block.SCHEMA, "archives": records,
        "audit": {POISON: {"gaps": ["FORBIDDEN_SYNTHETIC_SENTINEL"]}},
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return root, manifest


@pytest.fixture(scope="module")
def completed_view(synthetic_source, tmp_path_factory):
    source, manifest = synthetic_source
    output = tmp_path_factory.mktemp("measurement_parent") / "view"
    before = fingerprints(source)
    actual_loads = json.loads

    def selected_only(payload, *args, **kwargs):
        text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        assert POISON not in text
        assert '"audit"' not in text
        return actual_loads(payload, *args, **kwargs)

    with patch.object(view, "json", wraps=json) as decoder, \
            patch.object(zipfile, "ZipFile", side_effect=AssertionError("Builder must never parse ZIPs")):
        decoder.loads.side_effect = selected_only
        report = view.build_view(source, output)
        assert decoder.loads.call_count == 920
    assert fingerprints(source) == before
    return source, output, manifest, report


def metadata_file(tmp_path):
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps({"brackets": {"BTCUSDT": {}}, "exchange_info": {}}), encoding="utf-8")
    return path


def test_exact_copy_scope_records_hashes_and_no_test_health(completed_view):
    source, output, original, report = completed_view
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    allowed = [record for record in original["archives"] if record["month"] <= "2022-08"]
    assert manifest["archives"] == allowed
    assert len(allowed) == len(list(output.rglob("*.zip"))) == 920
    assert len(list(output.rglob("*.CHECKSUM"))) == 920
    assert report["copied_zip_count"] == report["copied_checksum_count"] == 920
    assert report["zip_hash_matches"] == report["checksum_hash_matches"] == 920
    assert report["excluded_zip_count"] == 360
    assert report["excluded_checksum_count"] == 360
    assert manifest["measurement_view"]["test_months_excluded"] is True
    assert manifest["measurement_view"]["health_audit_included"] is False
    assert "audit" not in manifest and "stage_overlap" not in manifest
    assert POISON not in (output / "manifest.json").read_text(encoding="utf-8")
    assert manifest["source_manifest_sha256"] == view.sha256_file(source / "manifest.json")
    assert report["manifest_sha256"] == view.sha256_file(output / "manifest.json")
    for record in allowed:
        source_zip = source.joinpath(*record["path"].split("/"))
        copied_zip = output.joinpath(*record["path"].split("/"))
        assert not source_zip.samefile(copied_zip)
        assert view.sha256_file(copied_zip) == record["sha256"]
        checksum = copied_zip.with_name(copied_zip.name + ".CHECKSUM")
        assert view.sha256_file(checksum) == record["checksum_file_sha256"]
        assert record["month"] < "2022-09"


@pytest.mark.parametrize("month", ["2020-09", "2022-09", "2022-10", "2023-05"])
def test_copy_month_guard_explicitly_denies_out_of_scope(month):
    with pytest.raises(view.MeasurementScopeError, match="denied"):
        view.require_measurement_month(month)


def test_existing_output_stops_before_source_read(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "do-not-touch.txt"
    marker.write_text("keep", encoding="utf-8")
    with patch.object(Path, "read_bytes", side_effect=AssertionError("No source read permitted")), \
            pytest.raises(FileExistsError, match="already exists"):
        view.build_view(tmp_path / "missing-source", output)
    assert marker.read_text(encoding="utf-8") == "keep"


def test_source_and_repository_outputs_are_rejected(synthetic_source, tmp_path):
    source, _manifest = synthetic_source
    for target in (source / "nested-view", view.REPOSITORY / tmp_path.name):
        with pytest.raises(view.MeasurementScopeError, match="outside"):
            view.build_view(source, target)
        assert not target.exists()


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "path", "month", "hash", "status", "health"])
def test_bad_selected_manifest_fails_closed_before_copy(synthetic_source, mutation):
    _source, original = synthetic_source
    manifest = copy.deepcopy(original)
    if mutation == "missing":
        manifest["archives"].pop(0)
    elif mutation == "duplicate":
        manifest["archives"].append(copy.deepcopy(manifest["archives"][0]))
    elif mutation == "path":
        manifest["archives"][0]["path"] = "../outside-2020-10.zip"
    elif mutation == "month":
        manifest["archives"][0]["month"] = "2022-09"
    elif mutation == "hash":
        manifest["archives"][0]["sha256"] = "0" * 64
    elif mutation == "status":
        manifest["archives"][0]["status"] = "UNAVAILABLE"
    else:
        manifest["archives"][0]["audit"] = {"records": 123}
    with pytest.raises(view.MeasurementScopeError):
        view.selected_records(json.dumps(manifest).encode("utf-8"))


@pytest.mark.parametrize("suffix", ["", ".CHECKSUM"])
def test_source_file_checksum_mismatch_leaves_output_absent(synthetic_source, tmp_path, suffix):
    source, _manifest = synthetic_source
    output = tmp_path / "not-created"
    real_hash = view.sha256_file
    selected = block.planned_archives("2020-10", "2020-10")[0]
    poisoned = source.joinpath(*selected.path.split("/"))
    if suffix:
        poisoned = poisoned.with_name(poisoned.name + suffix)

    def wrong_hash(path):
        return "0" * 64 if path == poisoned else real_hash(path)

    with patch.object(view, "sha256_file", side_effect=wrong_hash), \
            pytest.raises(view.MeasurementScopeError, match="Source file SHA"):
        view.build_view(source, output)
    assert not output.exists()


def test_copy_failure_never_publishes_final_manifest(synthetic_source, tmp_path):
    source, _manifest = synthetic_source
    output = tmp_path / "incomplete"
    with patch.object(view.shutil, "copyfileobj", side_effect=OSError("Synthetic copy failure")), \
            pytest.raises(OSError, match="Synthetic copy failure"):
        view.build_view(source, output)
    assert output.is_dir()
    assert not (output / "manifest.json").exists()


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (view.TEST_START, view.TEST_START + 900),
        (view.TEST_START + 900, view.TEST_START + 1800),
        (view.TEST_START - 900, view.TEST_START + 900),
    ],
)
def test_guard_denies_test_before_any_zip_or_manifest_read(completed_view, tmp_path, start, end):
    _source, output, _original, _report = completed_view
    metadata = metadata_file(tmp_path)
    with patch.object(Path, "open", side_effect=AssertionError("Denied request must open no file")), \
            patch.object(backtest_data, "load_dataset", side_effect=AssertionError("Native call is forbidden")), \
            pytest.raises(view.MeasurementScopeError, match="TEST access denied"):
        view.load_measurement_dataset(output, metadata, start, end)


@pytest.mark.parametrize(
    ("start", "end"),
    [(True, view.START + 900), (view.START, view.START), (view.START + 1, view.START + 900)],
)
def test_invalid_period_rejected_explicitly(start, end):
    with pytest.raises(view.MeasurementScopeError, match="Invalid measurement"):
        view.load_measurement_dataset(Path("unused"), Path("unused"), start, end)


def test_earlier_warmup_reads_are_denied():
    with pytest.raises(view.MeasurementScopeError, match="earlier reads denied"):
        view.load_measurement_dataset(Path("unused"), Path("unused"), view.START - 900, view.START)


def test_allowed_request_calls_real_native_loader_on_tiny_synthetic_archives(completed_view, tmp_path):
    _source, output, _original, _report = completed_view
    metadata = metadata_file(tmp_path)
    with patch.object(backtest_data, "load_dataset", wraps=backtest_data.load_dataset) as actual:
        data = view.load_measurement_dataset(output, metadata, view.START, view.START + 900)
    assert actual.call_count == 1
    assert isinstance(data, backtest_data.Dataset)
    assert data.report["symbols"]["BTCUSDT"]["klines_15m"]["rows_in_period"] == 1
    assert len(data.report["sources"]) == 23 * 5
    assert all(source["sha256"] for source in data.report["sources"])


def test_validation_half_open_end_at_test_boundary_is_allowed(completed_view, tmp_path):
    _source, output, _original, _report = completed_view
    result = view.load_measurement_dataset(
        output, metadata_file(tmp_path), view.TEST_START - 900, view.TEST_START,
    )
    assert isinstance(result, backtest_data.Dataset)


def test_raw_native_loader_does_not_supply_the_new_test_guard(completed_view, tmp_path):
    """Document the limitation honestly; do not assert nonexistent native errors."""
    _source, output, _original, _report = completed_view
    result = backtest_data.load_dataset(
        output, metadata_file(tmp_path), view.TEST_START, view.TEST_START + 900,
    )
    report = result.report["symbols"]["BTCUSDT"]["klines_15m"]
    assert report["rows_in_period"] == 0
    assert report["missing_candles"] == 1


@pytest.mark.parametrize(
    "mutation", ["scope", "test_archive", "source_hash", "duplicate", "health", "root_type", "end_month"],
)
def test_tampered_view_is_denied_before_native_read(completed_view, tmp_path, mutation):
    _source, output, _original, _report = completed_view
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    if mutation == "scope":
        manifest["measurement_view"]["test_months_excluded"] = False
    elif mutation == "test_archive":
        manifest["archives"][0]["month"] = "2022-09"
    elif mutation == "source_hash":
        manifest["source_manifest_sha256"] = "not-a-hash"
    elif mutation == "duplicate":
        manifest["archives"][-1] = copy.deepcopy(manifest["archives"][0])
    elif mutation == "health":
        manifest["audit"] = {"test_details": POISON}
    elif mutation == "root_type":
        manifest = []
    else:
        manifest["end_month"] = "2022-09"
    root = tmp_path / "tampered"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with patch.object(backtest_data, "load_dataset", side_effect=AssertionError("Native must not run")), \
            pytest.raises(view.MeasurementScopeError):
        view.load_measurement_dataset(root, metadata_file(tmp_path), view.START, view.START + 900)


def test_cli_failure_is_explicit(tmp_path, capsys):
    existing = tmp_path / "existing"
    existing.mkdir()
    assert view.main(["--source", str(tmp_path / "none"), "--output", str(existing)]) == 1
    assert "MEASUREMENT_VIEW_FAILED" in capsys.readouterr().err
