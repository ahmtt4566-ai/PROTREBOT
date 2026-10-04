"""Shared credential selection for browser cookies and native API clients.

Public browser markers are never credentials. Selecting a token does not verify
it: authentication must still check the signature and authoritative session.
"""
from __future__ import annotations

from typing import Any

from .browser_security import COOKIE_SESSION_PREFIX, USER_SESSION_COOKIE

SESSION_COOKIE_NAME = USER_SESSION_COOKIE


def request_session_token(request: Any) -> str:
    headers = getattr(request, "headers", {})
    authorization = str(headers.get("authorization", "")).strip()
    if authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        if token and not token.casefold().startswith(COOKIE_SESSION_PREFIX):
            return token
    token = str(headers.get("x-protrebot-session", "")).strip()
    if token and not token.casefold().startswith(COOKIE_SESSION_PREFIX):
        return token
    token = str(getattr(request, "cookies", {}).get(SESSION_COOKIE_NAME, "")).strip()
    return token if token and not token.casefold().startswith(COOKIE_SESSION_PREFIX) else ""
