"""Authenticated, read-only customer assistant endpoints."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import anthropic
import asyncpg
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .analyst_credits import CreditTransaction
from .assistant_config import AssistantConfig, load_assistant_config
from .assistant_fastpath import answer as fastpath_answer
from .assistant_fastpath import detect_language
from .assistant_llm import (
    AnthropicTransport,
    LLMRequest,
    build_request,
    run_conversation,
)
from .assistant_models import AssistantStopped, ChatInput, Language, ModelAnswer
from .assistant_proactive import CheckInInput, PreferenceInput, ProactiveAssistant
from .assistant_storage import AssistantStorageError, AssistantStore, month_key
from .assistant_tools import ARGUMENT_MODELS, AssistantTools, ConfirmAnalysis
from .v22_commercial import authenticated_user

router = APIRouter(prefix="/api/assistant", tags=["Customer assistant"])
logger = logging.getLogger(__name__)
TOKENS_PER_MILLION = Decimal(1_000_000)


def reply_response(reply: str, language: Language, status: int = 200, *, retry_after: int | None = None) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return JSONResponse({"reply": reply, "language": language, "sources": []}, status_code=status, headers=headers)


def text(language: Language, tr: str, en: str) -> str:
    return tr if language == "tr" else en


def contains_secret(payload: ChatInput, config: AssistantConfig) -> bool:
    pattern = re.compile(
        rf"(?i)(?<![a-z])secret(?:[_ -]*key)?(?![a-z])|"
        rf"(?<![a-z0-9])[a-z0-9]{{{config.secret_min_alphanumeric_chars},}}(?![a-z0-9])|"
        r"\bsk-(?:ant-)?[a-z0-9_-]+|\bbearer\s+\S+|"
        r"\b(?:password|parola|api[_ -]*key|token)\s*[:=]\s*\S+",
    )
    return any(pattern.search(value) for value in (
        payload.message, payload.page_context or "", *(entry.content for entry in payload.history),
    ))


def estimated_input_tokens(language: Language, payload: ChatInput) -> int:
    return build_request(AssistantConfig(), language, payload).estimated_input()


def quota_state(row: dict[str, Any] | None, now: float) -> tuple[int, int, float, int]:
    current = datetime.fromtimestamp(now, timezone.utc)
    daily_count = int(row["daily_count"]) if row and row["day_key"] == current.date().isoformat() else 0
    minute_start = float(row["minute_start"]) if row else now
    minute_count = int(row["minute_count"]) if row else 0
    minute_seconds = timedelta(minutes=1).total_seconds()
    if now >= minute_start + minute_seconds:
        minute_start, minute_count = now, 0
    daily_retry = max(1, math.ceil(((current.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)) - current).total_seconds()))
    return daily_count, minute_count, minute_start, daily_retry


def audit(user_id: str, now: float, answer: ModelAnswer | None, cost: Decimal | None, *, warning: bool = False, estimated: bool = False, sources: tuple[str, ...] = ()) -> None:
    emit = logger.warning if warning else logger.info
    emit(
        "user_id=%s time=%s input_tokens=%s output_tokens=%s cost_usd=%s sources=%s estimated=%s cache_creation_input_tokens=%s cache_read_input_tokens=%s",
        user_id, datetime.fromtimestamp(now, timezone.utc).isoformat(),
        answer.input_tokens if answer else None, answer.output_tokens if answer else None, cost, list(sources), estimated,
        answer.cache_creation_input_tokens if answer else None, answer.cache_read_input_tokens if answer else None,
    )


class AssistantService:
    def __init__(
        self, store: AssistantStore, *,
        provider: Callable[[AssistantConfig, Language, ChatInput], Awaitable[ModelAnswer]] | None = None,
        token_counter: Callable[[LLMRequest], Awaitable[int]] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.store, self.provider, self.clock = store, provider, clock
        self.token_counter = token_counter
        self.pending: set[asyncio.Task[JSONResponse]] = set()

    async def start(self, user_id: str, language: Language, payload: ChatInput, config: AssistantConfig, tools: AssistantTools | None = None) -> JSONResponse:
        task = asyncio.create_task(self.chat(user_id, language, payload, config, tools))
        self.pending.add(task)
        def completed(done: asyncio.Task[JSONResponse]) -> None:
            self.pending.discard(done)
            if not done.cancelled() and done.exception() is not None:
                audit(user_id, self.clock(), None, None, warning=True)
        task.add_done_callback(completed)
        # A disconnected client must not roll back accounting for a billed call.
        return await asyncio.shield(task)

    async def close(self) -> None:
        if self.pending:
            await asyncio.gather(*self.pending)

    async def chat(self, user_id: str, language: Language, payload: ChatInput, config: AssistantConfig, tools: AssistantTools | None = None) -> JSONResponse:
        transport = AnthropicTransport(config)
        started = False

        async def count(request: LLMRequest) -> int:
            await self.preflight(user_id, config, first=not started)
            if self.token_counter is not None:
                return await self.token_counter(request)
            if self.provider is not None:
                return request.estimated_input()
            return await transport.count(request)

        async def paid(request: LLMRequest, first: bool) -> ModelAnswer:
            nonlocal started
            answer = await self.paid_turn(user_id, language, config, request, first, transport)
            started = True
            return answer

        reply = await run_conversation(config, language, payload, user_id, tools, paid, count)
        headers = {"Retry-After": str(reply.retry_after)} if reply.retry_after is not None else None
        body: dict[str, Any] = {"reply": reply.reply, "language": reply.language, "sources": list(reply.sources)}
        if reply.needs_confirmation is not None:
            body["needs_confirmation"] = reply.needs_confirmation
        if reply.error_code is not None:
            body["error_code"] = reply.error_code
        return JSONResponse(body, status_code=reply.status, headers=headers)

    async def ledger(self, transaction: CreditTransaction, month: str, config: AssistantConfig) -> tuple[Decimal, bool]:
        row = await transaction.row("SELECT * FROM assistant_monthly_spend WHERE month_key=$1", month)
        if row is None:
            raise AssistantStorageError("Assistant monthly ledger was not created")
        spent = Decimal(row["spent_usd"])
        if not spent.is_finite() or spent < 0:
            raise AssistantStorageError("Invalid assistant accounting balance")
        if row["accounting_blocked"]:
            raise AssistantStopped("storage")
        if spent >= config.monthly_budget_usd:
            raise AssistantStopped("budget")
        return spent, bool(row["warned"])

    def check_quota(self, row: dict[str, Any] | None, now: float, config: AssistantConfig) -> tuple[int, int, float]:
        daily, minute, start, daily_retry = quota_state(row, now)
        if daily >= config.daily_limit:
            raise AssistantStopped("daily", 429, daily_retry)
        if minute >= config.per_minute_limit:
            raise AssistantStopped("minute", 429, max(1, math.ceil(start + timedelta(minutes=1).total_seconds() - now)))
        return daily, minute, start

    async def preflight(self, user_id: str, config: AssistantConfig, *, first: bool) -> None:
        now, month = self.clock(), month_key(self.clock())
        try:
            if await self.store.accounting_blocked(month):
                raise AssistantStopped("storage")
            async with self.store.monthly_transaction(month, config.request_timeout_seconds) as transaction:
                if month_key(self.clock()) != month:
                    raise AssistantStopped("budget")
                await self.ledger(transaction, month, config)
            if first:
                row = await self.store.usage(user_id, config.request_timeout_seconds)
                self.check_quota(row, self.clock(), config)
        except (AssistantStorageError, asyncpg.PostgresError, asyncpg.InterfaceError, sqlite3.Error, OSError, InvalidOperation, TimeoutError):
            audit(user_id, now, None, None, warning=True)
            raise AssistantStopped("storage") from None

    async def paid_turn(self, user_id: str, language: Language, config: AssistantConfig, request: LLMRequest, first: bool, transport: AnthropicTransport) -> ModelAnswer:
        now = self.clock()
        month = month_key(now)
        sent = False
        answer: ModelAnswer | None = None
        cost: Decimal | None = None
        estimated = False
        try:
            if await self.store.accounting_blocked(month):
                raise AssistantStopped("storage")
            async with self.store.monthly_transaction(month, config.request_timeout_seconds) as transaction:
                now = self.clock()
                if month_key(now) != month:
                    raise AssistantStopped("budget")
                spent, warned = await self.ledger(transaction, month, config)
                await transaction.execute(
                    """INSERT INTO assistant_usage (user_id,day_key,daily_count,minute_start,minute_count)
                       VALUES ($1,$2,0,$3,0) ON CONFLICT (user_id) DO NOTHING""",
                    user_id, datetime.fromtimestamp(now, timezone.utc).date().isoformat(), now,
                )
                row = await transaction.row(
                    "SELECT * FROM assistant_usage WHERE user_id=$1" + (" FOR UPDATE" if transaction.postgres else ""),
                    user_id,
                )
                now = self.clock()
                if month_key(now) != month:
                    raise AssistantStopped("budget")
                if row is None:
                    raise AssistantStorageError("Assistant user counter was not created")
                if first:
                    daily_count, minute_count, minute_start = self.check_quota(row, now, config)
                    await transaction.execute(
                        """INSERT INTO assistant_usage (user_id,day_key,daily_count,minute_start,minute_count)
                       VALUES ($1,$2,$3,$4,$5) ON CONFLICT (user_id) DO UPDATE SET
                       day_key=excluded.day_key,daily_count=excluded.daily_count,
                       minute_start=excluded.minute_start,minute_count=excluded.minute_count""",
                    user_id, datetime.fromtimestamp(now, timezone.utc).date().isoformat(),
                    daily_count + 1, minute_start, minute_count + 1,
                    )
                sent = True
                try:
                    answer = await asyncio.wait_for(
                        self.provider(config, language, request.payload) if self.provider is not None else transport.generate(request),
                        timeout=config.request_timeout_seconds,
                    )
                except (anthropic.APIError, TimeoutError, json.JSONDecodeError):
                    answer = ModelAnswer("", None, None, failed=True)
                values = (answer.input_tokens, answer.output_tokens, answer.cache_creation_input_tokens, answer.cache_read_input_tokens)
                if any(type(value) is not int or value < 0 for value in values):
                    estimated = True
                    answer = replace(answer, input_tokens=request.estimated_input(), output_tokens=request.max_tokens,
                                     cache_creation_input_tokens=0, cache_read_input_tokens=0)
                input_price = max(config.input_price_usd_per_million, config.cache_write_price_usd_per_million, config.cache_read_price_usd_per_million) if estimated else config.input_price_usd_per_million
                cost = ((answer.input_tokens or 0) * input_price
                        + (answer.output_tokens or 0) * config.output_price_usd_per_million
                        + (answer.cache_creation_input_tokens or 0) * config.cache_write_price_usd_per_million
                        + (answer.cache_read_input_tokens or 0) * config.cache_read_price_usd_per_million) / TOKENS_PER_MILLION
                total = spent + cost
                warn = not warned and total >= config.monthly_budget_usd * config.budget_warning_fraction
                await transaction.execute(
                    "UPDATE assistant_monthly_spend SET spent_usd=$1,warned=$2 WHERE month_key=$3",
                    str(total), int(warned or warn), month,
                )
                await transaction.execute(
                    """INSERT INTO assistant_calls (id,user_id,month_key,called_at,input_tokens,output_tokens,cost_usd,estimated,cache_creation_input_tokens,cache_read_input_tokens)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
                    uuid.uuid4().hex, user_id, month, now, answer.input_tokens, answer.output_tokens, str(cost), estimated,
                    answer.cache_creation_input_tokens, answer.cache_read_input_tokens,
                )
            audit(user_id, now, answer, cost, warning=bool(warn) or estimated, estimated=estimated,
                  sources=tuple(call.name for call in answer.tool_calls if call.name in ARGUMENT_MODELS))
            return answer
        except (AssistantStorageError, asyncpg.PostgresError, asyncpg.InterfaceError, sqlite3.Error, OSError, InvalidOperation, TimeoutError):
            if sent:
                try:
                    await self.store.block_accounting(month, config.request_timeout_seconds)
                except (AssistantStorageError, asyncpg.PostgresError, asyncpg.InterfaceError, sqlite3.Error, OSError, TimeoutError):
                    audit(user_id, now, answer, cost, warning=True)
            audit(user_id, now, answer, cost, warning=True)
            raise AssistantStopped("storage") from None


