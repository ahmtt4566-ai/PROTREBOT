"""Lossless legacy access: no renamed fields, defaults or numeric rounding."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .contracts import StrategyResult


def legacy_output(result: StrategyResult) -> dict[str, Any]:
    """Return an isolated copy with the native keys, values and ordering intact."""
    return deepcopy(result.legacy)
