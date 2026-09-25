import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app import v25_execution


class PreviewExchangeClient:
    def __init__(self, symbol_config=None):
        self.calls = []
        self.symbol_config = symbol_config if symbol_config is not None else [{
            "symbol": "MUBARAKUSDT",
            "marginType": "ISOLATED",
            "isAutoAddMargin": "false",
            "leverage": 5,
            "maxNotionalValue": "1000000",
        }]

    async def signed(self, method, path, params):
        self.calls.append((method, path, params))
        if path == "/fapi/v3/positionRisk":
            return [{
                "symbol": "MUBARAKUSDT",
                "positionAmt": "1000",
                "entryPrice": "0.1000",
            }]
        if path == "/fapi/v1/openAlgoOrders":
            return [
                {
                    "symbol": "MUBARAKUSDT",
                    "algoId": 101,
                    "clientAlgoId": "PTBLV_SL_EXTERNAL",
                    "side": "SELL",
                    "orderType": "STOP_MARKET",
                    "algoStatus": "NEW",
                    "triggerPrice": "0.0900",
                    "quantity": "0",
                    "positionSide": "BOTH",
                    "closePosition": "true",
                },
                {
                    "symbol": "MUBARAKUSDT",
                    "algoId": 102,
                    "clientAlgoId": "PTBLV_TP1_EXTERNAL",
                    "side": "SELL",
                    "orderType": "TAKE_PROFIT_MARKET",
                    "algoStatus": "NEW",
                    "triggerPrice": "0.1200",
                    "quantity": "0",
                    "positionSide": "BOTH",
                    "closePosition": "true",
                },
            ]
        if path == "/fapi/v1/symbolConfig":
            return self.symbol_config
        raise AssertionError(f"Unexpected signed call: {method} {path}")

    async def public_get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if path == "/fapi/v1/exchangeInfo":
            return {
                "symbols": [{
                    "symbol": "MUBARAKUSDT",
                    "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.001"}],
                }],
            }
        raise AssertionError(f"Unexpected public call: {path}")


def test_preview_route_preserves_candidate_response_after_helper_extraction():
    async def exercise():
        client = PreviewExchangeClient()
        state = v25_execution.initial_state()
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        request = SimpleNamespace(app=application)
        body = v25_execution.AdoptExternalPositionPreviewRequest(symbol="MUBARAKUSDT")
        with patch.object(v25_execution, "execution_owner", return_value={}), patch.object(
            v25_execution, "client_for", return_value=client
        ):
            response = await v25_execution.v25_adopt_external_position_preview(request, body)
        return response, client.calls

    response, calls = asyncio.run(exercise())
    candidate_plan = response["candidate_plan"]
    expected_candidate_plan = {
        "symbol": "MUBARAKUSDT",
        "direction": "LONG",
        "quantity": "1000",
        "entry_price": "0.1000",
        "stop_loss": {
            "price": "0.0900",
            "close_position": True,
            "quantity": None,
            "algo_id": 101,
        },
        "targets": [{
            "price": "0.1200",
            "close_position": True,
            "quantity": None,
            "algo_id": 102,
        }],
        "protection_ids": [101, 102],
        "protection_schema": "PARTIAL_TARGETS_WITH_CLOSE_ALL_V1",
        "target_coverage": "PARTIAL_PLUS_CLOSE_ALL",
        "leverage": 5,
        "margin_type": "isolated",
        "provenance_state": "ADOPTED_EXTERNAL",
        "source": "external_adoption",
    }
    expected_token = hashlib.sha256(
        json.dumps(expected_candidate_plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    expected_response = {
        "would_create_plan": True,
        "candidate_plan": expected_candidate_plan,
        "confirm_token": expected_token,
        "matched_protection_orders": [
            {
                "symbol": "MUBARAKUSDT",
                "algo_id": 101,
                "client_algo_id": "PTBLV_SL_EXTERNAL",
                "side": "SELL",
                "type": "STOP_MARKET",
                "status": "NEW",
                "trigger_price": "0.0900",
                "quantity": "0",
                "position_side": "BOTH",
                "close_position": True,
            },
            {
                "symbol": "MUBARAKUSDT",
                "algo_id": 102,
                "client_algo_id": "PTBLV_TP1_EXTERNAL",
                "side": "SELL",
                "type": "TAKE_PROFIT_MARKET",
                "status": "NEW",
                "trigger_price": "0.1200",
                "quantity": "0",
                "position_side": "BOTH",
                "close_position": True,
            },
        ],
        "unmatched_protection_orders": [],
        "warnings": [],
        "state_mutation": False,
    }
    assert response == expected_response
    assert calls == [
        ("GET", "/fapi/v3/positionRisk", {"symbol": "MUBARAKUSDT"}),
        ("GET", "/fapi/v1/openAlgoOrders", {"symbol": "MUBARAKUSDT"}),
        ("GET", "/fapi/v1/symbolConfig", {"symbol": "MUBARAKUSDT"}),
        ("GET", "/fapi/v1/exchangeInfo", None),
    ]


def test_preview_route_fails_closed_when_symbol_config_is_missing():
    async def exercise():
        client = PreviewExchangeClient(symbol_config=[])
        state = v25_execution.initial_state()
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        request = SimpleNamespace(app=application)
        body = v25_execution.AdoptExternalPositionPreviewRequest(symbol="MUBARAKUSDT")
        with patch.object(v25_execution, "execution_owner", return_value={}), patch.object(
            v25_execution, "client_for", return_value=client
        ):
            return await v25_execution.v25_adopt_external_position_preview(request, body)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(exercise())
    assert raised.value.status_code == 422