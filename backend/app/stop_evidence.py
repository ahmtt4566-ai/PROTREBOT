"""Read-only STOP event evidence collection for Binance Demo user streams."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

OBSERVATION_FIELDS = (
    "observation_id",
    "local_timestamp",
    "exchange_event_time",
    "event_type",
    "symbol",
    "algo_id",
    "client_algo_id",
    "actual_order_id",
    "actual_client_algo_id",
    "order_id",
    "client_order_id",
    "trade_id",
    "execution_type",
    "order_status",
    "side",
    "position_side",
    "last_fill_qty",
    "cumulative_fill_qty",
    "reduce_only",
    "position_amt",
    "entry_price",
    "mark_price",
    "liquidation_price",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _raw(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _next_observation_id(state: dict[str, Any]) -> str:
    number = int(state.get("evidence_sequence", 0)) + 1
    state["evidence_sequence"] = number
    return f"OBS-{number:06d}"


def _append(state: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    observation = {field: fields.get(field) for field in OBSERVATION_FIELDS}
    observation["observation_id"] = _next_observation_id(state)
    observation["local_timestamp"] = _now()
    state.setdefault("evidence_observations", []).append(observation)
    state["evidence_status"] = "LIVE OBSERVATIONS AVAILABLE"
    return observation


def observe_stream_payload(state: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    """Append one raw user-stream observation without changing lifecycle state."""
    event_type = payload.get("e")
    if event_type not in {"ALGO_UPDATE", "ORDER_TRADE_UPDATE"}:
        return None
    event = payload.get("o") if isinstance(payload.get("o"), dict) else payload.get("a", {})
    if not isinstance(event, dict):
        event = {}
    exchange_time = _raw(payload, "T", "E")
    if event_type == "ALGO_UPDATE":
        return _append(state, {
            "exchange_event_time": exchange_time,
            "event_type": event_type,
            "symbol": _raw(event, "s", "symbol"),
            "algo_id": _raw(event, "aid", "algoId"),
            "client_algo_id": _raw(event, "ca", "clientAlgoId"),
            "actual_order_id": _raw(event, "ai", "actualOrderId"),
            "actual_client_algo_id": _raw(event, "ac", "actualClientAlgoId"),
            "order_status": _raw(event, "X", "algoStatus", "status"),
            "mark_price": _raw(event, "sp", "triggerPrice"),
        })
    return _append(state, {
        "exchange_event_time": exchange_time,
        "event_type": event_type,
        "symbol": _raw(event, "s"),
        "order_id": _raw(event, "i"),
        "client_order_id": _raw(event, "c"),
        "trade_id": _raw(event, "t"),
        "execution_type": _raw(event, "x"),
        "order_status": _raw(event, "X"),
        "side": _raw(event, "S"),
        "position_side": _raw(event, "ps"),
        "last_fill_qty": _raw(event, "l"),
        "cumulative_fill_qty": _raw(event, "z"),
        "reduce_only": _raw(event, "R"),
    })


def observe_position_snapshot(state: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Append every raw positionRisk row, including zero-position rows."""
    observations = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        observations.append(_append(state, {
            "exchange_event_time": None,
            "event_type": "POSITION_RISK_SNAPSHOT",
            "symbol": _raw(row, "symbol"),
            "position_side": _raw(row, "positionSide"),
            "position_amt": _raw(row, "positionAmt"),
            "entry_price": _raw(row, "entryPrice"),
            "mark_price": _raw(row, "markPrice"),
            "liquidation_price": _raw(row, "liquidationPrice"),
        }))
    return observations


def correlation_report(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Report observed identity comparisons without attributing any lifecycle event."""
    algo_observations = [item for item in observations if item.get("event_type") == "ALGO_UPDATE"]
    order_observations = [item for item in observations if item.get("event_type") == "ORDER_TRADE_UPDATE"]
    report = []
    for algo in algo_observations:
        for order in order_observations:
            report.append({
                "algo_observation_id": algo.get("observation_id"),
                "order_observation_id": order.get("observation_id"),
                "actual_order_id_equals_order_id": (
                    algo.get("actual_order_id") is not None
                    and algo.get("actual_order_id") == order.get("order_id")
                ),
                "actual_client_algo_id_equals_client_order_id": (
                    algo.get("actual_client_algo_id") is not None
                    and algo.get("actual_client_algo_id") == order.get("client_order_id")
                ),
            })
    return report
