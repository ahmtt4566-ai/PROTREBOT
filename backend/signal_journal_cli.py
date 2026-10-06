"""Offline signal-journal import, closed-candle fill, export and summary."""

import argparse
import csv
import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

from app.local_storage import DATA_DIR
from app.signal_journal import Journal
from app.signal_outcomes import fill_outcomes, import_data


def export(journal: Journal, destination: Path, format_name: str) -> int:
    if format_name not in {"csv", "parquet"}:
        raise ValueError("Unsupported observation export format")
    rows = journal.rows()
    flat = [{key: json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value
             for key, value in row.items()} for row in rows]
    destination.parent.mkdir(parents=True, exist_ok=True)
    if format_name == "csv":
        fields = sorted({key for row in flat for key in row}) or ["signal_id", "decision"]
        with destination.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(flat)
    else:
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise RuntimeError("Parquet export requires the optional pyarrow dependency.") from exc
        fields = sorted({key for row in flat for key in row})
        normalized = [{key: row.get(key) for key in fields} for row in flat]
        table = pa.Table.from_pylist(normalized) if normalized else pa.table({"signal_id": pa.array([], type=pa.string()), "decision": pa.array([], type=pa.string())})
        pq.write_table(table, destination)
    return len(rows)


def summary(journal: Journal) -> dict:
    rows = journal.rows()
    rejected = [row for row in rows if row["decision"] == "REJECTED"]
    return {
        "decisions": dict(Counter(row["decision"] for row in rows)),
        "first_reject_reasons": dict(Counter(row["reject_reason"] for row in rejected)),
        "all_reject_reasons": dict(Counter(reason for row in rejected for reason in set(row["reject_reasons"]))),
        "unknown_gates": dict(Counter(reason for row in rows for reason in row["unknown_gates"])),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("PROTREBOT_SIGNAL_JOURNAL_PATH") or DATA_DIR / "live-signal-journal.sqlite3"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("summary")
    exporting = subparsers.add_parser("export")
    exporting.add_argument("--format", choices=("csv", "parquet"), default="csv")
    exporting.add_argument("--output", type=Path, required=True)
    for name in ("fill", "import-data"):
        operation = subparsers.add_parser(name)
        operation.add_argument("--as-of", required=True, help="Timezone-aware ISO timestamp")
        if name == "import-data":
            operation.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    journal = Journal(args.db)
    if args.command == "summary":
        result = summary(journal)
    elif args.command == "export":
        result = {"exported": export(journal, args.output, args.format)}
    else:
        instant = datetime.fromisoformat(args.as_of)
        if instant.tzinfo is None:
            parser.error("--as-of must include a timezone")
        as_of = int(instant.timestamp())
        if args.command == "fill":
            result = fill_outcomes(journal, as_of)
        else:
            result = import_data(journal, json.loads(args.input.read_text(encoding="utf-8")), as_of)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
