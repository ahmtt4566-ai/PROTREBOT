"""Checksummed Stage 5 public GET acquisition, sealed holdout kept separate."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from app.backtest_data import gap_report, normalize_candles, read_rows
from app.regime_study import development_manifest, lock_plan
from backtest_download import EXCHANGE_INFO, PUBLIC_BRACKETS, download, months


def repair_development(root: Path) -> None:
    plan = lock_plan(root)["plan"]
    target = root / "development"
    development_manifest(target, plan)
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    before = {}
    for symbol in plan["research_symbols"]:
        path = target / "markPriceKlines" / symbol / "15m" / f"{symbol}-15m-2026-06.zip"
        evidence = next(row for row in manifest["archives"] if row["path"] == str(path.relative_to(target)).replace("\\", "/"))
        if hashlib.sha256(path.read_bytes()).hexdigest() != evidence["sha256"]:
            raise ValueError("Monthly repair evidence checksum mismatch")
        series, _ = normalize_candles(read_rows(path), "15m")
        before[symbol] = gap_report(series, 1782691200, 1782777600)
        if not before[symbol]["missing_candles"]:
            continue
        result = download(target, symbol, "markPriceKlines", "15m", "2026-06-29", daily=True)
        if result["status"] != "OK":
            raise ValueError(f"Official daily repair unavailable: {symbol}")
        if not any(row["path"] == result["path"] for row in manifest["archives"]):
            manifest["archives"].append(result)
        repair = {"symbol": symbol, "date": "2026-06-29", "source": result["url"],
                  "sha256": result["sha256"], "original_missing_candles": before[symbol]["missing_candles"]}
        if repair not in manifest["repairs"]:
            manifest["repairs"].append(repair)
    (root / "development-monthly-mark-gap-before-repair.json").write_text(json.dumps(before, indent=2), encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def acquire(root: Path, previous: Path, workers: int = 6) -> None:
    if root.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("Research data must remain outside Git")
    plan = lock_plan(root)["plan"]
    original = json.loads((previous / "manifest.json").read_text(encoding="utf-8"))
    reusable = {row["path"]: row for row in original["archives"] if row["status"] == "OK"}
    first = datetime.fromtimestamp(plan["splits"]["TRAIN"]["start"], timezone.utc).strftime("%Y-%m")
    jobs = [(symbol, kind, interval, month)
            for month in months(plan["archive_start_month"], plan["archive_end_month"])
            for symbol in plan["research_symbols"]
            for kind, interval in (("klines", "15m"), ("klines", "1h"), ("klines", "4h"),
                                   ("markPriceKlines", "15m"), ("fundingRate", ""))]

    def job(args: tuple[str, str, str, str]) -> tuple[str, dict]:
        symbol, kind, interval, month = args
        scope = "development" if month >= first else "sealed"
        target = root / scope
        relative = f"{kind}\\{symbol}\\" + (f"{interval}\\" if interval else "") + f"{symbol}-{interval or kind}-{month}.zip"
        old = reusable.get(relative.replace("\\", "/"))
        if old:
            source = previous / relative
            if hashlib.sha256(source.read_bytes()).hexdigest() != old["sha256"]:
                raise ValueError(f"Original archive changed: {relative}")
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            return scope, old
        return scope, download(target, symbol, kind, interval, month)

    grouped: dict[str, list[dict]] = {"development": [], "sealed": []}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for index, (scope, row) in enumerate(pool.map(job, jobs), 1):
            grouped[scope].append(row)
            if index % 50 == 0:
                print(f"ARCHIVES {index}/{len(jobs)}", flush=True)
    for row in original["archives"]:
        if row["path"].startswith("daily/") and row["status"] == "OK" and row["month"][:7] >= first:
            destination = root / "development" / row["path"]
            source = previous / row["path"]
            if hashlib.sha256(source.read_bytes()).hexdigest() != row["sha256"]:
                raise ValueError("Original daily repair changed")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            grouped["development"].append(row)
    for scope, rows in grouped.items():
        target = root / scope
        target.mkdir(parents=True, exist_ok=True)
        (target / "manifest.json").write_text(json.dumps({
            "archives": rows, "repairs": original.get("repairs", []) if scope == "development" else [],
            "sealed": scope == "sealed", "checksum_only_no_candle_inspection": scope == "sealed",
        }, indent=2), encoding="utf-8")
    if any(row["status"] != "OK" for row in grouped["development"]):
        raise ValueError("Missing development archives; inspect manifest before repair")
    repair_development(root)

    metadata = json.loads((previous / "current-metadata.json").read_text(encoding="utf-8"))
    extras = set(plan["research_symbols"]) - set(plan["original_symbols"])
    def public_get(url: str) -> dict:
        if url not in (EXCHANGE_INFO, PUBLIC_BRACKETS):
            raise ValueError("Metadata endpoint not permitted")
        with urllib.request.urlopen(url, timeout=45) as response:
            if response.geturl() != url:
                raise ValueError("Unexpected public metadata redirect")
            return json.load(response)

    exchange, public = public_get(EXCHANGE_INFO), public_get(PUBLIC_BRACKETS)
    if public.get("code") != "000000":
        raise ValueError("Public maintenance table unavailable")
    metadata["exchange_info"]["symbols"].extend(row for row in exchange["symbols"] if row["symbol"] in extras)
    for row in public["data"]["brackets"]:
        if row["symbol"] in extras:
            metadata["brackets"][row["symbol"]] = {"symbol": row["symbol"], "brackets": [
                {"notionalFloor": tier["bracketNotionalFloor"], "notionalCap": tier["bracketNotionalCap"],
                 "maintMarginRatio": tier["bracketMaintenanceMarginRate"], "cum": tier["cumFastMaintenanceAmount"],
                 "initialLeverage": tier["maxOpenPosLeverage"]} for tier in row["riskBrackets"]]}
    if set(metadata["brackets"]) != set(plan["research_symbols"]):
        raise ValueError("Incomplete expanded metadata")
    metadata["extra_symbols_observed_at"] = datetime.now(timezone.utc).isoformat()
    metadata["original_metadata_sha256"] = hashlib.sha256((previous / "current-metadata.json").read_bytes()).hexdigest()
    (root / "development" / "current-metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    unavailable = {scope: sum(row["status"] != "OK" for row in rows) for scope, rows in grouped.items()}
    print(f"ACQUISITION_DONE {json.dumps(unavailable)}", flush=True)
    if any(unavailable.values()):
        raise ValueError("Missing public archives; inspect manifests before proceeding")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous-data", type=Path)
    parser.add_argument("--repair-development", action="store_true")
    parser.add_argument("--workers", type=int, default=6, choices=range(1, 9))
    arguments = parser.parse_args()
    if arguments.repair_development:
        repair_development(arguments.output.resolve())
    elif arguments.previous_data is None:
        parser.error("--previous-data is required for acquisition")
    else:
        acquire(arguments.output.resolve(), arguments.previous_data.resolve(), arguments.workers)
