"""Read-only monitor for transient reconciliation recovery events."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_STATUS_URL = "https://protrebot.onrender.com/api/v25/status"
WATCHED_KINDS = {
    "RECONCILIATION_FAILURE",
    "RECONCILIATION_CLEAN",
    "AUTO_RECOVERED_FROM_TRANSIENT_RECONCILIATION",
}


def status_url() -> str:
    return os.getenv("PROTREBOT_STATUS_URL", DEFAULT_STATUS_URL).strip()


def interval_seconds() -> float:
    try:
        return max(10.0, float(os.getenv("PROTREBOT_STATUS_INTERVAL_SECONDS", "150")))
    except ValueError:
        return 150.0


def request_headers() -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": "ProTreBot-recovery-watch/1.0"}
    owner_token = os.getenv("PROTREBOT_OWNER_TOKEN", "").strip()
    session_token = os.getenv("PROTREBOT_SESSION_TOKEN", "").strip()
    if owner_token:
        headers["X-ProTreBot-Owner"] = owner_token
        headers["Authorization"] = f"Bearer {owner_token}"
    if session_token:
        headers["X-ProTreBot-Session"] = session_token
    return headers


def fetch_status() -> dict:
    request = Request(status_url(), headers=request_headers(), method="GET")
    with urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("status endpoint returned a non-object JSON payload")
    return payload


def event_key(event: dict) -> str:
    return json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)


def event_timestamp(event: dict) -> str:
    for key in ("timestamp", "created_at", "at", "time"):
        value = event.get(key)
        if value:
            return str(value)
    return datetime.now(timezone.utc).isoformat()


def print_event(event: dict) -> None:
    kind = str(event.get("kind") or "UNKNOWN")
    details = {
        key: value
        for key, value in event.items()
        if key not in {"kind", "timestamp", "created_at", "at", "time", "message"}
    }
    message = str(event.get("message") or "").strip()
    prefix = "\n!!! AUTO RECOVERED !!!" if kind == "AUTO_RECOVERED_FROM_TRANSIENT_RECONCILIATION" else "EVENT"
    print(f"{prefix} {event_timestamp(event)} {kind}", flush=True)
    if message:
        print(f"  message={message}", flush=True)
    if details:
        print(f"  details={json.dumps(details, ensure_ascii=False, sort_keys=True, default=str)}", flush=True)


def watch() -> None:
    seen: set[str] = set()
    delay = interval_seconds()
    print(f"Watching {status_url()} every {delay:g}s (read-only). Ctrl+C to stop.", flush=True)
    while True:
        try:
            payload = fetch_status()
            events = payload.get("events", [])
            if not isinstance(events, list):
                raise ValueError("status payload events is not a list")
            for event in events:
                if not isinstance(event, dict) or event.get("kind") not in WATCHED_KINDS:
                    continue
                key = event_key(event)
                if key not in seen:
                    seen.add(key)
                    print_event(event)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"WATCH_ERROR {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            print("Stopped.", flush=True)
            return


if __name__ == "__main__":
    try:
        watch()
    except KeyboardInterrupt:
        print("Stopped.", flush=True)