def service(request: Request) -> AssistantService:
    existing = getattr(request.app.state, "assistant_service", None)
    if existing is None:
        existing = AssistantService(AssistantStore(request.app))
        request.app.state.assistant_service = existing
    return existing


async def shutdown_assistant(application: Any) -> None:
    existing = getattr(application.state, "assistant_service", None)
    if existing is not None:
        await existing.close()


@router.post("/chat")
async def assistant_chat(request: Request) -> JSONResponse:
    user = authenticated_user(request)
    language: Language = "tr"
    try:
        body = await request.json()
        if isinstance(body, dict) and isinstance(body.get("message"), str):
            language = detect_language(body["message"])
        payload = ChatInput.model_validate(body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
        return reply_response(text(language, "Geçersiz mesaj.", "Invalid message."), language, 422)
    try:
        config = load_assistant_config()
    except ValidationError:
        audit(str(user["id"]), time.time(), None, None, warning=True)
        return reply_response(text(language, "Asistan kullanılamıyor.", "The assistant is unavailable."), language, 503)
    if len(payload.message) > config.max_input_chars or len(payload.history) > config.history_messages or len(payload.page_context or "") > config.page_context_max_chars:
        return reply_response(text(
            language,
            f"Mesaj en fazla {config.max_input_chars} karakter, geçmiş en fazla {config.history_messages} mesaj ve sayfa bağlamı en fazla {config.page_context_max_chars} karakter olabilir.",
            f"Message limit: {config.max_input_chars} characters; history limit: {config.history_messages} messages; page context limit: {config.page_context_max_chars} characters.",
        ), language, 422)
    if contains_secret(payload, config):
        return reply_response(text(
            language,
            "Secret'ı sohbete yazma, değiştirmeni öneririm.",
            "Do not share a secret in chat. I recommend rotating it.",
        ), language)
    for entry in payload.history:
        entry.content = entry.content[:config.history_message_max_chars]
    if config.enabled:
        assistant = service(request)
        tools = AssistantTools(request, config=config, clock=assistant.clock)
        try:
            quick = await fastpath_answer(tools, str(user["id"]), payload.message, language,
                                          usage_reader=lambda: usage_snapshot(assistant, str(user["id"]), config))
        except (HTTPException, asyncpg.PostgresError, asyncpg.InterfaceError, sqlite3.Error, OSError, TimeoutError):
            audit(str(user["id"]), assistant.clock(), None, None, warning=True)
            return reply_response(text(language, "Asistan bilgisi alınamadı.", "Assistant information could not be fetched."), language, 503)
        if quick is not None:
            audit(str(user["id"]), assistant.clock(), None, Decimal(0), sources=tuple(quick["sources"]))
            return JSONResponse(quick)
    if not config.available:
        return reply_response(text(language, "Asistan kullanılamıyor.", "The assistant is unavailable."), language, 503)
    return await service(request).start(str(user["id"]), language, payload, config, tools)


async def tool_request(request: Request, name: str, *, confirmation: bool = False) -> JSONResponse:
    user = authenticated_user(request)
    user_id = str(user["id"])
    if name not in ARGUMENT_MODELS:
        audit(user_id, time.time(), None, None, warning=True)
        return JSONResponse({"detail": "Unknown assistant read tool"}, status_code=404)
    try:
        config = load_assistant_config()
    except ValidationError:
        audit(user_id, time.time(), None, None, warning=True)
        return JSONResponse({"detail": "Assistant configuration is unavailable"}, status_code=503)
    if not config.enabled:
        return JSONResponse({"detail": "Assistant is disabled"}, status_code=503)
    tools = AssistantTools(request, config=config, clock=service(request).clock)
    try:
        body = await request.json()
        if confirmation:
            result = await tools.confirm_analysis(user_id, ConfirmAnalysis.model_validate(body))
        else:
            if not isinstance(body, dict):
                raise HTTPException(422, "Tool arguments must be an object")
            result = await tools.dispatch(user_id, name, body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
        audit(user_id, tools.clock(), None, None, warning=True, sources=(name,))
        return JSONResponse({"detail": "Invalid assistant tool arguments"}, status_code=422)
    except HTTPException as exc:
        audit(user_id, tools.clock(), None, None, warning=True, sources=(name,))
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    except (asyncpg.PostgresError, asyncpg.InterfaceError, sqlite3.Error, OSError, TimeoutError):
        audit(user_id, tools.clock(), None, None, warning=True, sources=(name,))
        return JSONResponse({"detail": "Assistant tool data is unavailable"}, status_code=503)
    audit(user_id, tools.clock(), None, Decimal(0), sources=(name,))
    return JSONResponse({**result, "sources": [name]})


@router.post("/tools/{name}")
async def assistant_read_tool(name: str, request: Request) -> JSONResponse:
    return await tool_request(request, name)


@router.post("/analysis/confirm")
async def assistant_confirm_analysis(request: Request) -> JSONResponse:
    return await tool_request(request, "get_analysis", confirmation=True)


async def usage_snapshot(assistant: AssistantService, user_id: str, config: AssistantConfig) -> dict[str, Any]:
    now = assistant.clock()
    row = await assistant.store.usage(user_id, config.request_timeout_seconds)
    daily_count, _, _, _ = quota_state(row, now)
    current = datetime.fromtimestamp(now, timezone.utc)
    return {
        "remaining": max(0, config.daily_limit - daily_count),
        "total": config.daily_limit,
        "limits": {
            "max_input_chars": config.max_input_chars,
            "history_messages": config.history_messages,
            "history_message_max_chars": config.history_message_max_chars,
            "page_context_max_chars": config.page_context_max_chars,
            "secret_min_alphanumeric_chars": config.secret_min_alphanumeric_chars,
        },
        "resetsAt": (current.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).isoformat(),
    }


@router.get("/usage")
async def assistant_usage(request: Request) -> JSONResponse:
    user = authenticated_user(request)
    try:
        config = load_assistant_config()
        assistant = service(request)
        return JSONResponse(await usage_snapshot(assistant, str(user["id"]), config))
    except (ValidationError, AssistantStorageError, asyncpg.PostgresError, asyncpg.InterfaceError, sqlite3.Error, OSError, TimeoutError):
        audit(str(user["id"]), time.time(), None, None, warning=True)
        return JSONResponse({"detail": "Asistan kullanım bilgisi alınamadı."}, status_code=503)


async def proactive_request(request: Request, operation: str) -> JSONResponse:
    user_id = str(authenticated_user(request)["id"])
    try:
        config = load_assistant_config()
        assistant = service(request)
        tools = AssistantTools(request, config=config, clock=assistant.clock)
        proactive = ProactiveAssistant(assistant.store, tools, config)
        if operation == "read":
            result = await proactive.preference(user_id)
        else:
            try:
                body = await request.json()
                payload = PreferenceInput.model_validate(body) if operation == "save" else CheckInInput.model_validate(body)
            except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
                audit(user_id, assistant.clock(), None, None, warning=True)
                return JSONResponse({"detail": "Invalid assistant preference or check-in request"}, status_code=422)
            async with asyncio.timeout(config.request_timeout_seconds):
                result = await proactive.preference(user_id, payload.enabled) if isinstance(payload, PreferenceInput) else await proactive.check_in(user_id, payload.language)
        message = result.get("message")
        audit(user_id, assistant.clock(), None, Decimal(0), sources=tuple(message["sources"]) if message else ())
        return JSONResponse(result)
    except (ValidationError, AssistantStorageError, asyncpg.PostgresError, asyncpg.InterfaceError, sqlite3.Error, OSError, TimeoutError):
        audit(user_id, time.time(), None, None, warning=True)
        return JSONResponse({"detail": "Assistant check-in or preference is unavailable"}, status_code=503)


@router.get("/proactive/preferences")
async def assistant_proactive_preferences(request: Request) -> JSONResponse:
    return await proactive_request(request, "read")


@router.post("/proactive/preferences")
async def assistant_proactive_save_preferences(request: Request) -> JSONResponse:
    return await proactive_request(request, "save")


@router.post("/proactive/check-in")
async def assistant_proactive_check_in(request: Request) -> JSONResponse:
    return await proactive_request(request, "check")
