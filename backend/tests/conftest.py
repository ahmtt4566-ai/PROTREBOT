from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_binance_global_ban(tmp_path: Path):
    from app.binance_rate_limit import BINANCE_GLOBAL_BAN

    previous_path = BINANCE_GLOBAL_BAN.path
    BINANCE_GLOBAL_BAN.path = tmp_path / "binance_global_ban.json"
    BINANCE_GLOBAL_BAN.reset(clear_persistent=True)
    try:
        yield
    finally:
        BINANCE_GLOBAL_BAN.reset(clear_persistent=True)
        BINANCE_GLOBAL_BAN.path = previous_path
