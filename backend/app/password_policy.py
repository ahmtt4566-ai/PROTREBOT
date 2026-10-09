"""New-password policy; existing passwords remain usable for authentication."""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import HTTPException

POLICY = json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))
MIN_LENGTH = POLICY["min_length"]
MAX_LENGTH = POLICY["max_length"]
PATTERNS = {name: re.compile(pattern) for name, pattern in POLICY["patterns"].items()}
PASSWORD_POLICY_MESSAGE = (
    "Parola 10–256 karakter olmalı; büyük harf, küçük harf, rakam ve sembol içermelidir."
)


def password_rules(password: str) -> dict[str, bool]:
    return {
        "length": MIN_LENGTH <= len(password) <= MAX_LENGTH,
        **{name: bool(pattern.search(password)) for name, pattern in PATTERNS.items()},
    }


def validate_new_password(password: str) -> str:
    if not all(password_rules(password).values()):
        raise HTTPException(422, PASSWORD_POLICY_MESSAGE)
    return password
