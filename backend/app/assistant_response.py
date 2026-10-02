"""Server-side guards for model text; never record rejected content."""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from .assistant_models import Language
from .assistant_prompt import SYSTEM_POLICY
from .premium_access import PRIVATE_KEYS

LEVEL_REDIRECTS = {
    "tr": "Entry/SL/TP seviyeleri için Master Trade ekranını kullan; asistan bu seviyeleri paylaşmaz.",
    "en": "For Entry/SL/TP levels, use Master Trade; the assistant does not share these levels.",
}

_LEVEL_LABEL = r"(?:entry(?:[_\s-]?(?:price|preview))?|(?:initial[_\s-]?)?stop(?:[_\s-]?loss)?|sl|tp[1-3]?|take[_\s-]?profit|giriş|giris)"
_LEVEL_VALUE = re.compile(
    rf'\b{_LEVEL_LABEL}\b[\s"\'`*_]*'
    r'(?:(?:[:=]|is|at|price|seviyesi|fiyatı)\s*)?[\s"\'`*_]*'
    r"(?:[$€₺]\s*)?[-+\u2212]?\d+(?:[.,]\d+)*(?!\w)",
    re.IGNORECASE,
)
_REASON_LABEL = re.compile(
    r'\b(?:reasons?|rationale|analysis[_\s-]?reason|long[_\s-]?case|short[_\s-]?case|'
    r'analiz\s+gerekçesi|gerekçe(?:ler(?:i)?)?|gerekce(?:ler(?:i)?)?)\b'
    r'[\s"\'`*_]*(?:[:=]|is\b|are\b)',
    re.IGNORECASE,
)
_ANALYSIS_CAUSE = re.compile(
    r"\b(?:signal|analysis|analiz|sinyal|long|short)\b[^\n.!?]{0,100}"
    r"\b(?:because|due to|since|çünkü|nedeni|gerekçesi|nedeniyle)\b",
    re.IGNORECASE,
)


def normalized(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold().replace("\u0307", "")
    return " ".join(re.findall(r"\w+", value))


def policy_signatures() -> frozenset[str]:
    signatures = {normalized(title) for title in re.findall(r"^(?:TR|EN) — .+$", SYSTEM_POLICY, re.MULTILINE)}
    for title in ("TR — Talimat güvenliği ve biçim", "EN — Instruction safety and presentation"):
        section = SYSTEM_POLICY.split(title, 1)[1].split("\n\n", 1)[0]
        words = normalized(section).split()
        signatures.update(" ".join(words[index:index + 12]) for index in range(len(words) - 11))
    return frozenset(signatures)


_POLICY_SIGNATURES = policy_signatures()
_LEVEL_KEYS = frozenset(
    normalized(key).replace(" ", "") for key in PRIVATE_KEYS | {"tp", "giriş", "giris"}
    if re.match(r"entry|(?:initial[_-])?stop|sl$|tp[1-3]?$|take[_-]?profit|target|giriş|giris", key, re.IGNORECASE)
)
_REASON_KEYS = frozenset(normalized(key).replace(" ", "") for key in PRIVATE_KEYS if key in {
    "reason", "reasons", "explanation", "long_case", "short_case", "longCase", "shortCase", "why_wait", "whyWait",
})


def contains_private_fields(value: Any, *, free: bool) -> bool:
    if isinstance(value, list):
        return any(contains_private_fields(item, free=free) for item in value)
    if isinstance(value, dict):
        for key, item in value.items():
            name = normalized(key).replace(" ", "")
            if (name in _LEVEL_KEYS or free and name in _REASON_KEYS) and item not in (None, "", [], {}):
                return True
            if contains_private_fields(item, free=free):
                return True
    return False


def safe_response(reply: str, language: Language, *, free: bool) -> str:
    candidate = normalized(reply)
    if any(signature in candidate for signature in _POLICY_SIGNATURES):
        return "Bu isteğe yardımcı olamam. Platformun kullanıcı ekranları hakkında yardımcı olabilirim." if language == "tr" else (
            "I cannot help with that request. I can help with the platform's customer-facing screens."
        )
    structured = re.sub(r"^```(?:json)?\s*|\s*```$", "", reply.strip(), flags=re.IGNORECASE)
    try:
        private = contains_private_fields(json.loads(structured), free=free)
    except json.JSONDecodeError:
        private = False
    if private or _LEVEL_VALUE.search(reply) or free and (_REASON_LABEL.search(reply) or _ANALYSIS_CAUSE.search(reply)):
        prefix = "Bu analiz detaylarını şu an doğrulayamadım." if language == "tr" else "I could not verify these analysis details right now."
        return prefix + " " + LEVEL_REDIRECTS[language]
    return reply
