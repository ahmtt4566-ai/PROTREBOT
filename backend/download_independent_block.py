"""Independent Binance USD-M monthly archives; no strategy or Stage imports.

Dates are mandatory. Output must be a new folder outside the repository and
existing study/data blocks. No resume, overwrite, repair or interpolation.
--dry-run lists URLs without network or filesystem writes.
--verify-only calls audit_block(), which only reads an existing block.
Any checksum, missing-data, duplicate or validation issue fails the data gate.
Funding gaps use the provider's interval with 60 seconds of settlement tolerance;
an unknown funding schedule is never replaced with an assumed eight-hour grid.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from typing import Any

BASE = "https://data.binance.vision/data/futures/um/monthly/"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
           "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT")
STREAMS = (("klines", "15m"), ("klines", "1h"), ("klines", "4h"),
           ("markPriceKlines", "15m"), ("fundingRate", None))
SECONDS = {"15m": 900, "1h": 3600, "4h": 14400}
REPOSITORY = Path(__file__).resolve().parents[1]
SEEN_START = datetime(2023, 6, 1, tzinfo=timezone.utc).timestamp()
SEEN_END = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
SCHEMA = "independent-usdm-block-v1"
CANDLE_HEADER = ("open_time", "open", "high", "low", "close", "volume",
                 "close_time", "quote_volume", "count", "taker_buy_volume",
                 "taker_buy_quote_volume", "ignore")
FUNDING_HEADER = ("calc_time", "funding_interval_hours", "last_funding_rate")


class OverlapError(ValueError):
    """Requested or actual data intersects Stage 3-6b inputs, including warmup."""


def month_start(value: str) -> datetime:
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ValueError("Month must use YYYY-MM")
    return datetime.strptime(value, "%Y-%m").replace(tzinfo=timezone.utc)


def next_month(value: datetime) -> datetime:
    return value.replace(year=value.year + 1, month=1) if value.month == 12 else value.replace(month=value.month + 1)


def independent_range(start_month: str, end_month: str) -> tuple[int, int]:
    start, last = month_start(start_month), month_start(end_month)
    if start > last:
        raise ValueError("Start month must not follow end month")
    end = next_month(last)
    require_independent(start.timestamp(), end.timestamp())
    return int(start.timestamp()), int(end.timestamp())


def require_independent(start: float | Decimal, end: float | Decimal) -> None:
    if start < SEEN_END and end > SEEN_START:
        raise OverlapError("Data intersects Stage 3-6b [2023-06-01, 2026-10-01) UTC")


@dataclass(frozen=True)
class Archive:
    symbol: str
    kind: str
    interval: str | None
    month: str
    path: str
    url: str
    checksum_url: str

    @property
    def filename(self) -> str:
        return self.path.rsplit("/", 1)[-1]


def planned_archives(start_month: str, end_month: str) -> list[Archive]:
    independent_range(start_month, end_month)
    current, last = month_start(start_month), month_start(end_month)
    jobs = []
    while current <= last:
        month = f"{current.year:04d}-{current.month:02d}"
        for symbol in SYMBOLS:
            for kind, interval in STREAMS:
                prefix = f"{kind}/{symbol}/" + (f"{interval}/" if interval else "")
                path = prefix + f"{symbol}-{interval or kind}-{month}.zip"
                jobs.append(Archive(symbol, kind, interval, month, path, BASE + path, BASE + path + ".CHECKSUM"))
        current = next_month(current)
    return jobs


def output_path(output: Path, *, new: bool) -> Path:
    root = output.resolve()
    if root.is_relative_to(REPOSITORY):
        raise ValueError("Output must be outside the repository")
    if new:
        if root.exists():
            raise FileExistsError("Output must be a new, nonexistent folder")
        for parent in root.parents:
            if any((parent / name).is_file() for name in ("manifest.json", "study-lock.json", "holdout-lock.json")):
                raise ValueError("Output must not be inside an existing data/study block")
    return root


def parse_checksum(payload: bytes, filename: str) -> str:
    parts = payload.decode("ascii").split()
    if len(parts) != 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]) or parts[1].lstrip("*") != filename:
        raise ValueError("Invalid provider SHA-256 checksum or archive filename")
    return parts[0].lower()


def checksum_evidence(job: Archive, payload: bytes, checksum: bytes) -> dict[str, Any]:
    expected = parse_checksum(checksum, job.filename)
    actual = hashlib.sha256(payload).hexdigest()
    return {
        **asdict(job), "sha256": actual, "expected_sha256": expected,
        "checksum_file_sha256": hashlib.sha256(checksum).hexdigest(),
        "checksum_matches": actual == expected,
        "status": "OK" if actual == expected else "CHECKSUM_MISMATCH",
    }


def fetch_public(url: str) -> bytes:
    if not url.startswith(BASE):
        raise ValueError("Only static monthly USD-M archives are allowed")
    with urllib.request.urlopen(url, timeout=45) as response:
        if response.geturl() != url:
            raise ValueError("Unexpected archive redirect")
        return response.read()


def acquire_archive(root: Path, job: Archive) -> dict[str, Any]:
    record: dict[str, Any] = {
        **asdict(job), "status": "UNAVAILABLE", "sha256": None,
        "expected_sha256": None, "checksum_file_sha256": None, "checksum_matches": False,
    }
    phase = "CHECKSUM"
    try:
        checksum = fetch_public(job.checksum_url)
        record["checksum_file_sha256"] = hashlib.sha256(checksum).hexdigest()
        record["expected_sha256"] = parse_checksum(checksum, job.filename)
        phase = "ZIP"
        payload = fetch_public(job.url)
        record = checksum_evidence(job, payload, checksum)
        if not record["checksum_matches"]:
            record["error"] = "Archive SHA-256 differs from provider .CHECKSUM"
            return record
        destination = root.joinpath(*job.path.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(payload)
        with destination.with_name(destination.name + ".CHECKSUM").open("xb") as stream:
            stream.write(checksum)
        return record
    except urllib.error.HTTPError as exc:
        record.update(status="HTTP_404" if exc.code == 404 else "HTTP_ERROR",
                      http_status=exc.code, phase=phase, error=str(exc))
    except (urllib.error.URLError, TimeoutError) as exc:
        record.update(status="NETWORK_ERROR", phase=phase, error=str(exc))
    except (ValueError, UnicodeError) as exc:
        record.update(status="INVALID_CHECKSUM_OR_RESPONSE", phase=phase, error=str(exc))
    return record


def decimal_value(raw: str) -> Decimal:
    value = Decimal(raw)
    if not value.is_finite():
        raise ValueError("Non-finite numeric field")
    return value


def epoch(raw: str) -> Decimal:
    value = decimal_value(raw)
    return value / 1000000 if value >= Decimal("1e15") else value / 1000 if value >= Decimal("1e12") else value


def iso(value: int | Decimal) -> str:
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat()


def missing_ranges(times: set[int], start: int, end: int, step: int) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for at in range(start, end, step):
        if at in times:
            continue
        if gaps and gaps[-1]["end_exclusive"] == at:
            gaps[-1]["end_exclusive"] += step
            gaps[-1]["missing_candles"] += 1
        else:
            gaps.append({"start": at, "end_exclusive": at + step, "missing_candles": 1})
    return gaps


def inspect_archive(payload: bytes, job: Archive) -> dict[str, Any]:
    start = int(month_start(job.month).timestamp())
    end = int(next_month(month_start(job.month)).timestamp())
    step = SECONDS[job.interval] if job.interval is not None else None
    values: dict[int | Decimal, tuple[Decimal, ...]] = {}
    counts: Counter[str] = Counter()
    errors = []
    header = CANDLE_HEADER if step else FUNDING_HEADER
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        if archive.namelist() != [job.filename.removesuffix(".zip") + ".csv"]:
            raise ValueError("Expected exactly the matching CSV inside ZIP")
        with archive.open(archive.namelist()[0]) as member:
            reader = csv.reader(io.TextIOWrapper(member, encoding="utf-8-sig"))
            for index, row in enumerate(reader):
                if index == 0 and tuple(row) == header:
                    continue
                counts["records"] += 1
                try:
                    if len(row) != len(header):
                        raise ValueError("Unexpected CSV schema/column count")
                    at = epoch(row[0])
                    require_independent(at, at + (step or Decimal("0.000001")))
                    if step:
                        if at != int(at) or int(at) % step:
                            raise ValueError("Misaligned candle timestamp")
                        try:
                            o, h, low, c, volume = (decimal_value(raw) for raw in row[1:6])
                        except (ValueError, InvalidOperation) as exc:
                            counts["invalid_ohlc"] += 1
                            raise ValueError("Invalid/non-finite OHLCV") from exc
                        if not 0 < low <= min(o, c) <= max(o, c) <= h or volume < 0:
                            counts["invalid_ohlc"] += 1
                            raise ValueError("Invalid OHLCV")
                        quote = decimal_value(row[7])
                        close_raw = decimal_value(row[6])
                        unit = Decimal("0.000001") if close_raw >= Decimal("1e15") else (
                            Decimal("0.001") if close_raw >= Decimal("1e12") else Decimal(1))
                        if quote < 0 or epoch(row[6]) != at + step - unit:
                            raise ValueError("Invalid quote volume or provider closing time")
                        normalized = (o, h, low, c, volume, quote)
                    else:
                        hours, rate = decimal_value(row[1]), decimal_value(row[2])
                        if hours <= 0:
                            raise ValueError("Funding interval must be positive")
                        normalized = (hours, rate)
                    if not start <= at < end:
                        counts["out_of_month"] += 1
                        raise ValueError("Record lies outside declared archive month")
                    if at in values:
                        counts["duplicates"] += 1
                        if values[at] != normalized:
                            counts["conflicting_duplicates"] += 1
                    values[at] = normalized
                except OverlapError:
                    raise
                except (ValueError, InvalidOperation) as exc:
                    counts["invalid_rows"] += 1
                    if len(errors) < 10:
                        errors.append({"row": index + 1, "error": str(exc)})
    times = sorted(values)
    if step:
        gaps = missing_ranges({int(at) for at in times}, start, end, step)
        leading = not times or times[0] > start
        trailing = not times or times[-1] < end - step
    else:
        gaps = [{"start": float(left), "end_exclusive": float(right)}
                for left, right in pairwise(times)
                if right - left > values[right][0] * 3600 + 60]
        leading = not times or times[0] > start + 60
        trailing = not times or times[-1] + values[times[-1]][0] * 3600 + 60 < end
    return {
        **asdict(job), **{key: counts[key] for key in (
            "records", "duplicates", "conflicting_duplicates", "invalid_rows", "invalid_ohlc", "out_of_month")},
        "unique_records": len(times), "first_record": iso(times[0]) if times else None,
        "last_record": iso(times[-1]) if times else None,
        "missing_candles": sum(gap["missing_candles"] for gap in gaps) if step else None,
        "gaps": gaps, "partial_month": bool(leading or trailing),
        "leading_incomplete": bool(leading), "trailing_incomplete": bool(trailing),
        "funding_interval_hours": sorted({float(v[0]) for v in values.values()}) if not step else [],
        "funding_first_time": str(times[0]) if not step and times else None,
        "funding_last_time": str(times[-1]) if not step and times else None,
        "funding_first_interval_hours": str(values[times[0]][0]) if not step and times else None,
        "errors": errors,
    }


def audit_block(output: Path) -> dict[str, Any]:
    """Read-only checksum/content audit; returns evidence, never writes or repairs."""
    root = output_path(output, new=False)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA or manifest.get("source") != BASE:
        raise ValueError("Unsupported independent-block manifest")
    first, last = manifest.get("start_month"), manifest.get("end_month")
    if not isinstance(first, str) or not isinstance(last, str):
        raise TypeError("Manifest requires explicit start/end months")
    start, end = independent_range(first, last)
    jobs = planned_archives(first, last)
    records = manifest.get("archives")
    if not isinstance(records, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("path"), str) for row in records
    ):
        raise ValueError("Invalid manifest archive records")
    by_path = {row["path"]: row for row in records}
    if len(by_path) != len(records) or set(by_path) - {job.path for job in jobs}:
        raise ValueError("Duplicate or unexpected manifest archive path")
    reports, missing, issues = [], [], []
    for job in jobs:
        row = by_path.get(job.path)
        path = root.joinpath(*job.path.split("/"))
        checksum_path = path.with_name(path.name + ".CHECKSUM")
        if not path.resolve().is_relative_to(root) or not checksum_path.resolve().is_relative_to(root):
            raise ValueError("Archive/checksum path escapes block")
        if row is None or not path.is_file() or not checksum_path.is_file():
            missing.append(asdict(job))
            continue
        if any(row.get(key) != value for key, value in asdict(job).items()):
            raise ValueError("Manifest archive identity/URL mismatch")
        try:
            payload, checksum = path.read_bytes(), checksum_path.read_bytes()
            evidence = checksum_evidence(job, payload, checksum)
            if row.get("status") != "OK" or row.get("checksum_matches") is not True or any(row.get(key) != evidence[key] for key in (
                "sha256", "expected_sha256", "checksum_file_sha256", "checksum_matches",
            )) or not evidence["checksum_matches"]:
                raise ValueError("Archive, checksum or manifest digest mismatch")
            report = inspect_archive(payload, job)
            report["boundary_month"] = (
                "FIRST_AND_LAST" if job.month == manifest["start_month"] == manifest["end_month"]
                else "FIRST" if job.month == manifest["start_month"]
                else "LAST" if job.month == manifest["end_month"] else "INTERIOR")
            reports.append(report)
        except OverlapError:
            raise
        except (OSError, ValueError, UnicodeError, zipfile.BadZipFile, csv.Error) as exc:
            issues.append({"path": job.path, "error": str(exc)})
    groups = []
    for symbol in SYMBOLS:
        for kind, interval in STREAMS:
            selected = [row for row in reports if (row["symbol"], row["kind"], row["interval"]) == (symbol, kind, interval)]
            absent = [row["month"] for row in missing if (row["symbol"], row["kind"], row["interval"]) == (symbol, kind, interval)]
            unavailable = [job.month for job in jobs if (job.symbol, job.kind, job.interval) == (symbol, kind, interval)
                           and any(issue["path"] == job.path for issue in issues)]
            missing_count = sum(row["missing_candles"] for row in selected) if interval else None
            if interval:
                missing_count += sum(
                    (int(next_month(month_start(month)).timestamp()) - int(month_start(month).timestamp())) // SECONDS[interval]
                    for month in [*absent, *unavailable])
            funding_gaps = []
            if not interval:
                funding_gaps = [gap for row in selected for gap in row["gaps"]]
                boundaries = sorted((row for row in selected if row["funding_first_time"]), key=lambda row: row["month"])
                for left, right in pairwise(boundaries):
                    previous, current = Decimal(left["funding_last_time"]), Decimal(right["funding_first_time"])
                    if current - previous > Decimal(right["funding_first_interval_hours"]) * 3600 + 60:
                        funding_gaps.append({"start": float(previous), "end_exclusive": float(current)})
                funding_gaps.sort(key=lambda gap: gap["start"])
            groups.append({
                "symbol": symbol, "kind": kind, "interval": interval,
                "records": sum(row["records"] for row in selected),
                "unique_records": sum(row["unique_records"] for row in selected),
                "missing_candles": missing_count, "missing_months": absent, "unusable_months": unavailable,
                "duplicates": sum(row["duplicates"] for row in selected),
                "invalid_ohlc": sum(row["invalid_ohlc"] for row in selected),
                "invalid_rows": sum(row["invalid_rows"] for row in selected),
                "partial_months": [row["month"] for row in selected if row["partial_month"]],
                "funding_first": min((row["first_record"] for row in selected if row["first_record"]), default=None) if not interval else None,
                "funding_last": max((row["last_record"] for row in selected if row["last_record"]), default=None) if not interval else None,
                "funding_interval_hours": sorted({v for row in selected for v in row["funding_interval_hours"]}),
                "funding_gaps": funding_gaps,
            })
    valid = not missing and not issues and all(
        not row["duplicates"] and not row["invalid_rows"] and not row["partial_month"]
        and not row["gaps"] for row in reports) and not any(group["funding_gaps"] for group in groups)
    return {
        "valid": valid, "stage_overlap": False,
        "period": {"start_inclusive": iso(start), "end_exclusive": iso(end)},
        "groups": groups, "archives": reports, "missing_archives": missing, "issues": issues,
        "source_failures": [row for row in records if row.get("status") != "OK"],
    }


def download_block(output: Path, start_month: str, end_month: str) -> dict[str, Any]:
    jobs = planned_archives(start_month, end_month)
    root = output_path(output, new=True)
    root.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "schema": SCHEMA, "source": BASE, "start_month": start_month, "end_month": end_month,
        "downloaded_at": datetime.now(timezone.utc).isoformat(), "archives": [],
        "stage_exclusion": "[2023-06-01, 2026-10-01) UTC INCLUDING WARMUP",
        "interpolation": False,
    }
    path = root / "manifest.json"
    for job in jobs:
        record = acquire_archive(root, job)
        manifest["archives"].append(record)
        path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if record["status"] != "OK":
            print(f"ARCHIVE_FAILED {job.path}: {json.dumps(record)}", file=sys.stderr)
    manifest["unavailable_count"] = sum(row["status"] != "OK" for row in manifest["archives"])
    manifest["audit"] = audit_block(root)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start-month", required=True)
    parser.add_argument("--end-month", required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        jobs = planned_archives(args.start_month, args.end_month)
        if args.dry_run:
            output_path(args.output, new=True)
            for job in jobs:
                print(job.url)
                print(job.checksum_url)
            return 0
        if args.verify_only:
            report = audit_block(args.output)
            expected_start, expected_end = independent_range(args.start_month, args.end_month)
            if report["period"] != {"start_inclusive": iso(expected_start), "end_exclusive": iso(expected_end)}:
                raise ValueError("Requested verification range differs from manifest")
        else:
            report = download_block(args.output, args.start_month, args.end_month)["audit"]
        print(json.dumps(report, indent=2))
        return 0 if report["valid"] else 1
    except (OSError, ValueError, TypeError, zipfile.BadZipFile) as exc:
        print(f"INDEPENDENT_BLOCK_FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
