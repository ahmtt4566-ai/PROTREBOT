"""OWNER-only rollout must stop all chart routes before chart storage or AI."""

import pytest
from app import chart_ai_api
from test_chart_ai_api import BODY, start_trial
from test_chart_ai_api import chart_api as canonical_chart_api
from test_chart_ai_api import flags as canonical_flags
from test_chart_ai_api import setup as canonical_setup
from test_chart_ai_api import trial_api as canonical_trial_api
from test_moderator_role import headers

chart_api = canonical_chart_api
flags = canonical_flags
setup = canonical_setup
trial_api = canonical_trial_api

ENDPOINTS = [
    ("GET", "/api/chart-ai/usage"),
    ("POST", "/api/chart-ai/acknowledgement"),
    ("POST", "/api/chart-ai/analyze"),
    ("GET", "/api/chart-ai/requests/00000000-0000-0000-0000-000000000001"),
]


@pytest.mark.parametrize("value", [None, "true", "1"])
@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_owner_only_defaults_closed_for_customer_on_every_route(chart_api, monkeypatch, value, method, path):
    client, authority, pool = chart_api
    start_trial(authority)
    if value is None:
        monkeypatch.delenv("CHART_AI_OWNER_ONLY")
    else:
        monkeypatch.setenv("CHART_AI_OWNER_ONLY", value)

    def forbidden(_request):
        pytest.fail("OWNER gate must run before chart storage or provider access")

    monkeypatch.setattr(chart_ai_api, "store", forbidden)
    monkeypatch.setattr(chart_ai_api, "analysis_service", forbidden)
    response = client.request(method, path, headers=headers("customer", "OWNER"),
                              json=BODY if method == "POST" else None)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "owner_only"
    assert pool.queries == []


@pytest.mark.parametrize("value", ["", "invalid-private-setting"])
def test_bad_pilot_flag_fails_closed_without_echo(chart_api, monkeypatch, value, caplog):
    client, _, pool = chart_api
    monkeypatch.setenv("CHART_AI_OWNER_ONLY", value)
    response = client.get("/api/chart-ai/usage", headers=headers("owner", "OWNER"))
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "configuration_invalid"
    assert "invalid-private-setting" not in caplog.text
    assert pool.queries == []


def test_owner_can_read_and_acknowledge_with_default_owner_gate(chart_api, monkeypatch):
    client, _, _ = chart_api
    monkeypatch.delenv("CHART_AI_OWNER_ONLY")
    auth = headers("owner", "OWNER")
    response = client.get("/api/chart-ai/usage", headers=auth)
    assert response.status_code == 200
    assert response.json()["access_type"] == "owner"
    assert client.post("/api/chart-ai/acknowledgement", headers=auth, json=BODY).status_code == 200


def test_owner_generates_and_reads_only_own_result_with_fake_provider(chart_api, monkeypatch):
    from app.chart_ai_service import ChartAnalysisService
    from test_chart_ai_analysis import FakeProvider, body

    client, _, pool = chart_api
    monkeypatch.delenv("CHART_AI_OWNER_ONLY")
    provider = FakeProvider()
    client.app.state.chart_ai_service = ChartAnalysisService(provider)
    auth = headers("owner", "OWNER")
    assert client.post("/api/chart-ai/acknowledgement", headers=auth, json=BODY).status_code == 200
    payload = body(pool)
    response = client.post("/api/chart-ai/analyze", headers=auth, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "succeeded"
    saved = client.get("/api/chart-ai/requests/" + payload["request_key"], headers=auth)
    assert saved.status_code == 200
    assert saved.json()["result_text"] == response.json()["result_text"]
    assert provider.counts == provider.generations == 1


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_owner_gate_preserves_authentication_and_global_off(chart_api, monkeypatch, method, path):
    client, _, pool = chart_api
    monkeypatch.delenv("CHART_AI_OWNER_ONLY")
    response = client.request(method, path, json=BODY if method == "POST" else None)
    assert response.status_code == 401
    monkeypatch.setenv("CHART_AI_ENABLED", "false")
    response = client.request(method, path, headers=headers("owner", "OWNER"),
                              json=BODY if method == "POST" else None)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "feature_disabled"
    assert pool.queries == []
