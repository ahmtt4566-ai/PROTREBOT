"""Deterministic member information answers without a model or message quota."""
from __future__ import annotations

import re
import logging
import sqlite3
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import asyncpg
from fastapi import HTTPException

from .assistant_prompt import IDENTITY_REPLIES
from .assistant_storage import AssistantStorageError
from .assistant_llm import summary
from .assistant_tools import AssistantTools
from .assistant_prompt import RISK_NOTES
from .assistant_response import safe_response

Language = Literal["tr", "en"]
logger = logging.getLogger(__name__)


def words_of(message: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", message.casefold().replace("ı", "i"))
    normalized = "".join(character for character in normalized if not unicodedata.combining(character))
    return set(re.findall(r"\w+", normalized))


def analysis_targets(message: str) -> tuple[list[str], str]:
    words = words_of(message)
    aliases = {"bitcoin": "BTC", "btc": "BTC", "eth": "ETH", "ethereum": "ETH", "sol": "SOL",
               "bnb": "BNB", "xrp": "XRP", "ada": "ADA", "doge": "DOGE", "avax": "AVAX", "dot": "DOT", "link": "LINK"}
    symbols = sorted({(aliases[word] + "USDT") if word in aliases else word.upper()
                      for word in words if word in aliases or re.fullmatch(r"[a-z0-9]{2,20}usdt", word)})
    intervals = sorted(words & {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"})
    return symbols, intervals[0] if len(intervals) == 1 else "15m" if not intervals else ""


def detect_language(message: str) -> Language:
    words = words_of(message)
    turkish = {"merhaba", "selam", "nasil", "nasıl", "nedir", "neden", "ben", "bana", "icin", "kredi", "kredim", "krediler", "kredilerim", "hesap", "yardim", "planim", "aboneligim", "benim", "fiyat", "ucret", "deneme", "abonelik", "kalan", "ne",
               "sistem", "yaz", "göster", "goster", "kaç", "kac", "var", "mı", "mi", "sen", "kimsin"}
    return "tr" if re.search("[çğıöşüÇĞİÖŞÜ]", message) or words & turkish or any(
        word.startswith(("anahtar", "uyelik", "gunluk", "kullanim", "mesaj", "dakikalik", "analiz", "durum", "sonuc"))
        for word in words
    ) else "en"


def intent(message: str) -> str | None:
    words = words_of(message)
    normalized = " ".join(re.findall(r"\w+|[?!.]", unicodedata.normalize("NFKD", message.casefold().translate(str.maketrans("çğıöşü", "cgiosu"))).replace("\u0307", "")))
    if words & {"trade", "order", "execute", "arm", "consent", "emir", "islem", "buy", "sell"}:
        return None
    if re.fullmatch(r"(?:sen kimsin|kimsin|adin ne|senin adin ne|who are you|what is your name)(?:\s*[?!.])*", normalized.strip()):
        return "identity"
    if any(word.startswith(("analiz", "analysis")) for word in words) and any(
        word.startswith(("durum", "sonuc", "status", "result", "available", "mevcut")) or word == "var" for word in words
    ) and analysis_targets(message)[0]:
        return "get_analysis"
    if words & {"bitcoin", "btc", "eth", "pricefeed"}:
        return None
    if (words & {"gunluk", "daily", "dakikalik"} and any(
        word.startswith(("limit", "kullanim", "mesaj", "usage", "message", "allowance")) for word in words
    )) or (any(word.startswith(("mesaj", "message")) for word in words) and
           any(word.startswith(("hak", "kalan", "remaining", "limit")) for word in words)):
        return "usage"
    if any(word == "api" or word.startswith("apikey") for word in words) and any(
        word.startswith(("anahtar", "key", "apikey", "credential")) for word in words
    ) and any(word.startswith(("gir", "ekle", "kaydet", "nasil", "nere", "how", "where", "enter", "input", "setup", "configure", "add")) for word in words):
        return "search_help"
    if not words & {"price", "cost", "fee", "much", "fiyat", "ucret"} and ("planim" in words or "aboneligim" in words
        or ("my" in words and words & {"plan", "subscription", "access", "membership"})
        or ("benim" in words and words & {"plan", "abonelik"})):
        return "get_my_access"
    if words & {"credit", "credits", "kredi", "kredim", "krediler", "kredilerim"}:
        return "get_my_credits"
    if words & {"trial", "deneme", "fiyat", "ucret", "fiyati", "plans", "pricing"}:
        return "get_plans"
    if any(word.startswith(("plan", "subscription", "abonelik", "premium", "membership", "uyelik")) for word in words) and (
        any(word.startswith(("price", "cost", "fee", "much", "fiyat", "ucret")) for word in words) or words & {"kadar", "kac", "para"}
    ):
        return "get_plans"
    if words & {"price", "cost", "much"} and words <= {"what", "is", "the", "price", "how", "much", "does", "it", "cost"}:
        return "get_plans"
    return None


async def answer(
    tools: AssistantTools, user_id: str, message: str, language: Language,
    usage_reader: Callable[[], Awaitable[dict[str, Any]]] | None = None,
) -> dict | None:
    name = intent(message)
    if name is None:
        return None
    if name == "identity":
        return {"reply": IDENTITY_REPLIES[language], "language": language, "sources": []}
    if name == "usage":
        try:
            if usage_reader is None:
                raise AssistantStorageError("Usage reader unavailable")
            data = await usage_reader()
        except (AssistantStorageError, asyncpg.PostgresError, asyncpg.InterfaceError, sqlite3.Error, OSError, TimeoutError):
            logger.warning("assistant_fastpath_usage_unavailable user_id=%s", user_id)
            return {"reply": "Kullanım bilgini şu an alamadım; ekrandaki günlük mesaj hakkı sayacını kontrol et." if language == "tr" else
                    "I could not fetch your usage right now; check the daily message counter on screen.",
                    "language": language, "sources": []}
        used = data["total"] - data["remaining"]
        return {"reply": (f"Bugün kullanılan: {used}. Kalan: {data['remaining']}. Günlük limit: {data['total']} mesaj. "
                          f"Dakikalık limit: {tools.config.per_minute_limit} mesaj.") if language == "tr" else
                         (f"Used today: {used}. Remaining: {data['remaining']}. Daily limit: {data['total']} messages. "
                          f"Per-minute limit: {tools.config.per_minute_limit} messages."),
                "language": language, "sources": ["usage"]}
    if name == "search_help":
        result = await tools.dispatch(user_id, name, {"query": "API connection demo save verify" if language == "en" else
                                                    "API bağlantı Demo kaydet doğrula", "language": language})
        article = next((row for row in result["data"]["results"] if row["id"] == "api-connection"), None)
        if article is None:
            raise HTTPException(503, "API connection help unavailable")
        return {"reply": article["content"], "language": language, "sources": [name],
                "fetched_at": result["fetched_at"], "stale": result["stale"]}
    if name == "get_analysis":
        symbols, timeframe = analysis_targets(message)
        if len(symbols) != 1 or not timeframe:
            return {"reply": "Tek bir sembol ve zaman dilimi belirt." if language == "tr" else
                    "Please specify one symbol and timeframe.", "language": language, "sources": []}
        try:
            result = await tools.dispatch(user_id, name, {"symbol": symbols[0], "timeframe": timeframe})
        except (HTTPException, asyncpg.PostgresError, asyncpg.InterfaceError, sqlite3.Error, OSError, TimeoutError):
            logger.warning("assistant_fastpath_analysis_unavailable user_id=%s", user_id)
            result = None
        if result is None or "needs_confirmation" in result["data"] or "direction" not in result["data"]:
            return {"reply": "Analiz bulunamadı, istersen yeni analiz isteyebilirsin." if language == "tr" else
                    "No analysis was found; you can request a new analysis if you wish.",
                    "language": language, "sources": [name]}
        reply = safe_response(summary(name, result, language), language, free=not tools.premium(user_id)) + "\n\n" + RISK_NOTES[language]
        if result["stale"]:
            reply = ("Bayat/doğrulanmamış veri. " if language == "tr" else "Stale/unverified data. ") + reply
        return {"reply": reply, "language": language, "sources": [name],
                "fetched_at": result["fetched_at"], "stale": result["stale"]}
    result = await tools.dispatch(user_id, name, {})
    data = result["data"]
    if name == "get_plans":
        plan = next(row for row in data["plans"] if row["id"] == "MASTER_MODE")
        price = f"{plan['monthly_price']:.2f}"
        if language == "tr":
            reply = f"{plan['name']}: {price.replace('.', ',')} {plan['currency']}/ay. Deneme süresi {data['trial_days']} gün."
        else:
            reply = f"{plan['name']}: {price} {plan['currency']}/month. Trial: {data['trial_days']} days."
        cancellation = data["cancellation"]
        if cancellation["cancel_anytime"]:
            reply += " İstediğin zaman iptal edebilirsin." if language == "tr" else " You can cancel anytime."
        if cancellation["default_effective"] == "period_end":
            reply += " Varsayılan iptal dönem sonunda gerçekleşir." if language == "tr" else " Cancellation defaults to the end of the billing period."
        if cancellation["immediate_cancellation_supported"]:
            reply += " Anında iptal seçeneği de vardır." if language == "tr" else " Immediate cancellation is also supported."
    elif name == "get_my_access":
        plan = data["plan"] or ("Ücretsiz" if language == "tr" else "Free")
        premium = ("evet" if data["isPremium"] else "hayır") if language == "tr" else ("yes" if data["isPremium"] else "no")
        reply = (f"Planın: {plan}. Durum: {data['status']}. Premium: {premium}." if language == "tr"
                 else f"Your plan: {plan}. Status: {data['status']}. Premium: {premium}.")
    elif data["unlimited"]:
        reply = "Premium üyeliğinde analiz sınırsızdır; kredi düşmez." if language == "tr" else "Premium analysis is unlimited; no credits are spent."
    else:
        reply = (
            f"Kalan kredi: {data['remaining']}/{data['total']}. Taze analiz {data['analysis_cost']} kredi. "
            f"Pencere ilk harcamada başlar ve {data['window_hours']} saat sürer. "
            f"Aynı analiz {data['cache_minutes']} dakika içinde cache'ten ücretsizdir; hata durumunda kredi bir kez iade edilir."
        ) if language == "tr" else (
            f"Credits remaining: {data['remaining']}/{data['total']}. Fresh analysis costs {data['analysis_cost']} credits. "
            f"The window starts on the first spend and lasts {data['window_hours']} hours. "
            f"The same cached analysis is free within {data['cache_minutes']} minutes; failed analysis is refunded once."
        )
        if data["resets_in_seconds"] is not None:
            remaining = data["resets_in_seconds"]
            reply += (f" Yenilenmeye {remaining} saniye kaldı." if language == "tr"
                      else f" Resets in {remaining} seconds.")
    return {"reply": reply, "language": language, "sources": [name],
            "fetched_at": result["fetched_at"], "stale": result["stale"]}
