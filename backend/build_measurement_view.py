"""Build a new, hash-verified TRAIN/VALIDATION-only file view; no ZIP parsing.

Usage: python build_measurement_view.py --source SOURCE --output NEW_DIRECTORY
The fixed scope is 2020-10 through 2022-08, eight symbols and five streams.
Source files are only read; existing outputs, links and repository outputs fail.
Source JSON is scanned structurally as opaque bytes. Only archive objects with
an allowed month are decoded; TEST objects and the entire audit are not decoded.
Archive records are preserved verbatim as JSON values; no health audit is copied
or recomputed. File/hash verification is NOT a candle/funding completeness claim.

Use load_measurement_dataset for measurement: it denies TEST dates before any
file is opened and validates the view manifest before calling native load_dataset.
Native load_dataset itself is unchanged and does not enforce this date guard.
This is an API guard and a physical TEST-free view, not an OS access-control rule.
On copy failure the new directory is left incomplete, without a final manifest;
there is no overwrite, resume, automatic cleanup, hardlink or source mutation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from download_independent_block import Archive, month_start, planned_archives

if TYPE_CHECKING:
    from app.backtest_data import Dataset

FIRST_MONTH = "2020-10"
LAST_MONTH = "2022-08"
TEST_MONTH = "2022-09"
START = int(month_start(FIRST_MONTH).timestamp())
TEST_START = int(month_start(TEST_MONTH).timestamp())
SCHEMA = "measurement-view-train-validation-v1"
REPOSITORY = Path(__file__).resolve().parents[1]
ARCHIVE_KEYS = {
    "symbol", "kind", "interval", "month", "path", "url", "checksum_url",
    "sha256", "expected_sha256", "checksum_file_sha256", "checksum_matches", "status",
}
VIEW_KEYS = {
    "schema", "start_month", "end_month", "source_manifest_sha256",
    "measurement_view", "archives", "copy_report",
}


class MeasurementScopeError(ValueError):
    """Fail-closed measurement date, manifest or file authorization error."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_measurement_month(month: str) -> None:
    current = month_start(month)
    if not month_start(FIRST_MONTH) <= current <= month_start(LAST_MONTH):
        raise MeasurementScopeError("Archive month outside TRAIN/VALIDATION; TEST copy denied")


def _space(data: bytes, at: int) -> int:
    while at < len(data) and data[at] in b" \t\r\n":
        at += 1
    return at


def _string_end(data: bytes, at: int) -> int:
    if at >= len(data) or data[at] != ord('"'):
        raise MeasurementScopeError("Malformed manifest string")
    at += 1
    while at < len(data):
        if data[at] == ord("\\"):
            at += 2
        elif data[at] == ord('"'):
            return at + 1
        else:
            at += 1
    raise MeasurementScopeError("Unterminated manifest string")


def _value_end(data: bytes, at: int) -> int:
    at = _space(data, at)
    if at >= len(data):
        raise MeasurementScopeError("Missing manifest value")
    if data[at] == ord('"'):
        return _string_end(data, at)
    if data[at] in b"{[":
        stack = [ord("}") if data[at] == ord("{") else ord("]")]
        at += 1
        while at < len(data):
            value = data[at]
            if value == ord('"'):
                at = _string_end(data, at)
                continue
            if value in b"{[":
                stack.append(ord("}") if value == ord("{") else ord("]"))
            elif value in b"}]":
                if value != stack.pop():
                    raise MeasurementScopeError("Unbalanced manifest container")
                if not stack:
                    return at + 1
            at += 1
        raise MeasurementScopeError("Unterminated manifest container")
    end = at
    while end < len(data) and data[end] not in b" \t\r\n,}]":
        end += 1
    if end == at:
        raise MeasurementScopeError("Missing manifest primitive")
    return end


