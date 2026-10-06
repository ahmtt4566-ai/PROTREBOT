"""Synthetic local ZIP/checksum fixtures only; all real network access forbidden.

Branch matrix: eight-symbol URLs, mandatory dates, overlap boundaries and actual
overlapping rows, checksum syntax/mismatch/tampering, HTTP 404, missing months,
partial first/last months, duplicates, bad OHLC/CSV/ZIP/funding, read-only audit,
new output restrictions and network/write-free dry-run.
Fixture dates are synthetic and are not a selected research/holdout period.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import socket
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import download_independent_block as block


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Real network access is forbidden in synthetic tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)


def rows_for(job):
    start = int(block.month_start(job.month).timestamp())
    end = int(block.next_month(block.month_start(job.month)).timestamp())
    if job.interval is None:
        return [[str(at * 1000), "8", "0.0001"] for at in range(start, end, 28800)]
    step = block.SECONDS[job.interval]
    volume, quote = ("0", "0") if job.kind == "markPriceKlines" else ("20", "2000")
    return [[str(at * 1000), "100", "101", "99", "100", volume,
             str((at + step) * 1000 - 1), quote, "1", "0", "0", "0"]
            for at in range(start, end, step)]


def payload_for(job, rows):
    csv_text = io.StringIO()
    writer = csv.writer(csv_text)
    writer.writerow(block.CANDLE_HEADER if job.interval else block.FUNDING_HEADER)
    writer.writerows(rows)
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(job.filename.removesuffix(".zip") + ".csv", csv_text.getvalue())
    return zipped.getvalue()


def write_job(root, job, rows=None, payload=None):
    if payload is None:
        payload = payload_for(job, rows_for(job) if rows is None else rows)
    digest = hashlib.sha256(payload).hexdigest()
    checksum = f"{digest}  {job.filename}\n".encode("ascii")
    path = root.joinpath(*job.path.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.with_name(path.name + ".CHECKSUM").write_bytes(checksum)
    return block.checksum_evidence(job, payload, checksum)


def write_manifest(root, records, first="2020-01", last="2020-01"):
    root.mkdir(parents=True, exist_ok=True)
    manifest = {"schema": block.SCHEMA, "source": block.BASE,
                "start_month": first, "end_month": last, "archives": records}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


@pytest.fixture
def complete_block(tmp_path):
    root = tmp_path / "independent"
    jobs = block.planned_archives("2020-01", "2020-01")
    records = [write_job(root, job) for job in jobs]
    write_manifest(root, records)
    return root, jobs, records


def group(report, job):
    return next(row for row in report["groups"]
                if (row["symbol"], row["kind"], row["interval"]) == (job.symbol, job.kind, job.interval))


def test_url_generation_all_eight_symbols_and_five_streams():
    jobs = block.planned_archives("2020-01", "2020-02")
    assert len(jobs) == 80 and len({job.path for job in jobs}) == 80
    assert {job.symbol for job in jobs} == set(block.SYMBOLS)
    assert len(block.SYMBOLS) == 8
    assert {(job.kind, job.interval) for job in jobs} == set(block.STREAMS)
    assert jobs[0].url == block.BASE + "klines/BTCUSDT/15m/BTCUSDT-15m-2020-01.zip"
    funding = next(job for job in jobs if job.kind == "fundingRate")
    assert funding.url == block.BASE + "fundingRate/BTCUSDT/BTCUSDT-fundingRate-2020-01.zip"
    assert all(job.checksum_url == job.url + ".CHECKSUM" for job in jobs)


@pytest.mark.parametrize("first,last", [
    ("2023-06", "2023-06"), ("2023-05", "2023-06"),
    ("2026-09", "2026-10"), ("2020-01", "2027-01"),
])
def test_stage_overlap_raises_before_network_or_output(first, last, tmp_path, capsys):
    output = tmp_path / "never-created"
    with pytest.raises(block.OverlapError):
        block.planned_archives(first, last)
    assert block.main(["--output", str(output), "--start-month", first,
                       "--end-month", last, "--dry-run"]) == 2
    assert "Stage 3-6b" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize("first,last", [("2023-05", "2023-05"), ("2026-10", "2026-10")])
def test_half_open_overlap_boundary_accepts_adjacent_months(first, last):
    assert len(block.planned_archives(first, last)) == 40


@pytest.mark.parametrize("first,last", [("2020-1", "2020-02"), ("2020-13", "2020-13"),
                                       ("2020-02", "2020-01")])
def test_invalid_or_reversed_month_rejected(first, last):
    with pytest.raises(ValueError):
        block.planned_archives(first, last)


def test_dates_are_mandatory_no_defaults(tmp_path):
    with pytest.raises(SystemExit) as exc:
        block.main(["--output", str(tmp_path / "new"), "--dry-run"])
    assert exc.value.code == 2


def test_checksum_mismatch_retains_expected_actual_and_never_writes(tmp_path, monkeypatch):
    job = block.planned_archives("2020-01", "2020-01")[0]
    expected = hashlib.sha256(b"expected").hexdigest()
    checksum = f"{expected}  {job.filename}\n".encode()
    monkeypatch.setattr(block, "fetch_public", lambda url: checksum if url.endswith(".CHECKSUM") else b"different")
    output = tmp_path / "never-created"
    record = block.acquire_archive(output, job)
    assert record["status"] == "CHECKSUM_MISMATCH"
    assert record["expected_sha256"] == expected
    assert record["sha256"] == hashlib.sha256(b"different").hexdigest()
    assert record["checksum_matches"] is False
    assert "differs" in record["error"]
    assert not output.exists()


@pytest.mark.parametrize("text", ["", "g" * 64 + " file.zip", "a" * 64 + " wrong.zip", "a" * 64])
def test_bad_checksum_is_explicit_error(text):
    with pytest.raises(ValueError):
        block.parse_checksum(text.encode(), "file.zip")


@pytest.mark.parametrize("phase", ["CHECKSUM", "ZIP"])
def test_http_404_reports_exact_phase_and_month_without_network(tmp_path, monkeypatch, phase):
    job = block.planned_archives("2020-01", "2020-01")[0]
    checksum = f"{'a' * 64}  {job.filename}\n".encode()

    def fake(url):
        if phase == "ZIP" and url.endswith(".CHECKSUM"):
            return checksum
        raise urllib.error.HTTPError(url, 404, "not found", None, None)

    monkeypatch.setattr(block, "fetch_public", fake)
    record = block.acquire_archive(tmp_path / "never-created", job)
    assert record["status"] == "HTTP_404" and record["http_status"] == 404
    assert record["month"] == "2020-01" and record["phase"] == phase
    assert record["error"]
    assert not (tmp_path / "never-created").exists()


def test_complete_audit_is_read_only_and_counts_every_stream(complete_block):
    root, jobs, _ = complete_block
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}
    report = block.audit_block(root)
    assert report["valid"] is True and report["stage_overlap"] is False
    assert report["missing_archives"] == report["issues"] == report["source_failures"] == []
    assert len(report["archives"]) == len(report["groups"]) == 40
    for job in jobs:
        result = group(report, job)
        assert result["records"] == result["unique_records"] == len(rows_for(job))
        assert result["missing_candles"] == (0 if job.interval else None)
        assert result["duplicates"] == result["invalid_rows"] == result["invalid_ohlc"] == 0
        assert result["partial_months"] == result["missing_months"] == result["funding_gaps"] == []
        if job.kind == "fundingRate":
            assert result["funding_interval_hours"] == [8.0]
            assert result["funding_first"] == "2020-01-01T00:00:00+00:00"
            assert result["funding_last"] == "2020-01-31T16:00:00+00:00"
    after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}
    assert before == after


def test_missing_month_report_does_not_invent_records_or_funding_schedule(tmp_path):
    root = tmp_path / "missing"
    job = block.planned_archives("2020-01", "2020-01")[0]
    records = [write_job(root, job)]
    write_manifest(root, records, last="2020-02")
    report = block.audit_block(root)
    assert report["valid"] is False and len(report["missing_archives"]) == 79
    candle = group(report, job)
    assert candle["missing_months"] == ["2020-02"]
    assert candle["missing_candles"] == 29 * 96
    funding = next(row for row in report["groups"] if row["symbol"] == job.symbol and row["kind"] == "fundingRate")
    assert funding["records"] == 0 and funding["funding_interval_hours"] == []
    assert funding["funding_first"] is funding["funding_last"] is funding["missing_candles"] is None


def test_partial_first_last_months_and_duplicates_are_reported_without_repair(tmp_path):
    root = tmp_path / "partial"
    jobs = [job for job in block.planned_archives("2020-01", "2020-02")
            if job.symbol == "BTCUSDT" and job.kind == "klines" and job.interval == "1h"]
    first_rows, last_rows = rows_for(jobs[0])[24:], rows_for(jobs[1])[:-24]
    first_rows.append(first_rows[0])
    records = [write_job(root, jobs[0], first_rows), write_job(root, jobs[1], last_rows)]
    write_manifest(root, records, last="2020-02")
    before = hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    report = block.audit_block(root)
    result = group(report, jobs[0])
    assert result["missing_candles"] == 48 and result["duplicates"] == 1
    assert result["partial_months"] == ["2020-01", "2020-02"]
    first, last = report["archives"]
    assert first["boundary_month"] == "FIRST" and first["leading_incomplete"] is True
    assert last["boundary_month"] == "LAST" and last["trailing_incomplete"] is True
    assert report["valid"] is False
    assert hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest() == before


@pytest.mark.parametrize("case", ["ohlc", "nonfinite_ohlc", "funding_gap", "funding_duplicate", "funding_interval",
                                 "csv_schema", "invalid_zip", "tampered_zip", "tampered_checksum"])
def test_bad_local_data_cannot_pass_audit(complete_block, case):
    root, jobs, records = complete_block
    job = next(job for job in jobs if job.kind == "fundingRate") if case.startswith("funding") else jobs[0]
    rows = rows_for(job)
    if case == "ohlc":
        rows[10][2] = "90"
    elif case == "nonfinite_ohlc":
        rows[10][2] = "NaN"
    elif case == "funding_gap":
        del rows[10]
    elif case == "funding_duplicate":
        rows.append(rows[10])
    elif case == "funding_interval":
        rows[10][1] = "0"
    elif case == "csv_schema":
        rows[10] = rows[10][:3]
    if case in {"tampered_zip", "tampered_checksum"}:
        path = root.joinpath(*job.path.split("/"))
        if case == "tampered_checksum":
            path = path.with_name(path.name + ".CHECKSUM")
        path.write_bytes(b"tampered")
    else:
        replacement = write_job(root, job, rows, payload=b"not a ZIP" if case == "invalid_zip" else None)
        records[jobs.index(job)] = replacement
        write_manifest(root, records)
    report = block.audit_block(root)
    assert report["valid"] is False
    result = group(report, job)
    if case in {"ohlc", "nonfinite_ohlc"}:
        assert result["invalid_ohlc"] == result["invalid_rows"] == result["missing_candles"] == 1
    elif case == "funding_gap":
        assert len(result["funding_gaps"]) == 1
    elif case == "funding_duplicate":
        assert result["duplicates"] == 1
    elif case in {"funding_interval", "csv_schema"}:
        assert result["invalid_rows"] == 1
    else:
        assert report["issues"] and result["unusable_months"] == ["2020-01"]


def test_actual_overlapping_row_raises_even_when_manifest_range_is_independent(tmp_path):
    root = tmp_path / "false-label"
    job = block.planned_archives("2020-01", "2020-01")[0]
    rows = rows_for(job)[:1]
    rows[0][0] = str(int(block.SEEN_START) * 1000)
    write_manifest(root, [write_job(root, job, rows)])
    with pytest.raises(block.OverlapError):
        block.audit_block(root)


@pytest.mark.parametrize("case", ["inside_repo", "existing", "inside_study"])
def test_new_output_restrictions_precede_any_acquisition(tmp_path, case):
    if case == "inside_repo":
        path = block.REPOSITORY / "never-created-independent-block"
    elif case == "existing":
        path = tmp_path
    else:
        (tmp_path / "study-lock.json").write_text("{}", encoding="utf-8")
        path = tmp_path / "new-block"
    with pytest.raises((ValueError, FileExistsError)):
        block.output_path(path, new=True)


def test_dry_run_lists_every_zip_and_checksum_without_network_or_writes(tmp_path, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Dry-run cannot acquire, audit or write a manifest")

    monkeypatch.setattr(block, "download_block", forbidden)
    monkeypatch.setattr(block, "audit_block", forbidden)
    output = tmp_path / "not-created"
    assert block.main(["--output", str(output), "--start-month", "2020-01",
                       "--end-month", "2020-01", "--dry-run"]) == 0
    lines = capsys.readouterr().out.splitlines()
    jobs = block.planned_archives("2020-01", "2020-01")
    assert lines == [url for job in jobs for url in (job.url, job.checksum_url)]
    assert len(lines) == 80 and not output.exists()


def test_verify_only_never_downloads_and_rejects_wrong_requested_range(complete_block, monkeypatch, capsys):
    root, _, _ = complete_block

    def forbidden(*args, **kwargs):
        raise AssertionError("Verify-only must not download")

    monkeypatch.setattr(block, "download_block", forbidden)
    assert block.main(["--output", str(root), "--start-month", "2020-01",
                       "--end-month", "2020-01", "--verify-only"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert block.main(["--output", str(root), "--start-month", "2020-02",
                       "--end-month", "2020-02", "--verify-only"]) == 2
    assert "differs from manifest" in capsys.readouterr().err


def test_duplicate_manifest_path_is_explicit_failure(complete_block):
    root, _, records = complete_block
    write_manifest(root, [*records, records[0]])
    with pytest.raises(ValueError, match="Duplicate"):
        block.audit_block(root)


def test_fetch_rejects_nonarchive_source_before_network():
    with pytest.raises(ValueError, match="Only static"):
        block.fetch_public("https://fapi.binance.com/fapi/v1/exchangeInfo")


def test_manifest_boolean_checksum_cannot_be_replaced_with_numeric_true(complete_block):
    root, _, records = complete_block
    records[0]["checksum_matches"] = 1
    write_manifest(root, records)
    report = block.audit_block(root)
    assert report["valid"] is False and len(report["issues"]) == 1


@pytest.mark.parametrize("missing", [False, True])
def test_manifest_publication_with_in_memory_provider_only(tmp_path, monkeypatch, capsys, missing):
    """All response bytes are generated here; no external archives or network."""
    jobs = block.planned_archives("2020-01", "2020-01")
    responses = {}
    for job in jobs:
        payload = payload_for(job, rows_for(job))
        responses[job.url] = payload
        responses[job.checksum_url] = f"{hashlib.sha256(payload).hexdigest()}  {job.filename}\n".encode()
    calls = []

    def synthetic_provider(url):
        calls.append(url)
        if missing and url == jobs[0].checksum_url:
            raise urllib.error.HTTPError(url, 404, "synthetic missing month", None, None)
        return responses[url]

    monkeypatch.setattr(block, "fetch_public", synthetic_provider)
    output = tmp_path / "synthetic-block"
    result = block.download_block(output, "2020-01", "2020-01")
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8")) == result
    assert len(result["archives"]) == 40
    assert result["unavailable_count"] == int(missing)
    assert result["audit"]["valid"] is (not missing)
    assert result["interpolation"] is False
    assert len(calls) == (79 if missing else 80)
    if missing:
        assert result["archives"][0]["status"] == "HTTP_404"
        assert group(result["audit"], jobs[0])["missing_months"] == ["2020-01"]
        assert "ARCHIVE_FAILED" in capsys.readouterr().err
    else:
        assert all(row["checksum_matches"] is True for row in result["archives"])
        assert len(list(output.rglob("*.zip"))) == len(list(output.rglob("*.CHECKSUM"))) == 40


def test_funding_interval_change_gap_across_month_boundary_cannot_pass(tmp_path):
    root = tmp_path / "funding-transition"
    jobs = block.planned_archives("2020-01", "2020-02")
    records = []
    for job in jobs:
        rows = rows_for(job)
        if job.symbol == "BTCUSDT" and job.kind == "fundingRate" and job.month == "2020-01":
            rows = rows[::3]
            for row in rows:
                row[1] = "24"
        records.append(write_job(root, job, rows))
    write_manifest(root, records, last="2020-02")
    report = block.audit_block(root)
    assert report["missing_archives"] == report["issues"] == []
    assert all(not row["partial_month"] and not row["gaps"] for row in report["archives"])
    funding = next(row for row in report["groups"] if row["symbol"] == "BTCUSDT" and row["kind"] == "fundingRate")
    assert funding["funding_interval_hours"] == [8.0, 24.0]
    assert len(funding["funding_gaps"]) == 1
    assert report["valid"] is False
