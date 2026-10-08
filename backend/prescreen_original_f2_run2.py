"""Explicitly authorized fresh output after a failure before either replay began.

Reuse the validated parent executor unchanged. Preserve the first output and
locked study plan; no parameter, budget, signal, cost or decision-rule changes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import prescreen_original_f2 as original

OUTPUT = Path.home() / "kaistrade-data" / "original-v2-f2-test-run2"
PARENT_RUNNER_SHA = "43fffefca58313dcbd436094271b9ff17f01458839d3f3c790629f5f957f774a"


def validate_previous_failure(root: Path) -> dict:
    original.parent.require(root.is_dir(), "PREVIOUS_OUTPUT_MISSING")
    children = list(root.iterdir())
    original.parent.require(len(children) == 1 and children[0].name == "progress.jsonl"
                            and children[0].is_file() and children[0].stat().st_size == 0,
                            "PREVIOUS_OUTPUT_NOT_PRE_REPLAY_FAILURE")
    return {"path": str(root), "files": {"progress.jsonl": original.sha256_file(children[0])},
            "phase_replay_started": False, "preserved": True}


def prepare(output: Path) -> tuple[dict, dict]:
    original.parent.require(output.resolve() == OUTPUT.resolve(), "RESTART_OUTPUT_SCOPE_DENIED")
    original.parent.require(not output.exists(), "OUTPUT_EXISTS_STOP")
    original.parent.require(original.sha256_file(Path(original.__file__)) == PARENT_RUNNER_SHA,
                            "VALIDATED_PARENT_RUNNER_CHANGED")
    previous = validate_previous_failure(original.OUTPUT)
    plan = original.locked_plan()
    authorization = {
        "schema": "original-v2-f2-fresh-output-authorization-v1",
        "authorization": "EXPLICIT_USER_APPROVED_RUN2_PRESERVE_PREVIOUS_OUTPUT",
        "prior_failure": previous,
        "output": str(output), "parent_plan_sha256": original.PLAN_SHA,
        "parent_runner_sha256": PARENT_RUNNER_SHA,
        "launcher_sha256": original.sha256_file(Path(__file__)),
        "profile_sha256": plan["profile_sha256"], "window": plan["window"],
        "ledger": plan["ledger"], "parameters_or_decision_rule_changed": False,
        "sleep": "EXPLICITLY_CONFIRMED_FOR_THIS_STUDY",
        "runs": ["BASELINE", "F2"], "single_process": True,
        "retry_or_early_stop": False, "previous_output_modified": False,
    }
    return plan, authorization


async def execute_authorized(
    plan: dict, output: Path, authorization: dict, *,
    _execute: Callable[[dict, Path], Awaitable[dict]] = original.execute,
) -> dict:
    path = output / "execution-authorization.json"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(authorization, stream, ensure_ascii=True, allow_nan=False, indent=2)
    result = await _execute(plan, output)
    result = {
        **result, "execution_authorization": authorization,
        "execution_authorization_sha256": original.sha256_file(path),
    }
    destination = output / "f2-test.json"
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, ensure_ascii=True, allow_nan=False, indent=2)
    original.emit_status({"status": "completed", "result": str(destination),
                          "sha256": original.sha256_file(destination), "decision": result["decision"]})
    return result


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    plan, authorization = prepare(OUTPUT)
    sys.dont_write_bytecode = True
    with asyncio.Runner() as runner:
        runner.get_loop()
        original.install_offline_guard(OUTPUT)
        OUTPUT.mkdir()
        original.emit_status({"status": "authorized_fresh_output", "output": str(OUTPUT),
                              "previous_output_preserved": True})
        runner.run(execute_authorized(plan, OUTPUT, authorization))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except original.MeasurementError as exc:
        original.emit_status({"status": "failed", "code": str(exc)})
        raise SystemExit(2) from exc
