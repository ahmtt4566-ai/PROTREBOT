"""Real SDK serialization with fake HTTP only; no provider, database or secrets."""

import asyncio
import copy
import json
from datetime import datetime
from pathlib import Path

import anthropic
import asyncpg
import httpx2
import pytest
from anthropic import AsyncAnthropic as SDKClient
from app.chart_ai_service import (
    MODEL,
    OUTPUT_TOOL,
    OUTPUT_TOOL_CHOICE,
    OUTPUT_TOOL_NAME,
    SYSTEM_PROMPT,
    AnthropicChartProvider,
    ModelExplanation,
    ProviderReply,
    ProviderRequest,
    usage_cost,
    validate_reply,
)
from app.chart_ai_store import ChartAiQuotaError
from app.chart_ai_validation import validate_context
from pydantic import SecretStr

CAPTURED = json.loads(
    (
        Path(__file__).parent / "fixtures" / "chart_ai_models_55_comparison.json"
    ).read_text(encoding="utf-8")
)
SAFE = {
    name: "Grafikte mevcut görünüm belirsiz." for name in ModelExplanation.model_fields
}
SAFE["image_warning"] = ""


def message(content=None, stop_reason="tool_use"):
    return {
        "id": "synthetic-response",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "content": content
        if content is not None
        else [
            {
                "type": "tool_use",
                "id": "output-only",
                "name": OUTPUT_TOOL_NAME,
                "input": dict(SAFE),
            },
        ],
        "usage": {"input_tokens": 100, "output_tokens": 120},
    }


@pytest.fixture
def fake_sdk(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Real HTTP/database access is forbidden")

    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(asyncpg, "connect", forbidden)
    monkeypatch.setattr(asyncpg, "create_pool", forbidden)

    def install(response):
        requests = []

        def handle(request):
            assert request.url.host == "api.anthropic.com"
            requests.append((request.url.path, json.loads(request.content)))
            if request.url.path.endswith("/count_tokens"):
                return httpx2.Response(200, json={"input_tokens": 100})
            assert request.url.path == "/v1/messages"
            return httpx2.Response(200, json=response)

        def factory(**kwargs):
            assert kwargs["max_retries"] == 0 and kwargs["timeout"] == 30
            return SDKClient(
                **kwargs,
                http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
            )

        monkeypatch.setattr(anthropic, "AsyncAnthropic", factory)
        return AnthropicChartProvider(SecretStr("synthetic-not-a-real-key")), requests

    return install


def test_sdk_count_and_generation_share_haiku55_strict_settings(fake_sdk):
    provider, requests = fake_sdk(message())
    request = ProviderRequest([{"role": "user", "content": "Sentetik test girdisi."}])

    async def run():
        assert await provider.count(request) == 100
        reply = await provider.generate(request)
        assert not reply.protocol_error and reply.payload == SAFE

    asyncio.run(run())
    assert len(requests) == 2
    count, create = (item[1] for item in requests)
    assert create.pop("max_tokens") == 1500
    assert (
        count
        == create
        == {
            "model": "claude-haiku-5-5",
            "messages": request.messages,
            "system": SYSTEM_PROMPT,
            "tools": [OUTPUT_TOOL],
            "tool_choice": OUTPUT_TOOL_CHOICE,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "low"},
        }
    )
    tool = count["tools"][0]
    assert tool["strict"] is True and len(count["tools"]) == 1
    assert tool["input_schema"]["additionalProperties"] is False
    assert set(tool["input_schema"]["required"]) == set(ModelExplanation.model_fields)
    for prop in tool["input_schema"]["properties"].values():
        assert not {"minLength", "maxLength", "default"} & prop.keys()
    for invalid_length in ("", "x" * 2001):
        with pytest.raises(ValueError):
            ModelExplanation.model_validate({**SAFE, "risks": invalid_length})


