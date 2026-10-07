"""Registered final count-only exception; the preceding risk profile is unchanged."""

from dataclasses import dataclass

from app.strategies.original_offline_risk import OriginalOfflineRiskProfile

REFERENCE_SHA256 = "87fc20777716dd6b265f5a9318a4a13d509524cf3eaa7c94c57416af26cce176"
EXCEPTION_ID = "original-v2-final-count-only-gap18d-exception-v1"


@dataclass(frozen=True)
class OriginalGapRiskProfile(OriginalOfflineRiskProfile):
    profile_id: str = "original-fixed-cap6-lev3-gap18d-v1"
    gap_blackout_seconds: int = 18 * 86400
    exception_id: str = EXCEPTION_ID


PROFILE = OriginalGapRiskProfile()


def exception_record() -> dict:
    return {
        "exception_id": EXCEPTION_ID,
        "rule_excepted": "STOP_IF_VALIDATION_FULLY_VERIFIED_COMPLETED_LT_150",
        "authorization": "ONE_USER_AUTHORIZED_DOCUMENTED_EXCEPTION",
        "final_attempt": True,
        "reason": (
            "Run2 VALIDATION had a BTC data gap on 2022-07-31 followed by the native "
            "accounting lock: 1546 rejected candidates and zero August entries. "
            "The user authorizes this final attempt on counts alone; no performance was viewed."
        ),
        "reference_report_sha256": REFERENCE_SHA256,
        "reference_fully_verified_completed": 129,
        "reference_accounting_rejections": 1546,
        "reference_august_entries": 0,
        "reference_completed_duration_p99_hours": 408.56972222222197,
        "blackout_seconds": PROFILE.gap_blackout_seconds,
        "new_validation_minimum_fully_verified_completed": 150,
        "below_threshold_action": "DO_NOT_RUN_TRAIN_ABANDON_ORIGINAL_V2",
    }
