"""Shared assistant request, provider and response types."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Language = Literal["tr", "en"]


class HistoryMessage(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class ChatInput(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    message: str = Field(min_length=1)
    history: list[HistoryMessage] = Field(default_factory=list)
    page_context: str | None = None


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelAnswer:
    reply: str
    input_tokens: int | None
    output_tokens: int | None
    cache_creation_input_tokens: int | None = 0
    cache_read_input_tokens: int | None = 0
    tool_calls: tuple[ToolCall, ...] = ()
    protocol_error: bool = False
    failed: bool = False


@dataclass(frozen=True)
class AssistantReply:
    reply: str
    language: Language
    sources: tuple[str, ...] = ()
    status: int = 200
    retry_after: int | None = None
    needs_confirmation: dict[str, Any] | None = None
    error_code: str | None = None


class AssistantStopped(Exception):
    def __init__(self, reason: str, status: int = 503, retry_after: int | None = None):
        self.reason, self.status, self.retry_after = reason, status, retry_after
        super().__init__(reason)
