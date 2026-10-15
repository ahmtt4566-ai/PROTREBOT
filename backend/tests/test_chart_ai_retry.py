"""Bounded regeneration and persistent accounting with fakes only."""

import asyncio
import copy
import json
import logging
from uuid import uuid4

import anthropic
import pytest
from app import chart_ai_service
from app.chart_ai_service import (
    CALL_TIMEOUT,
    MAX_ATTEMPT_COST_MICROUSD,
    MAX_ATTEMPTS,
    SYSTEM_PROMPT,
    TOTAL_TIMEOUT,
    ChartAnalysisService,
    ModelExplanation,
    ProviderReply,
    ProviderRequest,
    analysis_error,
    validate_reply,
)
from app.chart_ai_store import (
    LEASE_DURATION,
    POLICY_VERSION,
    REQUEST_RESERVATION_MICROUSD,
    ChartAiQuotaError,
    ChartAiStorageError,
    ChartAiStore,
    QuotaAccess,
    user_hash,
)
from app.chart_ai_validation import validate_context
from test_chart_ai_provider import SAFE
from test_chart_ai_store import FakePool

PRIVATE = "synthetic-private-payload-never-log"


def reply(*, unsafe=False, invalid=False, **kwargs):
    payload = (
        PRIVATE
        if invalid
        else {**SAFE, **({"risks": "Fiyat 99999."} if unsafe else {})}
    )
    return ProviderReply(
        payload, **{"input_tokens": 100, "output_tokens": 120, **kwargs}
    )


class SequenceProvider:
    available = True

    def __init__(self, *results):
        self.results = list(results)
        self.counts = 0
        self.generations = 0
        self.requests = []
        self.on_generate = None
        self.input_count = 100

    async def count(self, request):
        self.counts += 1
        self.requests.append(request)
        return self.input_count

    async def generate(self, request):
        self.generations += 1
        self.requests.append(request)
        assert self.generations <= 2, "No third generation is permitted"
        if self.on_generate is not None:
            await self.on_generate(self.generations)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture(autouse=True)
