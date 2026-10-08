"""Immutable offline trend trial; records three disclosed protocol relaxations."""

from dataclasses import dataclass

from app.strategies.original_gap_risk import OriginalGapRiskProfile

PARENT_HASH = "b21a9fbf7916808c6b0b9b26e6700725defe38dc4fb6a0151731deab8b251d81"


@dataclass(frozen=True)
class OriginalTrendRiskProfile(OriginalGapRiskProfile):
    profile_id: str = "original-fixed-cap6-lev3-gap18d-trend100d-v1"
    parent_profile_hash: str = PARENT_HASH
    trend_interval: str = "4h"
    trend_period: int = 600


PROFILE = OriginalTrendRiskProfile()


def trial_record() -> dict:
    return {
        "relaxation_ledger": [
            {"number": 1, "change": "GAP_BLACKOUT_EXCEPTION_TO_VALIDATION_N_LT_150_STOP"},
            {"number": 2, "change": "TREND_DIRECTION_FILTER_SELECTED_AFTER_VALIDATION_PERFORMANCE"},
            {"number": 3, "change": "N_150_TO_100_BEFORE_THIS_TRIAL_OUTCOMES",
             "reason": "The mechanical direction filter reduces eligible entries"},
        ],
        "attempt_budget": 5, "attempts_used": 3, "attempts_remaining": 2,
        "validation_clean": False,
        "validation_note": "VALIDATION is no longer clean: this filter was selected after viewing its previous result.",
        "selection_after_current_results": False,
        "train_history_note": "Measurement starts 2020-10; TRAIN's first approximately 100 days lack SMA600 warmup.",
        "validation_history_note": "VALIDATION SMA600 uses TRAIN history through a separate long closed-4h window.",
    }
