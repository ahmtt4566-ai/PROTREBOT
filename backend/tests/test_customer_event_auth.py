from unittest.mock import patch

import pytest

from app import customer_event_writer as writer
from test_account_settings import PASSWORD, setup
import test_account_settings as existing


def test_recording_exception_cannot_change_login_success_or_failure(setup, caplog):
    client = setup.runtime()
    with patch.object(writer, "record_customer_event", side_effect=RuntimeError("private@example.test token")):
        success = client.post("/api/v22/auth/login", json={"email": "member@example.test", "password": PASSWORD})
        failed = client.post("/api/v22/auth/login", json={"email": "member@example.test", "password": "wrong-password"})
    assert success.status_code == 200 and success.json()["user"]["id"] == "customer"
    assert failed.status_code == 401 and failed.json()["detail"] == "E-posta veya parola hatalı"
    assert "Customer event recording failed (RuntimeError)" in caplog.text
    assert "private@example.test" not in caplog.text


def test_nonexistent_login_never_schedules_an_event(setup):
    client = setup.runtime()
    with patch.object(writer, "start_task") as schedule:
        failed = client.post("/api/v22/auth/login", json={"email": "absent@example.test", "password": PASSWORD})
    assert failed.status_code == 401 and schedule.call_count == 0


@pytest.mark.parametrize("regression", [
    existing.test_shared_reset_token_expiry_replay_and_totp_enforcement,
    existing.test_verification_resend_real_delivery_cross_worker_single_use,
])
def test_original_security_regression_is_unchanged_when_recorder_raises(setup, regression):
    with patch.object(writer, "record_customer_event", side_effect=RuntimeError("private recording failure")):
        regression(setup)


def test_legacy_verification_replay_records_one_failure_without_changing_original_regression(setup):
    with patch.object(writer, "record_customer_event") as recorder:
        existing.test_verification_resend_real_delivery_cross_worker_single_use(setup)
    failed = [call for call in recorder.call_args_list if call.args[2] == "auth.email_verification_failed"]
    assert len(failed) == 1 and failed[0].args[3:6] == ("verification_failed", "verification", 400)
