"""Chart AI metadata only; no image input, analysis request or trading payload."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .chart_ai_store import POLICY_VERSION, PRIVACY_NOTICE, AccessType

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
    available: Literal[False] = False
    code: Literal["analysis_not_enabled"] = "analysis_not_enabled"
    message: str = "Analiz gönderimi henüz etkin değil."


class ChartAiUsage(BaseModel):
    feature_enabled: Literal[True] = True
    analysis_enabled: Literal[False] = False
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
