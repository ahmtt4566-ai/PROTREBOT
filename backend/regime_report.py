"""Complete the preregistered A/B combination report from frozen offline outcomes."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def execute(root: Path) -> dict:
    root = root.resolve()
    if root.is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("Research reports must remain outside Git")
    os.environ.update({"DATA_DIR": str(root / "runtime"),
                       "PROTREBOT_DATA_DIR": str(root / "runtime"),
                       "DATABASE_URL": "", "PROTREBOT_DURABLE_AUTH_REQUIRED": "0",
                       "ASSISTANT_LIVE_TESTS": "0"})
    from regime_cli import deny_network
    from app.backtest_baseline import Config
    from app.backtest_diagnostics import utc
    from app.regime_statistics import combination_comparisons
    from app.regime_study import lock_plan
    from backtest_diagnostics_cli import write_json

    deny_network()
    locked = lock_plan(root)
    plan = locked["plan"]
    report = json.loads((root / "regime-study.json").read_text(encoding="utf-8"))
    if (report["protocol_sha256"] != locked["sha256"]
            or report["test_candles_loaded"] != 0 or report["test_results_inspected"] != 0):
        raise ValueError("Study provenance or sealed holdout differs")
    results = {}
    for ordering in plan["orderings"]:
        cohorts = json.loads((root / f"cohorts-{ordering.lower()}.json").read_text(encoding="utf-8"))
        config = Config(plan["splits"]["TRAIN"]["evaluate_start"], plan["period"]["end"],
                        intrabar=ordering)
        for key in ("A", "B"):
            for row in cohorts[key]["trades"]:
                if not config.start <= int(utc(row["opened_at"]).timestamp()) < config.end:
                    raise ValueError("Combination input outside development period")
        results[ordering] = combination_comparisons(
            cohorts["A"]["trades"], cohorts["B"]["trades"], config, plan)
    output = {"protocol_sha256": locked["sha256"], "test_results_inspected": 0,
              "results": results, "no_new_configuration_or_threshold": True}
    write_json(root / "combination-comparisons.json", output)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    execute(parser.parse_args().output)