@pytest.mark.parametrize(
    "recorded",
    CAPTURED["responses"],
    ids=lambda item: f"{item['model']}-case-{item['case']}",
)
def test_eight_original_sdk_payloads_are_replayed_without_network(
    fake_sdk, recorded, caplog
):
    response = message(copy.deepcopy(recorded["content"]), recorded["stop_reason"])
    response["usage"] = {
        "input_tokens": recorded["input_tokens"],
        "output_tokens": recorded["output_tokens"],
    }
    provider, requests = fake_sdk(response)
    request = ProviderRequest([{"role": "user", "content": recorded["question"]}])
    reply = asyncio.run(provider.generate(request))
    source = CAPTURED["cases"][recorded["case"] - 1]["context"]
    context = validate_context(
        source,
        expected_symbol="BTCUSDT",
        expected_timeframe="15m",
        now=datetime.fromisoformat(source["captured_at"]),
    )
    if recorded["expected_code"] is None:
        validate_reply(reply, context)
    else:
        with pytest.raises(ChartAiQuotaError) as caught:
            validate_reply(reply, context)
        assert caught.value.code == recorded["expected_code"]
        if recorded["expected_rule"]:
            assert f"rule={recorded['expected_rule']}" in caplog.text
    assert usage_cost(reply) == recorded["expected_haiku55_microusd"]
    assert len(requests) == 1 and requests[0][1]["model"] == "claude-haiku-5-5"


@pytest.mark.parametrize(
    "kind",
    [
        "text",
        "missing_warning",
        "extra_field",
        "wrong_tool",
        "two_tools",
        "thinking",
        "refusal",
    ],
)
def test_strict_provider_never_salvages_nonconforming_replies(fake_sdk, kind):
    response = message()
    block = response["content"][0]
    if kind == "text":
        response["content"] = [{"type": "text", "text": json.dumps(SAFE)}]
    elif kind == "missing_warning":
        block["input"].pop("image_warning")
    elif kind == "extra_field":
        block["input"]["unexpected"] = "Ek alan."
    elif kind == "wrong_tool":
        block["name"] = "unavailable_tool"
    elif kind == "two_tools":
        response["content"].append(copy.deepcopy(block))
    elif kind == "thinking":
        response["content"].insert(
            0, {"type": "thinking", "thinking": "Gizli muhakeme.", "signature": ""}
        )
    else:
        response["stop_reason"] = "refusal"
    provider, requests = fake_sdk(response)
    reply = asyncio.run(
        provider.generate(ProviderRequest([{"role": "user", "content": "Grafik."}]))
    )
    source = CAPTURED["cases"][0]["context"]
    context = validate_context(
        source,
        expected_symbol="BTCUSDT",
        expected_timeframe="15m",
        now=datetime.fromisoformat(source["captured_at"]),
    )
    with pytest.raises(ChartAiQuotaError) as caught:
        validate_reply(reply, context)
    assert caught.value.code == ("provider_refusal" if kind == "refusal" else "response_invalid")
    assert len(requests) == 1


@pytest.mark.parametrize(
    "input_tokens,output_tokens,expected",
    [
        (0, 0, 0),
        (1, 0, 1),
        (0, 1, 1),
        (5, 1, 1),
        (6, 1, 2),
        (100, 120, 70),
        (5797, 1073, 1117),
        (8000, 1500, 1550),
        (100_000, 0, 10_000),
        (100_001, 0, None),
        (True, 120, None),
        (None, 120, None),
        (-1, 120, None),
        (100, 1.2, None),
        (100, -1, None),
        (100, 40_001, None),
    ],
)
def test_haiku55_integer_accounting_rounds_up_and_rejects_unknown_usage(
    input_tokens, output_tokens, expected
):
    assert (
        usage_cost(ProviderReply(dict(SAFE), input_tokens, output_tokens)) == expected
    )


@pytest.mark.parametrize("cache_creation,cache_read", [(1, 0), (0, 1)])
def test_unrequested_cache_usage_is_not_silently_priced(cache_creation, cache_read):
    assert (
        usage_cost(
            ProviderReply(
                dict(SAFE),
                100,
                120,
                cache_creation_tokens=cache_creation,
                cache_read_tokens=cache_read,
            )
        )
        is None
    )
