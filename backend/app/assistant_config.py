"""Environment-backed settings for the read-only customer assistant."""
from __future__ import annotations

from decimal import Decimal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AssistantConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ASSISTANT_",
        frozen=True,
        str_strip_whitespace=True,
        hide_input_in_errors=True,
    )

    enabled: bool = True
    model: str = Field(default="claude-haiku-4-5-20251001", min_length=1)
    api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="ANTHROPIC_API_KEY",
        repr=False,
        exclude=True,
    )
    max_input_chars: int = Field(default=500, gt=0)
    history_messages: int = Field(default=4, ge=0)
    history_message_max_chars: int = Field(default=2000, gt=0)
    page_context_max_chars: int = Field(default=300, gt=0)
    request_timeout_seconds: int = Field(default=30, gt=0)
    protection_stale_seconds: int = Field(default=30, ge=30)
    proactive_enabled: bool = True
    proactive_inactive_days: int = Field(default=7, gt=0)
    proactive_cooldown_hours: int = Field(default=24, ge=24)
    proactive_poll_seconds: int = Field(default=300, ge=60)
    secret_min_alphanumeric_chars: int = Field(default=40, gt=0)
    budget_warning_fraction: Decimal = Field(default=Decimal("0.8"), gt=0, le=1, allow_inf_nan=False)
    per_minute_limit: int = Field(default=6, gt=0)
    daily_limit: int = Field(default=20, gt=0)
    monthly_budget_usd: Decimal = Field(default=Decimal(50), ge=0, allow_inf_nan=False)
    max_output_tokens: int = Field(default=500, gt=0)
    max_llm_calls_per_message: int = Field(default=3, gt=0, le=3)
    max_total_input_tokens_per_message: int = Field(default=12000, gt=0)
    max_total_output_tokens_per_message: int = Field(default=1500, gt=0)
    input_price_usd_per_million: Decimal = Field(default=Decimal(1), ge=0, allow_inf_nan=False)
    output_price_usd_per_million: Decimal = Field(default=Decimal(5), ge=0, allow_inf_nan=False)
    cache_write_price_usd_per_million: Decimal = Field(default=Decimal("1.25"), ge=0, allow_inf_nan=False)
    cache_read_price_usd_per_million: Decimal = Field(default=Decimal("0.10"), ge=0, allow_inf_nan=False)

    @field_validator("api_key", mode="before")
    @classmethod
    def normalize_api_key(cls, value: object) -> object:
        if isinstance(value, SecretStr):
            return SecretStr(value.get_secret_value().strip())
        if isinstance(value, str):
            return value.strip()
        return value

    @property
    def available(self) -> bool:
        return self.enabled and bool(self.api_key.get_secret_value())

    @property
    def unavailable_reason(self) -> str | None:
        if not self.enabled:
            return "Asistan kullanılamıyor: özellik devre dışı."
        if not self.api_key.get_secret_value():
            return "Asistan kullanılamıyor: servis yapılandırılmamış."
        return None


def load_assistant_config() -> AssistantConfig:
    return AssistantConfig()
