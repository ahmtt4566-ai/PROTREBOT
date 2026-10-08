"""Immutable fourth offline trial; no production strategy registration."""

from dataclasses import dataclass

from app.strategies.original_gap_risk import OriginalGapRiskProfile

PARENT_HASH = "b21a9fbf7916808c6b0b9b26e6700725defe38dc4fb6a0151731deab8b251d81"


@dataclass(frozen=True)
class OriginalF2RiskProfile(OriginalGapRiskProfile):
    profile_id: str = "original-fixed-cap6-lev3-gap18d-f2-v1"
    parent_profile_hash: str = PARENT_HASH
    feature_interval: str = "15m"
    feature_window: int = 259
    f2_threshold: float = 1.0096773571199356


PROFILE = OriginalF2RiskProfile()