def _members(data: bytes, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    if data[start:end][:1] != b"{":
        raise MeasurementScopeError("Expected manifest object")
    at, seen = _space(data, start + 1), set()
    if at < end and data[at] == ord("}"):
        return
    while at < end:
        key_end = _string_end(data, at)
        key = data[at:key_end]
        if key in seen:
            raise MeasurementScopeError("Duplicate manifest key")
        seen.add(key)
        at = _space(data, key_end)
        if at >= end or data[at] != ord(":"):
            raise MeasurementScopeError("Missing manifest colon")
        start_value = _space(data, at + 1)
        end_value = _value_end(data, start_value)
        yield key, start_value, end_value
        at = _space(data, end_value)
        if at == end - 1 and data[at] == ord("}"):
            return
        if at >= end or data[at] != ord(","):
            raise MeasurementScopeError("Missing manifest object separator")
        at = _space(data, at + 1)
    raise MeasurementScopeError("Unterminated manifest object")


def _items(data: bytes, start: int, end: int) -> Iterator[tuple[int, int]]:
    if data[start:end][:1] != b"[":
        raise MeasurementScopeError("Expected archive array")
    at = _space(data, start + 1)
    if at < end and data[at] == ord("]"):
        return
    while at < end:
        finish = _value_end(data, at)
        yield at, finish
        at = _space(data, finish)
        if at == end - 1 and data[at] == ord("]"):
            return
        if at >= end or data[at] != ord(","):
            raise MeasurementScopeError("Missing archive separator")
        at = _space(data, at + 1)
    raise MeasurementScopeError("Unterminated archive array")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MeasurementScopeError("Duplicate decoded manifest key")
        result[key] = value
    return result


def _validate_record(record: object, jobs: dict[str, Archive]) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != ARCHIVE_KEYS:
        raise MeasurementScopeError("Unsupported archive record fields; no health payload is accepted")
    month = record["month"]
    if not isinstance(month, str):
        raise MeasurementScopeError("Invalid archive month")
    require_measurement_month(month)
    path = record["path"]
    if not isinstance(path, str) or path not in jobs:
        raise MeasurementScopeError("Archive path not in measurement allowlist")
    job = jobs[path]
    expected = {
        "symbol": job.symbol, "kind": job.kind, "interval": job.interval,
        "month": job.month, "path": job.path, "url": job.url, "checksum_url": job.checksum_url,
    }
    if any(record[key] != value for key, value in expected.items()):
        raise MeasurementScopeError("Archive identity/date does not match its allowed path")
    if record["status"] != "OK" or record["checksum_matches"] is not True:
        raise MeasurementScopeError("Archive lacks successful checksum evidence")
    for key in ("sha256", "expected_sha256", "checksum_file_sha256"):
        if not isinstance(record[key], str) or not re.fullmatch(r"[0-9a-f]{64}", record[key]):
            raise MeasurementScopeError("Invalid archive SHA-256 evidence")
    if record["sha256"] != record["expected_sha256"]:
        raise MeasurementScopeError("Archive expected/actual SHA-256 mismatch")
    return record


def selected_records(data: bytes) -> list[dict[str, Any]]:
    jobs = {job.path: job for job in planned_archives(FIRST_MONTH, LAST_MONTH)}
    allowed_months = {json.dumps(job.month).encode("ascii") for job in jobs.values()}
    start = _space(data, 0)
    end = _value_end(data, start)
    if _space(data, end) != len(data):
        raise MeasurementScopeError("Trailing source manifest content")
    array = None
    for key, left, right in _members(data, start, end):
        if key == b'"archives"':
            array = (left, right)
    if array is None:
        raise MeasurementScopeError("Source manifest has no archives")
    records: dict[str, dict[str, Any]] = {}
    for left, right in _items(data, *array):
        members = {key: (a, b) for key, a, b in _members(data, left, right)}
        month = members.get(b'"month"')
        if month is None:
            raise MeasurementScopeError("Archive lacks a canonical month selector")
        if data[month[0]:month[1]] not in allowed_months:
            continue
        record = _validate_record(json.loads(data[left:right], object_pairs_hook=_unique_object), jobs)
        if record["path"] in records:
            raise MeasurementScopeError("Duplicate selected archive")
        records[record["path"]] = record
    if records.keys() != jobs.keys():
        raise MeasurementScopeError("Incomplete TRAIN/VALIDATION archive grid; expected 920 ZIPs")
    return [records[path] for path in jobs]


def _file_under(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative)
    if parts.is_absolute() or ".." in parts.parts or "\\" in relative:
        raise MeasurementScopeError("Unsafe archive path")
    path = root.joinpath(*parts.parts)
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or path.is_symlink() or not path.is_file():
        raise MeasurementScopeError("Archive is missing, linked or outside its root")
    return resolved


def _scope() -> dict[str, Any]:
    return {
        "train": {"start_month": FIRST_MONTH, "end_month": "2021-12"},
        "validation": {"start_month": "2022-01", "end_month": LAST_MONTH},
        "start_inclusive": "2020-10-01T00:00:00+00:00",
        "end_exclusive": "2022-09-01T00:00:00+00:00",
        "test_months_excluded": True,
        "health_audit_included": False,
        "verification": "FILE_SHA256_ONLY_NOT_DATA_COMPLETENESS",
    }


def build_view(source: Path, output: Path) -> dict[str, Any]:
    if output.exists() or output.is_symlink():
        raise FileExistsError("Output already exists; refusing overwrite")
    source, output = source.resolve(), output.resolve()
    if not source.is_dir():
        raise MeasurementScopeError("Source directory is unavailable")
    if output.is_relative_to(source) or source.is_relative_to(output) or output.is_relative_to(REPOSITORY):
        raise MeasurementScopeError("Output must be outside source and repository")
    source_manifest = _file_under(source, "manifest.json")
    source_bytes = source_manifest.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    records = selected_records(source_bytes)
    selected_paths = {record["path"] for record in records}
    zip_paths = {path.relative_to(source).as_posix() for path in source.rglob("*.zip")}
    checksum_paths = {path.relative_to(source).as_posix() for path in source.rglob("*.CHECKSUM")}
    for relative in zip_paths - selected_paths:
        match = re.search(r"-(\d{4}-\d{2})\.zip$", relative)
        if match is None or FIRST_MONTH <= match[1] <= LAST_MONTH:
            raise MeasurementScopeError("Unexpected ZIP in measurement scope or invalid ZIP filename")
    copies = []
    for record in records:
        for relative, expected in (
            (record["path"], record["sha256"]),
            (record["path"] + ".CHECKSUM", record["checksum_file_sha256"]),
        ):
            path = _file_under(source, relative)
            if sha256_file(path) != expected:
                raise MeasurementScopeError("Source file SHA-256 mismatch")
            copies.append((path, relative, expected))
    if sha256_file(source_manifest) != source_hash:
        raise MeasurementScopeError("Source manifest changed during preflight")
    output.mkdir(parents=True, exist_ok=False)
    for path, relative, expected in copies:
        target = output.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with path.open("rb") as incoming, target.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        if sha256_file(target) != expected:
            raise MeasurementScopeError("Copied file SHA-256 mismatch; incomplete view left in place")
    if sha256_file(source_manifest) != source_hash:
        raise MeasurementScopeError("Source manifest changed during copying; no final manifest published")
    report = {
        "copied_zip_count": len(records), "copied_checksum_count": len(records),
        "excluded_zip_count": len(zip_paths - selected_paths), "zip_hash_matches": len(records),
        "excluded_checksum_count": len(checksum_paths - {path + ".CHECKSUM" for path in selected_paths}),
        "checksum_hash_matches": len(records),
    }
    manifest = {
        "schema": SCHEMA, "start_month": FIRST_MONTH, "end_month": LAST_MONTH,
        "source_manifest_sha256": source_hash, "measurement_view": _scope(),
        "archives": records, "copy_report": report,
    }
    path = output / "manifest.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    return {**report, "source_manifest_sha256": source_hash, "manifest_sha256": sha256_file(path)}


def load_measurement_dataset(root: Path, metadata_path: Path, start: int, end: int) -> Dataset:
    if type(start) is not int or type(end) is not int or start >= end or start % 900 or end % 900:
        raise MeasurementScopeError("Invalid measurement period")
    if start >= TEST_START or end > TEST_START:
        raise MeasurementScopeError("TEST access denied: measurement ends before 2022-09-01 UTC")
    if start < START:
        raise MeasurementScopeError("Measurement starts at 2020-10-01 UTC; earlier reads denied")
    root = root.resolve()
    manifest = json.loads(_file_under(root, "manifest.json").read_bytes(), object_pairs_hook=_unique_object)
    if not isinstance(manifest, dict) or set(manifest) != VIEW_KEYS:
        raise MeasurementScopeError("Invalid measurement manifest fields")
    if (
        manifest.get("schema") != SCHEMA or manifest.get("measurement_view") != _scope()
        or manifest.get("start_month") != FIRST_MONTH or manifest.get("end_month") != LAST_MONTH
    ):
        raise MeasurementScopeError("Unrecognized or modified measurement scope")
    digest = manifest.get("source_manifest_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise MeasurementScopeError("Missing source manifest fingerprint")
    jobs = {job.path: job for job in planned_archives(FIRST_MONTH, LAST_MONTH)}
    records = manifest.get("archives")
    if not isinstance(records, list) or len(records) != len(jobs):
        raise MeasurementScopeError("Incomplete measurement view")
    paths = [_validate_record(record, jobs)["path"] for record in records]
    if len(set(paths)) != len(jobs):
        raise MeasurementScopeError("Duplicate or missing measurement archive")
    for relative in paths:
        _file_under(root, relative)
    from app.backtest_data import load_dataset
    return load_dataset(root, metadata_path, start, end)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = build_view(args.source, args.output)
    except (OSError, ValueError) as exc:
        print(f"MEASUREMENT_VIEW_FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
