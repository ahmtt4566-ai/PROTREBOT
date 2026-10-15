"""Separate, default-off chart metadata, analysis and owned-result endpoints."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import SecretStr, ValidationError
from starlette.requests import ClientDisconnect

from .assistant_api import contains_secret
from .assistant_config import AssistantConfig
from .assistant_models import ChatInput
from .chart_ai_models import (
    MAX_ANALYZE_BODY_BYTES,
    ChartAiAcknowledgement,
    ChartAiAcknowledgementInput,
    ChartAiAcknowledgementResponse,
    ChartAiAnalyzeInput,
    ChartAiAvailability,
    ChartAiQuota,
    ChartAiRequestError,
    ChartAiRequestResponse,
    ChartAiUsage,
)
from .chart_ai_service import (
    AnthropicChartProvider,
    ChartAnalysisService,
    analysis_error,
    build_request,
)
from .chart_ai_store import (
    POLICY_VERSION,
    ChartAiQuotaError,
    ChartAiStorageError,
    ChartAiStore,
    ChartRequest,
    QuotaAccess,
)
from .chart_ai_validation import (
    ChartAiValidationError,
    validate_context,
    validate_image,
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


def require_owner_pilot(request: Request) -> None:
    value = os.getenv("CHART_AI_OWNER_ONLY", "true").strip().lower()
    if value not in {"true", "false", "1", "0"}:
        logger.warning("Chart AI configuration rejected code=configuration_invalid")
        raise HTTPException(503, {"code": "configuration_invalid", "message": "Grafik AI pilot ayarı doğrulanamadı."})
    user = authenticated_user(request)
    if value in {"true", "1"} and user["role"] != "OWNER":
        raise HTTPException(403, {"code": "owner_only", "message": "Grafik AI şu anda yalnız OWNER için açık."})


router = APIRouter(
    prefix="/api/chart-ai", tags=["Chart AI"],
    dependencies=[Depends(require_enabled), Depends(require_owner_pilot)],
)


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


def analysis_service(request: Request) -> ChartAnalysisService:
    existing = getattr(request.app.state, "chart_ai_service", None)
    if isinstance(existing, ChartAnalysisService):
        return existing
    service = ChartAnalysisService(AnthropicChartProvider(SecretStr(os.getenv("ANTHROPIC_API_KEY", "").strip())))
    request.app.state.chart_ai_service = service
    return service


async def shutdown_chart_ai(app: FastAPI) -> None:
    service = getattr(app.state, "chart_ai_service", None)
    if isinstance(service, ChartAnalysisService):
        await service.shutdown()


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
    ready = analysis_service(request).available
    return ChartAiUsage(
        analysis_enabled=ready,
        access_type=access.kind,
        quota=ChartAiQuota(limit=period.limit, used=snapshot.used, reserved=snapshot.reserved,
                           remaining=snapshot.remaining, unlimited=period.limit is None),
        period_start=period.start, period_end=period.end,
        resets_at=period.end if access.kind == "subscriber" else None,
        trial_expires_at=period.end if access.kind == "trial" else None,
        server_now=snapshot.server_now,
        acknowledgement=ChartAiAcknowledgement(accepted_at=accepted_at, required=accepted_at is None),
        availability=ChartAiAvailability(
            available=ready, code="ready" if ready else "provider_unavailable",
            message="Gönderimde kota ve bütçe ayrıca doğrulanır." if ready else "Analiz servisi şu an kullanılamıyor.",
        ),
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


async def _read_analysis_input(request: Request) -> ChartAiAnalyzeInput:
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise ChartAiQuotaError("content_type_invalid", "Analiz girdisi JSON olmalı.", 415)
    data = bytearray()
    try:
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_ANALYZE_BODY_BYTES:
                raise ChartAiQuotaError("request_too_large", "Analiz girdisi boyut sınırını aşıyor.", 413)
            data.extend(chunk)
    except ClientDisconnect:
        raise ChartAiQuotaError("request_disconnected", "Girdi aktarımı tamamlanamadı.", 400) from None
    try:
        body = ChartAiAnalyzeInput.model_validate_json(bytes(data))
    except ValidationError:
        raise ChartAiQuotaError("input_invalid", "Analiz girdisi eksik veya geçersiz.", 422) from None
    if not body.question.strip():
        raise ChartAiQuotaError("question_invalid", "Analiz sorusu boş olamaz.", 422)
    # Reuse the existing scanner with trusted chart defaults, not chat quota/enable settings.
    if contains_secret(ChatInput(message=body.question), AssistantConfig.model_construct()):
        raise ChartAiQuotaError("question_sensitive", "Soruda anahtar veya gizli bilgi paylaşmayın.", 422)
    return body


def _request_response(record: ChartRequest) -> JSONResponse:
    error = None
    if record.status == "processing":
        status = 202
    elif record.status == "succeeded":
        status = 200
    elif record.status == "expired":
        status = 410
        error = ChartAiRequestError(code="result_expired", message="Analiz sonucunun 24 saatlik saklama süresi doldu.")
    else:
        failure = analysis_error(record.error_code or "analysis_failed")
        status = failure.status_code
        error = ChartAiRequestError(code=failure.code, message=str(failure))
        logger.warning("Chart AI request failed code=%s", failure.code)
    response = ChartAiRequestResponse(
        request_key=record.request_key, status=record.status, quota_consumed=record.quota_consumed,
        created_at=record.created_at, completed_at=record.completed_at,
        result_expires_at=record.result_expires_at, result_text=record.result_text, error=error,
    )
    return JSONResponse(response.model_dump(mode="json"), status_code=status,
                        headers={"Retry-After": "2"} if status == 202 else None)


@router.post("/analyze", response_model=ChartAiRequestResponse)
async def analyze(request: Request) -> JSONResponse:
    try:
        user_id, access = member_access(request)
        body = await _read_analysis_input(request)
        try:
            raw_image = base64.b64decode(body.image.data_base64, validate=True)
        except (binascii.Error, ValueError):
            raise ChartAiQuotaError("image_base64_invalid", "Görsel kodlaması geçersiz.", 422) from None
        context = validate_context(body.context, expected_symbol=body.symbol, expected_timeframe=body.timeframe)
        image = await asyncio.to_thread(validate_image, raw_image, body.image.media_type)
        identity = json.dumps({
            "symbol": body.symbol, "timeframe": body.timeframe, "question": body.question,
            "source": body.image.source, "media_type": body.image.media_type,
            "context": context.context.model_dump(mode="json"),
        }, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        digest = hashlib.sha256(identity + b"\0" + raw_image).hexdigest()
        ledger = store(request)
        reservation = await ledger.reserve(user_id, access, body.request_key, digest)
        if not reservation.created:
            return _request_response(reservation.request)
        token = reservation.lease_token
        if token is None:
            raise ChartAiStorageError("Analiz işlem sahipliği doğrulanamadı.")
        job = analysis_service(request).launch(
            ledger, user_id, body.request_key, token, build_request(body.question, image, context), context,
        )
        # The job owns accounting even if this HTTP consumer disconnects/cancels.
        return _request_response(await asyncio.shield(job))
    except ChartAiValidationError as exc:
        return error_response(ChartAiQuotaError(exc.code, str(exc), exc.status_code))
    except (ChartAiQuotaError, ChartAiStorageError) as exc:
        return error_response(exc)


@router.get("/requests/{request_key}", response_model=ChartAiRequestResponse)
async def request_result(request: Request, request_key: str) -> JSONResponse:
    user = authenticated_user(request)
    try:
        try:
            key = UUID(request_key)
        except ValueError:
            raise ChartAiQuotaError("request_key_invalid", "Geçerli bir UUID istek anahtarı gerekli.", 422) from None
        return _request_response(await store(request).request(user["id"], key))
    except (ChartAiQuotaError, ChartAiStorageError) as exc:
        return error_response(exc)