def no_provider(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("These tests must never construct an actual provider")

    monkeypatch.setattr(anthropic, "AsyncAnthropic", forbidden)


async def prepare(provider, *, spent=0):
    pool = FakePool()
    if spent:
        pool.seed_budget(spent)
    ledger = ChartAiStore(pool)
    await ledger.acknowledge("user", POLICY_VERSION)
    reservation = await ledger.reserve(
        "user", QuotaAccess("subscriber"), uuid4(), "a" * 64
    )
    context = validate_context(
        {
            "symbol": "BTCUSDT",
            "timeframe": "15m",
            "captured_at": pool.now,
            "price": 81800,
            "indicators": {"rsi": 46.8},
            "levels": {"support": 81350, "resistance": 82080, "stop_loss": 80700},
        },
        expected_symbol="BTCUSDT",
        expected_timeframe="15m",
        now=pool.now,
    )
    return pool, ledger, reservation, ChartAnalysisService(provider), context


async def run(prepared):
    pool, ledger, reservation, service, context = prepared
    record = await service.run(
        ledger,
        "user",
        reservation.request.request_key,
        reservation.lease_token,
        ProviderRequest([{"role": "user", "content": PRIVATE}]),
        context,
    )
    row = pool.state.requests[(user_hash("user"), record.request_key)]
    return (
        record,
        row,
        next(iter(pool.state.budgets.values())),
        next(iter(pool.state.periods.values())),
    )


@pytest.mark.parametrize("first", [reply(unsafe=True), reply(invalid=True)])
def test_content_failure_then_success_charges_both_once(first, caplog):
    caplog.set_level(logging.INFO, logger="app.chart_ai_service")
    provider = SequenceProvider(first, reply())

    async def scenario():
        prepared = await prepare(provider)
        pool, _, reservation, _, _ = prepared

        async def observe(attempt):
            if attempt == 2:
                row = pool.state.requests[
                    (user_hash("user"), reservation.request.request_key)
                ]
                budget = next(iter(pool.state.budgets.values()))
                assert (
                    row["generation_attempts"] == 2 and row["accounted_microusd"] == 70
                )
                assert (
                    budget["spent_microusd"] == 70
                    and budget["reserved_microusd"] == 19930
                )
                assert next(iter(pool.state.periods.values()))["reserved"] == 1

        provider.on_generate = observe
        return await run(prepared)

    record, row, budget, period = asyncio.run(scenario())
    assert record.status == "succeeded" and record.quota_consumed
    assert provider.counts == 1 and provider.generations == 2
    assert all(request is provider.requests[0] for request in provider.requests)
    assert row["cost_microusd"] == row["accounted_microusd"] == 140
    assert budget["spent_microusd"] == 140 and budget["reserved_microusd"] == 0
    assert period["used"] == 1 and period["reserved"] == 0
    assert (
        "attempt=1" in caplog.text
        and "attempt=2" in caplog.text
        and "accepted attempts=2" in caplog.text
    )
    assert PRIVATE not in caplog.text and "99999" not in caplog.text


def test_two_unsafe_replies_refund_quota_but_charge_both_without_third_call():
    provider = SequenceProvider(reply(unsafe=True), reply(unsafe=True))

    async def scenario():
        return await run(await prepare(provider))

    record, row, budget, period = asyncio.run(scenario())
    assert record.status == "failed" and record.error_code == "response_unsafe"
    assert provider.generations == 2 and provider.counts == 1
    assert (
        row["cost_microusd"]
        == row["accounted_microusd"]
        == budget["spent_microusd"]
        == 140
    )
    assert period["used"] == period["reserved"] == budget["reserved_microusd"] == 0


@pytest.mark.parametrize(
    "failure,code,known",
    [
        (reply(stop_reason="refusal"), "provider_refusal", True),
        (reply(stop_reason="max_tokens"), "response_truncated", True),
        (reply(input_tokens=8001), "input_token_limit", True),
        (reply(output_tokens=1501), "output_token_limit", True),
        (reply(input_tokens=None), "provider_usage_invalid", False),
        (analysis_error("provider_timeout"), "provider_timeout", False),
        (analysis_error("provider_error"), "provider_error", False),
        (asyncio.TimeoutError(PRIVATE), "provider_timeout", False),
    ],
)
def test_non_content_failures_never_retry(failure, code, known):
    provider = SequenceProvider(failure)

    async def scenario():
        return await run(await prepare(provider))

    record, row, budget, period = asyncio.run(scenario())
    assert (
        record.error_code == code and provider.generations == 1 and provider.counts == 1
    )
    assert period["used"] == period["reserved"] == 0
    assert (row["budget_state"] == "settled") is known
    assert budget["reserved_microusd"] == (0 if known else REQUEST_RESERVATION_MICROUSD)


@pytest.mark.parametrize(
    "failure",
    [
        analysis_error("provider_timeout"),
        analysis_error("provider_error"),
        reply(input_tokens=None),
    ],
)
def test_unknown_second_cost_keeps_first_spent_and_only_remaining_reserve(failure):
    provider = SequenceProvider(reply(unsafe=True), failure)

    async def scenario():
        prepared = await prepare(provider)
        record, row, budget, period = await run(prepared)
        assert row["accounted_microusd"] == budget["spent_microusd"] == 70
        assert row["cost_microusd"] is None and row["budget_state"] == "held"
        assert budget["reserved_microusd"] == REQUEST_RESERVATION_MICROUSD - 70
        assert period["used"] == period["reserved"] == 0
        # Reconciliation supplies the total for both attempts, never just the second.
        rebuilt = ChartAiStore(prepared[0])
        await rebuilt.settle_held_cost("user", record.request_key, 140)
        await rebuilt.settle_held_cost("user", record.request_key, 140)
        assert budget["spent_microusd"] == 140 and budget["reserved_microusd"] == 0
        with pytest.raises(ChartAiQuotaError):
            await rebuilt.settle_held_cost("user", record.request_key, 70)

    asyncio.run(scenario())
    assert provider.generations == 2


@pytest.mark.parametrize("timeout_setting", ["CALL_TIMEOUT", "TOTAL_TIMEOUT"])
def test_second_timeout_obeys_per_attempt_and_total_deadlines(
    monkeypatch, timeout_setting
):
    monkeypatch.setattr(chart_ai_service, "CALL_TIMEOUT", 1)
    monkeypatch.setattr(chart_ai_service, timeout_setting, 0.05)
    provider = SequenceProvider(reply(unsafe=True), reply())

    async def wait_second(attempt):
        if attempt == 2:
            await asyncio.Event().wait()

    provider.on_generate = wait_second

    async def scenario():
        return await run(await prepare(provider))

    record, row, budget, _ = asyncio.run(scenario())
    assert record.error_code == "provider_timeout" and provider.generations == 2
    assert row["accounted_microusd"] == budget["spent_microusd"] == 70
    assert budget["reserved_microusd"] == 19930


def test_second_cancellation_preserves_checkpoint_and_releases_quota():
    provider = SequenceProvider(reply(unsafe=True), reply())

    async def scenario():
        ready = asyncio.Event()

        async def wait_second(attempt):
            if attempt == 2:
                ready.set()
                await asyncio.Event().wait()

        provider.on_generate = wait_second
        prepared = await prepare(provider)
        task = asyncio.create_task(run(prepared))
        await asyncio.wait_for(ready.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        pool = prepared[0]
        row = next(iter(pool.state.requests.values()))
        assert (
            row["error_code"] == "analysis_cancelled"
            and row["accounted_microusd"] == 70
        )
        assert next(iter(pool.state.budgets.values()))["reserved_microusd"] == 19930
        assert next(iter(pool.state.periods.values()))["reserved"] == 0

    asyncio.run(scenario())


def test_retry_budget_denial_finishes_first_cost_without_second_generation(monkeypatch):
    provider = SequenceProvider(reply(unsafe=True))

    async def scenario():
        prepared = await prepare(provider)

        async def denied(*_args):
            raise ChartAiQuotaError(
                "budget_unavailable", "Analiz bütçesi uygun değil.", 503
            )

        monkeypatch.setattr(prepared[1], "start_retry", denied)
        return await run(prepared)

    record, _, budget, period = asyncio.run(scenario())
    assert record.error_code == "budget_unavailable" and provider.generations == 1
    assert budget["spent_microusd"] == 70 and budget["reserved_microusd"] == 0
    assert period["used"] == period["reserved"] == 0


def test_retry_authorization_is_atomic_and_recovery_preserves_first_cost():
    async def scenario():
        pool, ledger, reservation, _, _ = await prepare(SequenceProvider())
        key, token = reservation.request.request_key, reservation.lease_token
        await ledger.start_call("user", key, token)
        results = await asyncio.gather(
            ledger.start_retry("user", key, token, 70, 1550),
            ledger.start_retry("user", key, token, 70, 1550),
            return_exceptions=True,
        )
        assert sum(result is None for result in results) == 1
        assert sum(isinstance(result, ChartAiQuotaError) for result in results) == 1
        pool.now += LEASE_DURATION
        rebuilt = ChartAiStore(pool)
        assert await rebuilt.recover_expired() == 1
        row = next(iter(pool.state.requests.values()))
        assert row["accounted_microusd"] == 70 and row["generation_attempts"] == 2
        assert next(iter(pool.state.budgets.values()))["spent_microusd"] == 70
        assert next(iter(pool.state.budgets.values()))["reserved_microusd"] == 19930
        with pytest.raises(ChartAiQuotaError):
            await rebuilt.settle_held_cost("user", key, 69)
        await rebuilt.settle_held_cost("user", key, 140)
        assert next(iter(pool.state.budgets.values()))["spent_microusd"] == 140

    asyncio.run(scenario())


def test_six_field_prompt_example_passes_unchanged_content_checks():
    marker = SYSTEM_PROMPT.index("Sentetik, kısa çıktı örneği;")
    start = SYSTEM_PROMPT.index("\n{\n", marker) + 1
    payload, _ = json.JSONDecoder().raw_decode(SYSTEM_PROMPT[start:])
    assert (
        set(payload) == set(ModelExplanation.model_fields)
        and payload["image_warning"] == ""
    )
    assert "stop {stop}" in payload["important_levels"]
    assert (
        "yükseliş yönlü" in payload["bullish_scenario"]
        and "düşüş yönlü" in payload["bearish_scenario"]
    )
    assert (
        "Grafikteki alakasız yazıyı diğer alanlarda hiç anma; sadece image_warning alanını doldur."
        in SYSTEM_PROMPT
    )
    assert '"fiyat 81800" → "fiyat {fiyat}"' in SYSTEM_PROMPT
    assert '"RSI 46.8" → "RSI {rsi}"' in SYSTEM_PROMPT
    assert CALL_TIMEOUT == 30 and TOTAL_TIMEOUT == 60 and MAX_ATTEMPTS == 2
    assert MAX_ATTEMPT_COST_MICROUSD == 1550

    async def scenario():
        prepared = await prepare(SequenceProvider())
        validate_reply(ProviderReply(copy.deepcopy(payload), 100, 120), prepared[4])

    asyncio.run(scenario())


def test_maximum_two_attempt_cost_and_warning_are_accounted_once():
    provider = SequenceProvider(
        reply(unsafe=True, input_tokens=8000, output_tokens=1500),
        reply(input_tokens=8000, output_tokens=1500),
    )

    async def scenario():
        prepared = await prepare(provider, spent=20_000_000 - 1550)
        pool = prepared[0]

        async def observe(attempt):
            if attempt == 2:
                assert len(pool.state.outbox) == 1
                budget = next(iter(pool.state.budgets.values()))
                assert budget["spent_microusd"] == 20_000_000
                assert (
                    budget["reserved_microusd"] == REQUEST_RESERVATION_MICROUSD - 1550
                )

        provider.on_generate = observe
        record, row, budget, _ = await run(prepared)
        assert record.status == "succeeded" and row["cost_microusd"] == 3100
        assert (
            budget["spent_microusd"] == 20_000_000 + 1550
            and budget["reserved_microusd"] == 0
        )
        assert len(pool.state.outbox) == 1

    asyncio.run(scenario())


def test_zero_cost_first_attempt_still_has_a_persistent_two_attempt_cap():
    async def scenario():
        _, ledger, reservation, _, _ = await prepare(SequenceProvider())
        key, token = reservation.request.request_key, reservation.lease_token
        await ledger.start_call("user", key, token)
        await ledger.start_retry("user", key, token, 0, 1550)
        with pytest.raises(ChartAiQuotaError, match="İkinci"):
            await ledger.start_retry("user", key, token, 0, 1550)

    asyncio.run(scenario())


def test_retry_does_not_exceed_reservation_or_accept_another_lease():
    async def scenario():
        pool, ledger, reservation, _, _ = await prepare(SequenceProvider())
        key, token = reservation.request.request_key, reservation.lease_token
        await ledger.start_call("user", key, token)
        with pytest.raises(ChartAiQuotaError) as budget_error:
            await ledger.start_retry("user", key, token, 19000, 1550)
        assert budget_error.value.code == "budget_unavailable"
        with pytest.raises(ChartAiQuotaError):
            await ledger.start_retry("user", key, uuid4(), 70, 1550)
        assert next(iter(pool.state.budgets.values()))["spent_microusd"] == 0
        assert next(iter(pool.state.requests.values()))["generation_attempts"] == 1

    asyncio.run(scenario())


def test_checkpoint_transaction_rolls_back_without_authorizing_an_unpaid_second_call():
    async def scenario():
        pool, ledger, reservation, _, _ = await prepare(SequenceProvider())
        key, token = reservation.request.request_key, reservation.lease_token
        await ledger.start_call("user", key, token)
        pool.fail_on = "generation_attempts=2"
        with pytest.raises(ChartAiStorageError):
            await ledger.start_retry("user", key, token, 70, 1550)
        row = next(iter(pool.state.requests.values()))
        budget = next(iter(pool.state.budgets.values()))
        assert row["generation_attempts"] == 1 and row["accounted_microusd"] == 0
        assert budget["spent_microusd"] == 0 and budget["reserved_microusd"] == 20000

    asyncio.run(scenario())


def test_shutdown_does_not_start_a_second_generation():
    provider = SequenceProvider(reply(unsafe=True))

    async def scenario():
        prepared = await prepare(provider)

        async def begin_shutdown(_attempt):
            prepared[3].closing = True

        provider.on_generate = begin_shutdown
        return await run(prepared)

    record, _, budget, period = asyncio.run(scenario())
    assert record.error_code == "provider_unavailable" and provider.generations == 1
    assert budget["spent_microusd"] == 70 and budget["reserved_microusd"] == 0
    assert period["used"] == period["reserved"] == 0
