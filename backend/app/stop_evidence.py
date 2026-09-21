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


def _existing_observation(state: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any] | None:
    observations = state.get("evidence_observations", [])
    if not isinstance(observations, list):
        return None
    event_type = fields.get("event_type")
    for observation in observations:
        if not isinstance(observation, dict) or observation.get("event_type") != event_type:
            continue
        if event_type == "ORDER_TRADE_UPDATE":
            trade_id = _identifier(fields.get("trade_id"))
            if trade_id and _identifier(observation.get("trade_id")) == trade_id:
                return observation
            continue
        identity = (
            fields.get("exchange_event_time"),
            _identifier(fields.get("algo_id")),
            _identifier(fields.get("client_algo_id")),
            _identifier(fields.get("actual_order_id")),
            _identifier(fields.get("actual_client_algo_id")),
            str(fields.get("order_status") or "").upper(),
        )
        existing_identity = (
            observation.get("exchange_event_time"),
            _identifier(observation.get("algo_id")),
            _identifier(observation.get("client_algo_id")),
            _identifier(observation.get("actual_order_id")),
            _identifier(observation.get("actual_client_algo_id")),
            str(observation.get("order_status") or "").upper(),
        )
        if identity[1:] != (None, None, None, None, "") and identity == existing_identity:
            return observation
    return None


