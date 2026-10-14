"""Validated Original LIVE risk choices and atomic, append-only evidence."""
from __future__ import annotations

import copy
import json
import logging
import math
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .audit_log import AuditActor, write_audit
from .strategies import original_v2_demo as strategy

logger = logging.getLogger(__name__)
WARNING_VERSION = "original-live-risk-v1"
HIGH_RISK_CONFIRMATION = "YÜKSEK RİSKİ KABUL EDİYORUM"


def trade_warning(percent: float) -> str:
    return "RED" if percent > 5 else "ORANGE" if percent > 2 else "YELLOW" if percent > 1 else "NORMAL"


def daily_warning(percent: float) -> str:
    return "RED" if percent > 10 else "ORANGE" if percent > 5 else "NORMAL"


class RiskChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    risk_percent: float = Field(default=0.5, ge=0.5, le=10, allow_inf_nan=False, strict=True)
    daily_loss_percent: float = Field(default=5, ge=1, le=25, allow_inf_nan=False, strict=True)
    risk_acknowledged: bool = Field(default=False, strict=True)
    understood: bool = Field(default=False, strict=True)
    high_risk_confirmation: str = Field(default="", max_length=64, repr=False)
    warning_balance_usdt: float | None = Field(default=None, gt=0, allow_inf_nan=False, strict=True)
    expected_risk_revision: int = Field(default=0, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_consent(self) -> RiskChoice:
        if self.daily_loss_percent < self.risk_percent:
            raise ValueError("Günlük kayıp sınırı işlem riskinden küçük olamaz.")
        if 2 < self.risk_percent <= 5 and not self.risk_acknowledged:
            raise ValueError("Bu riski anladım kutusunu onaylayın.")
        if self.risk_percent > 5 and self.high_risk_confirmation != HIGH_RISK_CONFIRMATION:
            raise ValueError("YÜKSEK RİSKİ KABUL EDİYORUM yazın.")
        return self


class RiskUpdate(RiskChoice):
    totp_code: str | None = Field(default=None, min_length=6, max_length=32, repr=False)


def verified_wallet(snapshot: dict[str, Any]) -> float:
    value = snapshot.get("wallet_balance")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        logger.warning("ORIGINAL_RISK_BALANCE_UNVERIFIED")
        raise HTTPException(423, "Güncel Binance bakiyesi doğrulanamadı; risk ayarı değiştirilmedi.")
    return float(value)


def limits(state: dict[str, Any]) -> dict[str, float]:
    fixed = strategy.fixed_profile()["execution_policy"]
    current = state["policy"]
    return {
        "max_loss_per_trade_usdt": min(current["max_loss_per_trade"], fixed["max_loss_per_trade"]),
        "daily_loss_limit_usdt": min(current["daily_loss_limit"], fixed["daily_loss_limit"]),
        "max_margin_usdt": min(current["max_margin_per_trade"], fixed["max_margin_per_trade"]),
        "min_margin_usdt": 5,
        "leverage": 3,
    }


def projection(wallet: float, risk: float, daily: float, opening_wallet: float | None = None) -> dict[str, Any]:
    return {
        "wallet_balance_usdt": wallet,
        "estimated_trade_loss_usdt": wallet * risk / 100,
        "balance_after_5_losses_usdt": wallet * (1 - risk / 100) ** 5,
        "balance_after_10_losses_usdt": wallet * (1 - risk / 100) ** 10,
        "baseline_1_percent_after_5_losses_usdt": wallet * 0.99 ** 5,
        "baseline_1_percent_after_10_losses_usdt": wallet * 0.99 ** 10,
        "daily_loss_limit_usdt": (opening_wallet if opening_wallet is not None else wallet) * daily / 100,
    }


def audit_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    risk = config.get("risk_percent", 0.5)
    daily = config.get("daily_loss_percent", 5)
    return {
        "risk_percent": risk, "daily_loss_percent": daily,
        "warning_level": trade_warning(risk), "daily_warning_level": daily_warning(daily),
        "risk_acknowledged": config.get("risk_acknowledged", False),
        "high_risk_confirmed": config.get("high_risk_confirmed", False),
        "understood": config.get("understood", False),
        "warning_balance_usdt": config.get("warning_balance_usdt"),
        "warning_version": WARNING_VERSION,
    }


async def save_choice(
    request: Request, user: dict[str, Any], state: dict[str, Any], body: RiskChoice,
    snapshot: dict[str, Any], fingerprint: str,
) -> dict[str, Any]:
    from . import v25_execution as live

    previous = state.get("original_v2_live", {})
    if previous and (previous.get("owner_id") != user["id"] or previous.get("fingerprint") != fingerprint):
        raise HTTPException(423, "Original V2 hesap bağlantısı değişti; risk ve günlük limit kaydı korunuyor.")
    if body.expected_risk_revision != previous.get("risk_revision", 0):
        raise HTTPException(409, "Risk ayarı başka bir istekte değişti; güncel değerleri inceleyip yeniden onaylayın.")
    wallet = verified_wallet(snapshot)
    if body.warning_balance_usdt is None and body.risk_percent > 2:
        raise HTTPException(422, "Yüksek risk için gösterilen bakiye ve kayıp tahminini yeniden onaylayın.")
    if body.warning_balance_usdt is not None and abs(wallet - body.warning_balance_usdt) > 0.01:
        raise HTTPException(409, "Binance bakiyesi değişti; güncel kayıp tahminini inceleyip yeniden onaylayın.")
    candidate = {
        **previous, "owner_id": user["id"], "fingerprint": fingerprint,
        "risk_percent": body.risk_percent, "daily_loss_percent": body.daily_loss_percent,
        "risk_acknowledged": body.risk_acknowledged,
        "understood": body.understood,
        "high_risk_confirmed": body.high_risk_confirmation == HIGH_RISK_CONFIRMATION,
        "warning_balance_usdt": wallet, "warning_version": WARNING_VERSION,
    }
    changed = not previous or any(previous.get(key, default) != candidate[key] for key, default in (
        ("risk_percent", 0.5), ("daily_loss_percent", 5),
        ("risk_acknowledged", False), ("high_risk_confirmed", False),
        ("understood", False),
    ))
    if not changed:
        return previous
    candidate["risk_revision"] = previous.get("risk_revision", 0) + 1
    current_day = previous.get("daily") or {}
    opening = None
    if current_day.get("day") == datetime.now(timezone.utc).date().isoformat() and current_day.get("opening_wallet") is not None:
        opening = verified_wallet({"wallet_balance": current_day["opening_wallet"]})
    candidate["risk_projection"] = projection(wallet, body.risk_percent, body.daily_loss_percent, opening)
    candidate.pop("last_risk_adjustment", None)
    day = None
    if isinstance(previous.get("daily"), dict) and previous["daily"].get("opening_wallet") is not None:
        day = copy.deepcopy(previous["daily"])
        day_wallet = verified_wallet({"wallet_balance": day["opening_wallet"]})
        loss = day.get("loss", 0)
        if isinstance(loss, bool) or not isinstance(loss, (int, float)) or not math.isfinite(loss) or loss < 0:
            logger.warning("ORIGINAL_RISK_DAILY_UNVERIFIED")
            raise HTTPException(423, "Günlük kayıp kaydı doğrulanamadı; risk ayarı değiştirilmedi.")
        day["limit"] = day_wallet * body.daily_loss_percent / 100
        day["locked"] = bool(day.get("locked")) or loss >= day["limit"]
        candidate["daily"] = day
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(503, "Risk değişikliği ve onay kaydı için kalıcı veritabanı gerekli.")
    revision = live._advance_persistence_revision(state)
    payload = live.sanitized_state({**state, "plans": copy.deepcopy(state["plans"]), "original_v2_live": candidate})
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(live.V25_RISK_SNAPSHOT_UPSERT_SQL, live.V25_SNAPSHOT_KEY,
                                          json.dumps(payload, ensure_ascii=False), body.expected_risk_revision)
                if row is None:
                    raise HTTPException(409, "Kalıcı durum sürümü değişti; risk ayarı uygulanmadı.")
                if row["write_status"] not in {"INSERTED", "UPDATED"}:
                    raise RuntimeError("Risk snapshot write status invalid")
                if state["persistence_revision"] != revision:
                    raise HTTPException(409, "Canlı durum doğrulama sırasında değişti; risk ayarı uygulanmadı.")
                await write_audit(
                    conn, AuditActor.from_request(request, user), "original_live.risk_changed",
                    "USER", user["id"], audit_snapshot(previous), audit_snapshot(candidate),
                )
                if state["persistence_revision"] != revision:
                    raise HTTPException(409, "Canlı durum kayıt sırasında değişti; risk ayarı uygulanmadı.")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("ORIGINAL_RISK_SAVE_FAILED error_type=%s", type(exc).__name__)
        raise HTTPException(503, "Risk ayarı ve onay kaydı birlikte kaydedilemedi; eski ayarlar korundu.") from exc
    previous.pop("last_risk_adjustment", None)
    for key in (
        "owner_id", "fingerprint", "risk_percent", "daily_loss_percent", "risk_acknowledged",
        "high_risk_confirmed", "understood", "warning_balance_usdt", "warning_version", "risk_revision", "risk_projection",
    ):
        previous[key] = candidate[key]
    if day is not None and isinstance(previous.get("daily"), dict):
        previous["daily"].update(limit=day["limit"], locked=bool(previous["daily"].get("locked")) or day["locked"])
    state["original_v2_live"] = previous
    request.app.state.v25_last_awaited_persistence_write = {
        "result": str(row["write_status"]), "submitted_revision": revision, "checked_at": live.now_iso(),
    }
    return previous
