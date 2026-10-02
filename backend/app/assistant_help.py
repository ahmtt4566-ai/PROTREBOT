"""Packaged, language-scoped help with runtime business values."""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from string import Formatter
from typing import Any, Literal

from . import binance_demo, subscription_core, v25_execution
from .analyst_credits import CreditConfig

logger = logging.getLogger(__name__)
HELP_RESULT_LIMIT = 3
KB_DIRECTORY = Path(__file__).with_name("assistant_kb")
Language = Literal["tr", "en"]


class KnowledgeBaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class Article:
    id: str
    title: str
    content: str
    keywords: tuple[str, ...]
    code_sources: tuple[str, ...]


def placeholder_values(config: CreditConfig) -> dict[str, str]:
    return {
        "ANALYST_BUDGET": str(config.total),
        "ANALYSIS_COST": str(config.cost),
        "CREDIT_WINDOW_HOURS": str(config.window_hours),
        "ANALYST_CACHE_MINUTES": str(config.cache_minutes),
        "MASTER_PLAN_NAME": str(subscription_core.PLAN_CATALOG["MASTER_MODE"]["name"]),
        "MASTER_MONTHLY_PRICE": f'{subscription_core.PLAN_CATALOG["MASTER_MODE"]["monthly_price"]:.2f}',
        "TRIAL_DAYS": str(subscription_core.TRIAL_DAYS),
        "DEMO_ARM_MINUTES": f"{binance_demo.ARM_SECONDS / 60:g}",
        "LIVE_CONSENT_HOURS": f"{v25_execution.LIVE_CONSENT_SECONDS / 3600:g}",
        "LIVE_ARM_HOURS": f"{v25_execution.LIVE_ARM_SECONDS / 3600:g}",
        "AUTO_SESSION_MINUTES": f"{v25_execution.LIVE_AUTO_SESSION_SECONDS / 60:g}",
        "CONSENT_GRACE_MINUTES": f"{v25_execution.LIVE_CONSENT_GRACE_SECONDS / 60:g}",
    }


def kb_error(language: str, category: str) -> KnowledgeBaseError:
    logger.error("assistant_kb_unavailable language=%s category=%s", language, category)
    return KnowledgeBaseError("Help knowledge base is unavailable")


@lru_cache(maxsize=2)
def load_articles(language: Language) -> tuple[Article, ...]:
    if language not in {"tr", "en"}:
        raise kb_error("invalid", "language")
    try:
        raw = json.loads((KB_DIRECTORY / f"{language}.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise kb_error(language, type(exc).__name__) from exc
    if not isinstance(raw, list) or not raw:
        raise kb_error(language, "schema")
    articles = []
    identifiers: set[str] = set()
    allowed = placeholder_values(CreditConfig()).keys()
    for row in raw:
        if not isinstance(row, dict):
            raise kb_error(language, "schema")
        if any(not isinstance(row.get(key), str) or not row[key].strip() for key in ("id", "title", "content")):
            raise kb_error(language, "schema")
        if any(not isinstance(row.get(key), list) or not row[key] or
               any(not isinstance(value, str) or not value.strip() for value in row[key])
               for key in ("keywords", "code_sources")):
            raise kb_error(language, "schema")
        if not re.fullmatch(r"[a-z][a-z_-]*", row["id"]) or row["id"] in identifiers:
            raise kb_error(language, "identifier")
        identifiers.add(row["id"])
        try:
            for text in (row["title"], row["content"]):
                for _, field, spec, conversion in Formatter().parse(text):
                    if field is not None and (field not in allowed or spec or conversion):
                        raise ValueError("Unsupported placeholder")
        except ValueError as exc:
            raise kb_error(language, "placeholder") from exc
        articles.append(Article(row["id"], row["title"], row["content"], tuple(row["keywords"]), tuple(row["code_sources"])))
    return tuple(articles)


def entries(config: CreditConfig, language: Language) -> list[dict[str, Any]]:
    values = placeholder_values(config)
    return [{
        "id": article.id,
        "title": article.title.format_map(values),
        "content": article.content.format_map(values),
        "source": f"backend/app/assistant_kb/{language}.json#{article.id}",
        "code_sources": list(article.code_sources),
        "language": language,
    } for article in load_articles(language)]


def terms(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", value.casefold().replace("ı", "i"))
    normalized = "".join(character for character in normalized if not unicodedata.combining(character))
    aliases = {"credits": "credit", "kredim": "kredi", "krediler": "kredi", "kredilerim": "kredi"}
    return {aliases.get(word, word) for word in re.findall(r"\w+", normalized)}


STOP_WORDS = {
    "tr": terms("bir bu ve ile için nasıl nedir nerede ne benim bana mı mi mu mü"),
    "en": terms("a an the and or of to in on for is are my me how what where can do does"),
}


def search(query: str, language: Language, config: CreditConfig) -> list[dict[str, Any]]:
    articles = load_articles(language)
    words = terms(query) - STOP_WORDS[language]
    ranked = []
    for index, article in enumerate(articles):
        title = terms(article.title)
        keywords = terms(" ".join(article.keywords))
        body = terms(article.content)
        vocabulary = title | keywords | body
        score = 0
        for term in words:
            matched = {token for token in vocabulary if token == term or
                       (len(term) >= 4 and len(token) >= 4 and term.startswith(token))}
            score += max((4 if token in title else 3 if token in keywords else 1 for token in matched), default=0)
        if score:
            ranked.append((score, index))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    rendered = entries(config, language)
    return [rendered[index] for _, index in ranked[:HELP_RESULT_LIMIT]]
