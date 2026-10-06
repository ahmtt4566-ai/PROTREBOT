"""Stage 5 locked development-only regime study. No network access."""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import ipaddress
import json
import multiprocessing
import os
import socket
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path


def deny_network() -> None:
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    resolve = socket.getaddrinfo

    def local(host) -> bool:
        if host is None or host in ("localhost", b"localhost"):
            return True
        try:
            return ipaddress.ip_address(host.decode() if isinstance(host, bytes) else host).is_loopback
        except ValueError:
            return False

    def check_address(connection, address) -> None:
        if connection.family == getattr(socket, "AF_UNIX", None):
            return
        if connection.family in (socket.AF_INET, socket.AF_INET6) and local(address[0]):
            return
        raise OSError("External network forbidden in Stage 5 offline research")

    def guarded_connect(connection, address):
        check_address(connection, address)
        return connect(connection, address)

    def guarded_connect_ex(connection, address):
        check_address(connection, address)
        return connect_ex(connection, address)

    def guarded_resolve(host, *args, **kwargs):
        if not local(host):
            raise OSError("External network forbidden in Stage 5 offline research")
        return resolve(host, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.getaddrinfo = guarded_resolve


if __name__ in ("__main__", "__mp_main__"):
    bootstrap_parser = argparse.ArgumentParser(add_help=False)
    bootstrap_parser.add_argument("--output", type=Path)
    bootstrap_arguments, _ = bootstrap_parser.parse_known_args()
    if bootstrap_arguments.output is not None:
        runtime_root = bootstrap_arguments.output.resolve()
        if runtime_root.is_relative_to(Path(__file__).resolve().parents[1]):
            raise ValueError("Research outputs/runtime must remain outside Git")
        os.environ.update({"DATA_DIR": str(runtime_root / "runtime"),
                           "PROTREBOT_DATA_DIR": str(runtime_root / "runtime"),
                           "DATABASE_URL": "", "PROTREBOT_DURABLE_AUTH_REQUIRED": "0",
                           "ASSISTANT_LIVE_TESTS": "0"})
    deny_network()

from app import execution_core as core
from app.backtest_ablation import PRESETS, frozen_replay, prepare_entries, verify_control
from app.backtest_baseline import Config, run
from app.backtest_data import Dataset, load_dataset
from app.backtest_diagnostics import trade_set_changes, utc
from app.regime_statistics import cohort_periods, complete, evaluate_filters, joint_contrasts, summary
from app.regime_study import (
    ResearchDataset, causal_percentiles, closed_features, common_period_dataset, development_manifest,
    filter_masks, lock_plan, native_pair,
)
from backtest_cli import cached_native_analysis, epoch_iso, export_trades
from backtest_diagnostics_cli import write_json, write_table


def fingerprint(data: Dataset, locked: dict) -> str:
    app = Path(__file__).parent / "app"
    sources = {name: hashlib.sha256((app / name).read_bytes()).hexdigest() for name in (
        "analysis.py", "main.py", "execution_core.py", "v25_execution.py",
        "backtest_data.py", "regime_study.py")}
    return hashlib.sha256(json.dumps({
        "sources": sources, "plan": locked["sha256"], "archives": data.report["sources"],
        "worker_source": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }, sort_keys=True).encode()).hexdigest()


def worker(symbol: str, frames: dict, config: Config, policy: dict, cache: Path, digest: str) -> str:
    deny_network()
    path = cache / f"{symbol}.jsonl.gz"
    header = {"fingerprint": digest, "symbol": symbol, "start": config.start, "end": config.end,
              "confidence": policy["min_confidence"], "either": policy["mtf_allow_either_timeframe"]}
    if path.exists():
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            if json.loads(next(stream)) != header:
                raise ValueError("Native cache fingerprint mismatch")
        return symbol
    temporary = path.with_suffix(".part")
    with gzip.open(temporary, "wt", encoding="utf-8") as stream, cached_native_analysis():
        stream.write(json.dumps(header) + "\n")
        for at in range(config.start, config.end, 900):
            if all(series.history_complete(at) for series in frames.values()):
                on, off = native_pair(symbol, frames, at, policy)
                stream.write(json.dumps({"at": at, "on": on, "off": off if off != on else None}) + "\n")
            if (at - config.start) % (10000 * 900) == 0:
                print(f"NATIVE_PROGRESS {symbol} {at}", flush=True)
    temporary.replace(path)
    return symbol


def precompute(data: ResearchDataset, config: Config, workers: int, cache: Path, digest: str) -> None:
    if not 1 <= workers <= 4:
        raise ValueError("Native precompute allows 1 to 4 workers")
    cache.mkdir(parents=True, exist_ok=True)
    policy = core.sanitize_execution_policy(config.policy, preserve_empty_allowed_symbols=True)
    with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        jobs = [pool.submit(worker, symbol, frames, config, policy, cache, digest)
                for symbol, frames in data.frames.items()]
        for job in as_completed(jobs):
            symbol = job.result()
            with gzip.open(cache / f"{symbol}.jsonl.gz", "rt", encoding="utf-8") as stream:
                next(stream)
                previous = config.start - 900
                for line in stream:
                    item = json.loads(line)
                    if not previous < item["at"] < config.end or item["at"] < config.start or item["at"] % 900:
                        raise ValueError("Invalid or out-of-scope native cached timestamp")
                    previous = item["at"]
                    key = (symbol, item["at"], policy["min_confidence"], policy["mtf_allow_either_timeframe"])
                    data.decisions[(*key, True)] = item["on"]
                    data.decisions[(*key, False)] = item["off"] or item["on"]
            print(f"NATIVE_CACHE_DONE {symbol}", flush=True)


def features_for(data: ResearchDataset, rows: list[dict], config: Config, cache: Path, digest: str) -> dict:
    result = {}
    for symbol, frames in data.frames.items():
        path = cache / f"{symbol}-percentiles.json.gz"
        if path.exists():
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                saved = json.load(stream)
            if saved["fingerprint"] != digest:
                raise ValueError("Feature cache fingerprint mismatch")
            percentiles = {int(key): value for key, value in saved["values"].items()}
        else:
            percentiles = causal_percentiles(frames["15m"], config.start, config.end)
            temporary = path.with_suffix(".part")
            with gzip.open(temporary, "wt", encoding="utf-8") as stream:
                json.dump({"fingerprint": digest, "values": percentiles}, stream)
            temporary.replace(path)
        for row in rows:
            if row["symbol"] != symbol:
                continue
            at = int(utc(row["opened_at"]).timestamp())
            adx1, _, _ = closed_features(frames["1h"], at)
            adx4, _, _ = closed_features(frames["4h"], at)
            result[row["signal_id"]] = {**percentiles.get(at, {"atr_percentile": None, "bb_percentile": None}),
                                        "adx_1h": adx1, "adx_4h": adx4}
        print(f"FEATURES_DONE {symbol}", flush=True)
    return result


def exits(data: Dataset, config: Config, reference: dict) -> dict:
    entries = asyncio.run(prepare_entries(data, config, reference))
    control = frozen_replay(data, config, entries, PRESETS["A"])
    verify_control(control, reference)
    return frozen_replay(data, config, entries, PRESETS["B"])


def execute(args: argparse.Namespace) -> dict:
    deny_network()
    root = args.output.resolve()
    if root.is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("Research results must remain outside Git")
    locked = lock_plan(root)
    plan = locked["plan"]
    data_root = root / "development"
    development_manifest(data_root, plan)
    config = Config(plan["splits"]["TRAIN"]["evaluate_start"], plan["period"]["end"],
                    conditional_current_metadata=True, policy={
                        "allowed_symbols": plan["research_symbols"], "max_total_exposure_usdt": 350})
    original = load_dataset(data_root, data_root / "current-metadata.json", config.start, config.end)
    data = ResearchDataset(original.frames, original.marks, original.funding, original.metadata,
                           original.report, original.funding_months)
    write_json(root / "development-data-quality.json", data.report)
    digest = fingerprint(data, locked)
    precompute(data, config, args.workers, root / "cache", digest)

    # Reset the old four-symbol portfolio; expanded state/universe is a different cohort.
    reference = json.loads(args.reference_a.read_text(encoding="utf-8"))
    common_config = Config(epoch_iso(reference["period"]["start_inclusive"]),
                           epoch_iso(reference["period"]["end_exclusive"]),
                           conditional_current_metadata=True, policy=reference["policy"])
    original_manifest = json.loads((args.reference_data / "manifest.json").read_text(encoding="utf-8"))
    original_metadata = json.loads((args.reference_data / "current-metadata.json").read_text(encoding="utf-8"))
    if hashlib.sha256((args.reference_data / "current-metadata.json").read_bytes()).hexdigest() != data.metadata["original_metadata_sha256"]:
        raise ValueError("Original metadata provenance changed")
    common_data = common_period_dataset(data, original_manifest, original_metadata)
    common_a = run(common_data, common_config)
    for key in ("trades", "summary", "bootstrap", "stage1_gate_rejections", "all_rejections"):
        if common_a[key] != reference[key]:
            raise ValueError(f"Original 18-month A parity failed: {key}")
    common_b = exits(common_data, common_config, common_a)
    old_b = json.loads(args.reference_b.read_text(encoding="utf-8"))["frozen"]
    verify_control(common_b, old_b)
    write_json(root / "common-period-parity.json", {
        "A": "EXACT", "B": "EXACT", "trade_count": len(common_a["trades"]),
        "original_four_only_flat_start": True,
        "reference_a_sha256": hashlib.sha256(args.reference_a.read_bytes()).hexdigest(),
        "reference_b_sha256": hashlib.sha256(args.reference_b.read_bytes()).hexdigest(),
    })

    all_results, tables = {}, []
    feature_values = None
    entries_on, entries_off = None, None
    native_a, native_off_a = None, None
    for ordering in plan["orderings"]:
        current = replace(config, intrabar=ordering)
        data.short_gate_on = True
        if ordering == "STOP_FIRST":
            native_a = run(data, current)
            entries_on = asyncio.run(prepare_entries(data, current, native_a))
            a = frozen_replay(data, current, entries_on, PRESETS["A"])
            verify_control(a, native_a)
        else:
            if entries_on is None:
                raise ValueError("STOP_FIRST entry schedule must be frozen first")
            a = frozen_replay(data, current, entries_on, PRESETS["A"])
        if entries_on is None or native_a is None:
            raise ValueError("Missing native baseline schedule")
        b = frozen_replay(data, current, entries_on, PRESETS["B"])
        if feature_values is None:
            feature_values = features_for(data, b["trades"], current, root / "cache", digest)
        masks = filter_masks(b["trades"], feature_values, plan)
        filtered = evaluate_filters(b["trades"], masks, current, plan)
        data.short_gate_on = False
        if ordering == "STOP_FIRST":
            native_off_a = run(data, current)
            entries_off = asyncio.run(prepare_entries(data, current, native_off_a))
            off_a = frozen_replay(data, current, entries_off, PRESETS["A"])
            verify_control(off_a, native_off_a)
        else:
            if entries_off is None:
                raise ValueError("SHORT OFF STOP_FIRST schedule must be frozen first")
            off_a = frozen_replay(data, current, entries_off, PRESETS["A"])
        if entries_off is None or native_off_a is None:
            raise ValueError("Missing native SHORT OFF schedule")
        off_b = frozen_replay(data, current, entries_off, PRESETS["B"])
        data.short_gate_on = True
        base = {row["signal_id"]: row for row in b["trades"]}
        off = {row["signal_id"]: row for row in off_b["trades"]}
        # One resampling unit per signal, even when risk-gated paths resize it.
        identifiers = list(dict.fromkeys([*base, *off]))
        union = [base[identifier] if identifier in base and complete(base[identifier])
                 else off[identifier] if identifier in off else base[identifier] for identifier in identifiers]
        base_members = [identifier in base and complete(base[identifier]) for identifier in identifiers]
        off_members = [identifier in off and complete(off[identifier]) for identifier in identifiers]
        off_values = [off[identifier]["net_r"] if keep else None
                      for identifier, keep in zip(identifiers, off_members)]
        settings = plan["bootstrap"]
        short_intervals = joint_contrasts(
            union, {"SHORT_GATE_OFF": off_members}, current,
            samples=settings["samples"], family_size=settings["family_size"],
            alpha=settings["alpha"], base_mask=base_members, target_values={"SHORT_GATE_OFF": off_values})
        comparison = {
            "A": {"summary": summary(a["trades"], current, 60),
                  "stage1_gate_rejections": native_a["stage1_gate_rejections"],
                  "gate_count_source": "STOP_FIRST_NATIVE_ENTRY_SCHEDULE"},
            "B": summary(b["trades"], current, 60),
            "periods_B": cohort_periods(b["trades"], current, plan),
            "filters": filtered,
            "SHORT_GATE_OFF": {"summary": summary(off_b["trades"], current, 60),
                               "cohort_changes": trade_set_changes(off_b["trades"], b["trades"], current),
                               "contrast": short_intervals["SHORT_GATE_OFF"],
                               "stage1_gate_rejections": native_off_a["stage1_gate_rejections"]},
        }
        all_results[ordering] = comparison
        write_json(root / f"cohorts-{ordering.lower()}.json", {"A": a, "B": b, "SHORT_GATE_OFF_A": off_a, "SHORT_GATE_OFF_B": off_b})
        export_trades(b["trades"], root / f"B-{ordering.lower()}-trades.csv")
        export_trades(off_b["trades"], root / f"SHORT_GATE_OFF-{ordering.lower()}-trades.csv")
        for name, item in filtered.items():
            value = item["retained"]
            tables.append({"ordering": ordering, "configuration": name,
                           "remaining_ratio": item["remaining_ratio"],
                           **{key: value[key] for key in (
                               "trade_count", "r_eligible_count", "net_expectancy_r", "profit_factor", "win_rate",
                               "profit_factor_status", "max_drawdown_usdt", "closed_curve_net_pnl", "sample_status")},
                           "eliminated_mean_r": item["eliminated"]["net_expectancy_r"],
                           "mean_r_difference": item["selection_contrast"]["mean_r_difference"],
                           "intervals": json.dumps(item["selection_contrast"])})
        print(f"REGIME_DONE {ordering} A={len(a['trades'])} B={len(b['trades'])} OFF={len(off_b['trades'])}", flush=True)
    if feature_values is None:
        raise ValueError("No study orderings evaluated")
    report = {
        "label": "TRAIN+VALIDATION ONLY; REVERSE-TIME HOLDOUT; NO WINNER",
        "protocol_sha256": locked["sha256"], "fingerprint": digest,
        "test_candles_loaded": 0, "test_results_inspected": 0,
        "registered_noncontrol_comparisons": 11, "comparisons_inspected_per_ordering": 11,
        "orderings_inspected": 2, "ci_family": "11 primary comparisons; TP_FIRST is sensitivity, not independent confirmation",
        "parameters_changed_after_results": False,
        "missing_feature_trade_count": sum(any(value is None for value in feature.values()) for feature in feature_values.values()),
        "results": all_results,
        "limitations": [
            "Original4 metadata frozen from Stage3; extra4 current metadata held constant, not historical.",
            "All frozen-cohort filters remove entries only, do not refill portfolio capacity or rerun daily gates.",
            "B exit cohort follows A entry schedule; not a deployable BE-filtered portfolio.",
            "SHORT>=80 OFF is a separate native rescanned portfolio experiment and can change quantity/cohort.",
            "Both intrabar orderings use the same STOP_FIRST frozen entry schedules; native daily gates not rerun for TP_FIRST.",
            "Selection contrasts resample joint memberships, not independent groups; common exit delta is zero for filters.",
            "Training-boundary-crossing trades retained combined, excluded from TRAIN-only descriptive metrics.",
            "Nominal95 CIs are unadjusted; Bonferroni95 family intervals also supplied. No winner or TEST inference.",
            "Fewer than60 complete trades is YETERSIZ_ORNEKLEM; no interpretation.",
        ],
    }
    write_json(root / "regime-study.json", report)
    write_json(root / "entry-features.json", feature_values)
    write_table(root / "comparison.csv", tables)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference-a", type=Path, required=True)
    parser.add_argument("--reference-b", type=Path, required=True)
    parser.add_argument("--reference-data", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 5))
    execute(parser.parse_args())
