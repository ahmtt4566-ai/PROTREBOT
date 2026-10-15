"""Isolated chart analysis contracts; no trading payload or client entitlement."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .chart_ai_store import POLICY_VERSION, PRIVACY_NOTICE, AccessType
from .chart_ai_validation import MAX_IMAGE_BYTES, Symbol, Timeframe

DISCLAIMER = "Bu analiz yatırım tavsiyesi değildir."
NOTICE = f"{PRIVACY_NOTICE}\n{DISCLAIMER}"


class ChartAiAcknowledgementInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    policy_version: str = Field(min_length=1, max_length=64)
    accepted: StrictBool


class ChartAiAcknowledgement(BaseModel):
    policy_version: str = POLICY_VERSION
    accepted_at: datetime | None
    required: bool
    text: str = NOTICE


class ChartAiQuota(BaseModel):
    limit: int | None
    used: int
    reserved: int
    remaining: int | None
    unlimited: bool


class ChartAiAvailability(BaseModel):
    available: bool = False
    code: Literal["analysis_not_enabled", "provider_unavailable", "ready"] = "analysis_not_enabled"
    message: str = "Analiz gönderimi henüz etkin değil."


class ChartAiUsage(BaseModel):
    feature_enabled: Literal[True] = True
    analysis_enabled: bool = False
    eligible: Literal[True] = True
    access_type: AccessType
    quota: ChartAiQuota
    period_start: datetime | None
    period_end: datetime | None
    resets_at: datetime | None
    trial_expires_at: datetime | None
    server_now: datetime
    acknowledgement: ChartAiAcknowledgement
    availability: ChartAiAvailability = Field(default_factory=ChartAiAvailability)


class ChartAiAcknowledgementResponse(BaseModel):
    acknowledgement: ChartAiAcknowledgement
    quota_consumed: Literal[False] = False


MAX_BASE64_CHARS = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
MAX_ANALYZE_BODY_BYTES = MAX_BASE64_CHARS + 64 * 1024


class ChartAiImageInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    source: Literal["screenshot", "upload"]
    data_base64: str = Field(min_length=1, max_length=MAX_BASE64_CHARS, repr=False)


class ChartAiAnalyzeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    request_key: UUID
    symbol: Symbol
    timeframe: Timeframe
    question: str = Field(default="Bu grafiği yorumla", min_length=1, max_length=800, repr=False)
    image: ChartAiImageInput = Field(repr=False)
    context: dict[str, object] = Field(repr=False)


class ChartAiRequestError(BaseModel):
    code: str
    message: str


class ChartAiRequestResponse(BaseModel):
    request_key: UUID
    status: Literal["processing", "succeeded", "failed", "expired"]
    quota_consumed: bool
    created_at: datetime
    completed_at: datetime | None
    result_expires_at: datetime | None
    result_text: str | None
    error: ChartAiRequestError | None