def _identifier(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _positive_quantity(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _position_observed(state: dict[str, Any], symbol: str, position_side: Any) -> bool:
    expected_side = _identifier(position_side)
    if not expected_side:
        return False
    for observation in state.get("evidence_observations", []):
        if not isinstance(observation, dict) or observation.get("event_type") != "POSITION_RISK_SNAPSHOT":
            continue
        if str(observation.get("symbol") or "") != symbol:
            continue
        observed_side = _identifier(observation.get("position_side"))
        if observed_side and observed_side == expected_side:
            return True
    return False


def _stop_execution_matches_plan(plan: dict[str, Any], observation: dict[str, Any]) -> bool:
    if observation.get("execution_type") != "TRADE":
        return False
    if str(observation.get("order_status") or "").upper() not in {"FILLED", "PARTIALLY_FILLED"}:
        return False
    if not _identifier(observation.get("trade_id")) or not _positive_quantity(observation.get("last_fill_qty")):
        return False
    if observation.get("reduce_only") is not True:
        return False
    direction = str(plan.get("direction") or "").upper()
    expected_side = {"LONG": "SELL", "SHORT": "BUY"}.get(direction)
    if expected_side is None or str(observation.get("side") or "").upper() != expected_side:
        return False
    expected_position_side = _identifier(plan.get("position_side"))
    observed_position_side = _identifier(observation.get("position_side"))
    if not expected_position_side or not observed_position_side:
        return False
    return observed_position_side == expected_position_side


def _owned_stop_plans(demo_state: dict[str, Any], symbol: str, algo_id: Any, client_algo_id: Any) -> list[dict[str, Any]]:
    event_algo_id = _identifier(algo_id)
    event_client_id = _identifier(client_algo_id)
    matches = []
    for plan in (demo_state.get("plans") or {}).values():
        if not isinstance(plan, dict) or plan.get("provenance_state") != "CONFIRMED":
            continue
        if str(plan.get("symbol") or "") != symbol:
            continue
        stop_algo_id = _identifier(plan.get("stop_algo_id"))
        stop_client_id = _identifier(plan.get("stop_client_id"))
        if stop_algo_id and stop_client_id:
            identity_matches = event_algo_id == stop_algo_id and event_client_id == stop_client_id
        else:
            identity_matches = (event_algo_id == stop_algo_id if stop_algo_id else False) or (
                event_client_id == stop_client_id if stop_client_id else False
            )
        if identity_matches:
            matches.append(plan)
    return matches


def _record_algo_correlation(
    state: dict[str, Any],
    demo_state: dict[str, Any],
    observation: dict[str, Any],
) -> None:
    event = observation
    if str(event.get("order_status") or "").upper() not in {"TRIGGERED", "FILLED", "EXECUTED"}:
        return
    plans = _owned_stop_plans(
        demo_state,
        str(event.get("symbol") or ""),
        event.get("algo_id"),
        event.get("client_algo_id"),
    )
    if len(plans) != 1:
        return
    plan = plans[0]
    correlations = state.setdefault("stop_correlations", [])
    observation_id = event.get("observation_id")
    for item in correlations:
        if not isinstance(item, dict):
            continue
        if (
            item.get("plan_id") == plan.get("id")
            and _identifier(item.get("stop_algo_id")) == _identifier(event.get("algo_id"))
            and _identifier(item.get("stop_client_algo_id")) == _identifier(event.get("client_algo_id"))
            and _identifier(item.get("actual_order_id")) == _identifier(event.get("actual_order_id"))
            and _identifier(item.get("actual_client_algo_id")) == _identifier(event.get("actual_client_algo_id"))
        ):
            return
    correlations.append({
        "correlation_id": f"stop-correlation:{plan.get('id') or 'unknown'}:{observation_id}",
        "status": "INCOMPLETE" if not (event.get("actual_order_id") or event.get("actual_client_algo_id")) else "PENDING",
        "plan_id": plan.get("id"),
        "symbol": event.get("symbol"),
        "stop_algo_id": event.get("algo_id"),
        "stop_client_algo_id": event.get("client_algo_id"),
        "algo_observation_id": observation_id,
        "actual_order_id": event.get("actual_order_id"),
        "actual_client_algo_id": event.get("actual_client_algo_id"),
        "execution_trade_id": None,
        "execution_confirmed": False,
    })


def _confirm_stop_execution(
    state: dict[str, Any],
    demo_state: dict[str, Any],
    observation: dict[str, Any],
) -> dict[str, Any] | None:
    candidates = []
    event_order_id = _identifier(observation.get("order_id"))
    event_client_id = _identifier(observation.get("client_order_id"))
    for correlation in state.get("stop_correlations", []):
        if not isinstance(correlation, dict) or correlation.get("status") != "PENDING":
            continue
        if correlation.get("symbol") != observation.get("symbol"):
            continue
        comparisons = []
        if correlation.get("actual_order_id"):
            comparisons.append(_identifier(correlation.get("actual_order_id")) == event_order_id)
        if correlation.get("actual_client_algo_id"):
            comparisons.append(_identifier(correlation.get("actual_client_algo_id")) == event_client_id)
        if comparisons and all(comparisons):
            candidates.append(correlation)
    if len(candidates) != 1:
        return None
    correlation = candidates[0]
    trade_id = _identifier(observation.get("trade_id"))
    if any(
        isinstance(item, dict) and item.get("execution_trade_id") == trade_id
        for item in state.get("stop_correlations", [])
    ):
        return None
    plan = (demo_state.get("plans") or {}).get(correlation.get("plan_id"))
    if not isinstance(plan, dict) or plan.get("provenance_state") != "CONFIRMED":
        return None
    if not _stop_execution_matches_plan(plan, observation):
        return None
    if not _position_observed(state, str(observation.get("symbol") or ""), plan.get("position_side")):
        return None
    correlation.update({
        "status": "CONFIRMED",
        "execution_trade_id": trade_id,
        "execution_observation_id": observation.get("observation_id"),
        "execution_type": observation.get("execution_type"),
        "order_status": observation.get("order_status"),
        "execution_confirmed": True,
    })
    plan["stop_fill_confirmed"] = True
    plan["stop_execution_trade_id"] = trade_id
    plan["stop_execution_observation_id"] = observation.get("observation_id")
    return correlation


def observe_stream_payload(
    state: dict[str, Any],
    payload: dict[str, Any],
    demo_state: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Append one raw user-stream observation without changing lifecycle state."""
    event_type = payload.get("e")
    if event_type not in {"ALGO_UPDATE", "ORDER_TRADE_UPDATE"}:
        return None
    event = payload.get("o") if isinstance(payload.get("o"), dict) else payload.get("a", {})
    if not isinstance(event, dict):
        event = {}
    exchange_time = _raw(payload, "T", "E")
    if event_type == "ALGO_UPDATE":
        fields = {
            "exchange_event_time": exchange_time,
            "event_type": event_type,
            "symbol": _raw(event, "s", "symbol"),
            "algo_id": _raw(event, "aid", "algoId"),
            "client_algo_id": _raw(event, "ca", "clientAlgoId"),
            "actual_order_id": _raw(event, "ai", "actualOrderId"),
            "actual_client_algo_id": _raw(event, "ac", "actualClientAlgoId"),
            "order_status": _raw(event, "X", "algoStatus", "status"),
            "mark_price": _raw(event, "sp", "triggerPrice"),
        }
        observation = _existing_observation(state, {"event_type": event_type, **fields})
        if observation is None:
            observation = _append(state, {"event_type": event_type, **fields})
        else:
            return observation
        if demo_state is not None:
            _record_algo_correlation(state, demo_state, observation)
        return observation
    fields = {
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
    }
    observation = _existing_observation(state, {"event_type": event_type, **fields})
    if observation is not None:
        return observation
    observation = _append(state, {"event_type": event_type, **fields})
    if demo_state is not None:
        _confirm_stop_execution(state, demo_state, observation)
    return observation


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
