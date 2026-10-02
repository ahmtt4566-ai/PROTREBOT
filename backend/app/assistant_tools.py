"""Identity-bound assistant reads; confirmed analysis purchases are separate."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from . import assistant_help, subscription_core, v25_execution
from .analyst_credits import AnalystCredits, ConsumeAnalysis, CreditTransaction
from .analyst_credits import service as credit_service
from .assistant_config import AssistantConfig, load_assistant_config
from .exchange_connections import session_id
from .premium_access import public_projection
from .subscription_core import parse_datetime
from .v22_commercial import access_snapshot, authenticated_user, subscription_for_user

Language = Literal["tr", "en"]


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AnalysisArguments(NoArguments):
    symbol: str = ConsumeAnalysis.model_fields["symbol"]
    timeframe: str = ConsumeAnalysis.model_fields["timeframe"]


class HelpArguments(NoArguments):
    query: str = Field(min_length=1)
    language: Language = "tr"


class ProtectionArguments(NoArguments):
    symbol: str = ConsumeAnalysis.model_fields["symbol"]
    direction: Literal["LONG", "SHORT"] | None = None
    language: Language = "tr"


class ConfirmAnalysis(AnalysisArguments):
    confirm: StrictBool
    confirmation_token: str = Field(min_length=1)


ARGUMENT_MODELS: dict[str, type[BaseModel]] = {
    "get_plans": NoArguments,
    "get_my_access": NoArguments,
    "get_my_credits": NoArguments,
    "search_help": HelpArguments,
    "get_analysis": AnalysisArguments,
    "get_my_positions": NoArguments,
    "get_protection_status": ProtectionArguments,
}
TOOL_DEFINITIONS = [
    {"name": name, "input_schema": model.model_json_schema()}
    for name, model in ARGUMENT_MODELS.items()
]


class AssistantTools:
    def __init__(self, request: Request, *, config: AssistantConfig | None = None, clock: Callable[[], float] = time.time):
        self.request = request
        self.config = config or load_assistant_config()
        self.clock = clock

    def user(self, user_id: str) -> dict[str, Any]:
        user = authenticated_user(self.request)
        if str(user["id"]) != user_id:
            raise HTTPException(403, "Assistant identity does not match the authenticated member")
        return user

    def premium(self, user_id: str) -> bool:
        return bool(access_snapshot(self.request.app.state.v22_commercial["state"], self.user(user_id))["isPremium"])

    def credits(self) -> AnalystCredits:
        try:
            return credit_service(self.request)
        except ValueError:
            raise HTTPException(503, "Analyst credit configuration is unavailable") from None

    def result(self, data: dict[str, Any], *, stale: bool = False) -> dict[str, Any]:
        return {"data": data, "fetched_at": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(), "stale": stale}

    async def get_plans(self, user_id: str) -> dict[str, Any]:
        self.user(user_id)
        return self.result({
            "plans": [
                {"id": key, "name": row["name"], "monthly_price": row["monthly_price"], "currency": "USD"}
                for key, row in subscription_core.PLAN_CATALOG.items()
            ],
            "trial_days": subscription_core.TRIAL_DAYS,
            "cancellation": dict(subscription_core.CANCELLATION_RULES),
        })

    async def get_my_access(self, user_id: str) -> dict[str, Any]:
        user = self.user(user_id)
        state = self.request.app.state.v22_commercial["state"]
        access = access_snapshot(state, user)
        subscription = subscription_for_user(state, user_id)
        return self.result({"plan": subscription["plan"], "status": subscription["status"], "isPremium": bool(access["isPremium"])})

    async def get_my_credits(self, user_id: str) -> dict[str, Any]:
        premium = self.premium(user_id)
        credits = self.credits()
        snapshot = await credits.credits(user_id, premium)
        reset = parse_datetime(snapshot["resetsAt"])
        return self.result({
            **snapshot,
            "resets_in_seconds": max(0, math.ceil(reset.timestamp() - credits.clock())) if reset else None,
            "window_hours": credits.config.window_hours, "cache_minutes": credits.config.cache_minutes,
        })

    async def search_help(self, user_id: str, query: str, language: Language = "tr") -> dict[str, Any]:
        self.user(user_id)
        if len(query) > self.config.max_input_chars:
            raise HTTPException(422, "Help query exceeds the configured message limit")
        try:
            results = assistant_help.search(query, language, self.credits().config)
        except assistant_help.KnowledgeBaseError:
            raise HTTPException(503, "Help knowledge base is unavailable") from None
        return self.result({"results": results})

    def confirmation_token(self, user_id: str, symbol: str, timeframe: str, cost: int) -> str:
        self.user(user_id)
        credits = self.credits()
        claims = json.dumps({
            "symbol": symbol, "timeframe": timeframe, "cost": cost, "nonce": uuid.uuid4().hex,
            "expires_at": self.clock() + credits.config.cache_minutes * 60,
        }, sort_keys=True, separators=(",", ":"))
        encoded = base64.urlsafe_b64encode(claims.encode()).decode()
        secret = self.request.app.state.v22_commercial["secret"]
        signature = hmac.new(secret, f"assistant-analysis:{user_id}:{encoded}".encode(), hashlib.sha256).hexdigest()
        return f"{encoded}.{signature}"

    def verify_confirmation(self, user_id: str, payload: ConfirmAnalysis) -> str:
        if not payload.confirm:
            raise HTTPException(422, "Explicit analysis confirmation is required")
        try:
            encoded, signature = payload.confirmation_token.split(".")
            secret = self.request.app.state.v22_commercial["secret"]
            expected = hmac.new(secret, f"assistant-analysis:{user_id}:{encoded}".encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise HTTPException(403, "Invalid analysis confirmation")
            claims = json.loads(base64.urlsafe_b64decode(encoded))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(403, "Invalid analysis confirmation") from None
        credits = self.credits()
        cost = 0 if self.premium(user_id) else credits.config.cost
        if (
            claims["symbol"] != payload.symbol or claims["timeframe"] != payload.timeframe
            or claims["expires_at"] <= self.clock() or claims["cost"] != cost
        ):
            raise HTTPException(409, "Analysis confirmation expired or its target/cost changed")
        return hashlib.sha256(payload.confirmation_token.encode()).hexdigest()

    def analysis_summary(self, raw: dict[str, Any], user_id: str, symbol: str, timeframe: str, *, cached: bool) -> dict[str, Any]:
        timestamp = parse_datetime(raw.get("updated_at") or raw.get("timestamp"))
        delta = self.clock() - timestamp.timestamp() if timestamp else None
        age = math.ceil(delta) if delta is not None and delta >= 0 else None
        data = {
            "symbol": symbol, "timeframe": timeframe, "cached": cached,
            "direction": raw.get("direction"),
            "final_decision_score": raw.get("final_decision_score"),
            "confidence": raw.get("confidence"), "opportunity_score": raw.get("opportunity_score"),
            "mtf_alignment": raw.get("mtf_alignment"), "data_age_seconds": age,
            "data_age_reason": "ANALYSIS_TIMESTAMP_UNAVAILABLE" if age is None else None,
        }
        if not self.premium(user_id):
            data = public_projection(data)
        return self.result(data, stale=age is None or age >= self.credits().config.cache_minutes * 60)

    async def get_analysis(self, user_id: str, symbol: str, timeframe: str) -> dict[str, Any]:
        premium = self.premium(user_id)
        AnalysisArguments(symbol=symbol, timeframe=timeframe)
        credits = self.credits()
        cached = await credits.cached_analysis(user_id, symbol, timeframe)
        if cached is not None:
            return self.analysis_summary(cached["result"], user_id, symbol, timeframe, cached=True)
        cost = 0 if premium else credits.config.cost
        return self.result({"needs_confirmation": {
            "action": "get_analysis", "symbol": symbol, "timeframe": timeframe,
            "cost": cost, "analysis_cost": credits.config.cost,
            "confirmation_token": self.confirmation_token(user_id, symbol, timeframe, cost),
        }})

    async def confirm_analysis(self, user_id: str, payload: ConfirmAnalysis) -> dict[str, Any]:
        self.user(user_id)
        key = self.verify_confirmation(user_id, payload)
        producer = self.request.app.state.analyst_analysis
        response, status = await self.credits().consume(
            user_id, self.premium(user_id), payload.symbol, payload.timeframe, key,
            lambda: producer(payload.symbol, payload.timeframe),
        )
        if status != 200:
            raise HTTPException(status, response)
        return self.analysis_summary(response["result"], user_id, payload.symbol, payload.timeframe, cached=response["cached"])

    async def account_state(self, user_id: str) -> dict[str, Any] | None:
        self.user(user_id)
        state = getattr(self.request.app.state, "v25_execution", None)
        if not isinstance(state, dict) or not isinstance(state.get("lock"), asyncio.Lock):
            return None
        lock = state["lock"]
        await asyncio.wait_for(lock.acquire(), timeout=self.config.request_timeout_seconds)
        try:
            return v25_execution.read_owned_account_state(state, user_id, session_id(self.request))
        finally:
            lock.release()

    def account_age(self, state: dict[str, Any] | None) -> tuple[int | None, bool]:
        connection = (state or {}).get("connection")
        checked = parse_datetime(connection.get("last_checked")) if isinstance(connection, dict) else None
        delta = self.clock() - checked.timestamp() if checked else None
        threshold = max(self.config.protection_stale_seconds, 2 * v25_execution.RECONCILE_SECONDS)
        return (math.ceil(delta) if delta is not None and delta >= 0 else None,
                delta is None or delta < 0 or delta > threshold)

    async def get_my_positions(self, user_id: str) -> dict[str, Any]:
        state = await self.account_state(user_id)
        age, stale = self.account_age(state)
        if state is None:
            return self.result({"positions": [], "reason": "ACCOUNT_NOT_VERIFIED", "data_age_seconds": None}, stale=True)
        stale = stale or not state.get("connected") or bool(state["connection"].get("last_error")) or not state.get("recovery_ready")
        positions = [
            {key: row.get(key) for key in ("symbol", "direction", "quantity", "unrealized_pnl")}
            for row in state["snapshot"].get("positions", [])
            if row.get("quantity")
        ]
        if not self.premium(user_id):
            positions = public_projection(positions)
        return self.result({"positions": positions, "data_age_seconds": age}, stale=stale)

    async def get_my_activity(self, user_id: str, transaction: CreditTransaction) -> dict[str, Any]:
        # Internal check-in read only; not exposed as an LLM tool.
        self.user(user_id)
        row = await transaction.row("SELECT last_seen_at FROM assistant_proactive_state WHERE user_id=$1", user_id)
        seen = row["last_seen_at"] if row else None
        elapsed = self.clock() - seen if seen is not None else None
        valid = elapsed is not None and math.isfinite(elapsed) and elapsed >= 0
        return self.result({"inactive_seconds": math.floor(elapsed) if valid else None}, stale=not valid)

    async def get_protection_status(self, user_id: str, symbol: str, direction: str | None = None, language: Language = "tr") -> dict[str, Any]:
        state = await self.account_state(user_id)
        age, stale = self.account_age(state)
        reason = "ACCOUNT_NOT_VERIFIED"
        verified, protected = False, None
        protection_state = "UNKNOWN"
        if state is not None:
            snapshot = state["snapshot"]
            positions = [
                row for row in snapshot.get("positions", [])
                if row.get("symbol") == symbol and (direction is None or row.get("direction") == direction)
            ]
            plans = [
                row for row in (state.get("plans") or {}).values()
                if len(positions) == 1 and row.get("symbol") == symbol and row.get("direction") == positions[0].get("direction")
                and row.get("status") not in {"KAPANDI", "İPTAL"}
            ]
            if stale:
                reason = "STALE_SNAPSHOT"
            elif not state.get("connected") or not state.get("recovery_ready") or state.get("recovery_error") or state.get("reconciliation_required") or state["connection"].get("last_error"):
                reason = "RECONCILIATION_UNVERIFIED"
            elif snapshot.get("open_algo_orders_available") is not True or not isinstance(snapshot.get("open_algo_orders"), list) or not all(isinstance(row, dict) for row in snapshot["open_algo_orders"]):
                reason = "PROTECTION_SNAPSHOT_UNAVAILABLE"
            elif len(positions) != 1 or len(plans) != 1:
                reason = "POSITION_OR_PLAN_AMBIGUOUS"
            else:
                plan = plans[0]
                classified, _, match_reason = v25_execution.classify_plan_protection(plan, snapshot.get("open_algo_orders", []))
                protection_state = plan.get("protection_state", "UNKNOWN")
                verified = classified == protection_state and classified in {"MATCHED", "MISSING"}
                if classified == "MATCHED":
                    verified = verified and plan.get("protection_match_confidence") == "EXACT" and match_reason == "EXACT_IDENTITY"
                reason = match_reason if verified else "PROTECTION_NOT_VERIFIED"
                protected = classified == "MATCHED" if verified else None
        if not verified:
            protection_state = "UNKNOWN"
        age_text = (
            f"Veri {age} saniye önce alındı." if language == "tr" else f"Data was fetched {age} seconds ago."
        ) if age is not None else ("Veri yaşı doğrulanamadı." if language == "tr" else "Data age could not be verified.")
        status_text = (
            ("Koruma doğrulandı." if protected else "Doğrulanmış stop bulunmuyor.") if language == "tr"
            else ("Protection verified." if protected else "No verified stop was found.")
        ) if verified else ("Koruma doğrulanamadı." if language == "tr" else "Protection could not be verified.")
        data = {"symbol": symbol, "verified": verified, "protected": protected, "protection_state": protection_state,
                "verification_reason": reason, "data_age_seconds": age, "message": f"{status_text} {age_text}"}
        if not self.premium(user_id):
            data = public_projection(data)
        return self.result(data, stale=stale)

    async def dispatch(self, user_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.user(user_id)
        model = ARGUMENT_MODELS.get(name)
        if model is None:
            raise HTTPException(404, "Unknown assistant read tool")
        parsed = model.model_validate(arguments)
        return await getattr(self, name)(user_id, **parsed.model_dump())
