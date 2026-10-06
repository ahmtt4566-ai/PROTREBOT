"""Versioned metadata for new adapter evaluations, never legacy backfills."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .contracts import StrategyProvenance

KAIS_ORIGINAL_ID = "kais_original"
KAIS_ORIGINAL_VERSION = "adapter-v1"


def parameter_hash(parameters: Mapping[str, Any]) -> str:
    """Hash explicit effective parameters without I/O or persisted identifiers."""
    body = json.dumps(
        dict(parameters), sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def original_provenance(
    effective_policy: Mapping[str, Any],
    required_intervals: tuple[str, ...],
) -> StrategyProvenance:
    parameters = {
        "confidence_threshold": effective_policy["confidence_threshold"],
        "breakout_quality_threshold": effective_policy["breakout_quality_threshold"],
        "mtf_allow_either_timeframe": bool(effective_policy["mtf_allow_either_timeframe"]),
        "required_intervals": list(required_intervals),
    }
    return StrategyProvenance(
        strategy_id=KAIS_ORIGINAL_ID,
        strategy_version=KAIS_ORIGINAL_VERSION,
        parameter_hash=parameter_hash(parameters),
    )
