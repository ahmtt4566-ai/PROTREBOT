"""Synthetic authorized-output orchestration only; never start a real replay."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import prescreen_original_f2_run2 as launch
import test_offline_strategy_facade as native_tests

offline_only = native_tests.offline_only


def test_only_original_empty_progress_is_accepted_and_preserved(tmp_path):
    progress = tmp_path / "progress.jsonl"
    progress.write_text("")
    result = launch.validate_previous_failure(tmp_path)
    assert result["phase_replay_started"] is False and result["preserved"] is True
    assert result["files"]["progress.jsonl"] == launch.original.sha256_file(progress)
    assert progress.read_bytes() == b""
    progress.write_text('{"completed_bars":0}')
    with pytest.raises(launch.original.MeasurementError, match="PREVIOUS_OUTPUT_NOT_PRE_REPLAY_FAILURE"):
        launch.validate_previous_failure(tmp_path)


def test_previous_trade_or_report_file_never_permits_restart(tmp_path):
    (tmp_path / "progress.jsonl").write_text("")
    (tmp_path / "report-BASELINE.json").write_text("{}")
    with pytest.raises(launch.original.MeasurementError, match="PREVIOUS_OUTPUT_NOT_PRE_REPLAY_FAILURE"):
        launch.validate_previous_failure(tmp_path)


def test_unapproved_output_is_rejected_before_parent_or_data_read(tmp_path):
    with pytest.raises(launch.original.MeasurementError, match="RESTART_OUTPUT_SCOPE_DENIED"):
        launch.prepare(tmp_path)


def test_launcher_is_pinned_to_validated_parent_and_unchanged_plan():
    assert launch.original.sha256_file(Path(launch.original.__file__)) == launch.PARENT_RUNNER_SHA
    plan = launch.original.locked_plan()
    assert plan["profile_sha256"] == launch.original.PROFILE.profile_hash
    assert launch.OUTPUT.name == "original-v2-f2-test-run2"
    assert launch.original.OUTPUT.name == "original-v2-f2-test"


def test_synthetic_execution_writes_authorization_and_final_result_once(offline_only, tmp_path, capsys):
    calls = []
    plan, authorization = {"synthetic": True}, {"parameters_or_decision_rule_changed": False}

    async def synthetic_execute(received, output):
        assert received is plan and output == tmp_path
        assert (output / "execution-authorization.json").is_file()
        calls.append("synthetic_no_replay")
        return {"decision": {"value": "DUR"}, "synthetic_no_replay": True}

    result = offline_only.run_until_complete(launch.execute_authorized(
        plan, tmp_path, authorization, _execute=synthetic_execute))
    assert calls == ["synthetic_no_replay"]
    assert json.loads((tmp_path / "f2-test.json").read_text()) == result
    assert result["execution_authorization_sha256"] == launch.original.sha256_file(
        tmp_path / "execution-authorization.json")
    message = json.loads(capsys.readouterr().out)
    assert message["status"] == "completed"
    assert message["sha256"] == launch.original.sha256_file(tmp_path / "f2-test.json")


def test_synthetic_failure_keeps_authorization_and_does_not_retry(offline_only, tmp_path):
    calls = []

    async def fail_once(plan, output):
        calls.append("one_attempt")
        raise launch.original.MeasurementError("SYNTHETIC_FAILURE")

    with pytest.raises(launch.original.MeasurementError, match="SYNTHETIC_FAILURE"):
        offline_only.run_until_complete(launch.execute_authorized(
            {}, tmp_path, {}, _execute=fail_once))
    assert calls == ["one_attempt"]
    assert (tmp_path / "execution-authorization.json").exists()
    assert not (tmp_path / "f2-test.json").exists()
