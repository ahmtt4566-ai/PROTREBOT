"""Versioned registration and marketing evidence in the canonical account store."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

DOCUMENT_NAME = "registration-2026-10-14.json"
DOCUMENT = json.loads(
    (Path(__file__).parent / "legal" / DOCUMENT_NAME).read_text(encoding="utf-8")
)
TERMS_VERSION = DOCUMENT["version"]
MARKETING_VERSION = DOCUMENT["marketing_version"]
REQUIRED_MESSAGE = "Devam etmek için Kullanıcı Sözleşmesi'ni kabul etmelisiniz."


class MarketingConsentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted: bool = Field(strict=True)
    revision: str | None = Field(
        min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$"
    )
    operation_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")


def require_terms(accepted: bool) -> None:
    if accepted is not True:
        raise HTTPException(422, REQUIRED_MESSAGE)


def consent_record(
    user_id: str, kind: str, accepted: bool, source: str, *, withdrawn: bool = False
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    text = (
        "\n\n".join(DOCUMENT["paragraphs"])
        if kind == "TERMS"
        else DOCUMENT["marketing_text"]
    )
    return {
        "id": uuid.uuid4().hex,
        "user_id": user_id,
        "kind": kind,
        "accepted": accepted,
        "version": TERMS_VERSION if kind == "TERMS" else MARKETING_VERSION,
        "document_reference": f"{DOCUMENT_NAME}#{kind.lower()}",
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "recorded_at": now,
        "accepted_at": now if accepted else None,
        "withdrawn_at": now if withdrawn else None,
        "source": source,
    }


def append_registration(
    doc: dict[str, Any], user_id: str, marketing_accepted: bool, source: str
) -> list[dict[str, Any]]:
    records = [
        consent_record(user_id, "TERMS", True, source),
        consent_record(user_id, "MARKETING", marketing_accepted, source),
    ]
    records[0]["terms_accepted"] = True
    doc.setdefault("registration_consents", []).extend(records)
    doc["marketing_consent"] = records[1].copy()
    return records


def update_marketing(
    doc: dict[str, Any],
    user_id: str,
    accepted: bool,
    *,
    revision: str | None,
    operation_id: str,
    source: str = "ACCOUNT_SETTINGS",
) -> None:
    previous = doc.get("marketing_consent")
    current_revision = previous["id"] if previous else None
    for event in doc.get("registration_consents", []):
        if event.get("operation_id") == operation_id:
            if (
                event["accepted"] is accepted
                and event.get("previous_revision") == revision
                and event["source"] == source
                and current_revision == event["id"]
            ):
                return
            raise HTTPException(
                409, "Bu işlem artık güncel değil. Ticari ileti tercihini yenileyin."
            )
    if revision != current_revision:
        raise HTTPException(
            409, "Ticari ileti tercihi değişti. Güncel durumu yenileyin."
        )
    record = consent_record(
        user_id,
        "MARKETING",
        accepted,
        source,
        withdrawn=not accepted
        and (
            source == "EMAIL_UNSUBSCRIBE"
            or bool(previous and previous["accepted"] is True)
        ),
    )
    if (
        previous
        and previous["accepted"] is False
        and not accepted
        and previous.get("withdrawn_at")
    ):
        record["withdrawn_at"] = previous.get("withdrawn_at")
    record.update(operation_id=operation_id, previous_revision=current_revision)
    doc.setdefault("registration_consents", []).append(record)
    doc["marketing_consent"] = record.copy()


def public_marketing(doc: dict[str, Any]) -> dict[str, Any]:
    record = doc.get("marketing_consent") or {}
    return {
        key: record.get(key) for key in ("version", "accepted_at", "withdrawn_at")
    } | {
        "accepted": record.get("accepted") is True,
        "revision": record.get("id"),
    }
