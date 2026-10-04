"""Browser cookie transport and fail-closed CSRF checks."""
from __future__ import annotations

from collections.abc import Collection

from fastapi import HTTPException, Request, Response
from .web_security import env_flag

USER_SESSION_COOKIE = "protrebot_session"
OWNER_ACCESS_COOKIE = "protrebot_owner"
COOKIE_SESSION_PREFIX = "cookie-session:"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "testclient"})


def browser_request(request: Request) -> bool:
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def secure_cookie(request: Request) -> bool:
    return (
        request.url.scheme == "https"
        or env_flag("PROTREBOT_DURABLE_AUTH_REQUIRED")
        or request.url.hostname not in LOCAL_HOSTS
    )


def set_browser_cookie(
    response: Response, request: Request, name: str, value: str, *, max_age: int | None = None,
) -> None:
    response.set_cookie(
        name, value, max_age=max_age, path="/api", httponly=True, samesite="lax",
        secure=secure_cookie(request),
    )


def clear_browser_cookie(response: Response, request: Request, name: str) -> None:
    response.delete_cookie(
        name, path="/api", httponly=True, samesite="lax",
        secure=secure_cookie(request),
    )


def validate_browser_request(request: Request, allowed_origins: Collection[str]) -> None:
    if request.method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    uses_cookie = USER_SESSION_COOKIE in request.cookies or OWNER_ACCESS_COOKIE in request.cookies
    if not uses_cookie and not browser_request(request):
        return
    if not browser_request(request):
        raise HTTPException(403, "Browser request verification is required")
    origin = request.headers.get("origin", "").rstrip("/")
    own_origin = f"{request.url.scheme}://{request.url.netloc}"
    if origin and (origin == own_origin or origin in allowed_origins):
        return
    if not origin and request.url.hostname in LOCAL_HOSTS and not secure_cookie(request):
        return
    raise HTTPException(403, "Browser request origin is not allowed")
