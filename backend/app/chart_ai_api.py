"""Separate, default-off chart entitlement and acknowledgement endpoints."""
from __future__ import annotations

import logging
import os
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .chart_ai_models import (
    ChartAiAcknowledgement,
    ChartAiAcknowledgementInput,
    ChartAiAcknowledgementResponse,
    ChartAiQuota,
    ChartAiUsage,
)
from .chart_ai_store import (
    POLICY_VERSION,
    ChartAiQuotaError,
    ChartAiStorageError,
    ChartAiStore,
    QuotaAccess,
)
from .member_entitlements import EntitlementProof, subscription_for_request
from .subscription_core import parse_datetime
from .v22_commercial import authenticated_user

logger = logging.getLogger(__name__)


def error_response(exc: ChartAiQuotaError | ChartAiStorageError) -> JSONResponse:
    code = exc.code if isinstance(exc, ChartAiQuotaError) else "storage_unavailable"
    status = exc.status_code if isinstance(exc, ChartAiQuotaError) else 503
    logger.warning("Chart AI metadata rejected code=%s status=%s", code, status)
    return JSONResponse({"error": {"code": code, "message": str(exc)}, "quota_consumed": False}, status_code=status)


def require_enabled() -> None:
    value = os.getenv("CHART_AI_ENABLED", "false").strip().lower()
    if value not in {"", "true", "false", "1", "0"}:
        logger.warning("Chart AI configuration rejected code=configuration_invalid")
        raise HTTPException(503, {"code": "configuration_invalid", "message": "Grafik AI ayarı doğrulanamadı."})
    if value not in {"true", "1"}:
        raise HTTPException(503, {"code": "feature_disabled", "message": "Grafik AI özelliği şu anda kapalı."})


router = APIRouter(prefix="/api/chart-ai", tags=["Chart AI metadata"], dependencies=[Depends(require_enabled)])


def member_access(request: Request) -> tuple[str, QuotaAccess]:
    user = authenticated_user(request)
    proof = getattr(request.state, "member_entitlement_proof", None)
    if not isinstance(proof, EntitlementProof):
        raise ChartAiQuotaError("entitlement_unavailable", "Üyelik kanıtı doğrulanamadı.", 503)
    try:
        subscription = subscription_for_request(request, user)
    except HTTPException as exc:
        if exc.status_code == 403:
            raise ChartAiQuotaError("access_denied", "Grafik analizi erişiminizin süresi dolmuş.", 403) from None
        raise ChartAiQuotaError("entitlement_unavailable", "Üyelik kanıtı doğrulanamadı.", 503) from None
    if user["role"] == "OWNER":
        return user["id"], QuotaAccess("owner")
    if subscription.get("master_trade_access") is not True:
        raise ChartAiQuotaError("access_denied", "Grafik analizi için aktif deneme veya MASTER MODE gerekli.", 403)
    if subscription.get("mode") == "FREE_TRIAL" or subscription.get("plan") == "TRIAL":
        start = parse_datetime(subscription.get("trial_start"))
        end = parse_datetime(subscription.get("trial_end"))
        if subscription.get("status") != "TRIALING" or start is None or end is None or end - start != timedelta(days=7):
            raise ChartAiQuotaError("entitlement_invalid", "Deneme dönemi doğrulanamadı.", 503)
        return user["id"], QuotaAccess("trial", start, end)
    if subscription.get("plan") == "MASTER_MODE" and subscription.get("status") in {"ACTIVE", "PAST_DUE"}:
        return user["id"], QuotaAccess("subscriber")
    raise ChartAiQuotaError("access_denied", "Grafik analizi için aktif deneme veya MASTER MODE gerekli.", 403)


def store(request: Request) -> ChartAiStore:
    return ChartAiStore(getattr(request.app.state, "db_pool", None))


@router.get("/usage", response_model=ChartAiUsage)
async def usage(request: Request) -> ChartAiUsage | JSONResponse:
    try:
        user_id, access = member_access(request)
        ledger = store(request)
        snapshot = await ledger.usage(user_id, access)
        accepted_at = await ledger.acknowledgement(user_id)
    except (ChartAiQuotaError, ChartAiStorageError) as exc:
        return error_response(exc)
    period = snapshot.period
    return ChartAiUsage(
        access_type=access.kind,
        quota=ChartAiQuota(limit=period.limit, used=snapshot.used, reserved=snapshot.reserved,
                           remaining=snapshot.remaining, unlimited=period.limit is None),
        period_start=period.start, period_end=period.end,
        resets_at=period.end if access.kind == "subscriber" else None,
        trial_expires_at=period.end if access.kind == "trial" else None,
        server_now=snapshot.server_now,
        acknowledgement=ChartAiAcknowledgement(accepted_at=accepted_at, required=accepted_at is None),
    )


@router.post("/acknowledgement", response_model=ChartAiAcknowledgementResponse)
async def acknowledge(request: Request, body: ChartAiAcknowledgementInput) -> ChartAiAcknowledgementResponse | JSONResponse:
    try:
        user_id, _ = member_access(request)
        if not body.accepted:
            raise ChartAiQuotaError("acknowledgement_required", "Devam etmek için uyarıyı okuyup onaylayın.", 422)
        if body.policy_version != POLICY_VERSION:
            raise ChartAiQuotaError("stale_acknowledgement", "Güncel kullanım uyarısını onaylayın.", 403)
        accepted_at = await store(request).acknowledge(user_id, body.policy_version)
    except (ChartAiQuotaError, ChartAiStorageError) as exc:
        return error_response(exc)
    return ChartAiAcknowledgementResponse(
        acknowledgement=ChartAiAcknowledgement(accepted_at=accepted_at, required=False),
    )
