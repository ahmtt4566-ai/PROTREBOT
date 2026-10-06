"""Download public USD-M historical archives; never use a trading API."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://data.binance.vision/data/futures/um/monthly/"
DAILY = "https://data.binance.vision/data/futures/um/daily/"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")
EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
PUBLIC_BRACKETS = "https://www.binance.com/bapi/futures/v1/friendly/future/common/brackets"


def public_metadata(root: Path) -> None:
    def get(url: str) -> dict:
        with urllib.request.urlopen(url, timeout=45) as response:
            if response.geturl() != url:
                raise ValueError("Unexpected metadata redirect")
            return json.load(response)

    exchange = get(EXCHANGE_INFO)
    public = get(PUBLIC_BRACKETS)
    if public.get("code") != "000000":
        raise ValueError("Public maintenance table unavailable")
    brackets = {}
    for row in public["data"]["brackets"]:
        if row["symbol"] not in SYMBOLS:
            continue
        brackets[row["symbol"]] = {"symbol": row["symbol"], "brackets": [
            {"notionalFloor": tier["bracketNotionalFloor"], "notionalCap": tier["bracketNotionalCap"],
             "maintMarginRatio": tier["bracketMaintenanceMarginRate"], "cum": tier["cumFastMaintenanceAmount"],
             "initialLeverage": tier["maxOpenPosLeverage"]} for tier in row["riskBrackets"]]}
    selected = [row for row in exchange["symbols"] if row["symbol"] in SYMBOLS]
    if len(selected) != len(SYMBOLS) or set(brackets) != set(SYMBOLS):
        raise ValueError("Requested symbol metadata is incomplete")
    root.mkdir(parents=True, exist_ok=True)
    (root / "current-metadata.json").write_text(json.dumps({
        "observed_at": datetime.now(timezone.utc).isoformat(), "historical": False,
        "exchange_info_source": EXCHANGE_INFO, "brackets_source": PUBLIC_BRACKETS,
        "exchange_info": {"symbols": selected}, "brackets": brackets,
    }, indent=2), encoding="utf-8")


def months(start: str, end: str) -> list[str]:
    first = datetime.strptime(start, "%Y-%m")
    last = datetime.strptime(end, "%Y-%m")
    if first > last:
        raise ValueError("Start month must precede end month")
    result = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        result.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return result


def fetch(url: str) -> bytes:
    if not url.startswith((BASE, DAILY)):
        raise ValueError("Only static Binance USD-M archives are allowed")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=45) as response:
                if not response.geturl().startswith((BASE, DAILY)):
                    raise ValueError("Unexpected archive redirect")
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
        except (TimeoutError, urllib.error.URLError):
            if attempt == 2:
                raise
        time.sleep(2 ** attempt)
    raise RuntimeError("Archive retry limit exhausted")


def download(root: Path, symbol: str, kind: str, interval: str, month: str, daily: bool = False) -> dict:
    prefix = f"{kind}/{symbol}/" + (f"{interval}/" if interval else "")
    filename = f"{symbol}-{interval or kind}-{month}.zip"
    remote_path = prefix + filename
    relative = ("daily/" if daily else "") + remote_path
    url = (DAILY if daily else BASE) + remote_path
    destination = root / Path(relative)
    try:
        parts = fetch(url + ".CHECKSUM").decode("ascii").split()
        if not parts:
            raise ValueError("Missing SHA-256 checksum")
        checksum = parts[0]
        if len(checksum) != 64:
            raise ValueError("Invalid SHA-256 checksum")
        payload = destination.read_bytes() if destination.exists() else fetch(url)
        actual = hashlib.sha256(payload).hexdigest()
        if actual != checksum and destination.exists():
            payload = fetch(url)
            actual = hashlib.sha256(payload).hexdigest()
        if actual != checksum:
            raise ValueError(f"Archive checksum mismatch: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".part")
        temporary.write_bytes(payload)
        temporary.replace(destination)
        return {"symbol": symbol, "kind": kind, "interval": interval or None, "month": month,
                "path": relative, "url": url, "sha256": actual, "status": "OK"}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"ARCHIVE_UNAVAILABLE {relative}: {exc}", file=sys.stderr)
        return {"symbol": symbol, "kind": kind, "interval": interval or None, "month": month,
                "path": relative, "url": url, "status": "UNAVAILABLE", "error": str(exc)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start-month", required=True)
    parser.add_argument("--end-month", required=True)
    parser.add_argument("--workers", type=int, default=6, choices=range(1, 9))
    parser.add_argument("--public-metadata", action="store_true",
                        help="Explicit opt-in to the two unauthenticated read-only metadata endpoints")
    parser.add_argument("--repair-mark-date", help="Explicit static daily mark-price repair, YYYY-MM-DD")
    args = parser.parse_args()
    root = args.output.resolve()
    if root.is_relative_to(Path(__file__).resolve().parents[1]):
        parser.error("Historical archives must be outside the repository")
    if args.public_metadata:
        public_metadata(root)
    jobs = [(root, symbol, kind, interval, month)
            for month in months(args.start_month, args.end_month)
            for symbol in SYMBOLS
            for kind, interval in [("klines", "15m"), ("klines", "1h"), ("klines", "4h"),
                                   ("markPriceKlines", "15m"), ("fundingRate", "")]]
    if args.repair_mark_date:
        datetime.strptime(args.repair_mark_date, "%Y-%m-%d")
        jobs = [(root, symbol, "markPriceKlines", "15m", args.repair_mark_date, True) for symbol in SYMBOLS]
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        results = list(executor.map(lambda job: download(*job), jobs))
    previous = json.loads((root / "manifest.json").read_text(encoding="utf-8")) if (root / "manifest.json").exists() else {}
    merged = {row["path"]: row for row in [*previous.get("archives", []), *results]}
    manifest = {"source": BASE, "downloaded_at": datetime.now(timezone.utc).isoformat(),
                "start_month": min(previous.get("start_month", args.start_month), args.start_month),
                "end_month": max(previous.get("end_month", args.end_month), args.end_month),
                "archives": sorted(merged.values(), key=lambda row: row["path"]),
                "unavailable_count": sum(row["status"] != "OK" for row in merged.values()),
                "repairs": previous.get("repairs", [])}
    if args.repair_mark_date:
        repair = {"date": args.repair_mark_date, "kind": "markPriceKlines",
                  "source": DAILY, "reason": "Explicit daily supplement; original source gaps must be retained in the quality audit",
                  "archives": [row["path"] for row in results], "unavailable": sum(row["status"] != "OK" for row in results)}
        if repair not in manifest["repairs"]:
            manifest["repairs"].append(repair)
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"archives": len(results), "unavailable": manifest["unavailable_count"], "root": str(root)}))


if __name__ == "__main__":
    main()
