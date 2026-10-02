"""Deterministic member information answers without a model or message quota."""
from __future__ import annotations

import re
from typing import Literal

from .assistant_tools import AssistantTools

Language = Literal["tr", "en"]


def detect_language(message: str) -> Language:
    words = set(re.findall(r"\w+", message.casefold()))
    turkish = {"merhaba", "selam", "nasil", "nasıl", "nedir", "neden", "ben", "bana", "icin", "kredi", "kredim", "krediler", "kredilerim", "hesap", "yardim", "planim", "aboneligim", "benim", "fiyat", "ucret", "deneme", "abonelik", "kalan", "ne",
               "sistem", "yaz", "göster", "goster", "kaç", "kac", "var", "mı", "mi"}
    return "tr" if re.search("[çğıöşüÇĞİÖŞÜ]", message) or words & turkish else "en"


def intent(message: str) -> str | None:
    normalized = message.casefold().translate(str.maketrans("çğıöşü", "cgiosu"))
    words = set(re.findall(r"\w+", normalized))
    if words & {"trade", "order", "execute", "arm", "consent", "emir", "islem", "bitcoin", "btc", "eth", "pricefeed"}:
        return None
    if not words & {"price", "cost", "fee", "much", "fiyat", "ucret"} and ("planim" in words or "aboneligim" in words
        or ("my" in words and words & {"plan", "subscription", "access", "membership"})
        or ("benim" in words and words & {"plan", "abonelik"})):
        return "get_my_access"
    if words & {"credit", "credits", "kredi", "kredim", "krediler", "kredilerim"}:
        return "get_my_credits"
    if words & {"trial", "deneme", "fiyat", "ucret", "fiyati", "plans", "pricing"}:
        return "get_plans"
    if words & {"plan", "subscription", "abonelik", "premium", "membership"} and words & {"price", "cost", "fee", "much", "fiyat", "ucret"}:
        return "get_plans"
    if words & {"price", "cost", "much"} and words <= {"what", "is", "the", "price", "how", "much", "does", "it", "cost"}:
        return "get_plans"
    return None


async def answer(tools: AssistantTools, user_id: str, message: str, language: Language) -> dict | None:
    name = intent(message)
    if name is None:
        return None
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
