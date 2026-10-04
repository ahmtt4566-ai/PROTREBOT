"""Dependency-free security and pricing helpers for V24 Commercial Complete.

This module intentionally knows nothing about exchange credentials or order
execution.  It can be audited and tested without starting FastAPI.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from .commerce_core import default_business_settings


V23_VERSION = "25.0.0"
# /api/v22 ve eski Windows yardımcıları mevcut kurulumları bozmamak için
# korunuyor. Kullanıcıya görünen birleşik ürün sürümü artık V24'tür.
V22_VERSION = V23_VERSION
TOKEN_ALGORITHM = "HMAC-SHA256"
PASSWORD_ALGORITHM = "SCRYPT-N131072-R8-P1"
LEGACY_PASSWORD_ALGORITHM = "SCRYPT-N16384-R8-P1"
SCRYPT_MAXMEM = 256 * 1024 * 1024


PLAN_CATALOG: dict[str, dict[str, Any]] = {
    "TRIAL": {
        "name": "Deneme",
        "monthly_usd": 0.0,
        "days": 7,
        "agents": 1,
        "bots": 1,
        "features": ["Paper/Demo kokpit", "Net kâr koruması", "1 güvenli ajan"],
    },
    "STARTER": {
        "name": "Başlangıç",
        "monthly_usd": 19.0,
        "annual_usd": 190.0,
        "days": 30,
        "agents": 1,
        "bots": 2,
        "features": ["Tüm Demo araçları", "Lisans ve cihaz kilidi", "İşlem günlüğü"],
    },
    "PRO": {
        "name": "Profesyonel",
        "monthly_usd": 39.0,
        "annual_usd": 390.0,
        "days": 30,
        "agents": 3,
        "bots": 8,
        "features": ["Çoklu ajan", "Gelişmiş risk merkezi", "Öncelikli güncellemeler"],
    },
    "ELITE": {
        "name": "Elite",
        "annual_usd": 790.0,
        "monthly_usd": 79.0,
        "days": 30,
        "agents": 10,
        "bots": 25,
        "features": ["Ekip lisansı", "Denetim kayıtları", "Ticari destek altyapısı"],
    },
}


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def normalize_email(value: str) -> str:
    return value.strip().casefold()


def hash_password(password: str, *, salt: bytes | None = None) -> dict[str, str]:
    if len(password) < 10:
        raise ValueError("Parola en az 10 karakter olmalıdır")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**17, r=8, p=1, dklen=32, maxmem=SCRYPT_MAXMEM)
    return {"algorithm": PASSWORD_ALGORITHM, "salt": _b64encode(salt), "digest": _b64encode(digest)}


def verify_password(password: str, record: dict[str, str]) -> bool:
    try:
        algorithm = record.get("algorithm")
        if algorithm not in {PASSWORD_ALGORITHM, LEGACY_PASSWORD_ALGORITHM}:
            return False
        actual = _b64encode(hashlib.scrypt(
            password.encode("utf-8"), salt=_b64decode(record["salt"]),
            n=2**17 if algorithm == PASSWORD_ALGORITHM else 2**14,
            r=8, p=1, dklen=32, maxmem=SCRYPT_MAXMEM,
        ))
        return hmac.compare_digest(actual, record["digest"])
    except (KeyError, TypeError, ValueError):
        return False


def issue_token(
    subject: str,
    role: str,
    secret: bytes,
    *,
    kind: str = "USER",
    ttl_seconds: int = 8 * 60 * 60,
    token_version: int = 1,
    now: int | None = None,
) -> str:
    issued_at = int(time.time() if now is None else now)
    payload = {
        "sub": subject,
        "role": role,
        "kind": kind,
        "iat": issued_at,
        "exp": issued_at + max(60, int(ttl_seconds)),
        "jti": secrets.token_hex(8),
        "ver": max(1, int(token_version)),
    }
    encoded = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _b64encode(hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def verify_token(token: str, secret: bytes, *, expected_kind: str | None = None, now: int | None = None) -> dict[str, Any]:
    try:
        encoded, signature = token.split(".", 1)
        expected = _b64encode(hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Geçersiz imza")
        payload = json.loads(_b64decode(encoded).decode("utf-8"))
        current = int(time.time() if now is None else now)
        if int(payload.get("exp", 0)) <= current:
            raise ValueError("Oturum süresi doldu")
        if expected_kind and payload.get("kind") != expected_kind:
            raise ValueError("Yanlış oturum türü")
        if not payload.get("sub"):
            raise ValueError("Oturum sahibi bulunamadı")
        return payload
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError(str(exc) or "Geçersiz oturum") from exc


def device_fingerprint_hash(fingerprint: str) -> str:
    clean = "|".join(part.strip() for part in fingerprint.split("|") if part.strip())
    if len(clean) < 12:
        raise ValueError("Cihaz parmak izi çok kısa")
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()


def pairing_code_hash(code: str, secret: bytes) -> str:
    normalized = code.replace("-", "").replace(" ", "").upper()
    return hmac.new(secret, normalized.encode("ascii"), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class FeeGuardInput:
    entry: float
    target: float
    notional_usdt: float
    direction: str = "LONG"
    fee_bps_per_side: float = 4.0
    slippage_bps_per_side: float = 2.0
    funding_bps: float = 0.0
    minimum_net_usdt: float = 0.25
    minimum_net_pct: float = 0.05


def calculate_fee_guard(values: FeeGuardInput) -> dict[str, Any]:
    if values.entry <= 0 or values.target <= 0 or values.notional_usdt <= 0:
        raise ValueError("Giriş, hedef ve tutar sıfırdan büyük olmalıdır")
    direction = values.direction.upper()
    if direction not in {"LONG", "SHORT"}:
        raise ValueError("Yön LONG veya SHORT olmalıdır")
    signed_move = (values.target - values.entry) / values.entry
    gross_pct = signed_move if direction == "LONG" else -signed_move
    gross_usdt = values.notional_usdt * gross_pct
    fee_usdt = values.notional_usdt * max(0.0, values.fee_bps_per_side) * 2 / 10_000
    slippage_usdt = values.notional_usdt * max(0.0, values.slippage_bps_per_side) * 2 / 10_000
    funding_usdt = values.notional_usdt * abs(values.funding_bps) / 10_000
    cost_usdt = fee_usdt + slippage_usdt + funding_usdt
    net_usdt = gross_usdt - cost_usdt
    required_net = max(max(0.0, values.minimum_net_usdt), values.notional_usdt * max(0.0, values.minimum_net_pct) / 100)
    approved = gross_usdt > 0 and net_usdt >= required_net
    break_even_move_pct = (cost_usdt + required_net) / values.notional_usdt * 100
    return {
        "direction": direction,
        "gross_move_pct": round(gross_pct * 100, 5),
        "gross_usdt": round(gross_usdt, 6),
        "fee_usdt": round(fee_usdt, 6),
        "slippage_usdt": round(slippage_usdt, 6),
        "funding_usdt": round(funding_usdt, 6),
        "total_cost_usdt": round(cost_usdt, 6),
        "minimum_required_usdt": round(required_net, 6),
        "net_usdt": round(net_usdt, 6),
        "net_return_pct": round(net_usdt / values.notional_usdt * 100, 5),
        "break_even_move_pct": round(break_even_move_pct, 5),
        "approved": approved,
        "decision": "NET KÂR KAPISI AÇIK" if approved else "MALİYET KORUMASI ENGELLEDİ",
        "reason": "Tahmini net sonuç minimum eşiğin üzerinde." if approved else "Komisyon, kayma, fonlama ve minimum net kâr sonrası hedef yetersiz.",
        "demo_only": True,
    }


def calculate_grid_guard(
    *,
    lower: float,
    upper: float,
    grid_count: int,
    capital_usdt: float,
    maker_share_pct: float = 80.0,
    maker_fee_bps: float = 2.0,
    taker_fee_bps: float = 5.0,
    slippage_bps_per_side: float = 1.0,
    funding_bps: float = 0.0,
    minimum_cycle_net_usdt: float = 0.05,
) -> dict[str, Any]:
    if lower <= 0 or upper <= lower or capital_usdt <= 0:
        raise ValueError("Grid alt/üst sınırı ve sermaye geçerli olmalıdır")
    if grid_count < 3 or grid_count > 200:
        raise ValueError("Grid sayısı 3 ile 200 arasında olmalıdır")
    maker_share = min(100.0, max(0.0, maker_share_pct)) / 100
    effective_fee_bps = max(0.0, maker_fee_bps) * maker_share + max(0.0, taker_fee_bps) * (1 - maker_share)
    midpoint = (lower + upper) / 2
    step = (upper - lower) / (grid_count - 1)
    step_pct = step / midpoint * 100
    per_grid = capital_usdt / grid_count
    gross = per_grid * step_pct / 100
    fee = per_grid * effective_fee_bps * 2 / 10_000
    slippage = per_grid * max(0.0, slippage_bps_per_side) * 2 / 10_000
    funding = per_grid * abs(funding_bps) / 10_000
    net = gross - fee - slippage - funding
    approved = net >= max(0.0, minimum_cycle_net_usdt)
    return {
        "lower": round(lower, 8),
        "upper": round(upper, 8),
        "grid_count": grid_count,
        "grid_step": round(step, 8),
        "grid_step_pct": round(step_pct, 5),
        "capital_per_grid_usdt": round(per_grid, 6),
        "maker_share_pct": round(maker_share * 100, 2),
        "effective_fee_bps": round(effective_fee_bps, 4),
        "gross_cycle_usdt": round(gross, 6),
        "fee_cycle_usdt": round(fee, 6),
        "slippage_cycle_usdt": round(slippage, 6),
        "funding_cycle_usdt": round(funding, 6),
        "net_cycle_usdt": round(net, 6),
        "minimum_cycle_net_usdt": round(max(0.0, minimum_cycle_net_usdt), 6),
        "approved": approved,
        "decision": "GRID MALİYET SONRASI UYGUN" if approved else "GRID ARALIĞI ÇOK DAR",
        "demo_only": True,
    }


def default_commercial_state() -> dict[str, Any]:
    return {
        "version": V22_VERSION,
        "owner_user_id": None,
        "users": [],
        "profiles": [],
        "subscriptions": [],
        "auth_tokens": [],
        "stripe_event_ids": [],
        "stripe_checkout_sessions": [],
        "licenses": [],
        "pairing_codes": [],
        "agents": [],
        "audit": [],
        "business": default_business_settings(),
        "leads": [],
        "demo_invoices": [],
        "support_tickets": [],
        "acceptances": [],
        "plans": json.loads(json.dumps(PLAN_CATALOG)),
        "release_evidence": {
            "backup": {"status": "PENDING", "note": "Yedekleme tatbikatı bekleniyor.", "updated_at": None},
            "support": {"status": "PENDING", "note": "Müşteri destek süreci bekleniyor.", "updated_at": None},
            "legal": {"status": "PENDING", "note": "Hukuk ve sözleşme incelemesi bekleniyor.", "updated_at": None},
            "security_review": {"status": "PENDING", "note": "Bağımsız güvenlik testi bekleniyor.", "updated_at": None},
        },
        "billing": {"provider": "MANUAL_DEMO", "live": False, "currency": "USD"},
        "security": {
            "demo_only": True,
            "real_orders_enabled": False,
            "testnet_orders_enabled": False,
            "withdrawals_supported": False,
            "central_exchange_credentials": False,
            "local_agent_model": True,
        },
    }
