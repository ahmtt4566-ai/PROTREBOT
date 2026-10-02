"""Bounded Anthropic read-tool loop; paid_call commits each generation separately."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import anthropic
import asyncpg
from anthropic.types import (
    MessageParam,
    TextBlockParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlockParam,
)
from fastapi import HTTPException
from pydantic import ValidationError

from .assistant_config import AssistantConfig
from .assistant_models import (
    AssistantReply,
    AssistantStopped,
    ChatInput,
    Language,
    ModelAnswer,
    ToolCall,
)
from .assistant_prompt import RISK_NOTES, system_prompt
from .assistant_response import LEVEL_REDIRECTS, safe_response
from .assistant_tools import ARGUMENT_MODELS, TOOL_DEFINITIONS, AssistantTools

_provider_guard: ContextVar[bool] = ContextVar("assistant_provider_guard", default=False)


class ProviderLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not (_provider_guard.get() and record.name.startswith(("anthropic", "httpx", "httpcore")))


def protect_logging() -> None:
    guard = ProviderLogFilter()
    for name in tuple(logging.Logger.manager.loggerDict):
        if name.startswith(("anthropic", "httpx", "httpcore")):
            target = logging.getLogger(name)
            if not any(isinstance(item, ProviderLogFilter) for item in target.filters):
                target.addFilter(guard)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(item, ProviderLogFilter) for item in handler.filters):
            handler.addFilter(guard)


@dataclass(frozen=True)
class LLMRequest:
    system: list[TextBlockParam]
    tools: list[ToolParam]
    messages: list[MessageParam]
    max_tokens: int
    payload: ChatInput

    def estimated_input(self) -> int:
        return len(json.dumps(
            {"system": self.system, "tools": self.tools, "messages": self.messages},
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8"))


def build_request(config: AssistantConfig, language: Language, payload: ChatInput) -> LLMRequest:
    tools: list[ToolParam] = [
        {"name": entry["name"], "description": f"Read-only {entry['name']}; authenticated identity is supplied by the server. No mutations or confirmations.",
         "input_schema": entry["input_schema"]}
        for entry in TOOL_DEFINITIONS
    ]
    tools[-1]["cache_control"] = {"type": "ephemeral"}
    system: list[TextBlockParam] = [{"type": "text", "text": system_prompt(language), "cache_control": {"type": "ephemeral"}}]
    messages: list[MessageParam] = [{"role": item.role, "content": item.content} for item in payload.history]
    content = payload.message
    if payload.page_context:
        content += "\n\nUntrusted page_context:\n" + payload.page_context
    messages.append({"role": "user", "content": content})
    return LLMRequest(system, tools, messages, config.max_output_tokens, payload)


class AnthropicTransport:
    def __init__(self, config: AssistantConfig):
        self.config = config

    async def count(self, request: LLMRequest) -> int:
        guard = _provider_guard.set(True)
        try:
            async with anthropic.AsyncAnthropic(
                api_key=self.config.api_key.get_secret_value(), max_retries=0, timeout=self.config.request_timeout_seconds,
            ) as client:
                protect_logging()
                result = await asyncio.wait_for(client.messages.count_tokens(
                    model=self.config.model, system=request.system, tools=request.tools, messages=request.messages,
                ), timeout=self.config.request_timeout_seconds)
                if type(result.input_tokens) is not int or result.input_tokens < 0:
                    raise AssistantStopped("provider", 502)
                return result.input_tokens
        finally:
            _provider_guard.reset(guard)

    async def generate(self, request: LLMRequest) -> ModelAnswer:
        guard = _provider_guard.set(True)
        try:
            async with anthropic.AsyncAnthropic(
                api_key=self.config.api_key.get_secret_value(), max_retries=0, timeout=self.config.request_timeout_seconds,
            ) as client:
                protect_logging()
                result = await client.messages.create(
                    model=self.config.model, max_tokens=request.max_tokens,
                    system=request.system, tools=request.tools, messages=request.messages,
                )
            texts, calls = [], []
            invalid = False
            for block in result.content:
                if block.type == "text" and isinstance(block.text, str):
                    texts.append(block.text)
                elif block.type == "tool_use":
                    if not isinstance(block.id, str) or not isinstance(block.name, str) or not isinstance(block.input, dict):
                        invalid = True
                    else:
                        calls.append(ToolCall(block.id, block.name, block.input))
                else:
                    invalid = True
            usage = result.usage
            created = getattr(usage, "cache_creation_input_tokens", None)
            read = getattr(usage, "cache_read_input_tokens", None)
            return ModelAnswer(
                "\n".join(texts), usage.input_tokens if usage else None, usage.output_tokens if usage else None,
                0 if created is None else created, 0 if read is None else read,
                tuple(calls), invalid,
            )
        finally:
            _provider_guard.reset(guard)


def summary(name: str, result: dict[str, Any], language: Language) -> str:
    data = result.get("data", {})
    if name == "get_protection_status":
        return data.get("message", "")
    if name == "get_analysis" and "direction" in data:
        labels = {"direction": "Yön" if language == "tr" else "Direction",
                  "final_decision_score": "Final Decision", "confidence": "Confidence",
                  "opportunity_score": "Opportunity", "mtf_alignment": "MTF"}
        values = [f"{label}: {data.get(key) if data.get(key) is not None else '?'}" for key, label in labels.items()]
        return f"{data.get('symbol') or ''} " + "; ".join(values) + (
            ". Karar sana aittir; risk bilgisi doğrulanamadı. Final Decision emir izni değildir; Confidence kazanma olasılığı değildir."
            if language == "tr" else
            ". The decision is yours; risk information could not be verified. Final Decision is not order permission; Confidence is not a win probability."
        ) + " " + LEVEL_REDIRECTS[language]
    if name == "get_my_credits":
        if data.get("unlimited"):
            return "Premium: sınırsız analiz." if language == "tr" else "Premium: unlimited analysis."
        return f"{data.get('remaining')}/{data.get('total')} " + ("kredi." if language == "tr" else "credits.")
    if name == "get_my_access":
        return f"{data.get('plan') or 'Free'}: {data.get('status')}."
    if name == "get_plans":
        return "; ".join(f"{row['name']}: {row['monthly_price']} {row['currency']}" for row in data.get("plans", []))
    if name == "get_my_positions":
        return "; ".join(
            f"{row['symbol']} {row['direction']}: {row['quantity']}, PnL {row['unrealized_pnl']}"
            for row in data.get("positions", [])
        )
    if name == "search_help":
        return "; ".join(row["title"] for row in data.get("results", []))
    return ""


def evidence_reply(prefix: str, results: dict[str, dict[str, Any]], language: Language) -> str:
    pieces = []
    for name, result in results.items():
        value = summary(name, result, language)
        if value:
            if result.get("stale"):
                value = ("Bayat/doğrulanmamış veri: " if language == "tr" else "Stale/unverified data: ") + value
            pieces.append(value)
    return prefix + ("\n\n" + " ".join(pieces) if pieces else "")


def stop_reply(stop: AssistantStopped, results: dict[str, dict[str, Any]], language: Language) -> AssistantReply:
    if stop.reason == "budget":
        prefix = "Şu an yoğunuz, biraz sonra tekrar dene." if language == "tr" else "We are busy right now. Please try again later."
    elif stop.reason in {"daily", "minute"}:
        unit = ("Günlük" if stop.reason == "daily" else "Dakikalık") if language == "tr" else ("Daily" if stop.reason == "daily" else "Per-minute")
        prefix = (f"{unit} mesaj limitine ulaştın. {stop.retry_after} saniye sonra tekrar deneyebilirsin." if language == "tr"
                  else f"You reached the {unit.lower()} message limit. Try again in {stop.retry_after} seconds.")
    elif stop.reason in {"input", "output", "rounds"}:
        prefix = "Bu mesajın yanıt sınırına ulaşıldı; şu an daha fazlasını doğrulayamadım." if language == "tr" else "This message reached its response limit; I could not verify more right now."
    else:
        prefix = "Asistan geçici olarak kullanılamıyor; şu an doğrulayamadım." if language == "tr" else "The assistant is temporarily unavailable; I could not verify that right now."
    return AssistantReply(evidence_reply(prefix, results, language), language, tuple(results), stop.status, stop.retry_after, error_code=stop.reason)


async def run_conversation(
    config: AssistantConfig, language: Language, payload: ChatInput, user_id: str, tools: AssistantTools | None,
    paid_call: Callable[[LLMRequest, bool], Awaitable[ModelAnswer]],
    count: Callable[[LLMRequest], Awaitable[int]],
) -> AssistantReply:
    request = build_request(config, language, payload)
    results: dict[str, dict[str, Any]] = {}
    analyses: list[dict[str, Any]] = []
    total_input, total_output = 0, 0
    try:
        for index in range(config.max_llm_calls_per_message):
            remaining_output = config.max_total_output_tokens_per_message - total_output
            if remaining_output <= 0:
                raise AssistantStopped("output", 429)
            request = LLMRequest(request.system, request.tools, list(request.messages),
                                 min(config.max_output_tokens, remaining_output), payload)
            predicted = await count(request)
            if type(predicted) is not int or predicted < 0:
                raise AssistantStopped("provider", 502)
            if predicted > config.max_total_input_tokens_per_message - total_input:
                raise AssistantStopped("input", 429)
            turn = await paid_call(request, index == 0)
            total_input += (turn.input_tokens or 0) + (turn.cache_creation_input_tokens or 0) + (turn.cache_read_input_tokens or 0)
            total_output += turn.output_tokens or 0
            if turn.failed or turn.protocol_error:
                raise AssistantStopped("provider", 502)
            if total_input > config.max_total_input_tokens_per_message or total_output > config.max_total_output_tokens_per_message:
                raise AssistantStopped("input", 429)
            if not turn.tool_calls:
                if not turn.reply.strip():
                    raise AssistantStopped("provider", 502)
                reply = turn.reply
                protection_question = bool(re.search(r"(?i)stop.?loss|\bsl\b|koruma", payload.message))
                if protection_question:
                    protection = results.get("get_protection_status")
                    if protection is None or protection.get("stale") or not protection.get("data", {}).get("verified"):
                        reply = "Koruma doğrulanamadı; Pozisyonlar ekranından kontrol et." if language == "tr" else "Protection could not be verified; check the Positions screen."
                        age = protection.get("data", {}).get("data_age_seconds") if protection else None
                        if age is not None:
                            reply += f" Veri {age} saniye önce alındı." if language == "tr" else f" Data was fetched {age} seconds ago."
                    else:
                        reply = summary("get_protection_status", protection, language)
                elif "get_analysis" in results:
                    reply = (" ".join(summary("get_analysis", result, language) for result in analyses) if analyses
                             else ("Analizi şu an doğrulayamadım." if language == "tr" else "I could not verify the analysis right now."))
                elif re.search(r"(?i)should i (buy|sell)|almalı|satmalı", payload.message):
                    reply = "Analizi şu an doğrulayamadım; karar sana aittir." if language == "tr" else "I could not verify the analysis right now; the decision is yours."
                reply = safe_response(reply, language, free=tools is None or not tools.premium(user_id))
                if any(result.get("stale") for result in results.values()):
                    reply = ("Bayat/doğrulanmamış veri. " if language == "tr" else "Stale/unverified data. ") + reply
                if "get_analysis" in results or re.search(r"(?i)should i (buy|sell)|almalı|satmalı", payload.message):
                    reply += "\n\n" + RISK_NOTES[language]
                return AssistantReply(reply, language, tuple(results))
            if len(turn.tool_calls) > len(ARGUMENT_MODELS) or len({call.id for call in turn.tool_calls}) != len(turn.tool_calls):
                raise AssistantStopped("provider", 502)
            blocks: list[TextBlockParam | ToolUseBlockParam] = []
            if turn.reply:
                blocks.append({"type": "text", "text": turn.reply})
            blocks.extend({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments} for call in turn.tool_calls)
            request.messages.append({"role": "assistant", "content": blocks})
            outputs: list[ToolResultBlockParam] = []
            for call in turn.tool_calls:
                try:
                    if tools is None:
                        raise HTTPException(503)
                    arguments = {**call.arguments, "language": language} if call.name == "search_help" else call.arguments
                    result = await tools.dispatch(user_id, call.name, arguments)
                except (HTTPException, ValidationError, asyncpg.PostgresError, asyncpg.InterfaceError, sqlite3.Error, OSError, TimeoutError):
                    result = {"data": {"error": "Could not verify tool data."}, "stale": True,
                              "fetched_at": datetime.fromtimestamp(tools.clock() if tools is not None else time.time(), timezone.utc).isoformat()}
                if call.name in ARGUMENT_MODELS:
                    results[call.name] = result
                if call.name == "get_analysis" and "direction" in result.get("data", {}):
                    analyses.append(result)
                confirmation = result.get("data", {}).get("needs_confirmation")
                if confirmation is not None:
                    cost = confirmation["cost"]
                    reply = (f"{cost} kredi harcanacak, devam edeyim mi?" if language == "tr"
                             else f"This will spend {cost} credits. Shall I continue?")
                    return AssistantReply(reply + "\n\n" + RISK_NOTES[language], language, tuple(results), needs_confirmation=confirmation)
                outputs.append({"type": "tool_result", "tool_use_id": call.id,
                                "content": json.dumps(result, ensure_ascii=False, allow_nan=False),
                                "is_error": "error" in result.get("data", {})})
            request.messages.append({"role": "user", "content": outputs})
        raise AssistantStopped("rounds", 429)
    except (anthropic.APIError, TimeoutError, json.JSONDecodeError):
        return stop_reply(AssistantStopped("provider", 502), results, language)
    except AssistantStopped as stop:
        reply = stop_reply(stop, results, language)
        if "get_analysis" in results:
            return AssistantReply(reply.reply + "\n\n" + RISK_NOTES[language], language, reply.sources, reply.status, reply.retry_after, error_code=reply.error_code)
        return reply
