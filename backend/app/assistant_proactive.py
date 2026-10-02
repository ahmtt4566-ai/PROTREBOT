"""Zero-LLM, identity-bound in-app status check-ins."""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictBool

from .assistant_config import AssistantConfig
from .assistant_models import Language
from .assistant_storage import AssistantStorageError, AssistantStore
from .assistant_tools import AssistantTools

COPY = {
    "tr": {
        "positions": "{count} açık pozisyon. PnL: {pnl}.",
        "protected": "Koruma doğrulandı.",
        "unverified": "Koruma doğrulanamadı. Pozisyonlar ekranından kontrol et.",
        "missing": "Doğrulanmış stop bulunmayan pozisyon var. Koruma kapsamı doğrulanamadı; Pozisyonlar ekranından kontrol et.",
        "age": "Veri {seconds} saniye önce alındı.",
        "age_unknown": "Veri yaşı doğrulanamadı.",
        "stale": "Veri bayat veya güncelliği doğrulanamadı.",
        "unknown": "doğrulanamadı",
        "returning": "Tekrar hoş geldin. Uzun bir aranın ardından platform durumunu ilgili ekranlardan kontrol edebilirsin.",
    },
    "en": {
        "positions": "{count} open positions. PnL: {pnl}.",
        "protected": "Protection verified.",
        "unverified": "Protection could not be verified. Check the Positions screen.",
        "missing": "A position has no verified stop. Protection coverage could not be verified; check the Positions screen.",
        "age": "Data was fetched {seconds} seconds ago.",
        "age_unknown": "Data age could not be verified.",
        "stale": "Data is stale or its freshness could not be verified.",
        "unknown": "could not be verified",
        "returning": "Welcome back. After an extended absence, you can check platform status on the relevant screens.",
    },
}


class CheckInInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    language: Language = "tr"


class PreferenceInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: StrictBool


def pnl_text(positions: list[dict[str, Any]], language: Language) -> str:
    try:
        total = Decimal(0)
        for position in positions:
            value = position.get("unrealized_pnl")
            if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
                return COPY[language]["unknown"]
            amount = Decimal(str(value))
            if not amount.is_finite():
                return COPY[language]["unknown"]
            total += amount
        rendered = format(total.quantize(Decimal("0.01")), ",.2f")
        return rendered.translate(str.maketrans(".,", ",.")) if language == "tr" else rendered
    except InvalidOperation:
        return COPY[language]["unknown"]


async def status_message(tools: AssistantTools, user_id: str, language: Language, positions: dict[str, Any]) -> dict[str, Any] | None:
    rows = positions["data"]["positions"]
    if not rows:
        return None
    copy = COPY[language]
    statuses = []
    for position in rows:
        if not position.get("symbol") or position.get("direction") not in {"LONG", "SHORT"}:
            statuses.append({"data": {"verified": False}, "stale": True})
        else:
            statuses.append(await tools.get_protection_status(user_id, position["symbol"], position["direction"], language))
    stale = positions["stale"] or any(row["stale"] for row in statuses)
    verified = not stale and all(row["data"].get("verified") is True for row in statuses)
    protected = verified and all(row["data"].get("protected") is True for row in statuses)
    protection = copy["protected"] if protected else copy["missing"] if verified else copy["unverified"]
    ages = [result["data"].get("data_age_seconds") for result in [positions, *statuses]]
    known_age = all(isinstance(age, int) and not isinstance(age, bool) and age >= 0 for age in ages)
    age = copy["age"].format(seconds=max(ages)) if known_age else copy["age_unknown"]
    return {
        "reply": "\n".join([
            copy["positions"].format(count=len(rows), pnl=pnl_text(rows, language)),
            protection, age, *([copy["stale"]] if stale else []),
        ]),
        "language": language,
        "sources": ["get_my_positions", "get_protection_status"],
        "fetched_at": positions["fetched_at"],
        "stale": stale,
    }


class ProactiveAssistant:
    def __init__(self, store: AssistantStore, tools: AssistantTools, config: AssistantConfig):
        self.store, self.tools, self.config = store, tools, config

    def preferences(self, enabled: bool) -> dict[str, Any]:
        return {
            "enabled": enabled,
            "available": self.config.enabled and self.config.proactive_enabled,
            "poll_interval_seconds": self.config.proactive_poll_seconds,
        }

    async def preference(self, user_id: str, enabled: bool | None = None) -> dict[str, Any]:
        self.tools.user(user_id)
        async with self.store.proactive_transaction(user_id, self.config.request_timeout_seconds) as transaction:
            if enabled is not None:
                await transaction.execute("UPDATE assistant_proactive_state SET enabled=$1, last_seen_at=$2 WHERE user_id=$3", enabled, self.tools.clock(), user_id)
            row = await transaction.row("SELECT enabled FROM assistant_proactive_state WHERE user_id=$1", user_id)
            if row is None:
                raise AssistantStorageError("Assistant preference was not created")
            return self.preferences(bool(row["enabled"]))

    async def check_in(self, user_id: str, language: Language) -> dict[str, Any]:
        self.tools.user(user_id)
        async with self.store.proactive_transaction(user_id, self.config.request_timeout_seconds) as transaction:
            row = await transaction.row("SELECT * FROM assistant_proactive_state WHERE user_id=$1", user_id)
            if row is None:
                raise AssistantStorageError("Assistant check-in state was not created")
            response = {**self.preferences(bool(row["enabled"])), "message": None}
            if not response["enabled"] or not response["available"]:
                return response
            now = self.tools.clock()
            activity = await self.tools.get_my_activity(user_id, transaction)
            await transaction.execute("UPDATE assistant_proactive_state SET last_seen_at=$1 WHERE user_id=$2", now, user_id)
            last = row["last_checkin_at"]
            cooldown = timedelta(hours=self.config.proactive_cooldown_hours).total_seconds()
            if last is not None and now - last < cooldown:
                return response
            positions = await self.tools.get_my_positions(user_id)
            message = await status_message(self.tools, user_id, language, positions)
            inactive = activity["data"]["inactive_seconds"]
            if message is None and not activity["stale"] and inactive >= timedelta(days=self.config.proactive_inactive_days).total_seconds():
                copy = COPY[language]
                message = {
                    "reply": "\n".join([copy["returning"], *([copy["stale"]] if positions["stale"] else [])]),
                    "language": language, "sources": ["get_my_activity", "get_my_positions"],
                    "fetched_at": activity["fetched_at"], "stale": positions["stale"],
                }
            if message is not None:
                # Claim before responding: concurrent tabs/retries cannot deliver another check-in.
                await transaction.execute("UPDATE assistant_proactive_state SET last_checkin_at=$1 WHERE user_id=$2", now, user_id)
                response["message"] = {"id": str(uuid.uuid4()), **message}
            return response
